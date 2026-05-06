# Agent Instructions

This repository contains a small Flask application for tracking event jobs, personnel, fixed materials, consumables, and Google Calendar sync.

## Working Rules

- Keep code identifiers, route names, models, and comments in English.
- Keep user-facing UI text in German.
- Prefer small, focused changes that match the existing Flask/Jinja/CSS style.
- Do not commit secrets. Local OAuth values belong in `.env`, which is ignored by Git and Docker build context.
- Use `rg` for searching and `docker compose run --rm web pytest` for the full test suite.
- The app is intentionally simple: Flask, Flask-SQLAlchemy, SQLite, server-rendered Jinja templates, plain CSS.
- Avoid introducing a frontend framework unless explicitly requested.

## Common Commands

```bash
docker compose up --build
docker compose run --rm web pytest
docker compose run --rm -p 5001:5000 -e PREFERRED_URL_SCHEME=http -e TRUSTED_PROXY_COUNT=0 -e SESSION_COOKIE_SECURE=0 web
```

For local reverse-proxy production-style testing, keep the `docker-compose.yml` defaults:

```yaml
PREFERRED_URL_SCHEME: https
TRUSTED_PROXY_COUNT: "1"
SESSION_COOKIE_SECURE: "1"
```

For direct local HTTP OAuth testing, use:

```text
http://127.0.0.1:5001
```

Do not mix `localhost` and `127.0.0.1` during Google OAuth. The session cookie and redirect URI must use the same host.

## Important Files

- `app/__init__.py`: app factory, DB initialization, lightweight SQLite migrations, reverse proxy handling.
- `app/models.py`: SQLAlchemy models and inventory/personnel availability logic.
- `app/routes.py`: Flask routes for dashboard, settings, CRUD, assignments, closure, and Google Calendar actions.
- `app/google_calendar.py`: Google OAuth and Calendar API sync helpers.
- `app/templates/base.html`: common shell and burger menu.
- `app/templates/index.html`: event dashboard with list/calendar views and personnel.
- `app/templates/inventory.html`: inventory management page.
- `app/templates/settings.html`: settings page.
- `app/templates/_icons.html`: shared compact action icons.
- `app/templates/_google_calendar_settings.html`: Google Calendar settings panel.
- `app/static/styles.css`: all UI styling.
- `tests/test_inventory_logic.py`: main behavior and route test suite.

## Current Product Behavior

- Events use a date range only, no hours.
- Active events can be viewed as a list or monthly calendar on `/`.
- Event location is optional.
- Events can be `In Planung` or `Fixiert`.
- Planned events may over-assign material and show warnings.
- Fixed events actually book material and enforce availability.
- Fixed material returns after the event window or closure.
- Consumable material is reserved while a fixed event is planned. On successful completion, assigned consumable quantities are subtracted from the material's total quantity.
- Inventory management lives on `/inventory` and separates fixed material from consumables. Consumables show reserved stock, open used stock from past/not-yet-deducted events, already deducted usage, and available stock.
- Cancelled events release reservations.
- Personnel are busy only during planned overlapping events.
- Completed, cancelled, and past planned jobs appear in the archive.
- Material quantities and event material assignment quantities are editable.
- Google Calendar integration lives on `/settings`, reached through the burger menu.

## Google Calendar Notes

Required environment variables:

```bash
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://127.0.0.1:5001/google-calendar/oauth2callback
```

The app requests this scope:

```text
https://www.googleapis.com/auth/calendar.events
```

Google OAuth uses PKCE. The route stores both the OAuth `state` and `code_verifier` in the Flask session. If that flow breaks, check for:

- Mixed hosts, such as starting on `localhost` but redirecting to `127.0.0.1`.
- Missing `GOOGLE_REDIRECT_URI` in local development.
- Missing test user in Google Cloud OAuth consent settings.
- Reusing an old Google callback URL after restarting a flow.

## CI And Publishing

The GitHub workflow in `.github/workflows/docker-publish.yml` runs tests, then builds and pushes `adecrinis/event-job-tracker`.

Release tagging follows Conventional Commits:

- `fix:` or `perf:` creates a patch release.
- `feat:` creates a minor release.
- `feat!:` or `BREAKING CHANGE:` creates a major release.
- Image tags include semantic versions and `latest`.
