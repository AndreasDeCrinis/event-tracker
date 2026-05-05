# Claude Context

Use this file as the first stop when opening the project with a fresh context.

## Project Summary

`event-job-tracker` is a local, Dockerized Flask app with SQLite. It tracks event jobs, materials, consumables, personnel assignments, inventory availability, a job archive, and Google Calendar sync.

The UI is German. The code is English.

## Architecture

```text
app/
  __init__.py                 Flask app factory, migrations, ProxyFix
  models.py                   SQLAlchemy models and availability logic
  routes.py                   Dashboard/settings/routes/actions
  google_calendar.py          OAuth and Google Calendar sync
  static/styles.css           Plain CSS
  templates/
    base.html                 Header and burger menu
    index.html                Main dashboard
    settings.html             Settings page
    _google_calendar_settings.html
tests/
  test_inventory_logic.py     Behavior and route tests
```

## UX Conventions

- German labels and flash messages.
- Code, database fields, route names, and tests in English.
- The main screen is the working dashboard, not a marketing page.
- Settings are reached via the burger menu.
- Keep controls compact and practical.
- Existing small icon-only buttons should remain compact and accessible with `aria-label`.

## Domain Rules

- Events have `starts_at` and `ends_at` internally, but the UI uses date-only ranges.
- The visible end date is inclusive; internally `ends_at` is stored as the next day at midnight for all-day range handling.
- Event statuses:
  - `planned`
  - `completed`
  - `cancelled`
- Booking statuses:
  - `planning` maps to `In Planung`
  - `fixed` maps to `Fixiert`
- `In Planung` does not reserve inventory and can exceed availability.
- `Fixiert` books inventory and must not exceed availability.
- Fixed material is only reserved for overlapping active planned fixed events.
- Consumables count for fixed planned and fixed completed events; they remain deducted after completion.
- Cancelled events release fixed and consumable reservations.
- Personnel conflicts are based on overlapping planned events.

## Google Calendar Integration

The Google Calendar panel is on `/settings`.

The connection stores one calendar ID and OAuth credentials in SQLite via `GoogleCalendarConnection`.

Event sync fields are on `Event`:

- `google_event_id`
- `google_calendar_id`
- `google_event_link`
- `google_synced_at`
- `google_sync_error`

Sync behavior:

- Event creation syncs when connected.
- Booking status changes sync.
- Closure syncs and prefixes cancelled/completed summaries.
- Material/personnel assignment changes sync the description.
- Event deletion attempts to delete the Google Calendar event.

OAuth details:

- Uses `google-auth-oauthlib`.
- Scope: `https://www.googleapis.com/auth/calendar.events`.
- PKCE code verifier is stored in the Flask session during connect and passed during callback.
- Local testing should use `http://127.0.0.1:5001`, not `localhost`, unless the redirect URI and browser URL are changed together.

## Commands

```bash
docker compose run --rm web pytest
docker compose build
docker compose up --build
```

Local OAuth-friendly run:

```bash
docker compose run --rm -p 5001:5000 -e PREFERRED_URL_SCHEME=http -e TRUSTED_PROXY_COUNT=0 -e SESSION_COOKIE_SECURE=0 web
```

## Do Not Do

- Do not commit `.env` or OAuth secrets.
- Do not change UI language to English.
- Do not replace the simple server-rendered UI with a client framework without a direct request.
- Do not remove the lightweight migration code unless replacing it with a proper migration system intentionally.
