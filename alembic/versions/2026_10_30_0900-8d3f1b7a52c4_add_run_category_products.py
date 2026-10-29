"""add_run_category_products

Revision ID: 8d3f1b7a52c4
Revises: 415178f3b598
Create Date: 2026-10-30 09:00:00.000000+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8d3f1b7a52c4'
down_revision: str | None = '415178f3b598'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'run_category_products',
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False, server_default='listing'),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('run_id', 'category_id', 'product_id', 'source'),
    )
    op.create_index(
        op.f('ix_run_category_products_product_id'),
        'run_category_products',
        ['product_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_run_category_products_product_id'), table_name='run_category_products')
    op.drop_table('run_category_products')
