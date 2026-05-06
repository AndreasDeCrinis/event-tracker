from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import os
import threading

from . import db
from .google_calendar import GoogleCalendarError, delete_event_from_google, sync_event_to_google
from .models import (
    GOOGLE_SYNC_ACTION_DELETE,
    GOOGLE_SYNC_ACTION_UPSERT,
    GOOGLE_SYNC_STATUS_FAILED,
    GOOGLE_SYNC_STATUS_PENDING,
    GOOGLE_SYNC_STATUS_RUNNING,
    Event,
    GoogleCalendarConnection,
    GoogleCalendarSyncJob,
)


MAX_SYNC_ATTEMPTS = 3
WORKER_INTERVAL_SECONDS = 10
INITIAL_SYNC_DELAY_SECONDS = 2

_worker_started = False
_worker_wakeup = threading.Event()


def queue_google_event_sync(event, connection=None):
    with db.session.no_autoflush:
        connection = connection or _google_calendar_connection()
        if not _connection_can_sync(connection):
            return False

        run_after = _delayed_run_after()
        existing = GoogleCalendarSyncJob.query.filter_by(
            action=GOOGLE_SYNC_ACTION_UPSERT,
            event_id=event.id,
            status=GOOGLE_SYNC_STATUS_PENDING,
        ).first()

        if existing:
            existing.event_name = event.name
            existing.google_event_id = event.google_event_id
            existing.google_calendar_id = event.google_calendar_id
            existing.last_error = None
            existing.run_after = run_after
            existing.updated_at = _utc_now()
            return True

        db.session.add(
            GoogleCalendarSyncJob(
                action=GOOGLE_SYNC_ACTION_UPSERT,
                event_id=event.id,
                google_event_id=event.google_event_id,
                google_calendar_id=event.google_calendar_id,
                event_name=event.name,
                status=GOOGLE_SYNC_STATUS_PENDING,
                run_after=run_after,
            )
        )
    return True


def queue_google_event_deletion(event, connection=None):
    with db.session.no_autoflush:
        connection = connection or _google_calendar_connection()
        if not _connection_can_sync(connection):
            return False

        GoogleCalendarSyncJob.query.filter_by(
            action=GOOGLE_SYNC_ACTION_UPSERT,
            event_id=event.id,
            status=GOOGLE_SYNC_STATUS_PENDING,
        ).delete()

        if not event.google_event_id:
            return False

        db.session.add(
            GoogleCalendarSyncJob(
                action=GOOGLE_SYNC_ACTION_DELETE,
                event_id=event.id,
                google_event_id=event.google_event_id,
                google_calendar_id=event.google_calendar_id or connection.calendar_id,
                event_name=event.name,
                status=GOOGLE_SYNC_STATUS_PENDING,
                run_after=_delayed_run_after(),
            )
        )
    return True


def queue_all_google_event_syncs(events, connection=None):
    connection = connection or _google_calendar_connection()
    if not _connection_can_sync(connection):
        return 0

    queued = 0
    for event in events:
        if queue_google_event_sync(event, connection=connection):
            queued += 1
    return queued


def trigger_google_calendar_sync_worker():
    _worker_wakeup.set()


def start_google_calendar_sync_worker(app):
    global _worker_started

    if _worker_started:
        return

    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") not in {"true", "1"}:
        return

    _worker_started = True
    thread = threading.Thread(target=_worker_loop, args=(app,), daemon=True)
    thread.start()


def process_pending_google_calendar_jobs(limit=20):
    now = _utc_now()
    jobs = (
        GoogleCalendarSyncJob.query.filter(
            GoogleCalendarSyncJob.status == GOOGLE_SYNC_STATUS_PENDING,
            (GoogleCalendarSyncJob.run_after.is_(None)) | (GoogleCalendarSyncJob.run_after <= now),
        )
        .order_by(GoogleCalendarSyncJob.created_at.asc())
        .limit(limit)
        .all()
    )
    result = {"processed": 0, "failed": 0}

    for job in jobs:
        _process_google_calendar_job(job, result)

    return result


def _process_google_calendar_job(job, result):
    connection = _google_calendar_connection()
    if not _connection_can_sync(connection):
        job.status = GOOGLE_SYNC_STATUS_FAILED
        job.last_error = "Google Kalender ist nicht verbunden."
        job.updated_at = _utc_now()
        db.session.commit()
        result["failed"] += 1
        return

    job.status = GOOGLE_SYNC_STATUS_RUNNING
    job.attempts += 1
    job.updated_at = _utc_now()
    db.session.commit()

    try:
        if job.action == GOOGLE_SYNC_ACTION_UPSERT:
            _process_upsert_job(job, connection)
        else:
            _process_delete_job(job, connection)
    except GoogleCalendarError as error:
        _mark_job_failed(job, connection, str(error))
        result["failed"] += 1
        return

    connection.last_synced_at = _utc_now()
    connection.last_error = None
    db.session.delete(job)
    db.session.commit()
    result["processed"] += 1


def _process_upsert_job(job, connection):
    event = db.session.get(Event, job.event_id)
    if not event:
        return

    sync_event_to_google(event, connection)


def _process_delete_job(job, connection):
    event_snapshot = SimpleNamespace(
        name=job.event_name or "Event",
        google_event_id=job.google_event_id,
        google_calendar_id=job.google_calendar_id,
        google_event_link=None,
        google_synced_at=None,
        google_sync_error=None,
    )
    delete_event_from_google(event_snapshot, connection)


def _mark_job_failed(job, connection, error):
    now = _utc_now()
    job.last_error = error
    job.updated_at = now

    if job.action == GOOGLE_SYNC_ACTION_UPSERT and job.event_id:
        event = db.session.get(Event, job.event_id)
        if event:
            event.google_sync_error = error

    if job.attempts >= MAX_SYNC_ATTEMPTS:
        job.status = GOOGLE_SYNC_STATUS_FAILED
    else:
        job.status = GOOGLE_SYNC_STATUS_PENDING
        job.run_after = now + timedelta(seconds=5 * job.attempts)

    connection.last_error = error
    db.session.commit()


def _worker_loop(app):
    while True:
        try:
            with app.app_context():
                process_pending_google_calendar_jobs()
                db.session.remove()
        except Exception:
            app.logger.exception("Google Calendar background sync failed")

        _worker_wakeup.wait(WORKER_INTERVAL_SECONDS)
        _worker_wakeup.clear()


def _connection_can_sync(connection):
    return bool(connection and connection.calendar_id and connection.is_connected)


def _google_calendar_connection():
    return db.session.get(GoogleCalendarConnection, 1)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _delayed_run_after():
    return _utc_now() + timedelta(seconds=INITIAL_SYNC_DELAY_SECONDS)
