from datetime import datetime, time, timedelta, timezone
import secrets

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from . import db
from .google_calendar import (
    GoogleCalendarError,
    build_authorization_url,
    delete_event_from_google,
    exchange_authorization_response,
    google_oauth_is_configured,
    google_redirect_uri,
    sync_all_events_to_google,
    sync_event_to_google,
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
    material_shortage_quantity,
    personnel_has_conflict,
    personnel_is_available,
    personnel_planned_assignment_count,
)


bp = Blueprint("main", __name__)
GOOGLE_OAUTH_STATE_KEY = "google_calendar_oauth_state"

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


@bp.get("/")
def index():
    moment = datetime.now()
    today_start = _today_start()
    events = Event.query.order_by(Event.starts_at.asc(), Event.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()
    people = Personnel.query.order_by(Personnel.name.asc()).all()
    active_events = [event for event in events if not _event_is_archived(event, today_start)]
    archive_events = [event for event in events if _event_is_archived(event, today_start)]

    material_rows = [
        {
            "item": material,
            "allocated": material_allocated_quantity(material, moment=moment),
            "available": material_available_quantity(material, moment=moment),
        }
        for material in materials
    ]
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
        material_rows=material_rows,
        personnel_rows=personnel_rows,
        event_material_options=event_material_options,
        event_personnel_options=event_personnel_options,
        event_material_warnings=event_material_warnings,
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
        google_calendar_connection=_google_calendar_connection(),
        google_calendar_oauth_configured=google_oauth_is_configured(),
        google_calendar_redirect_uri=google_redirect_uri(),
    )


@bp.post("/events")
def create_event():
    name = _required_text("name", "Event-Name")
    starts_on_value = _required_text("starts_on", "Beginn")
    ends_on_value = _required_text("ends_on", "Ende")
    location = _optional_text("location")
    booking_status = request.form.get("booking_status", BOOKING_PLANNING)

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

    if ends_on < starts_on:
        flash("Das Enddatum darf nicht vor dem Beginndatum liegen.", "error")
        return _redirect("events")

    event = Event(
        name=name,
        starts_at=datetime.combine(starts_on, time.min),
        ends_at=datetime.combine(ends_on + timedelta(days=1), time.min),
        location=location,
        booking_status=booking_status,
        notes=_optional_text("notes"),
    )
    db.session.add(event)
    db.session.commit()
    _sync_event_if_google_connected(event)
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

    if _event_is_archived(event, _today_start()):
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
    db.session.commit()
    _sync_event_if_google_connected(event)
    flash(f"{event.name} ist jetzt {EVENT_BOOKING_STATUS_LABELS[event.booking_status]}.", "success")
    return _redirect("event-" + str(event.id))


@bp.post("/events/<int:event_id>/close")
def close_event(event_id):
    event = Event.query.get_or_404(event_id)
    status = request.form.get("status")

    if status not in EVENT_CLOSURE_STATUSES:
        flash("Bitte einen gültigen Abschlussstatus wählen.", "error")
        return _redirect("event-" + str(event.id))

    event.status = status
    db.session.commit()
    _sync_event_if_google_connected(event)
    flash(f"{event.name} wurde als {EVENT_STATUS_LABELS[event.status].lower()} markiert.", "success")
    return _redirect("event-" + str(event.id))


@bp.post("/events/<int:event_id>/delete")
def delete_event(event_id):
    event = Event.query.get_or_404(event_id)
    _delete_event_from_google_if_connected(event)
    db.session.delete(event)
    db.session.commit()
    flash(f"{event.name} wurde entfernt.", "success")
    return _redirect("events")


@bp.post("/google-calendar/settings")
def update_google_calendar_settings():
    calendar_id = _required_text("calendar_id", "Google Kalender-ID")

    if not calendar_id:
        return _redirect("google-calendar")

    connection = _get_or_create_google_calendar_connection()
    _set_google_calendar_id(connection, calendar_id)
    db.session.commit()

    if connection.is_connected:
        _sync_all_google_events(connection)
    else:
        flash("Google Kalender-ID wurde gespeichert. Verbinde Google Kalender, um Events zu synchronisieren.", "success")

    return _redirect("google-calendar")


@bp.post("/google-calendar/connect")
def connect_google_calendar():
    calendar_id = _required_text("calendar_id", "Google Kalender-ID")

    if not calendar_id:
        return _redirect("google-calendar")

    connection = _get_or_create_google_calendar_connection()
    _set_google_calendar_id(connection, calendar_id)
    db.session.commit()

    state = secrets.token_urlsafe(32)
    redirect_uri = google_redirect_uri()

    try:
        authorization_url, returned_state = build_authorization_url(state=state, redirect_uri=redirect_uri)
    except GoogleCalendarError as error:
        flash(str(error), "error")
        return _redirect("google-calendar")

    session[GOOGLE_OAUTH_STATE_KEY] = returned_state
    return redirect(authorization_url)


@bp.get("/google-calendar/oauth2callback")
def google_calendar_callback():
    expected_state = session.pop(GOOGLE_OAUTH_STATE_KEY, None)

    if request.args.get("error"):
        flash(f"Google Kalender wurde nicht verbunden: {request.args['error']}", "error")
        return _redirect("google-calendar")

    if not expected_state or request.args.get("state") != expected_state:
        flash("Google Kalender konnte wegen eines ungültigen OAuth-Status nicht verbunden werden.", "error")
        return _redirect("google-calendar")

    connection = _get_or_create_google_calendar_connection()

    try:
        connection.credentials_json = exchange_authorization_response(
            authorization_response=request.url,
            state=expected_state,
            redirect_uri=google_redirect_uri(),
            existing_credentials_json=connection.credentials_json,
        )
    except GoogleCalendarError as error:
        flash(str(error), "error")
        return _redirect("google-calendar")

    connection.connected_at = _utc_now()
    connection.updated_at = _utc_now()
    connection.last_error = None
    db.session.commit()

    _sync_all_google_events(connection)
    return _redirect("google-calendar")


