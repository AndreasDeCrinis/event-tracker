import calendar as calendar_module
from datetime import datetime, time, timedelta, timezone
import secrets

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from . import db
from .google_calendar import (
    GoogleCalendarError,
    build_authorization_url,
    exchange_authorization_response,
    google_oauth_is_configured,
    google_redirect_uri,
)
from .google_calendar_queue import (
    queue_all_google_event_syncs,
    queue_google_event_deletion,
    queue_google_event_sync,
    trigger_google_calendar_sync_worker,
)
from .models import (
    BOOKING_FIXED,
    BOOKING_PLANNING,
    EVENT_BOOKING_STATUSES,
    MATERIAL_CONSUMABLE,
    MATERIAL_FIXED,
    MATERIAL_KINDS,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PLANNED,
    Event,
    EventMaterial,
    EventPersonnel,
    GoogleCalendarConnection,
    Material,
    Personnel,
    material_assignable_quantity,
    material_allocated_quantity,
    material_available_quantity,
    material_deducted_used_quantity,
    material_open_used_quantity,
    material_reserved_quantity,
    material_shortage_quantity,
    personnel_has_conflict,
    personnel_is_available,
    personnel_planned_assignment_count,
)


bp = Blueprint("main", __name__)
GOOGLE_OAUTH_STATE_KEY = "google_calendar_oauth_state"
GOOGLE_OAUTH_CODE_VERIFIER_KEY = "google_calendar_oauth_code_verifier"

EVENT_STATUS_LABELS = {
    STATUS_PLANNED: "Geplant",
    STATUS_COMPLETED: "Erfolgreich abgeschlossen",
    STATUS_CANCELLED: "Abgesagt",
}

EVENT_CLOSURE_STATUSES = (STATUS_COMPLETED, STATUS_CANCELLED)

EVENT_BOOKING_STATUS_LABELS = {
    BOOKING_PLANNING: "In Planung",
    BOOKING_FIXED: "Fixiert",
}

MATERIAL_KIND_LABELS = {
    MATERIAL_FIXED: "Festes Material",
    MATERIAL_CONSUMABLE: "Verbrauchsmaterial",
}

MONTH_LABELS = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)

WEEKDAY_LABELS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


