import os
import sys
from logging.config import fileConfig
from urllib.parse import quote_plus

from sqlalchemy import create_engine, pool
from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _db_url() -> str:
    """Build connection URL with URL-encoded credentials (handles @, #, ! etc.)."""
    if url := os.getenv("DATABASE_URL"):
        return url
    return "postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}".format(
        user=quote_plus(os.getenv("DB_USER", "trader")),
        pw=quote_plus(os.getenv("DB_PASSWORD", "trader123")),
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        db=os.getenv("DB_NAME", "dhan_trading"),
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Build engine directly — avoids configparser's % interpolation issue
    connectable = create_engine(_db_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
