from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import Settings


class Base(DeclarativeBase):
    pass


def create_database(settings: Settings):
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5} if settings.app_env != "test" else {},
    )
    return engine, sessionmaker(engine, expire_on_commit=False)
