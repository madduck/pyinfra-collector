"""Collect pyinfra facts across the inventory into a SQLite store."""

from .collector import FactCollector, collect, register
from .store import FactStore, Observation

__all__ = [
    "FactCollector",
    "FactStore",
    "Observation",
    "collect",
    "register",
]
