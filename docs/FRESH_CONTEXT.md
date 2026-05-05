# Fresh Context Handoff

This document is for future Codex, Claude, or another coding agent starting without chat history.

## What The App Does

The app tracks event jobs and resources:

- Events with date ranges and optional locations.
- Personnel assignments.
- Fixed material, such as reusable equipment.
- Consumable material, such as fuel.
- Inventory availability.
- Job archive for old, completed, or cancelled events.
- Google Calendar sync to a user-specified calendar.
- Successful event closure reduces the total quantity of assigned consumable materials.

## Local State And Secrets

The repo may contain a local `.env` file, but it is ignored. It can hold:

```bash
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://127.0.0.1:5001/google-calendar/oauth2callback
```

Never print or commit real secrets.

For local OAuth testing, Google Cloud must allow this redirect URI:

```text
http://127.0.0.1:5001/google-calendar/oauth2callback
```

The Google account used for testing must be added as a test user unless the OAuth app is verified or internal to a Workspace domain.

## Data Model Snapshot

`Event`

- `name`
- `starts_at`
- `ends_at`
- `location`
- `status`
- `booking_status`
- `notes`
- Google sync fields

`Material`

- `name`
- `kind`
- `total_quantity`
- `unit`
- `notes`

`Personnel`

- `name`
- `role`
- `contact`
- `notes`

Assignment tables:

- `EventMaterial`
- `EventPersonnel`

Google connection:

- `GoogleCalendarConnection`

## Routes To Know

Dashboard:

- `GET /`

Settings:

- `GET /settings`

Events:

- `POST /events`
- `POST /events/<event_id>/booking-status`
- `POST /events/<event_id>/close`
- `POST /events/<event_id>/delete`

Assignments:

- `POST /events/<event_id>/materials`
- `POST /assignments/material/<assignment_id>/quantity`
- `POST /assignments/material/<assignment_id>/delete`
- `POST /events/<event_id>/personnel`
- `POST /assignments/personnel/<assignment_id>/delete`

Inventory/personnel:

- `POST /materials`
- `POST /materials/<material_id>/quantity`
- `POST /materials/<material_id>/delete`
- `POST /personnel`
- `POST /personnel/<personnel_id>/delete`

Google Calendar:

- `POST /google-calendar/settings`
- `POST /google-calendar/connect`
- `GET /google-calendar/oauth2callback`
- `POST /google-calendar/sync`
- `POST /google-calendar/disconnect`

Google Calendar routes redirect back to `/settings#google-calendar`.

## Migrations

This project currently uses lightweight startup migrations in `app/__init__.py`, not Alembic.

Existing migrations handle:

- Removing the old duration-hours schema.
- Making event location optional.
- Adding booking status.
- Adding Google Calendar fields to events.
- Creating indexes needed by the current models.

If schema changes grow, consider introducing Flask-Migrate/Alembic, but do not mix approaches casually.

## Testing Checklist

Run:

```bash
docker compose run --rm web pytest
```

Current expected result:

```text
31 passed
```

Known warnings:

- SQLAlchemy legacy `Query.get()` warning triggered through Flask-SQLAlchemy `get_or_404`.

Smoke-test locally:

```bash
curl -I http://127.0.0.1:5001/
curl -I http://127.0.0.1:5001/settings
```

## OAuth Troubleshooting

Symptoms and likely causes:

- `Access blocked: app has not completed verification`: add the Google account as a test user or verify the app.
- `ungültiger OAuth-Status`: browser host and redirect host differ, old callback URL was reused, or cookies were lost.
- `Missing code verifier`: PKCE session storage is broken; inspect `GOOGLE_OAUTH_CODE_VERIFIER_KEY` handling in `app/routes.py`.
- `redirect_uri_mismatch`: Google Cloud redirect URI differs from `GOOGLE_REDIRECT_URI`.

## Deployment Notes

Production is expected behind a reverse proxy at:

```text
https://foo.decrinis.com
```

Use:

```yaml
PREFERRED_URL_SCHEME: https
TRUSTED_PROXY_COUNT: "1"
SESSION_COOKIE_SECURE: "1"
```

The reverse proxy should forward:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Port $server_port;
```

Production Google OAuth redirect URI:

```text
https://foo.decrinis.com/google-calendar/oauth2callback
```

## Release Pipeline

The GitHub Actions workflow builds and pushes Docker images to:

```text
adecrinis/event-job-tracker
```

It creates semantic version tags from Conventional Commits and also pushes `latest`.

Examples:

- `fix: whatever` creates a patch release.
- `feat: whatever` creates a minor release.
- `feat!: whatever` creates a major release.