@bp.get("/")
def index():
    moment = datetime.now()
    event_view = request.args.get("view", "list")
    if event_view not in {"list", "calendar"}:
        event_view = "list"

    events = Event.query.order_by(Event.starts_at.asc(), Event.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()
    people = Personnel.query.order_by(Personnel.name.asc()).all()
    active_events = [event for event in events if not _event_is_archived(event, moment)]
    archive_events = [event for event in events if _event_is_archived(event, moment)]
    personnel_rows = [
        {
            "person": person,
            "planned_assignments": personnel_planned_assignment_count(person, moment=moment),
            "available": personnel_is_available(person, moment=moment),
        }
        for person in people
    ]
    event_material_options = {
        event.id: [
            {
                "item": material,
                "available": material_assignable_quantity(material, event),
            }
            for material in materials
        ]
        for event in events
    }
    event_personnel_options = {
        event.id: [
            {
                "person": person,
                "conflict": personnel_has_conflict(person, event),
            }
            for person in people
        ]
        for event in events
    }
    event_material_warnings = {
        event.id: _material_shortage_warnings(event)
        for event in events
        if event.booking_status == BOOKING_PLANNING
    }

    stats = {
        "events": len(events),
        "active_events": len(active_events),
        "archived_events": len(archive_events),
        "materials": len(materials),
        "people": len(people),
    }

    return render_template(
        "index.html",
        active_events=active_events,
        archive_events=archive_events,
        personnel_rows=personnel_rows,
        event_material_options=event_material_options,
        event_personnel_options=event_personnel_options,
        event_material_warnings=event_material_warnings,
        event_view=event_view,
        event_calendar=_event_calendar_context(active_events),
        stats=stats,
        material_kinds=MATERIAL_KINDS,
        material_kind_labels=MATERIAL_KIND_LABELS,
        material_fixed=MATERIAL_FIXED,
        material_consumable=MATERIAL_CONSUMABLE,
        event_status_labels=EVENT_STATUS_LABELS,
        event_closure_statuses=EVENT_CLOSURE_STATUSES,
        event_booking_statuses=EVENT_BOOKING_STATUSES,
        event_booking_status_labels=EVENT_BOOKING_STATUS_LABELS,
        booking_planning=BOOKING_PLANNING,
        booking_fixed=BOOKING_FIXED,
        planned_status=STATUS_PLANNED,
    )


@bp.get("/inventory")
def inventory():
    return render_template(
        "inventory.html",
        **_inventory_template_context(),
    )


@bp.get("/settings")
def settings():
    return render_template(
        "settings.html",
        **_google_calendar_template_context(),
    )


@bp.post("/events")
def create_event():
    name = _required_text("name", "Event-Name")
    starts_on_value = _required_text("starts_on", "Beginn")
    ends_on_value = _required_text("ends_on", "Ende")
    starts_at_time_value = _optional_text("starts_at_time")
    ends_at_time_value = _optional_text("ends_at_time")
    location = _optional_text("location")
    booking_status = request.form.get("booking_status", BOOKING_PLANNING)
    sync_to_google_calendar = _form_checkbox_checked("sync_to_google_calendar", default=True)

    if not all((name, starts_on_value, ends_on_value)):
        return _redirect("events")

    if booking_status not in EVENT_BOOKING_STATUSES:
        flash("Bitte In Planung oder Fixiert wählen.", "error")
        return _redirect("events")

    try:
        starts_on = _parse_date_local(starts_on_value)
        ends_on = _parse_date_local(ends_on_value)
    except ValueError:
        flash("Bitte ein gültiges Beginndatum und ein gültiges Enddatum verwenden.", "error")
        return _redirect("events")

    try:
        starts_at, ends_at = _event_datetimes(starts_on, ends_on, starts_at_time_value, ends_at_time_value)
    except ValueError as error:
        flash(str(error), "error")
        return _redirect("events")

    event = Event(
        name=name,
        starts_at=starts_at,
        ends_at=ends_at,
        location=location,
        booking_status=booking_status,
        notes=_optional_text("notes"),
        sync_to_google_calendar=sync_to_google_calendar,
    )
    db.session.add(event)
    db.session.flush()
    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"{event.name} wurde hinzugefügt.", "success")
    return _redirect("events")


@bp.post("/events/<int:event_id>/booking-status")
def set_event_booking_status(event_id):
    event = Event.query.get_or_404(event_id)
    booking_status = request.form.get("booking_status")

    if booking_status not in EVENT_BOOKING_STATUSES:
        flash("Bitte In Planung oder Fixiert wählen.", "error")
        return _redirect("event-" + str(event.id))

    if event.status != STATUS_PLANNED:
        flash("Nur geplante Events können zwischen In Planung und Fixiert wechseln.", "error")
        return _redirect("event-" + str(event.id))

    if _event_is_archived(event, datetime.now()):
        flash("Archivierte Events können nicht mehr fixiert werden.", "error")
        return _redirect("event-" + str(event.id))

    if booking_status == BOOKING_FIXED:
        warnings = _material_shortage_warnings(event)
        if warnings:
            shortage_text = ", ".join(
                f"{warning['material'].name}: {warning['shortage']} {warning['material'].unit}"
                for warning in warnings
            )
            flash(f"Event kann nicht fixiert werden. Material fehlt: {shortage_text}.", "error")
            return _redirect("event-" + str(event.id))

    event.booking_status = booking_status
    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"{event.name} ist jetzt {EVENT_BOOKING_STATUS_LABELS[event.booking_status]}.", "success")
    return _redirect("event-" + str(event.id))


@bp.post("/events/<int:event_id>/calendar-sync")
def set_event_calendar_sync(event_id):
    event = Event.query.get_or_404(event_id)
    event.sync_to_google_calendar = _form_checkbox_checked("sync_to_google_calendar")
    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"Kalender-Sync für {event.name} wurde aktualisiert.", "success")
    return _redirect("event-" + str(event.id))


