from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Shared SQLAlchemy declarative base.

    Every database model in the application must eventually inherit
    from this single Base class.
    """

    pass