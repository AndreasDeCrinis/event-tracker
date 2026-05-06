from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import text

from app import create_app, db
from app import routes as routes_module
from app.models import (
    BOOKING_FIXED,
    BOOKING_PLANNING,
    GOOGLE_SYNC_ACTION_DELETE,
    GOOGLE_SYNC_ACTION_UPSERT,
    GOOGLE_SYNC_STATUS_PENDING,
    MATERIAL_CONSUMABLE,
    MATERIAL_FIXED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PLANNED,
    Event,
    EventMaterial,
    EventPersonnel,
    EventTemplate,
    EventTemplateMaterial,
    EventTemplatePersonnel,
    GoogleCalendarConnection,
    GoogleCalendarSyncJob,
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
)
from app.google_calendar import google_event_body, sync_event_to_google
from app.google_calendar_queue import process_pending_google_calendar_jobs, queue_google_event_sync


def make_event(name, starts_on, ends_on, location="Hall", booking_status=BOOKING_FIXED):
    return Event(
        name=name,
        starts_at=datetime.combine(starts_on, time.min),
        ends_at=datetime.combine(ends_on + timedelta(days=1), time.min),
        location=location,
        booking_status=booking_status,
    )


@pytest.fixture()
def app():
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "test",
        }
    )

    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_fixed_material_availability_counts_active_planned_assignments(app):
    with app.app_context():
        flamethrowers = Material(name="Flamethrowers", kind=MATERIAL_FIXED, total_quantity=10, unit="pcs")
        party = make_event("Party", date(2026, 6, 1), date(2026, 6, 1))
        birthday = make_event("Birthday", date(2026, 6, 1), date(2026, 6, 1), location="Garden")
        db.session.add_all([flamethrowers, party, birthday])
        db.session.flush()
        db.session.add_all(
            [
                EventMaterial(event=party, material=flamethrowers, quantity=2),
                EventMaterial(event=birthday, material=flamethrowers, quantity=4),
            ]
        )
        db.session.commit()

        assert material_available_quantity(flamethrowers, moment=datetime(2026, 6, 1)) == 4
        assert material_available_quantity(flamethrowers, moment=datetime(2026, 6, 2)) == 10


def test_fixed_material_can_be_reused_for_non_overlapping_events(app):
    with app.app_context():
        item = Material(name="Projectors", kind=MATERIAL_FIXED, total_quantity=5, unit="pcs")
        day_one = make_event("Day one", date(2026, 6, 1), date(2026, 6, 1))
        overlap = make_event("Overlap", date(2026, 6, 1), date(2026, 6, 1))
        later = make_event("Later", date(2026, 6, 2), date(2026, 6, 2))
        db.session.add_all([item, day_one, overlap, later])
        db.session.flush()
        db.session.add(EventMaterial(event=day_one, material=item, quantity=4))
        db.session.commit()

        assert material_assignable_quantity(item, overlap) == 1
        assert material_assignable_quantity(item, later) == 5


def test_fixed_material_returns_after_completion(app):
    with app.app_context():
        item = Material(name="Stage lights", kind=MATERIAL_FIXED, total_quantity=6, unit="pcs")
        event = make_event("Show", date(2026, 6, 4), date(2026, 6, 4), location="Club")
        db.session.add_all([item, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=item, quantity=5))
        db.session.commit()

        assert material_available_quantity(item, moment=datetime(2026, 6, 4)) == 1

        event.status = STATUS_COMPLETED
        db.session.commit()

        assert material_available_quantity(item, moment=datetime(2026, 6, 4)) == 6


def test_consumable_material_reduces_total_quantity_after_successful_completion(app):
    with app.app_context():
        fuel = Material(name="Flamethrower fuel", kind=MATERIAL_CONSUMABLE, total_quantity=100, unit="bottles")
        event = make_event("Night show", date(2026, 6, 5), date(2026, 6, 5), location="Arena")
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()
        event_id = event.id
        fuel_id = fuel.id

        assert material_available_quantity(fuel) == 80

    response = app.test_client().post(f"/events/{event_id}/close", data={"status": STATUS_COMPLETED})

    assert response.status_code == 302
    with app.app_context():
        fuel = db.session.get(Material, fuel_id)
        event = db.session.get(Event, event_id)
        assert fuel.total_quantity == 80
        assert event.consumables_deducted_at is not None
        assert material_allocated_quantity(fuel) == 0
        assert material_available_quantity(fuel) == 80


def test_legacy_completed_consumable_without_deduction_marker_stays_allocated(app):
    with app.app_context():
        fuel = Material(name="Legacy completed fuel", kind=MATERIAL_CONSUMABLE, total_quantity=100, unit="bottles")
        event = make_event("Legacy completed event", date(2026, 6, 6), date(2026, 6, 6))
        event.status = STATUS_COMPLETED
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()

        assert event.consumables_deducted_at is None
        assert fuel.total_quantity == 100
        assert material_allocated_quantity(fuel) == 20
        assert material_available_quantity(fuel) == 80


def test_past_planned_consumable_is_visualized_as_open_used(app):
    with app.app_context():
        fuel = Material(name="Past open fuel", kind=MATERIAL_CONSUMABLE, total_quantity=100, unit="bottles")
        event = make_event("Past open fuel event", date(2000, 1, 1), date(2000, 1, 1))
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()

        assert material_reserved_quantity(fuel, moment=datetime(2026, 1, 1)) == 0
        assert material_open_used_quantity(fuel, moment=datetime(2026, 1, 1)) == 20
        assert material_available_quantity(fuel, moment=datetime(2026, 1, 1)) == 80


