"""Initial monitoring schema, frozen at revision 0001."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None


def upgrade():
    op.create_table("tags", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("name", sa.String, nullable=False, unique=True),
                    sa.Column("threshold", sa.Integer, nullable=False))
    op.create_table("products",
        sa.Column("id", sa.Integer, primary_key=True),
        *[sa.Column(n, sa.String, nullable=False) for n in ("marketplace", "card_id", "article")],
        *[sa.Column(n, sa.Text, nullable=False) for n in ("url", "title", "custom_name", "attributes", "status")],
        sa.Column("tag_id", sa.Integer, sa.ForeignKey("tags.id"), nullable=False),
        *[sa.Column(n, sa.Integer) for n in ("last_price", "last_success", "last_attempt", "last_changed")],
        sa.Column("next_check", sa.Integer, nullable=False), sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("failures", sa.Integer, nullable=False), sa.UniqueConstraint("marketplace", "card_id"))
    op.create_index("ix_products_due", "products", ["active", "next_check"])
    for name in ("price_history", "check_logs"):
        columns = [sa.Column("id", sa.Integer, primary_key=True),
                   sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
                   sa.Column("checked_at", sa.Integer, nullable=False)]
        if name == "price_history":
            columns.append(sa.Column("price", sa.Integer, nullable=False))
        else:
            columns.extend([sa.Column("success", sa.Boolean, nullable=False), sa.Column("detail", sa.Text, nullable=False)])
        op.create_table(name, *columns)
        op.create_index(f"ix_{name}_product_id", name, ["product_id"])
    op.create_table("notification_state",
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), primary_key=True),
        sa.Column("below", sa.Boolean, nullable=False), sa.Column("generation", sa.Integer, nullable=False))
    op.create_table("notification_queue", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False), sa.Column("text", sa.Text, nullable=False),
        sa.Column("state", sa.String, nullable=False), sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt", sa.Integer, nullable=False), sa.Column("message_id", sa.Integer),
        sa.Column("error", sa.Text), sa.UniqueConstraint("product_id", "generation"))
    op.create_index("ix_queue_due", "notification_queue", ["state", "next_attempt"])
    op.create_table("settings", sa.Column("key", sa.String, primary_key=True), sa.Column("value", sa.Text, nullable=False))
    op.create_table("processed_updates", sa.Column("update_id", sa.Integer, primary_key=True),
                    sa.Column("processed_at", sa.Integer, nullable=False))


def downgrade():
    for name in ("processed_updates", "settings", "notification_queue", "notification_state", "check_logs", "price_history", "products", "tags"):
        op.drop_table(name)
