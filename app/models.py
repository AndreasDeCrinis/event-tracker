from datetime import datetime, time, timedelta, timezone

from sqlalchemy import CheckConstraint, UniqueConstraint

from . import db


MATERIAL_FIXED = "fixed"
MATERIAL_CONSUMABLE = "consumable"
MATERIAL_KINDS = (MATERIAL_FIXED, MATERIAL_CONSUMABLE)

STATUS_PLANNED = "planned"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
EVENT_STATUSES = (STATUS_PLANNED, STATUS_COMPLETED, STATUS_CANCELLED)

BOOKING_PLANNING = "planning"
BOOKING_FIXED = "fixed"
EVENT_BOOKING_STATUSES = (BOOKING_PLANNING, BOOKING_FIXED)

GOOGLE_SYNC_ACTION_UPSERT = "upsert"
GOOGLE_SYNC_ACTION_DELETE = "delete"
GOOGLE_SYNC_ACTIONS = (GOOGLE_SYNC_ACTION_UPSERT, GOOGLE_SYNC_ACTION_DELETE)

GOOGLE_SYNC_STATUS_PENDING = "pending"
GOOGLE_SYNC_STATUS_RUNNING = "running"
GOOGLE_SYNC_STATUS_FAILED = "failed"
GOOGLE_SYNC_STATUSES = (GOOGLE_SYNC_STATUS_PENDING, GOOGLE_SYNC_STATUS_RUNNING, GOOGLE_SYNC_STATUS_FAILED)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    ends_at = db.Column(db.DateTime, nullable=False, index=True)
    location = db.Column(db.String(160), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PLANNED, index=True)
    booking_status = db.Column(db.String(20), nullable=False, default=BOOKING_PLANNING, index=True)
    notes = db.Column(db.Text, nullable=True)
    google_event_id = db.Column(db.String(255), nullable=True, index=True)
    google_calendar_id = db.Column(db.String(255), nullable=True)
    google_event_link = db.Column(db.String(500), nullable=True)
    google_synced_at = db.Column(db.DateTime, nullable=True)
    google_sync_error = db.Column(db.Text, nullable=True)
    sync_to_google_calendar = db.Column(db.Boolean, nullable=False, default=True)
    consumables_deducted_at = db.Column(db.DateTime, nullable=True)

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
        CheckConstraint(f"booking_status in {EVENT_BOOKING_STATUSES}", name="event_booking_status_valid"),
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
    def is_all_day(self):
        return self.starts_at.time() == time.min and self.ends_at.time() == time.min

    @property
    def starts_at_time(self):
        return None if self.is_all_day else self.starts_at.time()

    @property
    def ends_at_time(self):
        return None if self.is_all_day else self.ends_at.time()

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
    template_assignments = db.relationship(
        "EventTemplateMaterial",
        back_populates="material",
        cascade="all, delete-orphan",
        order_by="EventTemplateMaterial.id",
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
    template_assignments = db.relationship(
        "EventTemplatePersonnel",
        back_populates="personnel",
        cascade="all, delete-orphan",
        order_by="EventTemplatePersonnel.id",
    )


class EventTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    event_name = db.Column(db.String(120), nullable=False)
    duration_days = db.Column(db.Integer, nullable=False, default=1)
    starts_at_time = db.Column(db.Time, nullable=True)
    ends_at_time = db.Column(db.Time, nullable=True)
    location = db.Column(db.String(160), nullable=True)
    booking_status = db.Column(db.String(20), nullable=False, default=BOOKING_PLANNING, index=True)
    notes = db.Column(db.Text, nullable=True)
    sync_to_google_calendar = db.Column(db.Boolean, nullable=False, default=True)

    material_assignments = db.relationship(
        "EventTemplateMaterial",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="EventTemplateMaterial.id",
    )
    personnel_assignments = db.relationship(
        "EventTemplatePersonnel",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="EventTemplatePersonnel.id",
    )

    __table_args__ = (
        CheckConstraint("duration_days > 0", name="event_template_duration_positive"),
        CheckConstraint(f"booking_status in {EVENT_BOOKING_STATUSES}", name="event_template_booking_status_valid"),
        CheckConstraint(
            "(starts_at_time IS NULL AND ends_at_time IS NULL) OR "
            "(starts_at_time IS NOT NULL AND ends_at_time IS NOT NULL)",
            name="event_template_times_complete",
        ),
    )


class GoogleCalendarConnection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    calendar_id = db.Column(db.String(255), nullable=True)
    credentials_json = db.Column(db.Text, nullable=True)
    connected_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)

    @property
    def is_connected(self):
        return bool(self.credentials_json)


class GoogleCalendarSyncJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(20), nullable=False, index=True)
    event_id = db.Column(db.Integer, nullable=True, index=True)
    google_event_id = db.Column(db.String(255), nullable=True)
    google_calendar_id = db.Column(db.String(255), nullable=True)
    event_name = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=GOOGLE_SYNC_STATUS_PENDING, index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    run_after = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    __table_args__ = (
        CheckConstraint(f"action in {GOOGLE_SYNC_ACTIONS}", name="google_sync_job_action_valid"),
        CheckConstraint(f"status in {GOOGLE_SYNC_STATUSES}", name="google_sync_job_status_valid"),
        CheckConstraint("attempts >= 0", name="google_sync_job_attempts_non_negative"),
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


class EventTemplateMaterial(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("event_template.id"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("material.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)

    template = db.relationship("EventTemplate", back_populates="material_assignments")
    material = db.relationship("Material", back_populates="template_assignments")

    __table_args__ = (
        UniqueConstraint("template_id", "material_id", name="unique_material_per_event_template"),
        CheckConstraint("quantity > 0", name="event_template_material_quantity_positive"),
    )


class EventTemplatePersonnel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("event_template.id"), nullable=False)
    personnel_id = db.Column(db.Integer, db.ForeignKey("personnel.id"), nullable=False)

    template = db.relationship("EventTemplate", back_populates="personnel_assignments")
    personnel = db.relationship("Personnel", back_populates="template_assignments")

    __table_args__ = (UniqueConstraint("template_id", "personnel_id", name="unique_personnel_per_event_template"),)


def material_counted_statuses(kind):
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


def material_reserved_quantity(material, moment=None, exclude_event_id=None):
    if material.kind != MATERIAL_CONSUMABLE:
        return material_allocated_quantity(material, moment=moment, exclude_event_id=exclude_event_id)

    moment = moment or datetime.now()
    total = 0

    for assignment in material.assignments:
        event = assignment.event

        if exclude_event_id and event.id == exclude_event_id:
            continue

        if (
            event.booking_status == BOOKING_FIXED
            and event.status == STATUS_PLANNED
            and event.ends_at > moment
        ):
            total += assignment.quantity

    return total


def material_open_used_quantity(material, moment=None, exclude_event_id=None):
    if material.kind != MATERIAL_CONSUMABLE:
        return 0

    moment = moment or datetime.now()
    total = 0

    for assignment in material.assignments:
        event = assignment.event

        if exclude_event_id and event.id == exclude_event_id:
            continue

        if event.booking_status != BOOKING_FIXED:
            continue

        if event.status == STATUS_PLANNED and event.ends_at <= moment:
            total += assignment.quantity
        elif event.status == STATUS_COMPLETED and event.consumables_deducted_at is None:
            total += assignment.quantity

    return total


def material_deducted_used_quantity(material):
    if material.kind != MATERIAL_CONSUMABLE:
        return 0

    return sum(
        assignment.quantity
        for assignment in material.assignments
        if assignment.event.booking_status == BOOKING_FIXED
        and assignment.event.status == STATUS_COMPLETED
        and assignment.event.consumables_deducted_at is not None
    )


def material_planned_quantity(material, moment=None):
    moment = moment or datetime.now()
    return sum(
        assignment.quantity
        for assignment in material.assignments
        if assignment.event.status == STATUS_PLANNED and assignment.event.ends_at > moment
    )


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


def material_shortage_quantity(material, event):
    assigned_to_event = sum(
        assignment.quantity for assignment in event.material_assignments if assignment.material_id == material.id
    )
    available_for_event = material_available_quantity(material, target_event=event, exclude_event_id=event.id)
    return max(assigned_to_event - available_for_event, 0)


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
    if event.booking_status != BOOKING_FIXED:
        return False

    if material.kind == MATERIAL_CONSUMABLE:
        return event.status in material_counted_statuses(material.kind) or (
            event.status == STATUS_COMPLETED and event.consumables_deducted_at is None
        )

    if event.status != STATUS_PLANNED:
        return False

    if target_event:
        return event.overlaps(target_event)

    return event.is_active_at(moment)