def test_future_planned_consumable_is_visualized_as_reserved(app):
    with app.app_context():
        fuel = Material(name="Future reserved fuel", kind=MATERIAL_CONSUMABLE, total_quantity=100, unit="bottles")
        event = make_event("Future reserved fuel event", date(2999, 1, 1), date(2999, 1, 1))
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()

        assert material_reserved_quantity(fuel, moment=datetime(2026, 1, 1)) == 20
        assert material_open_used_quantity(fuel, moment=datetime(2026, 1, 1)) == 0
        assert material_available_quantity(fuel, moment=datetime(2026, 1, 1)) == 80


def test_completed_deducted_consumable_is_visualized_as_deducted_history(app):
    with app.app_context():
        fuel = Material(name="Deducted history fuel", kind=MATERIAL_CONSUMABLE, total_quantity=80, unit="bottles")
        event = make_event("Deducted history event", date(2026, 1, 1), date(2026, 1, 1))
        event.status = STATUS_COMPLETED
        event.consumables_deducted_at = datetime(2026, 1, 2)
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()

        assert material_reserved_quantity(fuel, moment=datetime(2026, 1, 3)) == 0
        assert material_open_used_quantity(fuel, moment=datetime(2026, 1, 3)) == 0
        assert material_deducted_used_quantity(fuel) == 20
        assert material_available_quantity(fuel, moment=datetime(2026, 1, 3)) == 80


def test_consumable_material_is_not_reduced_when_event_is_cancelled(app):
    with app.app_context():
        fuel = Material(name="Cancelled fuel", kind=MATERIAL_CONSUMABLE, total_quantity=100, unit="bottles")
        event = make_event("Cancelled fuel event", date(2026, 6, 6), date(2026, 6, 6))
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()
        event_id = event.id
        fuel_id = fuel.id

    response = app.test_client().post(f"/events/{event_id}/close", data={"status": STATUS_CANCELLED})

    assert response.status_code == 302
    with app.app_context():
        fuel = db.session.get(Material, fuel_id)
        event = db.session.get(Event, event_id)
        assert fuel.total_quantity == 100
        assert event.consumables_deducted_at is None
        assert material_available_quantity(fuel) == 100


def test_consumable_material_cannot_be_deducted_twice(app):
    with app.app_context():
        fuel = Material(name="Idempotent fuel", kind=MATERIAL_CONSUMABLE, total_quantity=100, unit="bottles")
        event = make_event("Idempotent fuel event", date(2026, 6, 7), date(2026, 6, 7))
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()
        event_id = event.id
        fuel_id = fuel.id

    client = app.test_client()
    client.post(f"/events/{event_id}/close", data={"status": STATUS_COMPLETED})
    response = client.post(f"/events/{event_id}/close", data={"status": STATUS_COMPLETED})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Material, fuel_id).total_quantity == 80


def test_consumable_completion_rejects_when_available_total_is_insufficient(app):
    with app.app_context():
        fuel = Material(name="Overused fuel", kind=MATERIAL_CONSUMABLE, total_quantity=10, unit="bottles")
        event = make_event(
            "Overused planning event",
            date(2026, 6, 8),
            date(2026, 6, 8),
            booking_status=BOOKING_PLANNING,
        )
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=20))
        db.session.commit()
        event_id = event.id
        fuel_id = fuel.id

    response = app.test_client().post(f"/events/{event_id}/close", data={"status": STATUS_COMPLETED})

    assert response.status_code == 302
    with app.app_context():
        fuel = db.session.get(Material, fuel_id)
        event = db.session.get(Event, event_id)
        assert fuel.total_quantity == 10
        assert event.status == STATUS_PLANNED
        assert event.consumables_deducted_at is None


def test_personnel_is_available_after_completed_event(app):
    with app.app_context():
        person = Personnel(name="Alex Morgan", role="Pyro tech")
        event = make_event("Launch", date(2026, 7, 1), date(2026, 7, 1), location="Pier")
        db.session.add_all([person, event])
        db.session.flush()
        db.session.add(EventPersonnel(event=event, personnel=person))
        db.session.commit()

        assert not personnel_is_available(person, moment=datetime(2026, 7, 1))
        assert personnel_is_available(person, moment=datetime(2026, 7, 2))

        event.status = STATUS_COMPLETED
        db.session.commit()

        assert personnel_is_available(person, moment=datetime(2026, 7, 1))


def test_personnel_conflict_only_blocks_overlapping_planned_events(app):
    with app.app_context():
        person = Personnel(name="Sam Rivera", role="Tech")
        first = make_event("First", date(2026, 8, 1), date(2026, 8, 1), location="A")
        overlap = make_event("Overlap", date(2026, 8, 1), date(2026, 8, 1), location="B")
        later = make_event("Later", date(2026, 8, 2), date(2026, 8, 2), location="C")
        db.session.add_all([person, first, overlap, later])
        db.session.flush()
        db.session.add(EventPersonnel(event=first, personnel=person))
        db.session.commit()

        assert personnel_has_conflict(person, overlap)
        assert not personnel_has_conflict(person, later)


def test_event_can_be_closed_as_successfully_completed(app):
    with app.app_context():
        event = make_event("Show", date(2026, 9, 1), date(2026, 9, 1))
        db.session.add(event)
        db.session.commit()
        event_id = event.id

    response = app.test_client().post(f"/events/{event_id}/close", data={"status": STATUS_COMPLETED})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Event, event_id).status == STATUS_COMPLETED


def test_event_can_be_closed_as_cancelled(app):
    with app.app_context():
        event = make_event("Cancelled show", date(2026, 9, 2), date(2026, 9, 2))
        db.session.add(event)
        db.session.commit()
        event_id = event.id

    response = app.test_client().post(f"/events/{event_id}/close", data={"status": STATUS_CANCELLED})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Event, event_id).status == STATUS_CANCELLED


