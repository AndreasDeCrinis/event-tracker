from datetime import date, datetime, time, timedelta

import pytest

from app import create_app, db
from app.models import (
    MATERIAL_CONSUMABLE,
    MATERIAL_FIXED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PLANNED,
    Event,
    EventMaterial,
    EventPersonnel,
    Material,
    Personnel,
    material_assignable_quantity,
    material_available_quantity,
    personnel_has_conflict,
    personnel_is_available,
)


def make_event(name, starts_on, ends_on, location="Hall"):
    return Event(
        name=name,
        starts_at=datetime.combine(starts_on, time.min),
        ends_at=datetime.combine(ends_on + timedelta(days=1), time.min),
        location=location,
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


def test_consumable_material_stays_deducted_after_completion(app):
    with app.app_context():
        fuel = Material(name="Flamethrower fuel", kind=MATERIAL_CONSUMABLE, total_quantity=20, unit="L")
        event = make_event("Night show", date(2026, 6, 5), date(2026, 6, 5), location="Arena")
        db.session.add_all([fuel, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=fuel, quantity=7))
        db.session.commit()

        assert material_available_quantity(fuel) == 13

        event.status = STATUS_COMPLETED
        db.session.commit()

        assert material_available_quantity(fuel) == 13


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