@bp.post("/events/<int:event_id>/close")
def close_event(event_id):
    event = Event.query.get_or_404(event_id)
    status = request.form.get("status")

    if status not in EVENT_CLOSURE_STATUSES:
        flash("Bitte einen gültigen Abschlussstatus wählen.", "error")
        return _redirect("event-" + str(event.id))

    if event.status != STATUS_PLANNED:
        flash("Abgeschlossene oder abgesagte Events können nicht erneut abgeschlossen werden.", "error")
        return _redirect("event-" + str(event.id))

    if status == STATUS_COMPLETED and not _deduct_consumables_for_completed_event(event):
        return _redirect("event-" + str(event.id))

    event.status = status
    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"{event.name} wurde als {EVENT_STATUS_LABELS[event.status].lower()} markiert.", "success")
    return _redirect("event-" + str(event.id))


@bp.post("/events/<int:event_id>/delete")
def delete_event(event_id):
    event = Event.query.get_or_404(event_id)
    google_sync_queued = _queue_event_deletion_if_google_connected(event)
    db.session.delete(event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"{event.name} wurde entfernt.", "success")
    return _redirect("events")


@bp.post("/google-calendar/settings")
def update_google_calendar_settings():
    calendar_id = _required_text("calendar_id", "Google Kalender-ID")

    if not calendar_id:
        return _settings_redirect("google-calendar")

    connection = _get_or_create_google_calendar_connection()
    _set_google_calendar_id(connection, calendar_id)
    db.session.commit()

    if connection.is_connected:
        _sync_all_google_events(connection)
    else:
        flash("Google Kalender-ID wurde gespeichert. Verbinde Google Kalender, um Events zu synchronisieren.", "success")

    return _settings_redirect("google-calendar")


@bp.post("/google-calendar/connect")
def connect_google_calendar():
    calendar_id = _required_text("calendar_id", "Google Kalender-ID")

    if not calendar_id:
        return _settings_redirect("google-calendar")

    connection = _get_or_create_google_calendar_connection()
    _set_google_calendar_id(connection, calendar_id)
    db.session.commit()

    state = secrets.token_urlsafe(32)
    redirect_uri = google_redirect_uri()

    try:
        authorization_url, returned_state, code_verifier = build_authorization_url(state=state, redirect_uri=redirect_uri)
    except GoogleCalendarError as error:
        flash(str(error), "error")
        return _settings_redirect("google-calendar")

    session[GOOGLE_OAUTH_STATE_KEY] = returned_state
    session[GOOGLE_OAUTH_CODE_VERIFIER_KEY] = code_verifier
    return redirect(authorization_url)


@bp.get("/google-calendar/oauth2callback")
def google_calendar_callback():
    expected_state = session.pop(GOOGLE_OAUTH_STATE_KEY, None)
    code_verifier = session.pop(GOOGLE_OAUTH_CODE_VERIFIER_KEY, None)

    if request.args.get("error"):
        flash(f"Google Kalender wurde nicht verbunden: {request.args['error']}", "error")
        return _settings_redirect("google-calendar")

    if not expected_state or request.args.get("state") != expected_state:
        flash("Google Kalender konnte wegen eines ungültigen OAuth-Status nicht verbunden werden.", "error")
        return _settings_redirect("google-calendar")

    if not code_verifier:
        flash("Google Kalender konnte wegen eines fehlenden OAuth-Code-Verifiers nicht verbunden werden.", "error")
        return _settings_redirect("google-calendar")

    connection = _get_or_create_google_calendar_connection()

    try:
        connection.credentials_json = exchange_authorization_response(
            authorization_response=request.url,
            state=expected_state,
            redirect_uri=google_redirect_uri(),
            code_verifier=code_verifier,
            existing_credentials_json=connection.credentials_json,
        )
    except GoogleCalendarError as error:
        flash(str(error), "error")
        return _settings_redirect("google-calendar")

    connection.connected_at = _utc_now()
    connection.updated_at = _utc_now()
    connection.last_error = None
    db.session.commit()

    _sync_all_google_events(connection)
    return _settings_redirect("google-calendar")


@bp.post("/google-calendar/sync")
def sync_google_calendar():
    connection = _google_calendar_connection()
    _sync_all_google_events(connection)
    return _settings_redirect("google-calendar")


