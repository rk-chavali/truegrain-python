"""Python client for the semantic engine.

An Apache Ossie semantic model compiled to governed, dialect-correct SQL. This
package talks to it over HTTP. There is no method that sends SQL, because there
is no endpoint that accepts it: the only expressible request is a semantic one.

Quick start:

    >>> from truegrain import Client, filters
    >>> client = Client.from_env()          # SEMANTIC_URL, SEMANTIC_TOKEN
    >>> for metric in client.metrics(search="revenue"):
    ...     print(metric.name, "-", metric.description)
    >>> result = client.query(
    ...     metrics=["sales.order_revenue"],
    ...     dimensions=["sales.customers.region"],
    ...     filters=[filters.in_("sales.orders.status", "shipped", "delivered")],
    ...     order_by=[{"field": "order_revenue", "desc": True}],
    ... )
    >>> result.to_dataframe()

Refusals are answers, not failures. Catch :class:`Refused` and branch on
``retry``:

    >>> try:
    ...     client.query(metrics=["sales.order_revenue"],
    ...                  dimensions=["sales.products.category"])
    ... except Refused as refusal:
    ...     if refusal.should_modify():
    ...         print("try instead:", refusal.hint)

The engine has no required dependencies here: this package uses only the
standard library. pandas is optional and needed only for
:meth:`Result.to_dataframe`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

from . import filters, tools
from .client import OPERATIONS, Client
from .errors import Refused, Retry, SemanticError, TransportError, Unauthorized
from .models import (
    AuditEvent,
    AuditPage,
    Compiled,
    Dimension,
    Health,
    Job,
    Metric,
    Namespace,
    Result,
)

# Read from the installed metadata rather than written here, so there is one
# version in the project and not two. The literal drifted from pyproject.toml
# and the release that caught it was the one that failed: a wheel whose
# version disagrees with its tag cannot be traced back to a commit.
try:
    __version__ = _metadata_version("truegrain")
except PackageNotFoundError:  # running from a source tree, never installed
    __version__ = "0+unknown"

__all__ = [
    "AuditEvent",
    "AuditPage",
    "Client",
    "Compiled",
    "Dimension",
    "Health",
    "Job",
    "Metric",
    "Namespace",
    "OPERATIONS",
    "Refused",
    "Result",
    "Retry",
    "SemanticError",
    "TransportError",
    "Unauthorized",
    "__version__",
    "filters",
    "tools",
]
