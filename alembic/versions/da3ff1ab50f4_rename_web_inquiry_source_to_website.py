"""rename web inquiry source to website

Revision ID: da3ff1ab50f4
Revises: b8c31a4d9e72
Create Date: 2026-07-31 21:09:42.739364

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da3ff1ab50f4'
down_revision: Union[str, Sequence[str], None] = 'b8c31a4d9e72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE inquiry_source_enum "
        "RENAME VALUE 'WEB' TO 'WEBSITE'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TYPE inquiry_source_enum "
        "RENAME VALUE 'WEBSITE' TO 'WEB'"
    )