@bp.post("/google-calendar/disconnect")
def disconnect_google_calendar():
    connection = _google_calendar_connection()

    if not connection:
        flash("Google Kalender ist nicht verbunden.", "error")
        return _settings_redirect("google-calendar")

    connection.credentials_json = None
    connection.connected_at = None
    connection.updated_at = _utc_now()
    connection.last_error = None
    db.session.commit()
    flash("Google Kalender wurde getrennt. Bestehende Kalendereinträge bleiben erhalten.", "success")
    return _settings_redirect("google-calendar")


@bp.post("/materials")
def create_material():
    name = _required_text("name", "Materialname")
    kind = request.form.get("kind", MATERIAL_FIXED)
    quantity = _non_negative_int("total_quantity", "Gesamtmenge")
    unit = _required_text("unit", "Einheit")

    if kind not in MATERIAL_KINDS:
        flash("Bitte festes Material oder Verbrauchsmaterial wählen.", "error")
        return _inventory_redirect()

    if not all((name, unit)) or quantity is None:
        return _inventory_redirect()

    material = Material(name=name, kind=kind, total_quantity=quantity, unit=unit, notes=_optional_text("notes"))
    db.session.add(material)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Dieses Material existiert bereits.", "error")
        return _inventory_redirect()

    flash(f"{material.name} wurde zum Inventar hinzugefügt.", "success")
    return _inventory_redirect()


@bp.post("/materials/<int:material_id>/quantity")
def update_material_quantity(material_id):
    material = Material.query.get_or_404(material_id)
    quantity = _non_negative_int("total_quantity", "Gesamtmenge")

    if quantity is None:
        return _inventory_redirect()

    material.total_quantity = quantity
    db.session.commit()
    flash(f"Gesamtmenge von {material.name} wurde aktualisiert.", "success")
    return _inventory_redirect()


@bp.post("/materials/<int:material_id>/delete")
def delete_material(material_id):
    material = Material.query.get_or_404(material_id)
    db.session.delete(material)
    db.session.commit()
    flash(f"{material.name} wurde aus dem Inventar entfernt.", "success")
    return _inventory_redirect()


@bp.post("/personnel")
def create_personnel():
    name = _required_text("name", "Name")
    role = _required_text("role", "Rolle")

    if not all((name, role)):
        return _redirect("personnel")

    person = Personnel(name=name, role=role, contact=_optional_text("contact"), notes=_optional_text("notes"))
    db.session.add(person)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Diese Person existiert bereits.", "error")
        return _redirect("personnel")

    flash(f"{person.name} wurde zum Personal hinzugefügt.", "success")
    return _redirect("personnel")


@bp.post("/personnel/<int:personnel_id>/delete")
def delete_personnel(personnel_id):
    person = Personnel.query.get_or_404(personnel_id)
    db.session.delete(person)
    db.session.commit()
    flash(f"{person.name} wurde entfernt.", "success")
    return _redirect("personnel")


@bp.post("/events/<int:event_id>/materials")
def assign_material(event_id):
    event = Event.query.get_or_404(event_id)
    material = Material.query.get_or_404(request.form.get("material_id", type=int))
    quantity = _positive_int("quantity", "Menge")

    if quantity is None:
        return _redirect("event-" + str(event.id))

    if not _event_is_open_for_assignment(event):
        flash("Material kann nur aktiven geplanten Events zugewiesen werden.", "error")
        return _redirect("event-" + str(event.id))

    if event.booking_status == BOOKING_FIXED:
        assignable = material_assignable_quantity(material, event)
        if quantity > assignable:
            flash(f"Von {material.name} sind für dieses Event nur noch {assignable} {material.unit} zuweisbar.", "error")
            return _redirect("event-" + str(event.id))

    assignment = EventMaterial.query.filter_by(event_id=event.id, material_id=material.id).first()
    if assignment:
        assignment.quantity += quantity
    else:
        assignment = EventMaterial(event=event, material=material, quantity=quantity)
        db.session.add(assignment)

    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"{quantity} {material.unit} {material.name} wurden {event.name} zugewiesen.", "success")
    return _redirect("event-" + str(event.id))


