import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import context
from sqlalchemy import create_engine

from app.config import settings
from app.models import Base

# Migrations run sync via psycopg; the app itself uses asyncpg.
url = settings.database_url.replace("+asyncpg", "+psycopg")
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
