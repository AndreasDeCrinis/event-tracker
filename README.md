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

The GitHub Actions workflow publishes `adcrinis/event-job-tracker` to Docker Hub when you push a semantic version tag:

```bash
git tag v1.2.3
git push origin v1.2.3
```

It pushes `1.2.3`, `1.2`, `1`, and `latest`.

Required GitHub repository secrets:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

## Inventory Logic

- Fixed materials, such as flamethrowers, are reserved only for planned events that overlap the same time window. Once the event window is over, they are free again.
- Consumables, such as flamethrower fuel, are deducted for planned events and remain deducted after an event is completed.
- Cancelled events release both fixed material reservations and consumable reservations.
- Personnel are unavailable only during planned events that overlap their assigned event time. After the event window, they are free again.
