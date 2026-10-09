from alembic import context

from app.core.config import get_settings
from app.persistence import models  # noqa: F401
from app.persistence.database import Base, create_database

target_metadata = Base.metadata


def run_migrations_online():
    engine, _ = create_database(get_settings())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