def test_event_closure_rejects_planned_status(app):
    with app.app_context():
        event = make_event("Still planned", date(2026, 9, 3), date(2026, 9, 3))
        db.session.add(event)
        db.session.commit()
        event_id = event.id

    response = app.test_client().post(f"/events/{event_id}/close", data={"status": STATUS_PLANNED})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Event, event_id).status == STATUS_PLANNED


def test_archive_lists_completed_cancelled_and_past_events(app):
    with app.app_context():
        active = make_event("Future active job", date(2999, 1, 1), date(2999, 1, 1))
        past = make_event("Past planned job", date(2000, 1, 1), date(2000, 1, 1))
        completed = make_event("Completed archive job", date(2999, 1, 2), date(2999, 1, 2))
        completed.status = STATUS_COMPLETED
        cancelled = make_event("Cancelled archive job", date(2999, 1, 3), date(2999, 1, 3))
        cancelled.status = STATUS_CANCELLED
        db.session.add_all([active, past, completed, cancelled])
        db.session.commit()

    html = app.test_client().get("/").data.decode()
    active_section = html.rindex("<h2>Aktive Events</h2>")
    archive_section = html.rindex("<h2>Job-Archiv</h2>")

    assert active_section < html.index("Future active job") < archive_section
    assert html.index("Past planned job") > archive_section
    assert html.index("Completed archive job") > archive_section
    assert html.index("Cancelled archive job") > archive_section


def test_past_planned_event_rejects_new_material_assignment(app):
    with app.app_context():
        material = Material(name="Archived item", kind=MATERIAL_FIXED, total_quantity=5, unit="pcs")
        event = make_event("Past event", date(2000, 1, 1), date(2000, 1, 1))
        db.session.add_all([material, event])
        db.session.commit()
        event_id = event.id
        material_id = material.id

    response = app.test_client().post(
        f"/events/{event_id}/materials",
        data={"material_id": material_id, "quantity": 1},
    )

    assert response.status_code == 302
    with app.app_context():
        assert EventMaterial.query.filter_by(event_id=event_id, material_id=material_id).count() == 0


def test_planning_event_material_can_exceed_inventory_without_booking(app):
    with app.app_context():
        material = Material(name="Planning flamethrowers", kind=MATERIAL_FIXED, total_quantity=2, unit="pcs")
        event = make_event(
            "Planning event",
            date(2999, 2, 1),
            date(2999, 2, 1),
            booking_status=BOOKING_PLANNING,
        )
        db.session.add_all([material, event])
        db.session.commit()
        event_id = event.id
        material_id = material.id

    response = app.test_client().post(
        f"/events/{event_id}/materials",
        data={"material_id": material_id, "quantity": 5},
    )

    assert response.status_code == 302
    with app.app_context():
        material = db.session.get(Material, material_id)
        event = db.session.get(Event, event_id)
        assert material_available_quantity(material, target_event=event) == 2
        assert material_shortage_quantity(material, event) == 3


def test_planning_event_with_shortage_renders_material_warning(app):
    with app.app_context():
        material = Material(name="Warning fuel rigs", kind=MATERIAL_FIXED, total_quantity=1, unit="pcs")
        event = make_event(
            "Warning event",
            date(2999, 3, 1),
            date(2999, 3, 1),
            booking_status=BOOKING_PLANNING,
        )
        db.session.add_all([material, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=material, quantity=3))
        db.session.commit()

    html = app.test_client().get("/").data.decode()

    assert "Material eventuell nicht ausreichend verfügbar" in html
    assert "Warning fuel rigs" in html


def test_planning_event_with_shortage_cannot_be_fixed(app):
    with app.app_context():
        material = Material(name="Short booking item", kind=MATERIAL_FIXED, total_quantity=1, unit="pcs")
        event = make_event(
            "Short booking",
            date(2999, 4, 1),
            date(2999, 4, 1),
            booking_status=BOOKING_PLANNING,
        )
        db.session.add_all([material, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=material, quantity=3))
        db.session.commit()
        event_id = event.id

    response = app.test_client().post(
        f"/events/{event_id}/booking-status",
        data={"booking_status": BOOKING_FIXED},
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Event, event_id).booking_status == BOOKING_PLANNING


def test_planning_event_without_shortage_can_be_fixed_and_books_inventory(app):
    with app.app_context():
        material = Material(name="Available booking item", kind=MATERIAL_FIXED, total_quantity=4, unit="pcs")
        event = make_event(
            "Available booking",
            date(2999, 5, 1),
            date(2999, 5, 1),
            booking_status=BOOKING_PLANNING,
        )
        db.session.add_all([material, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=material, quantity=3))
        db.session.commit()
        event_id = event.id
        material_id = material.id

    response = app.test_client().post(
        f"/events/{event_id}/booking-status",
        data={"booking_status": BOOKING_FIXED},
    )

    assert response.status_code == 302
    with app.app_context():
        event = db.session.get(Event, event_id)
        material = db.session.get(Material, material_id)
        assert event.booking_status == BOOKING_FIXED
        assert material_available_quantity(material, target_event=event) == 1


def test_event_location_is_optional(app):
    response = app.test_client().post(
        "/events",
        data={
            "name": "Location optional",
            "starts_on": "2999-06-01",
            "ends_on": "2999-06-01",
            "booking_status": BOOKING_PLANNING,
        },
    )

    assert response.status_code == 302
    with app.app_context():
        event = Event.query.filter_by(name="Location optional").one()
        assert event.location is None