@bp.post("/assignments/material/<int:assignment_id>/quantity")
def update_material_assignment_quantity(assignment_id):
    assignment = EventMaterial.query.get_or_404(assignment_id)
    quantity = _positive_int("quantity", "Menge")

    if quantity is None:
        return _redirect("event-" + str(assignment.event_id))

    if not _event_is_open_for_assignment(assignment.event):
        flash("Archivierte Zuweisungen bleiben unverändert.", "error")
        return _redirect("event-" + str(assignment.event_id))

    if assignment.event.booking_status == BOOKING_FIXED:
        available_for_event = material_available_quantity(
            assignment.material,
            target_event=assignment.event,
            exclude_event_id=assignment.event_id,
        )
        if quantity > available_for_event:
            flash(
                f"Von {assignment.material.name} sind für dieses Event nur noch "
                f"{available_for_event} {assignment.material.unit} verfügbar.",
                "error",
            )
            return _redirect("event-" + str(assignment.event_id))

    assignment.quantity = quantity
    google_sync_queued = _queue_event_sync_if_google_connected(assignment.event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"Menge von {assignment.material.name} wurde aktualisiert.", "success")
    return _redirect("event-" + str(assignment.event_id))


@bp.post("/assignments/material/<int:assignment_id>/delete")
def remove_material_assignment(assignment_id):
    assignment = EventMaterial.query.get_or_404(assignment_id)
    event = assignment.event
    event_id = assignment.event_id
    material_name = assignment.material.name

    if not _event_is_open_for_assignment(assignment.event):
        flash("Archivierte Zuweisungen bleiben unverändert.", "error")
        return _redirect("event-" + str(event_id))

    db.session.delete(assignment)
    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    db.session.expire(event, ["material_assignments"])
    _wake_google_sync_worker(google_sync_queued)
    flash(f"Zuweisung von {material_name} wurde entfernt.", "success")
    return _redirect("event-" + str(event_id))


@bp.post("/events/<int:event_id>/personnel")
def assign_personnel(event_id):
    event = Event.query.get_or_404(event_id)
    person = Personnel.query.get_or_404(request.form.get("personnel_id", type=int))

    if not _event_is_open_for_assignment(event):
        flash("Personal kann nur aktiven geplanten Events zugewiesen werden.", "error")
        return _redirect("event-" + str(event.id))

    existing = EventPersonnel.query.filter_by(event_id=event.id, personnel_id=person.id).first()
    if existing:
        flash(f"{person.name} ist {event.name} bereits zugewiesen.", "error")
        return _redirect("event-" + str(event.id))

    if personnel_has_conflict(person, event):
        flash(f"{person.name} ist in diesem Zeitraum bereits einem anderen Event zugewiesen.", "error")
        return _redirect("event-" + str(event.id))

    db.session.add(EventPersonnel(event=event, personnel=person))
    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    _wake_google_sync_worker(google_sync_queued)
    flash(f"{person.name} wurde {event.name} zugewiesen.", "success")
    return _redirect("event-" + str(event.id))


@bp.post("/assignments/personnel/<int:assignment_id>/delete")
def remove_personnel_assignment(assignment_id):
    assignment = EventPersonnel.query.get_or_404(assignment_id)
    event = assignment.event
    event_id = assignment.event_id
    person_name = assignment.personnel.name

    if not _event_is_open_for_assignment(assignment.event):
        flash("Archivierte Zuweisungen bleiben unverändert.", "error")
        return _redirect("event-" + str(event_id))

    db.session.delete(assignment)
    google_sync_queued = _queue_event_sync_if_google_connected(event)
    db.session.commit()
    db.session.expire(event, ["personnel_assignments"])
    _wake_google_sync_worker(google_sync_queued)
    flash(f"Zuweisung von {person_name} wurde entfernt.", "success")
    return _redirect("event-" + str(event_id))


@bp.app_template_filter("date_only")
def date_only(value):
    return value.strftime("%d.%m.%Y")


@bp.app_template_filter("event_range")
def event_range(event):
    if event.is_all_day:
        return f"{date_only(event.starts_on)} bis {date_only(event.ends_on)}"

    return (
        f"{date_only(event.starts_on)} {event.starts_at.strftime('%H:%M')} "
        f"bis {date_only(event.ends_on)} {event.ends_at.strftime('%H:%M')}"
    )


