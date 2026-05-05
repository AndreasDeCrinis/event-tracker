# Event Job Tracker

A small Flask application for tracking events/jobs with a start/end date range, personnel, fixed materials, consumables, and the assignments between them.

## Run With Docker

```bash
docker compose up --build
```

Open http://localhost:5000.

The SQLite database is stored in the Docker volume `event_job_data`, so your data survives container restarts.

## Run Tests

```bash
docker compose run --rm web pytest
```

## Publish Docker Image

The GitHub Actions workflow publishes `adecrinis/event-job-tracker` to Docker Hub from pushes to `main`.

It creates the next semantic version tag from Conventional Commit messages since the previous `vMAJOR.MINOR.PATCH` tag:

```bash
git commit -m "fix: correct inventory count"
git push origin main
```

Release rules:

- `fix:` and `perf:` create a patch release, such as `v1.2.3` to `v1.2.4`.
- `feat:` creates a minor release, such as `v1.2.3` to `v1.3.0`.
- `feat!:` or a `BREAKING CHANGE:` footer creates a major release, such as `v1.2.3` to `v2.0.0`.
- Commits without a release-worthy Conventional Commit prefix do not create a Docker release.

It pushes `1.2.3`, `1.2`, `1`, and `latest`.

Manually pushed semantic version tags like `v1.2.3` are also supported.

Required GitHub repository secrets:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

## Reverse Proxy

For `https://foo.decrinis.com`, terminate TLS at the reverse proxy and forward HTTP to the container on port `5000`.

The app trusts one proxy by default in `docker-compose.yml`:

```yaml
PREFERRED_URL_SCHEME: https
TRUSTED_PROXY_COUNT: "1"
SESSION_COOKIE_SECURE: "1"
```

Your proxy should forward these headers:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Port $server_port;
```

For direct local HTTP access without a proxy, set `TRUSTED_PROXY_COUNT=0` and `SESSION_COOKIE_SECURE=0`.

## Google Calendar Sync

The app can connect to Google Calendar with OAuth and sync every Event to one calendar ID entered in the UI.

Google Cloud setup:

1. Create or choose a Google Cloud project.
2. Enable the Google Calendar API.
3. Configure an OAuth consent screen.
4. Create an OAuth Client ID of type `Web application`.
5. Add this authorized redirect URI:

```text
https://foo.decrinis.com/google-calendar/oauth2callback
```

Container environment:

```yaml
GOOGLE_CLIENT_ID: your-client-id
GOOGLE_CLIENT_SECRET: your-client-secret
```

Optional override if your public URL differs from the generated reverse-proxy URL:

```yaml
GOOGLE_REDIRECT_URI: https://foo.decrinis.com/google-calendar/oauth2callback
```

In the app, open the burger menu, choose `Einstellungen`, enter the calendar ID, then click `Google Kalender verbinden`.
The calendar ID can be `primary` or the ID shown in Google Calendar settings under "Integrate calendar".

The app requests the narrow Calendar events scope:

```text
https://www.googleapis.com/auth/calendar.events
```

## Inventory Logic

- Events can be `In Planung` or `Fixiert`.
- `In Planung` events can list more material than is currently available. They do not reserve or consume inventory, and the UI warns when planned material may be insufficient.
- `Fixiert` events actually book material. Fixed materials, such as flamethrowers, are reserved only for fixed planned events that overlap the same date range.
- Consumables, such as flamethrower fuel, are deducted for fixed planned events and remain deducted after a fixed event is completed.
- Cancelled events release both fixed material reservations and consumable reservations.
- Personnel are unavailable only during planned events that overlap their assigned event time. After the event window, they are free again.