def test_event_can_be_created_with_optional_start_and_end_time(app):
    response = app.test_client().post(
        "/events",
        data={
            "name": "Timed event",
            "starts_on": "2999-06-01",
            "starts_at_time": "10:30",
            "ends_on": "2999-06-01",
            "ends_at_time": "12:00",
            "booking_status": BOOKING_PLANNING,
        },
    )

    assert response.status_code == 302
    with app.app_context():
        event = Event.query.filter_by(name="Timed event").one()
        assert event.starts_at == datetime(2999, 6, 1, 10, 30)
        assert event.ends_at == datetime(2999, 6, 1, 12, 0)
        assert not event.is_all_day

    html = app.test_client().get("/").data.decode()
    assert "01.06.2999 10:30 bis 01.06.2999 12:00" in html


def test_event_time_requires_start_and_end_time_together(app):
    response = app.test_client().post(
        "/events",
        data={
            "name": "Broken timed event",
            "starts_on": "2999-06-01",
            "starts_at_time": "10:30",
            "ends_on": "2999-06-01",
            "booking_status": BOOKING_PLANNING,
        },
    )

    assert response.status_code == 302
    with app.app_context():
        assert Event.query.filter_by(name="Broken timed event").count() == 0


def test_timed_events_only_block_overlapping_ranges(app):
    with app.app_context():
        material = Material(name="Timed projector", kind=MATERIAL_FIXED, total_quantity=1, unit="pcs")
        first = Event(
            name="Morning",
            starts_at=datetime(2999, 7, 1, 9, 0),
            ends_at=datetime(2999, 7, 1, 10, 0),
            booking_status=BOOKING_FIXED,
        )
        later = Event(
            name="Later",
            starts_at=datetime(2999, 7, 1, 10, 0),
            ends_at=datetime(2999, 7, 1, 11, 0),
            booking_status=BOOKING_FIXED,
        )
        overlap = Event(
            name="Overlap",
            starts_at=datetime(2999, 7, 1, 9, 30),
            ends_at=datetime(2999, 7, 1, 10, 30),
            booking_status=BOOKING_FIXED,
        )
        db.session.add_all([material, first, later, overlap])
        db.session.flush()
        db.session.add(EventMaterial(event=first, material=material, quantity=1))
        db.session.commit()

        assert material_assignable_quantity(material, later) == 1
        assert material_assignable_quantity(material, overlap) == 0


