from datetime import datetime

import pytest

from app import create_app, db
from app.models import (
    MATERIAL_CONSUMABLE,
    MATERIAL_FIXED,
    STATUS_COMPLETED,
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
        party = Event(
            name="Party",
            starts_at=datetime(2026, 6, 1, 18),
            ends_at=datetime(2026, 6, 1, 22),
            location="Hall",
        )
        birthday = Event(
            name="Birthday",
            starts_at=datetime(2026, 6, 1, 19),
            ends_at=datetime(2026, 6, 1, 22),
            location="Garden",
        )
        db.session.add_all([flamethrowers, party, birthday])
        db.session.flush()
        db.session.add_all(
            [
                EventMaterial(event=party, material=flamethrowers, quantity=2),
                EventMaterial(event=birthday, material=flamethrowers, quantity=4),
            ]
        )
        db.session.commit()

        assert material_available_quantity(flamethrowers, moment=datetime(2026, 6, 1, 20)) == 4
        assert material_available_quantity(flamethrowers, moment=datetime(2026, 6, 1, 23)) == 10


def test_fixed_material_can_be_reused_for_non_overlapping_events(app):
    with app.app_context():
        item = Material(name="Projectors", kind=MATERIAL_FIXED, total_quantity=5, unit="pcs")
        morning = Event(
            name="Morning",
            starts_at=datetime(2026, 6, 1, 10),
            ends_at=datetime(2026, 6, 1, 12),
            location="Hall",
        )
        overlap = Event(
            name="Overlap",
            starts_at=datetime(2026, 6, 1, 11),
            ends_at=datetime(2026, 6, 1, 13),
            location="Hall",
        )
        afternoon = Event(
            name="Afternoon",
            starts_at=datetime(2026, 6, 1, 13),
            ends_at=datetime(2026, 6, 1, 15),
            location="Hall",
        )
        db.session.add_all([item, morning, overlap, afternoon])
        db.session.flush()
        db.session.add(EventMaterial(event=morning, material=item, quantity=4))
        db.session.commit()

        assert material_assignable_quantity(item, overlap) == 1
        assert material_assignable_quantity(item, afternoon) == 5


def test_fixed_material_returns_after_completion(app):
    with app.app_context():
        item = Material(name="Stage lights", kind=MATERIAL_FIXED, total_quantity=6, unit="pcs")
        event = Event(
            name="Show",
            starts_at=datetime(2026, 6, 4, 19),
            ends_at=datetime(2026, 6, 4, 21),
            location="Club",
        )
        db.session.add_all([item, event])
        db.session.flush()
        db.session.add(EventMaterial(event=event, material=item, quantity=5))
        db.session.commit()

        assert material_available_quantity(item, moment=datetime(2026, 6, 4, 20)) == 1

        event.status = STATUS_COMPLETED
        db.session.commit()

        assert material_available_quantity(item, moment=datetime(2026, 6, 4, 20)) == 6


def test_consumable_material_stays_deducted_after_completion(app):
    with app.app_context():
        fuel = Material(name="Flamethrower fuel", kind=MATERIAL_CONSUMABLE, total_quantity=20, unit="L")
        event = Event(
            name="Night show",
            starts_at=datetime(2026, 6, 5, 21),
            ends_at=datetime(2026, 6, 5, 22, 30),
            location="Arena",
        )
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
        event = Event(
            name="Launch",
            starts_at=datetime(2026, 7, 1, 20),
            ends_at=datetime(2026, 7, 1, 22),
            location="Pier",
        )
        db.session.add_all([person, event])
        db.session.flush()
        db.session.add(EventPersonnel(event=event, personnel=person))
        db.session.commit()

        assert not personnel_is_available(person, moment=datetime(2026, 7, 1, 21))
        assert personnel_is_available(person, moment=datetime(2026, 7, 1, 23))

        event.status = STATUS_COMPLETED
        db.session.commit()

        assert personnel_is_available(person, moment=datetime(2026, 7, 1, 21))


def test_personnel_conflict_only_blocks_overlapping_planned_events(app):
    with app.app_context():
        person = Personnel(name="Sam Rivera", role="Tech")
        first = Event(
            name="First",
            starts_at=datetime(2026, 8, 1, 10),
            ends_at=datetime(2026, 8, 1, 12),
            location="A",
        )
        overlap = Event(
            name="Overlap",
            starts_at=datetime(2026, 8, 1, 11),
            ends_at=datetime(2026, 8, 1, 13),
            location="B",
        )
        later = Event(
            name="Later",
            starts_at=datetime(2026, 8, 1, 13),
            ends_at=datetime(2026, 8, 1, 15),
            location="C",
        )
        db.session.add_all([person, first, overlap, later])
        db.session.flush()
        db.session.add(EventPersonnel(event=first, personnel=person))
        db.session.commit()

        assert personnel_has_conflict(person, overlap)
        assert not personnel_has_conflict(person, later)
