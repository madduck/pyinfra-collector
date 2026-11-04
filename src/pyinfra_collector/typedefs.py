"""Fact types and identity shared across the package.

A fact is either a class (the pyinfra fact to collect) or its canonical id
string. Both the collector and the store name facts, so the type describing a
collectable fact and the helper that resolves it to its canonical name live
here, free of either module's concerns.
"""

from __future__ import annotations

from typing import Any

from pyinfra.api.facts import FactBase, ShortFactBase

#: A collectable fact: a plain fact or a short fact that derives from one.
FactClass = type[FactBase[Any] | ShortFactBase[Any]]


def fact_id(fact: str | FactClass) -> str:
    """Return the canonical name of a fact, given its id or its class.

    Args:
        fact: A fact class, or an already-canonical fact id such as
            ``server.Uptime``.

    Returns:
        The fact's registered name.
    """
    return fact if isinstance(fact, str) else fact.name
