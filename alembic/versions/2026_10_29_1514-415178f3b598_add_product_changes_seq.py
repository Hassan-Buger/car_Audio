"""add_product_changes_seq

Revision ID: 415178f3b598
Revises: 21467900a656
Create Date: 2026-10-29 15:14:07.405133+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '415178f3b598'
down_revision: Union[str, None] = '21467900a656'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS product_changes_seq")
    op.execute("ALTER TABLE product_changes ALTER COLUMN seq SET DEFAULT nextval('product_changes_seq')")


def downgrade() -> None:
    op.execute("ALTER TABLE product_changes ALTER COLUMN seq DROP DEFAULT")
    op.execute("DROP SEQUENCE IF EXISTS product_changes_seq")