@bp.post("/google-calendar/sync")
def sync_google_calendar():
    connection = _google_calendar_connection()
    _sync_all_google_events(connection)
    return _redirect("google-calendar")


@bp.post("/google-calendar/disconnect")
def disconnect_google_calendar():
    connection = _google_calendar_connection()

    if not connection:
        flash("Google Kalender ist nicht verbunden.", "error")
        return _redirect("google-calendar")

    connection.credentials_json = None
    connection.connected_at = None
    connection.updated_at = _utc_now()
    connection.last_error = None
    db.session.commit()
    flash("Google Kalender wurde getrennt. Bestehende Kalendereinträge bleiben erhalten.", "success")
    return _redirect("google-calendar")


@bp.post("/materials")
def create_material():
    name = _required_text("name", "Materialname")
    kind = request.form.get("kind", MATERIAL_FIXED)
    quantity = _non_negative_int("total_quantity", "Gesamtmenge")
    unit = _required_text("unit", "Einheit")

    if kind not in MATERIAL_KINDS:
        flash("Bitte festes Material oder Verbrauchsmaterial wählen.", "error")
        return _redirect("inventory")

    if not all((name, unit)) or quantity is None:
        return _redirect("inventory")

    material = Material(name=name, kind=kind, total_quantity=quantity, unit=unit, notes=_optional_text("notes"))
    db.session.add(material)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Dieses Material existiert bereits.", "error")
        return _redirect("inventory")

    flash(f"{material.name} wurde zum Inventar hinzugefügt.", "success")
    return _redirect("inventory")


@bp.post("/materials/<int:material_id>/quantity")
def update_material_quantity(material_id):
    material = Material.query.get_or_404(material_id)
    quantity = _non_negative_int("total_quantity", "Gesamtmenge")

    if quantity is None:
        return _redirect("inventory")

    material.total_quantity = quantity
    db.session.commit()
    flash(f"Gesamtmenge von {material.name} wurde aktualisiert.", "success")
    return _redirect("inventory")


@bp.post("/materials/<int:material_id>/delete")
def delete_material(material_id):
    material = Material.query.get_or_404(material_id)
    db.session.delete(material)
    db.session.commit()
    flash(f"{material.name} wurde aus dem Inventar entfernt.", "success")
    return _redirect("inventory")


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

    db.session.commit()
    _sync_event_if_google_connected(event)
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
    db.session.commit()
    _sync_event_if_google_connected(assignment.event)
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
    db.session.commit()
    db.session.expire(event, ["material_assignments"])
    _sync_event_if_google_connected(event)
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
    db.session.commit()
    _sync_event_if_google_connected(event)
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
    db.session.commit()
    db.session.expire(event, ["personnel_assignments"])
    _sync_event_if_google_connected(event)
    flash(f"Zuweisung von {person_name} wurde entfernt.", "success")
    return _redirect("event-" + str(event_id))


@bp.app_template_filter("date_only")
def date_only(value):
    return value.strftime("%d.%m.%Y")


def _parse_date_local(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _redirect(anchor):
    return redirect(url_for("main.index") + f"#{anchor}")


def _event_is_archived(event, today_start):
    return event.status in (STATUS_COMPLETED, STATUS_CANCELLED) or (
        event.status == STATUS_PLANNED and event.ends_at <= today_start
    )


def _event_is_open_for_assignment(event):
    return event.status == STATUS_PLANNED and not _event_is_archived(event, _today_start())


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


def _sync_event_if_google_connected(event):
    connection = _google_calendar_connection()

    if not connection or not connection.calendar_id or not connection.is_connected:
        return

    try:
        sync_event_to_google(event, connection)
    except GoogleCalendarError as error:
        event.google_sync_error = str(error)
        connection.last_error = str(error)
        db.session.commit()
        flash(f"Google Kalender konnte nicht synchronisiert werden: {error}", "error")
        return

    connection.last_synced_at = _utc_now()
    connection.last_error = None
    db.session.commit()


def _delete_event_from_google_if_connected(event):
    connection = _google_calendar_connection()

    if not connection or not connection.calendar_id or not connection.is_connected or not event.google_event_id:
        return

    try:
        delete_event_from_google(event, connection)
    except GoogleCalendarError as error:
        connection.last_error = str(error)
        flash(f"Google Kalender konnte nicht bereinigt werden: {error}", "error")


def _sync_all_google_events(connection):
    if not connection or not connection.calendar_id:
        flash("Bitte zuerst eine Google Kalender-ID speichern.", "error")
        return

    if not connection.is_connected:
        flash("Bitte zuerst Google Kalender verbinden.", "error")
        return

    try:
        result = sync_all_events_to_google(connection, Event.query.order_by(Event.starts_at.asc()).all())
    except GoogleCalendarError as error:
        connection.last_error = str(error)
        db.session.commit()
        flash(f"Google Kalender konnte nicht synchronisiert werden: {error}", "error")
        return

    db.session.commit()
    if result["failed"]:
        flash(f"{result['synced']} Events synchronisiert, {result['failed']} fehlgeschlagen.", "error")
    else:
        flash(f"{result['synced']} Events wurden mit Google Kalender synchronisiert.", "success")


def _today_start():
    return datetime.combine(datetime.now().date(), time.min)


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
