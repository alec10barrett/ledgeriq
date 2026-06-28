from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy ORM models.
    Every model should inherit from this class.
    """
    pass
