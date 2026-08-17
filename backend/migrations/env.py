"""Alembic environment. DATABASE_URL comes from the environment only (gate 1.6)."""

import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine


def _load_dotenv() -> None:
    """Load a .env file from the backend directory if present.

    Alembic does not use uvicorn's --env-file flag, so we load it here
    manually. Existing environment variables always take precedence so
    that CI/production values are never silently overridden.
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Never overwrite a value already set in the real environment.
        os.environ.setdefault(key, value)


_load_dotenv()


def _url() -> str:
    return os.environ["DATABASE_URL"]


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