def _parse_date_local(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_time_local(value):
    return datetime.strptime(value, "%H:%M").time()


def _event_datetimes(starts_on, ends_on, starts_at_time_value=None, ends_at_time_value=None):
    if bool(starts_at_time_value) != bool(ends_at_time_value):
        raise ValueError("Bitte Startzeit und Endzeit gemeinsam angeben oder beide leer lassen.")

    if starts_at_time_value and ends_at_time_value:
        try:
            starts_at_time = _parse_time_local(starts_at_time_value)
            ends_at_time = _parse_time_local(ends_at_time_value)
        except ValueError as error:
            raise ValueError("Bitte gültige Start- und Endzeiten verwenden.") from error

        starts_at = datetime.combine(starts_on, starts_at_time)
        ends_at = datetime.combine(ends_on, ends_at_time)
        if ends_at <= starts_at:
            raise ValueError("Ende muss nach Beginn liegen.")
        return starts_at, ends_at

    if ends_on < starts_on:
        raise ValueError("Das Enddatum darf nicht vor dem Beginndatum liegen.")

    return datetime.combine(starts_on, time.min), datetime.combine(ends_on + timedelta(days=1), time.min)


def _redirect(anchor):
    return redirect(url_for("main.index") + f"#{anchor}")


def _settings_redirect(anchor):
    return redirect(url_for("main.settings") + f"#{anchor}")


def _inventory_redirect():
    return redirect(url_for("main.inventory") + "#inventory")


def _event_calendar_context(events):
    selected_month = request.args.get("month")
    today = datetime.now().date()

    try:
        month_date = datetime.strptime(selected_month, "%Y-%m").date() if selected_month else today
    except ValueError:
        month_date = today

    year = month_date.year
    month = month_date.month
    weeks = calendar_module.Calendar(firstweekday=0).monthdatescalendar(year, month)
    calendar_start = weeks[0][0]
    calendar_end = weeks[-1][-1]
    events_by_date = {}

    for event in events:
        starts_on = max(event.starts_on, calendar_start)
        ends_on = min(event.ends_on, calendar_end)
        current_day = starts_on

        while current_day <= ends_on:
            events_by_date.setdefault(current_day, []).append(event)
            current_day += timedelta(days=1)

    previous_year, previous_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)

    return {
        "weeks": [
            {
                "days": [
                    {
                        "date": day,
                        "day_number": day.day,
                        "in_month": day.month == month,
                        "is_today": day == today,
                        "events": events_by_date.get(day, []),
                    }
                    for day in week
                ]
            }
            for week in weeks
        ],
        "weekday_labels": WEEKDAY_LABELS,
        "month_label": f"{MONTH_LABELS[month - 1]} {year}",
        "selected_month": _month_key(year, month),
        "previous_month": _month_key(previous_year, previous_month),
        "next_month": _month_key(next_year, next_month),
    }


def _inventory_template_context():
    moment = datetime.now()
    materials = Material.query.order_by(Material.name.asc()).all()
    material_rows = [
        {
            "item": material,
            "reserved": material_reserved_quantity(material, moment=moment),
            "open_used": material_open_used_quantity(material, moment=moment),
            "deducted_used": material_deducted_used_quantity(material),
            "available": material_available_quantity(material, moment=moment),
        }
        for material in materials
    ]

    return {
        "material_rows": material_rows,
        "fixed_material_rows": [row for row in material_rows if row["item"].kind == MATERIAL_FIXED],
        "consumable_material_rows": [row for row in material_rows if row["item"].kind == MATERIAL_CONSUMABLE],
        "material_kinds": MATERIAL_KINDS,
        "material_kind_labels": MATERIAL_KIND_LABELS,
        "material_fixed": MATERIAL_FIXED,
        "material_consumable": MATERIAL_CONSUMABLE,
    }


def _shift_month(year, month, delta):
    month_index = year * 12 + month - 1 + delta
    return month_index // 12, month_index % 12 + 1


def _month_key(year, month):
    return f"{year:04d}-{month:02d}"


def _event_is_archived(event, moment):
    return event.status in (STATUS_COMPLETED, STATUS_CANCELLED) or (
        event.status == STATUS_PLANNED and event.ends_at <= moment
    )


def _event_is_open_for_assignment(event):
    return event.status == STATUS_PLANNED and not _event_is_archived(event, datetime.now())


