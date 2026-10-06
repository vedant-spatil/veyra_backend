"""Pending sign-up codes."""

from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "003_pending_signup"
down_revision = "002_provider_billing"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    op.drop_table("pending_signups")
