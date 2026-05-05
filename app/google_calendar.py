import json
from datetime import datetime, timezone

from flask import current_app, url_for

from .models import (
    BOOKING_FIXED,
    BOOKING_PLANNING,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PLANNED,
)


GOOGLE_CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar.events",)
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

STATUS_LABELS = {
    STATUS_PLANNED: "Geplant",
    STATUS_COMPLETED: "Erfolgreich abgeschlossen",
    STATUS_CANCELLED: "Abgesagt",
}

BOOKING_STATUS_LABELS = {
    BOOKING_PLANNING: "In Planung",
    BOOKING_FIXED: "Fixiert",
}


class GoogleCalendarError(Exception):
    pass


def google_oauth_is_configured():
    return bool(current_app.config.get("GOOGLE_CLIENT_ID") and current_app.config.get("GOOGLE_CLIENT_SECRET"))


def google_redirect_uri():
    return current_app.config.get("GOOGLE_REDIRECT_URI") or url_for(
        "main.google_calendar_callback",
        _external=True,
        _scheme=current_app.config.get("PREFERRED_URL_SCHEME") or None,
    )


def build_authorization_url(state, redirect_uri):
    flow = _google_flow(state=state, redirect_uri=redirect_uri)
    authorization_url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return authorization_url, returned_state, flow.code_verifier


def exchange_authorization_response(
    authorization_response,
    state,
    redirect_uri,
    code_verifier,
    existing_credentials_json=None,
):
    flow = _google_flow(state=state, redirect_uri=redirect_uri, code_verifier=code_verifier)
    try:
        flow.fetch_token(authorization_response=authorization_response)
    except Exception as error:
        raise GoogleCalendarError(f"Google OAuth-Anmeldung konnte nicht abgeschlossen werden: {error}") from error
    return _credentials_to_json(flow.credentials, existing_credentials_json=existing_credentials_json)


def sync_all_events_to_google(connection, events):
    service = _calendar_service(connection)
    result = {"synced": 0, "failed": 0}

    for event in events:
        try:
            sync_event_to_google(event, connection, service=service)
            result["synced"] += 1
        except GoogleCalendarError as error:
            event.google_sync_error = str(error)
            result["failed"] += 1

    connection.last_synced_at = _utc_now()
    connection.last_error = None if result["failed"] == 0 else f"{result['failed']} Event(s) konnten nicht synchronisiert werden."
    return result


def sync_event_to_google(event, connection, service=None):
    _validate_connection(connection)
    service = service or _calendar_service(connection)
    body = google_event_body(event)
    use_existing_event = event.google_event_id and event.google_calendar_id == connection.calendar_id

    try:
        if use_existing_event:
            synced = (
                service.events()
                .update(calendarId=connection.calendar_id, eventId=event.google_event_id, body=body)
                .execute()
            )
        else:
            synced = service.events().insert(calendarId=connection.calendar_id, body=body).execute()
    except Exception as error:
        if use_existing_event and _google_error_status(error) in {404, 410}:
            synced = service.events().insert(calendarId=connection.calendar_id, body=body).execute()
        else:
            raise GoogleCalendarError(f"Google Kalender konnte {event.name} nicht synchronisieren: {error}") from error

    event.google_event_id = synced.get("id")
    event.google_calendar_id = connection.calendar_id
    event.google_event_link = synced.get("htmlLink")
    event.google_synced_at = _utc_now()
    event.google_sync_error = None
    connection.last_error = None
    return synced


def delete_event_from_google(event, connection):
    if not event.google_event_id:
        return False

    _validate_connection(connection)
    service = _calendar_service(connection)
    calendar_id = event.google_calendar_id or connection.calendar_id

    try:
        service.events().delete(calendarId=calendar_id, eventId=event.google_event_id).execute()
    except Exception as error:
        if _google_error_status(error) not in {404, 410}:
            raise GoogleCalendarError(f"Google Kalender konnte {event.name} nicht löschen: {error}") from error

    event.google_event_id = None
    event.google_calendar_id = None
    event.google_event_link = None
    event.google_synced_at = None
    event.google_sync_error = None
    return True


def google_event_body(event):
    description_lines = [
        f"Status: {STATUS_LABELS.get(event.status, event.status)}",
        f"Planungsstatus: {BOOKING_STATUS_LABELS.get(event.booking_status, event.booking_status)}",
    ]

    if event.notes:
        description_lines.extend(["", event.notes])

    if event.material_assignments:
        description_lines.extend(["", "Material:"])
        description_lines.extend(
            f"- {assignment.material.name}: {assignment.quantity} {assignment.material.unit}"
            for assignment in event.material_assignments
        )

    if event.personnel_assignments:
        description_lines.extend(["", "Personal:"])
        description_lines.extend(
            f"- {assignment.personnel.name} ({assignment.personnel.role})"
            for assignment in event.personnel_assignments
        )

    return {
        "summary": _google_summary(event),
        "location": event.location or "",
        "description": "\n".join(description_lines),
        "start": {"date": event.starts_on.isoformat()},
        "end": {"date": event.ends_at.date().isoformat()},
        "transparency": "transparent" if event.status == STATUS_CANCELLED else "opaque",
        "extendedProperties": {
            "private": {
                "eventJobTrackerId": str(event.id),
            }
        },
    }


def _google_summary(event):
    if event.status == STATUS_CANCELLED:
        return f"[Abgesagt] {event.name}"
    if event.status == STATUS_COMPLETED:
        return f"[Abgeschlossen] {event.name}"
    return event.name


def _validate_connection(connection):
    if not connection or not connection.calendar_id:
        raise GoogleCalendarError("Bitte zuerst eine Google Kalender-ID speichern.")
    if not connection.credentials_json:
        raise GoogleCalendarError("Bitte zuerst Google Kalender verbinden.")


def _calendar_service(connection):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as error:
        raise GoogleCalendarError("Google Kalender-Bibliotheken sind nicht installiert.") from error

    credentials = Credentials.from_authorized_user_info(
        json.loads(connection.credentials_json),
        scopes=GOOGLE_CALENDAR_SCOPES,
    )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        connection.credentials_json = credentials.to_json()

    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _google_flow(state, redirect_uri, code_verifier=None):
    if not google_oauth_is_configured():
        raise GoogleCalendarError("GOOGLE_CLIENT_ID und GOOGLE_CLIENT_SECRET müssen gesetzt sein.")

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as error:
        raise GoogleCalendarError("Google OAuth-Bibliotheken sind nicht installiert.") from error

    return Flow.from_client_config(
        _google_client_config(redirect_uri),
        scopes=GOOGLE_CALENDAR_SCOPES,
        state=state,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )


def _google_client_config(redirect_uri):
    return {
        "web": {
            "client_id": current_app.config["GOOGLE_CLIENT_ID"],
            "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def _credentials_to_json(credentials, existing_credentials_json=None):
    credential_data = json.loads(credentials.to_json())
    existing_data = json.loads(existing_credentials_json) if existing_credentials_json else {}

    if not credential_data.get("refresh_token") and existing_data.get("refresh_token"):
        credential_data["refresh_token"] = existing_data["refresh_token"]

    return json.dumps(credential_data)


def _google_error_status(error):
    response = getattr(error, "resp", None)
    return getattr(response, "status", None)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)