def test_material_total_quantity_can_be_updated(app):
    with app.app_context():
        material = Material(name="Editable total", kind=MATERIAL_FIXED, total_quantity=2, unit="pcs")
        db.session.add(material)
        db.session.commit()
        material_id = material.id

    response = app.test_client().post(
        f"/materials/{material_id}/quantity",
        data={"total_quantity": 8},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/inventory#inventory"
    with app.app_context():
        assert db.session.get(Material, material_id).total_quantity == 8


def test_inventory_renders_material_groups_and_consumable_usage_metrics(app):
    with app.app_context():
        fixed = Material(name="Projector", kind=MATERIAL_FIXED, total_quantity=2, unit="pcs")
        consumable = Material(name="Gas bottles", kind=MATERIAL_CONSUMABLE, total_quantity=100, unit="Stk.")
        event = make_event("Past gas event", date(2000, 1, 1), date(2000, 1, 1))
        db.session.add_all([fixed, consumable, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=consumable, quantity=20))
        db.session.commit()

    html = app.test_client().get("/inventory").data.decode()

    assert "<h2>Inventar</h2>" in html
    assert "Festes Material" in html
    assert "Verbrauchsmaterial" in html
    assert "Verbraucht offen" in html
    assert "20 Stk." in html
    assert "80 Stk." in html
    assert 'class="material-item material-kind-fixed' in html
    assert 'id="material-' in html
    assert 'data-collapse-state-key="material:' in html
    assert '<details class="material-item material-kind-fixed" open>' not in html
    assert 'class="material-item-header collapsible-summary"' in html
    assert "<table" not in html


def test_inventory_warns_when_planned_material_exceeds_stock(app):
    future_day = datetime.now().date() + timedelta(days=30)

    with app.app_context():
        material = Material(name="Warning inventory item", kind=MATERIAL_FIXED, total_quantity=3, unit="pcs")
        planning = make_event(
            "Planning demand",
            future_day,
            future_day,
            booking_status=BOOKING_PLANNING,
        )
        fixed = make_event("Fixed demand", future_day, future_day)
        cancelled = make_event("Cancelled demand", future_day, future_day)
        cancelled.status = STATUS_CANCELLED
        db.session.add_all([material, planning, fixed, cancelled])
        db.session.flush()
        db.session.add_all(
            [
                EventMaterial(event=planning, material=material, quantity=2),
                EventMaterial(event=fixed, material=material, quantity=2),
                EventMaterial(event=cancelled, material=material, quantity=10),
            ]
        )
        db.session.commit()

    html = app.test_client().get("/inventory").data.decode()

    assert "material-shortage" in html
    assert "1 pcs fehlen</span>" in html
    assert "Geplante Menge übersteigt den Bestand." in html
    assert "4 pcs geplant" in html
    assert "3 pcs im Bestand" in html
    assert "1 pcs fehlen" in html


def test_inventory_management_is_on_separate_page(app):
    client = app.test_client()
    dashboard_html = client.get("/").data.decode()
    inventory_html = client.get("/inventory").data.decode()

    assert '<h2>Inventar</h2>' not in dashboard_html
    assert 'href="/inventory"' in dashboard_html
    assert '<h2>Inventar</h2>' in inventory_html
    assert "/materials" in inventory_html


def test_event_templates_are_managed_on_dedicated_page(app):
    client = app.test_client()
    dashboard_html = client.get("/").data.decode()
    templates_html = client.get("/templates").data.decode()

    assert '<h2>Event-Vorlagen</h2>' not in dashboard_html
    assert 'href="/templates"' in dashboard_html
    assert '<h2>Event-Vorlagen</h2>' in templates_html
    assert "/templates" in templates_html
    assert "Vorlage hinzufügen" in templates_html
    assert "Aus Vorlage erstellen" in templates_html or "Keine Vorlagen" in templates_html


def test_event_list_view_is_default(app):
    with app.app_context():
        event = make_event("List view event", date(2999, 1, 10), date(2999, 1, 10))
        template = EventTemplate(name="Dashboard setup", event_name="Loaded dashboard event", duration_days=1)
        db.session.add(event)
        db.session.add(template)
        db.session.commit()
        event_id = event.id

    html = app.test_client().get("/").data.decode()

    assert 'class="event-list"' in html
    assert '<details id="event-' in html
    assert f'data-collapse-state-key="event:{event_id}"' in html
    assert f'action="/events/{event_id}/calendar-sync"' in html
    assert "Mit Google Kalender synchronisieren" in html
    assert 'src="/static/collapsible-state.js"' in html
    assert 'class="event-card status-planned " open>' not in html
    assert 'class="event-card-header collapsible-summary"' in html
    assert "List view event" in html
    assert "Vorlage laden" in html
    assert "Dashboard setup" in html
    assert 'src="/static/event-template-loader.js"' in html
    assert "Kalender" in html
    assert 'class="calendar-panel"' not in html


def test_event_list_orders_events_by_start_date_closest_to_today(app):
    today = datetime.now().date()

    with app.app_context():
        farther = make_event("Farther active event", today + timedelta(days=20), today + timedelta(days=20))
        closer = make_event("Closer active event", today + timedelta(days=2), today + timedelta(days=2))
        db.session.add_all([farther, closer])
        db.session.commit()

    html = app.test_client().get("/").data.decode()

    assert html.index("Closer active event") < html.index("Farther active event")


def test_event_calendar_view_renders_month_grid_and_event_links(app):
    with app.app_context():
        event = make_event("Calendar party", date(2999, 1, 10), date(2999, 1, 12))
        db.session.add(event)
        db.session.commit()
        event_id = event.id

    html = app.test_client().get("/?view=calendar&month=2999-01").data.decode()

    assert 'class="calendar-panel"' in html
    assert "Januar 2999" in html
    assert "Mo" in html
    assert "So" in html
    assert "Calendar party" in html
    assert "10.01.2999 bis 12.01.2999" in html
    assert f'/?view=list#event-{event_id}' in html


def test_event_template_can_create_event_with_assignments(app):
    with app.app_context():
        material = Material(name="Template lights", kind=MATERIAL_FIXED, total_quantity=5, unit="pcs")
        person = Personnel(name="Template tech", role="Tech")
        db.session.add_all([material, person])
        db.session.commit()
        material_id = material.id
        person_id = person.id

    client = app.test_client()
    response = client.post(
        "/templates",
        data={
            "name": "Show setup",
            "event_name": "Template show",
            "duration_days": "2",
            "starts_at_time": "10:00",
            "ends_at_time": "18:00",
            "location": "Main hall",
            "booking_status": BOOKING_FIXED,
            "notes": "Bring checklist",
            "sync_to_google_calendar": "1",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        template = EventTemplate.query.filter_by(name="Show setup").one()
        template_id = template.id

    assert client.post(
        f"/templates/{template_id}/materials",
        data={"material_id": material_id, "quantity": "3"},
    ).status_code == 302
    assert client.post(
        f"/templates/{template_id}/personnel",
        data={"personnel_id": person_id},
    ).status_code == 302

    response = client.post(
        f"/templates/{template_id}/events",
        data={"starts_on": "2999-02-01", "event_name": "Applied show"},
    )

    assert response.status_code == 302
    with app.app_context():
        event = Event.query.filter_by(name="Applied show").one()
        assert event.starts_at == datetime(2999, 2, 1, 10, 0)
        assert event.ends_at == datetime(2999, 2, 2, 18, 0)
        assert event.location == "Main hall"
        assert event.booking_status == BOOKING_FIXED
        assert event.notes == "Bring checklist"
        assert event.sync_to_google_calendar is True
        assert [(assignment.material.name, assignment.quantity) for assignment in event.material_assignments] == [
            ("Template lights", 3)
        ]
        assert [assignment.personnel.name for assignment in event.personnel_assignments] == ["Template tech"]


def test_event_template_create_form_can_save_default_material(app):
    with app.app_context():
        flamethrowers = Material(name="Template flamethrowers", kind=MATERIAL_FIXED, total_quantity=8, unit="Stk.")
        gas_cans = Material(name="Template gas cans", kind=MATERIAL_CONSUMABLE, total_quantity=20, unit="Kanister")
        db.session.add_all([flamethrowers, gas_cans])
        db.session.commit()
        flamethrower_id = flamethrowers.id
        gas_can_id = gas_cans.id

    response = app.test_client().post(
        "/templates",
        data={
            "name": "Edmunt",
            "event_name": "Edmunt",
            "duration_days": "1",
            "booking_status": BOOKING_PLANNING,
            "sync_to_google_calendar": "1",
            "template_material_ids": [str(flamethrower_id), str(gas_can_id)],
            f"template_material_quantity_{flamethrower_id}": "3",
            f"template_material_quantity_{gas_can_id}": "5",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        template = EventTemplate.query.filter_by(name="Edmunt").one()
        assert [(assignment.material.name, assignment.quantity) for assignment in template.material_assignments] == [
            ("Template flamethrowers", 3),
            ("Template gas cans", 5),
        ]

    html = app.test_client().get("/templates").data.decode()
    assert "Template flamethrowers" in html
    assert "Template gas cans" in html


def test_dashboard_event_form_can_apply_selected_template_assignments(app):
    with app.app_context():
        material = Material(name="Dashboard template lights", kind=MATERIAL_FIXED, total_quantity=4, unit="pcs")
        person = Personnel(name="Dashboard template tech", role="Tech")
        template = EventTemplate(
            name="Dashboard template",
            event_name="Dashboard loaded show",
            duration_days=1,
            booking_status=BOOKING_FIXED,
        )
        db.session.add_all([material, person, template])
        db.session.flush()
        db.session.add_all(
            [
                EventTemplateMaterial(template=template, material=material, quantity=2),
                EventTemplatePersonnel(template=template, personnel=person),
            ]
        )
        db.session.commit()
        template_id = template.id

    response = app.test_client().post(
        "/events",
        data={
            "event_template_id": str(template_id),
            "name": "Dashboard applied show",
            "starts_on": "2999-02-06",
            "ends_on": "2999-02-06",
            "booking_status": BOOKING_FIXED,
            "sync_to_google_calendar": "1",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        event = Event.query.filter_by(name="Dashboard applied show").one()
        assert [(assignment.material.name, assignment.quantity) for assignment in event.material_assignments] == [
            ("Dashboard template lights", 2)
        ]
        assert [assignment.personnel.name for assignment in event.personnel_assignments] == [
            "Dashboard template tech"
        ]


def test_event_template_can_disable_google_calendar_sync_for_created_event(app):
    with app.app_context():
        connection = GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}")
        template = EventTemplate(
            name="No calendar template",
            event_name="No calendar show",
            duration_days=1,
            booking_status=BOOKING_PLANNING,
            sync_to_google_calendar=False,
        )
        db.session.add_all([connection, template])
        db.session.commit()
        template_id = template.id

    response = app.test_client().post(
        f"/templates/{template_id}/events",
        data={"starts_on": "2999-02-03"},
    )

    assert response.status_code == 302
    with app.app_context():
        event = Event.query.filter_by(name="No calendar show").one()
        assert event.sync_to_google_calendar is False
        assert GoogleCalendarSyncJob.query.count() == 0


def test_fixed_event_template_checks_material_availability_when_creating_event(app):
    with app.app_context():
        material = Material(name="Scarce template gear", kind=MATERIAL_FIXED, total_quantity=1, unit="pcs")
        template = EventTemplate(
            name="Scarce setup",
            event_name="Blocked template show",
            duration_days=1,
            booking_status=BOOKING_FIXED,
        )
        db.session.add_all([material, template])
        db.session.flush()
        db.session.add(EventTemplateMaterial(template=template, material=material, quantity=2))
        db.session.commit()
        template_id = template.id

    response = app.test_client().post(
        f"/templates/{template_id}/events",
        data={"starts_on": "2999-02-04"},
    )

    assert response.status_code == 302
    with app.app_context():
        assert Event.query.filter_by(name="Blocked template show").count() == 0


def test_fixed_event_material_assignment_quantity_can_be_updated_when_available(app):
    with app.app_context():
        material = Material(name="Editable assignment", kind=MATERIAL_FIXED, total_quantity=5, unit="pcs")
        event = make_event("Editable fixed event", date(2999, 7, 1), date(2999, 7, 1))
        db.session.add_all([material, event])
        db.session.flush()
        assignment = EventMaterial(event=event, material=material, quantity=2)
        db.session.add(assignment)
        db.session.commit()
        assignment_id = assignment.id
        material_id = material.id
        event_id = event.id

    response = app.test_client().post(
        f"/assignments/material/{assignment_id}/quantity",
        data={"quantity": 4},
    )

    assert response.status_code == 302
    with app.app_context():
        assignment = db.session.get(EventMaterial, assignment_id)
        material = db.session.get(Material, material_id)
        event = db.session.get(Event, event_id)
        assert assignment.quantity == 4
        assert material_available_quantity(material, target_event=event) == 1


def test_fixed_event_material_assignment_quantity_rejects_overbooking(app):
    with app.app_context():
        material = Material(name="Rejected assignment edit", kind=MATERIAL_FIXED, total_quantity=3, unit="pcs")
        event = make_event("Rejected fixed event", date(2999, 8, 1), date(2999, 8, 1))
        db.session.add_all([material, event])
        db.session.flush()
        assignment = EventMaterial(event=event, material=material, quantity=2)
        db.session.add(assignment)
        db.session.commit()
        assignment_id = assignment.id

    response = app.test_client().post(
        f"/assignments/material/{assignment_id}/quantity",
        data={"quantity": 4},
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(EventMaterial, assignment_id).quantity == 2


def test_planning_event_material_assignment_quantity_can_exceed_inventory(app):
    with app.app_context():
        material = Material(name="Editable planning assignment", kind=MATERIAL_FIXED, total_quantity=2, unit="pcs")
        event = make_event(
            "Editable planning event",
            date(2999, 9, 1),
            date(2999, 9, 1),
            booking_status=BOOKING_PLANNING,
        )
        db.session.add_all([material, event])
        db.session.flush()
        assignment = EventMaterial(event=event, material=material, quantity=1)
        db.session.add(assignment)
        db.session.commit()
        assignment_id = assignment.id
        material_id = material.id
        event_id = event.id

    response = app.test_client().post(
        f"/assignments/material/{assignment_id}/quantity",
        data={"quantity": 5},
    )

    assert response.status_code == 302
    with app.app_context():
        assignment = db.session.get(EventMaterial, assignment_id)
        material = db.session.get(Material, material_id)
        event = db.session.get(Event, event_id)
        assert assignment.quantity == 5
        assert material_shortage_quantity(material, event) == 3


def test_burger_menu_links_to_settings(app):
    html = app.test_client().get("/").data.decode()

    assert 'class="brand-lockup" href="/"' in html
    assert 'rel="icon" href="/static/app-logo.svg"' in html
    assert 'class="app-logo" src="/static/app-logo.svg"' in html
    assert 'class="menu-button"' in html
    assert 'href="/templates"' in html
    assert "Vorlagen" in html
    assert 'href="/settings"' in html
    assert "Einstellungen" in html


def test_google_calendar_section_renders_on_settings_page(app):
    dashboard_html = app.test_client().get("/").data.decode()
    settings_html = app.test_client().get("/settings").data.decode()

    assert "Google Kalender verbinden" not in dashboard_html
    assert "Google Kalender verbinden" in settings_html
    assert "Kalender-ID" in settings_html
    assert "/google-calendar/connect" in settings_html


def test_google_calendar_calendar_id_can_be_saved(app):
    response = app.test_client().post(
        "/google-calendar/settings",
        data={"calendar_id": "events@example.com"},
    )

    assert response.status_code == 302
    with app.app_context():
        connection = db.session.get(GoogleCalendarConnection, 1)
        assert connection.calendar_id == "events@example.com"


def test_google_calendar_connect_stores_oauth_state_and_code_verifier(app, monkeypatch):
    def fake_authorization_url(state, redirect_uri):
        return "https://accounts.google.com/o/oauth2/auth", state, "test-code-verifier"

    monkeypatch.setattr(routes_module, "build_authorization_url", fake_authorization_url)

    client = app.test_client()
    response = client.post(
        "/google-calendar/connect",
        data={"calendar_id": "primary"},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "https://accounts.google.com/o/oauth2/auth"
    with client.session_transaction() as session:
        assert session[routes_module.GOOGLE_OAUTH_STATE_KEY]
        assert session[routes_module.GOOGLE_OAUTH_CODE_VERIFIER_KEY] == "test-code-verifier"


def test_google_calendar_callback_passes_stored_code_verifier(app, monkeypatch):
    captured = {}

    def fake_exchange_authorization_response(**kwargs):
        captured.update(kwargs)
        return "{}"

    monkeypatch.setattr(routes_module, "exchange_authorization_response", fake_exchange_authorization_response)
    monkeypatch.setattr(routes_module, "_sync_all_google_events", lambda connection: None)

    client = app.test_client()
    with client.session_transaction() as session:
        session[routes_module.GOOGLE_OAUTH_STATE_KEY] = "test-state"
        session[routes_module.GOOGLE_OAUTH_CODE_VERIFIER_KEY] = "test-code-verifier"

    response = client.get("/google-calendar/oauth2callback?state=test-state&code=test-code")

    assert response.status_code == 302
    assert captured["state"] == "test-state"
    assert captured["code_verifier"] == "test-code-verifier"
    with app.app_context():
        assert db.session.get(GoogleCalendarConnection, 1).credentials_json == "{}"


def test_google_calendar_event_body_uses_event_date_range_and_assignments(app):
    with app.app_context():
        material = Material(name="Stage lights", kind=MATERIAL_FIXED, total_quantity=6, unit="pcs")
        person = Personnel(name="Alex Morgan", role="Tech")
        event = make_event(
            "Calendar show",
            date(2999, 10, 1),
            date(2999, 10, 3),
            location=None,
            booking_status=BOOKING_FIXED,
        )
        event.notes = "Bring checklist."
        db.session.add_all([material, person, event])
        db.session.flush()
        db.session.add_all(
            [
                EventMaterial(event=event, material=material, quantity=2),
                EventPersonnel(event=event, personnel=person),
            ]
        )
        db.session.commit()

        body = google_event_body(event)

    assert body["summary"] == "Calendar show"
    assert body["location"] == ""
    assert body["start"] == {"date": "2999-10-01"}
    assert body["end"] == {"date": "2999-10-04"}
    assert "Stage lights: 2 pcs" in body["description"]
    assert "Alex Morgan (Tech)" in body["description"]


def test_google_calendar_event_body_uses_datetime_when_event_has_times(app):
    with app.app_context():
        event = Event(
            name="Timed calendar show",
            starts_at=datetime(2999, 10, 1, 10, 30),
            ends_at=datetime(2999, 10, 1, 12, 0),
            location=None,
            booking_status=BOOKING_FIXED,
        )
        db.session.add(event)
        db.session.commit()

        body = google_event_body(event)

    assert body["start"] == {"dateTime": "2999-10-01T10:30:00", "timeZone": "Europe/Vienna"}
    assert body["end"] == {"dateTime": "2999-10-01T12:00:00", "timeZone": "Europe/Vienna"}


def test_event_save_queues_google_sync_without_calling_google(app, monkeypatch):
    def fail_sync(*args, **kwargs):
        raise AssertionError("Google sync should not run during the request")

    monkeypatch.setattr("app.google_calendar_queue.sync_event_to_google", fail_sync)

    with app.app_context():
        db.session.add(GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}"))
        db.session.commit()

    response = app.test_client().post(
        "/events",
        data={
            "name": "Queued show",
            "starts_on": "2999-12-01",
            "ends_on": "2999-12-01",
            "booking_status": BOOKING_FIXED,
        },
    )

    assert response.status_code == 302
    with app.app_context():
        event = Event.query.filter_by(name="Queued show").one()
        job = GoogleCalendarSyncJob.query.one()
        assert event.sync_to_google_calendar is True
        assert job.action == GOOGLE_SYNC_ACTION_UPSERT
        assert job.status == GOOGLE_SYNC_STATUS_PENDING
        assert job.event_id == event.id


def test_event_can_be_created_without_google_calendar_sync(app):
    with app.app_context():
        db.session.add(GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}"))
        db.session.commit()

    response = app.test_client().post(
        "/events",
        data={
            "name": "Private show",
            "starts_on": "2999-12-03",
            "ends_on": "2999-12-03",
            "booking_status": BOOKING_FIXED,
            "sync_to_google_calendar": "0",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        event = Event.query.filter_by(name="Private show").one()
        assert event.sync_to_google_calendar is False
        assert GoogleCalendarSyncJob.query.count() == 0


def test_event_calendar_sync_can_be_disabled_and_queues_google_delete(app):
    with app.app_context():
        connection = GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}")
        event = make_event("Unsynced later", date(2999, 12, 4), date(2999, 12, 4))
        event.google_event_id = "google-event-old"
        event.google_calendar_id = "calendar@example.com"
        db.session.add_all([connection, event])
        db.session.commit()
        event_id = event.id

    response = app.test_client().post(
        f"/events/{event_id}/calendar-sync",
        data={"sync_to_google_calendar": "0"},
    )

    assert response.status_code == 302
    with app.app_context():
        event = db.session.get(Event, event_id)
        job = GoogleCalendarSyncJob.query.one()
        assert event.sync_to_google_calendar is False
        assert job.action == GOOGLE_SYNC_ACTION_DELETE
        assert job.event_id == event.id
        assert job.google_event_id == "google-event-old"


def test_google_sync_queue_does_not_autoflush_pending_event_changes(app):
    with app.app_context():
        connection = GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}")
        event = make_event("No autoflush show", date(2999, 12, 2), date(2999, 12, 2))
        db.session.add_all([connection, event])
        db.session.commit()
        event_id = event.id

        event.booking_status = BOOKING_PLANNING

        assert queue_google_event_sync(event)

        with db.session.no_autoflush:
            stored_status = db.session.execute(
                text("SELECT booking_status FROM event WHERE id = :event_id"),
                {"event_id": event_id},
            ).scalar_one()
        assert stored_status == BOOKING_FIXED

        db.session.commit()


def test_google_calendar_queue_worker_processes_pending_event_sync(app, monkeypatch):
    def fake_sync_event_to_google(event, connection):
        event.google_event_id = "google-event-queued"
        event.google_calendar_id = connection.calendar_id
        event.google_event_link = "https://calendar.google.com/event"
        event.google_sync_error = None

    monkeypatch.setattr("app.google_calendar_queue.sync_event_to_google", fake_sync_event_to_google)

    with app.app_context():
        connection = GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}")
        event = make_event("Queued worker show", date(2999, 12, 2), date(2999, 12, 2))
        db.session.add_all([connection, event])
        db.session.flush()
        db.session.add(
            GoogleCalendarSyncJob(
                action=GOOGLE_SYNC_ACTION_UPSERT,
                event_id=event.id,
                status=GOOGLE_SYNC_STATUS_PENDING,
            )
        )
        db.session.commit()
        event_id = event.id

        result = process_pending_google_calendar_jobs()

        event = db.session.get(Event, event_id)
        assert result["processed"] == 1
        assert GoogleCalendarSyncJob.query.count() == 0
        assert event.google_event_id == "google-event-queued"
        assert db.session.get(GoogleCalendarConnection, 1).last_error is None


def test_google_calendar_queue_worker_clears_event_link_after_sync_is_disabled(app, monkeypatch):
    def fake_delete_event_from_google(event, connection):
        event.google_event_id = None
        event.google_calendar_id = None
        event.google_event_link = None
        event.google_synced_at = None
        event.google_sync_error = None

    monkeypatch.setattr("app.google_calendar_queue.delete_event_from_google", fake_delete_event_from_google)

    with app.app_context():
        connection = GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}")
        event = make_event("Delete queued show", date(2999, 12, 5), date(2999, 12, 5))
        event.sync_to_google_calendar = False
        event.google_event_id = "google-event-delete"
        event.google_calendar_id = "calendar@example.com"
        event.google_event_link = "https://calendar.google.com/event"
        db.session.add_all([connection, event])
        db.session.flush()
        db.session.add(
            GoogleCalendarSyncJob(
                action=GOOGLE_SYNC_ACTION_DELETE,
                event_id=event.id,
                google_event_id=event.google_event_id,
                google_calendar_id=event.google_calendar_id,
                status=GOOGLE_SYNC_STATUS_PENDING,
            )
        )
        db.session.commit()
        event_id = event.id

        result = process_pending_google_calendar_jobs()

        event = db.session.get(Event, event_id)
        assert result["processed"] == 1
        assert GoogleCalendarSyncJob.query.count() == 0
        assert event.google_event_id is None
        assert event.google_calendar_id is None
        assert event.google_event_link is None


def test_google_calendar_sync_creates_google_event_with_configured_calendar(app):
    with app.app_context():
        event = make_event("Sync show", date(2999, 11, 1), date(2999, 11, 1))
        connection = GoogleCalendarConnection(id=1, calendar_id="calendar@example.com", credentials_json="{}")
        service = FakeCalendarService()
        db.session.add_all([event, connection])
        db.session.commit()

        synced = sync_event_to_google(event, connection, service=service)

        assert synced["id"] == "google-event-1"
        assert event.google_event_id == "google-event-1"
        assert event.google_calendar_id == "calendar@example.com"
        assert service.inserted["calendarId"] == "calendar@example.com"
        assert service.inserted["body"]["summary"] == "Sync show"


class FakeCalendarService:
    def __init__(self):
        self.inserted = None

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.inserted = {"calendarId": calendarId, "body": body}
        return FakeGoogleRequest({"id": "google-event-1", "htmlLink": "https://calendar.google.com/event"})


class FakeGoogleRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response
