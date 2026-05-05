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

## Inventory Logic

- Fixed materials, such as flamethrowers, are reserved only for planned events that overlap the same time window. Once the event window is over, they are free again.
- Consumables, such as flamethrower fuel, are deducted for planned events and remain deducted after an event is completed.
- Cancelled events release both fixed material reservations and consumable reservations.
- Personnel are unavailable only during planned events that overlap their assigned event time. After the event window, they are free again.
