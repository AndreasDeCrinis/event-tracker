from pathlib import Path
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix


db = SQLAlchemy()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    database_url = os.environ.get("DATABASE_URL") or f"sqlite:///{Path(app.instance_path) / 'event_jobs.db'}"

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        SQLALCHEMY_DATABASE_URI=database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PREFERRED_URL_SCHEME=os.environ.get("PREFERRED_URL_SCHEME", "http"),
        SESSION_COOKIE_SECURE=_env_bool("SESSION_COOKIE_SECURE", False),
        SESSION_COOKIE_SAMESITE="Lax",
    )

    if config:
        app.config.update(config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    _ensure_sqlite_directory(app.config["SQLALCHEMY_DATABASE_URI"])
    _apply_proxy_fix(app)

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


def _apply_proxy_fix(app):
    trusted_proxy_count = _env_int("TRUSTED_PROXY_COUNT", 0)
    if trusted_proxy_count <= 0:
        return

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=trusted_proxy_count,
        x_proto=trusted_proxy_count,
        x_host=trusted_proxy_count,
        x_port=trusted_proxy_count,
        x_prefix=trusted_proxy_count,
    )


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _migrate_database():
    inspector = inspect(db.engine)

    if "event" not in inspector.get_table_names():
        return

    event_columns = {column["name"]: column for column in inspector.get_columns("event")}

    if _event_table_needs_rebuild(event_columns):
        _rebuild_event_table(event_columns)
        inspector = inspect(db.engine)
        event_columns = {column["name"]: column for column in inspector.get_columns("event")}

    if "booking_status" not in event_columns:
        db.session.execute(
            text("ALTER TABLE event ADD COLUMN booking_status VARCHAR(20) NOT NULL DEFAULT 'fixed'")
        )
        db.session.commit()


def _event_table_needs_rebuild(event_columns):
    return "duration_hours" in event_columns or not event_columns["location"].get("nullable", True)


def _rebuild_event_table(event_columns):
    ends_at_expression = "ends_at"
    if "duration_hours" in event_columns:
        fallback_ends_at = "datetime(starts_at, '+' || COALESCE(duration_hours, 1.0) || ' hours')"
        ends_at_expression = f"COALESCE(ends_at, {fallback_ends_at})" if "ends_at" in event_columns else fallback_ends_at

    booking_status_expression = "booking_status" if "booking_status" in event_columns else "'fixed'"

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
                        location VARCHAR(160),
                        status VARCHAR(20) NOT NULL,
                        booking_status VARCHAR(20) NOT NULL DEFAULT 'fixed',
                        notes TEXT,
                        PRIMARY KEY (id),
                        CONSTRAINT event_date_range_positive CHECK (ends_at > starts_at),
                        CONSTRAINT event_status_valid CHECK (status in ('planned', 'completed', 'cancelled')),
                        CONSTRAINT event_booking_status_valid CHECK (booking_status in ('planning', 'fixed'))
                    )
                    """
                )
            )
            connection.execute(
                text(
                    f"""
                    INSERT INTO event_new (id, name, starts_at, ends_at, location, status, booking_status, notes)
                    SELECT id, name, starts_at, {ends_at_expression}, NULLIF(location, ''), status, {booking_status_expression}, notes
                    FROM event
                    """
                )
            )
            connection.execute(text("DROP TABLE event"))
            connection.execute(text("ALTER TABLE event_new RENAME TO event"))
            connection.execute(text("CREATE INDEX ix_event_starts_at ON event (starts_at)"))
            connection.execute(text("CREATE INDEX ix_event_ends_at ON event (ends_at)"))
            connection.execute(text("CREATE INDEX ix_event_status ON event (status)"))
            connection.execute(text("CREATE INDEX ix_event_booking_status ON event (booking_status)"))

        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
        connection.commit()
    finally:
        connection.close()
