"""Allow explicit experiments to pause automatic account gameplay scheduling."""
import sqlalchemy as sa
from alembic import op

revision = "0012_schedule_pause"
down_revision = "0011_account_schedule_state"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("account_schedule_states", sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("account_schedule_states", "paused")