def _deduct_consumables_for_completed_event(event):
    if event.consumables_deducted_at:
        return True

    deductions = [
        assignment
        for assignment in event.material_assignments
        if assignment.material.kind == MATERIAL_CONSUMABLE
    ]

    shortages = []
    for assignment in deductions:
        available = material_available_quantity(
            assignment.material,
            target_event=event,
            exclude_event_id=event.id,
        )
        if assignment.quantity > available:
            shortages.append(
                f"{assignment.material.name}: {assignment.quantity - available} {assignment.material.unit}"
            )

    if shortages:
        flash(
            "Event kann nicht abgeschlossen werden. Verbrauchsmaterial fehlt: "
            + ", ".join(shortages)
            + ".",
            "error",
        )
        return False

    for assignment in deductions:
        assignment.material.total_quantity -= assignment.quantity

    event.consumables_deducted_at = _utc_now()
    return True


def _google_calendar_template_context():
    return {
        "google_calendar_connection": _google_calendar_connection(),
        "google_calendar_oauth_configured": google_oauth_is_configured(),
        "google_calendar_redirect_uri": google_redirect_uri(),
    }


def _google_calendar_connection():
    return db.session.get(GoogleCalendarConnection, 1)


def _get_or_create_google_calendar_connection():
    connection = _google_calendar_connection()

    if not connection:
        connection = GoogleCalendarConnection(id=1)
        db.session.add(connection)

    return connection


def _set_google_calendar_id(connection, calendar_id):
    previous_calendar_id = connection.calendar_id
    connection.calendar_id = calendar_id
    connection.updated_at = _utc_now()

    if previous_calendar_id and previous_calendar_id != calendar_id:
        _clear_google_event_links(previous_calendar_id)


def _clear_google_event_links(calendar_id):
    for event in Event.query.filter_by(google_calendar_id=calendar_id).all():
        event.google_event_id = None
        event.google_calendar_id = None
        event.google_event_link = None
        event.google_synced_at = None
        event.google_sync_error = None


def _queue_event_sync_if_google_connected(event):
    return queue_google_event_sync(event)


def _queue_event_deletion_if_google_connected(event):
    return queue_google_event_deletion(event)


def _wake_google_sync_worker(queued):
    if queued:
        trigger_google_calendar_sync_worker()


def _sync_all_google_events(connection):
    if not connection or not connection.calendar_id:
        flash("Bitte zuerst eine Google Kalender-ID speichern.", "error")
        return

    if not connection.is_connected:
        flash("Bitte zuerst Google Kalender verbinden.", "error")
        return

    queued = queue_all_google_event_syncs(
        Event.query.order_by(Event.starts_at.asc()).all(),
        connection=connection,
    )
    db.session.commit()
    _wake_google_sync_worker(queued > 0)
    flash(f"{queued} Kalenderaktionen wurden vorgemerkt.", "success")


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _material_shortage_warnings(event):
    warnings = []
    for assignment in event.material_assignments:
        shortage = material_shortage_quantity(assignment.material, event)
        if shortage > 0:
            warnings.append(
                {
                    "material": assignment.material,
                    "assigned": assignment.quantity,
                    "available": material_available_quantity(
                        assignment.material,
                        target_event=event,
                        exclude_event_id=event.id,
                    ),
                    "shortage": shortage,
                }
            )
    return warnings


def _required_text(field, label):
    value = (request.form.get(field) or "").strip()
    if not value:
        flash(f"{label} ist erforderlich.", "error")
        return None
    return value


def _optional_text(field):
    return (request.form.get(field) or "").strip() or None


def _form_checkbox_checked(field, default=False):
    values = request.form.getlist(field)
    if not values:
        return default
    return "1" in values


def _positive_int(field, label):
    try:
        value = int(request.form.get(field, ""))
    except ValueError:
        flash(f"{label} muss eine ganze Zahl sein.", "error")
        return None

    if value <= 0:
        flash(f"{label} muss größer als null sein.", "error")
        return None
    return value


def _non_negative_int(field, label):
    try:
        value = int(request.form.get(field, ""))
    except ValueError:
        flash(f"{label} muss eine ganze Zahl sein.", "error")
        return None

    if value < 0:
        flash(f"{label} darf nicht negativ sein.", "error")
        return None
    return value
