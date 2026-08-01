from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from app.models import Document, DocumentJob, User  # noqa: F401  (metadata 등록)

config = context.config
# 앱 시작 경로는 이미 sidecar FileHandler를 설치했다. embedded Config의 명시적
# 플래그가 False일 때 fileConfig로 root handler를 덮어쓰지 않는다(CLI 기본은 유지).
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database_url_sync, pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
