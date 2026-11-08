"""Collect pyinfra facts across the inventory with optional SQLite persistence."""

from .collector import FactCollector, collect
from .store import FactStore, Observation

__all__ = [
    "FactCollector",
    "FactStore",
    "Observation",
    "collect",
]
