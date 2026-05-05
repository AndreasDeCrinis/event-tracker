from pathlib import Path
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text


db = SQLAlchemy()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    database_url = os.environ.get("DATABASE_URL") or f"sqlite:///{Path(app.instance_path) / 'event_jobs.db'}"

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        SQLALCHEMY_DATABASE_URI=database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    if config:
        app.config.update(config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    _ensure_sqlite_directory(app.config["SQLALCHEMY_DATABASE_URI"])

    db.init_app(app)

    from .routes import bp

    app.register_blueprint(bp)

    with app.app_context():
        _migrate_database()
        db.create_all()

    return app


def _ensure_sqlite_directory(database_url):
    if not database_url.startswith("sqlite:///") or database_url == "sqlite:///:memory:":
        return

    database_path = database_url.replace("sqlite:///", "", 1)
    if database_path:
        Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _migrate_database():
    inspector = inspect(db.engine)

    if "event" not in inspector.get_table_names():
        return

    event_columns = {column["name"] for column in inspector.get_columns("event")}

    if "duration_hours" in event_columns:
        _rebuild_event_table_without_duration(event_columns)


def _rebuild_event_table_without_duration(event_columns):
    ends_at_expression = "datetime(starts_at, '+' || COALESCE(duration_hours, 1.0) || ' hours')"
    if "ends_at" in event_columns:
        ends_at_expression = f"COALESCE(ends_at, {ends_at_expression})"

    connection = db.engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()

        with connection.begin():
            connection.execute(text("DROP TABLE IF EXISTS event_new"))
            connection.execute(
                text(
                    """
                    CREATE TABLE event_new (
                        id INTEGER NOT NULL,
                        name VARCHAR(120) NOT NULL,
                        starts_at DATETIME NOT NULL,
                        ends_at DATETIME NOT NULL,
                        location VARCHAR(160) NOT NULL,
                        status VARCHAR(20) NOT NULL,
                        notes TEXT,
                        PRIMARY KEY (id),
                        CONSTRAINT event_date_range_positive CHECK (ends_at > starts_at),
                        CONSTRAINT event_status_valid CHECK (status in ('planned', 'completed', 'cancelled'))
                    )
                    """
                )
            )
            connection.execute(
                text(
                    f"""
                    INSERT INTO event_new (id, name, starts_at, ends_at, location, status, notes)
                    SELECT id, name, starts_at, {ends_at_expression}, location, status, notes
                    FROM event
                    """
                )
            )
            connection.execute(text("DROP TABLE event"))
            connection.execute(text("ALTER TABLE event_new RENAME TO event"))
            connection.execute(text("CREATE INDEX ix_event_starts_at ON event (starts_at)"))
            connection.execute(text("CREATE INDEX ix_event_ends_at ON event (ends_at)"))
            connection.execute(text("CREATE INDEX ix_event_status ON event (status)"))

        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
        connection.commit()
    finally:
        connection.close()
