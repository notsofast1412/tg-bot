import os
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from paintbot.database.models import Base
from paintbot.database.store import Store

load_dotenv()
store = Store(os.environ.get("DATABASE_PATH", "var/paintbot.db"))
with store.engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
