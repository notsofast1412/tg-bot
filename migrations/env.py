from alembic import context
from sqlalchemy import engine_from_config, pool

from paintwatch.config import Config
from paintwatch.database.models import Base

config = context.config
if not config.attributes.get("configured_url"):
    # CLI передаёт тот же URL, что и .env; прямой alembic также учитывает DATA_DIR.
    if config.get_main_option("sqlalchemy.url") == "sqlite:///data/paintwatch.sqlite3":
        config.set_main_option(
            "sqlalchemy.url", Config().database_url.replace("%", "%%")
        )


def run():
    if context.is_offline_mode():
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            target_metadata=Base.metadata,
            literal_binds=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = engine_from_config(
            config.get_section(config.config_ini_section),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()


run()
