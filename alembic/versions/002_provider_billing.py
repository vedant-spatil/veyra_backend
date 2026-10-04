"""Provider sync timestamps and charge rows."""

from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "002_provider_billing"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    op.drop_table("provider_charges")
    op.drop_table("provider_sync")
