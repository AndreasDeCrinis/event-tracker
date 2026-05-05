from datetime import datetime, time, timedelta

from sqlalchemy import CheckConstraint, UniqueConstraint

from . import db


MATERIAL_FIXED = "fixed"
MATERIAL_CONSUMABLE = "consumable"
MATERIAL_KINDS = (MATERIAL_FIXED, MATERIAL_CONSUMABLE)

STATUS_PLANNED = "planned"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
EVENT_STATUSES = (STATUS_PLANNED, STATUS_COMPLETED, STATUS_CANCELLED)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    ends_at = db.Column(db.DateTime, nullable=False, index=True)
    location = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PLANNED, index=True)
    notes = db.Column(db.Text, nullable=True)

    material_assignments = db.relationship(
        "EventMaterial",
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="EventMaterial.id",
    )
    personnel_assignments = db.relationship(
        "EventPersonnel",
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="EventPersonnel.id",
    )

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="event_date_range_positive"),
        CheckConstraint(f"status in {EVENT_STATUSES}", name="event_status_valid"),
    )

    @property
    def starts_on(self):
        return self.starts_at.date()

    @property
    def ends_on(self):
        if self.starts_at.time() == time.min and self.ends_at.time() == time.min:
            return (self.ends_at - timedelta(days=1)).date()
        return self.ends_at.date()

    @property
    def status_label(self):
        return self.status.title()

    def overlaps(self, other):
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at

    def is_active_at(self, moment=None):
        moment = moment or datetime.now()
        return self.status == STATUS_PLANNED and self.starts_at <= moment < self.ends_at


class Material(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    kind = db.Column(db.String(20), nullable=False, default=MATERIAL_FIXED, index=True)
    total_quantity = db.Column(db.Integer, nullable=False, default=0)
    unit = db.Column(db.String(40), nullable=False, default="pcs")
    notes = db.Column(db.Text, nullable=True)

    assignments = db.relationship(
        "EventMaterial",
        back_populates="material",
        cascade="all, delete-orphan",
        order_by="EventMaterial.id",
    )

    __table_args__ = (
        CheckConstraint("total_quantity >= 0", name="material_quantity_non_negative"),
        CheckConstraint(f"kind in {MATERIAL_KINDS}", name="material_kind_valid"),
    )

    @property
    def kind_label(self):
        return "Fixed" if self.kind == MATERIAL_FIXED else "Consumable"


class Personnel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    role = db.Column(db.String(120), nullable=False, default="Crew")
    contact = db.Column(db.String(160), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    assignments = db.relationship(
        "EventPersonnel",
        back_populates="personnel",
        cascade="all, delete-orphan",
        order_by="EventPersonnel.id",
    )


class EventMaterial(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("material.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)

    event = db.relationship("Event", back_populates="material_assignments")
    material = db.relationship("Material", back_populates="assignments")

    __table_args__ = (
        UniqueConstraint("event_id", "material_id", name="unique_material_per_event"),
        CheckConstraint("quantity > 0", name="event_material_quantity_positive"),
    )


class EventPersonnel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    personnel_id = db.Column(db.Integer, db.ForeignKey("personnel.id"), nullable=False)

    event = db.relationship("Event", back_populates="personnel_assignments")
    personnel = db.relationship("Personnel", back_populates="assignments")

    __table_args__ = (UniqueConstraint("event_id", "personnel_id", name="unique_personnel_per_event"),)


def material_counted_statuses(kind):
    if kind == MATERIAL_CONSUMABLE:
        return (STATUS_PLANNED, STATUS_COMPLETED)
    return (STATUS_PLANNED,)


def material_allocated_quantity(material, target_event=None, moment=None, exclude_event_id=None):
    total = 0

    for assignment in material.assignments:
        event = assignment.event

        if exclude_event_id and event.id == exclude_event_id:
            continue

        if _assignment_counts_for_material(material, event, target_event, moment):
            total += assignment.quantity

    return total


def material_available_quantity(material, target_event=None, moment=None, exclude_event_id=None):
    allocated = material_allocated_quantity(
        material,
        target_event=target_event,
        moment=moment,
        exclude_event_id=exclude_event_id,
    )
    return max(material.total_quantity - allocated, 0)


def material_assignable_quantity(material, event):
    assigned_to_event = sum(
        assignment.quantity for assignment in event.material_assignments if assignment.material_id == material.id
    )
    available_for_event = material_available_quantity(material, target_event=event, exclude_event_id=event.id)
    return max(available_for_event - assigned_to_event, 0)


def personnel_planned_assignment_count(personnel, moment=None):
    return sum(1 for assignment in personnel.assignments if assignment.event.is_active_at(moment))


def personnel_is_available(personnel, moment=None):
    return personnel_planned_assignment_count(personnel, moment=moment) == 0


def personnel_has_conflict(personnel, event):
    return any(
        assignment.event_id != event.id
        and assignment.event.status == STATUS_PLANNED
        and assignment.event.overlaps(event)
        for assignment in personnel.assignments
    )


def _assignment_counts_for_material(material, event, target_event=None, moment=None):
    if material.kind == MATERIAL_CONSUMABLE:
        return event.status in material_counted_statuses(material.kind)

    if event.status != STATUS_PLANNED:
        return False

    if target_event:
        return event.overlaps(target_event)

    return event.is_active_at(moment)
