"""Declarative base shared by every table."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
