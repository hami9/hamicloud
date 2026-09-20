import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure apps/api is in sys.path
api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "apps", "api"))
if api_dir not in sys.path:
    sys.path.insert(0, api_dir)

from app.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silence every
    # logger already created in this process - including the whole app.* tree
    # whenever migrations run in-process, as the test suite does.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    if "DATABASE_URL_SYNC" in os.environ and os.environ["DATABASE_URL_SYNC"]:
        return os.environ["DATABASE_URL_SYNC"]
    try:
        from app.core.config import settings
        return settings.DATABASE_URL_SYNC
    except Exception:
        return "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
