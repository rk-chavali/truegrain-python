"""Constructors for structured filters.

A filter is never a SQL fragment. That is what keeps an ungoverned predicate
inexpressible, and it is the reason these are small functions rather than a
string builder.

Using them instead of hand-written dictionaries catches a misspelled operator at
the call site rather than as a refusal from the server.
"""

from __future__ import annotations

from typing import Any

Filter = dict[str, Any]


def _filter(dimension: str, op: str, *values: Any) -> Filter:
    out: Filter = {"dimension": dimension, "op": op}
    if values:
        out["values"] = list(values)
    return out


def eq(dimension: str, value: Any) -> Filter:
    """`dimension = value`."""
    return _filter(dimension, "eq", value)


def ne(dimension: str, value: Any) -> Filter:
    """`dimension <> value`."""
    return _filter(dimension, "ne", value)


def in_(dimension: str, *values: Any) -> Filter:
    """`dimension IN (...)`. Named with a trailing underscore; `in` is a keyword."""
    if not values:
        raise ValueError("in_ needs at least one value")
    return _filter(dimension, "in", *values)


def not_in(dimension: str, *values: Any) -> Filter:
    """`dimension NOT IN (...)`."""
    if not values:
        raise ValueError("not_in needs at least one value")
    return _filter(dimension, "not_in", *values)


def gt(dimension: str, value: Any) -> Filter:
    """`dimension > value`."""
    return _filter(dimension, "gt", value)


def gte(dimension: str, value: Any) -> Filter:
    """`dimension >= value`."""
    return _filter(dimension, "gte", value)


def lt(dimension: str, value: Any) -> Filter:
    """`dimension < value`."""
    return _filter(dimension, "lt", value)


def lte(dimension: str, value: Any) -> Filter:
    """`dimension <= value`."""
    return _filter(dimension, "lte", value)


def between(dimension: str, low: Any, high: Any) -> Filter:
    """`dimension BETWEEN low AND high`.

    The engine refuses a reversed range rather than matching no rows, because an
    empty result that looks like a real answer is worse than an error.
    """
    return _filter(dimension, "between", low, high)


def is_null(dimension: str) -> Filter:
    """`dimension IS NULL`."""
    return _filter(dimension, "is_null")


def is_not_null(dimension: str) -> Filter:
    """`dimension IS NOT NULL`."""
    return _filter(dimension, "is_not_null")
