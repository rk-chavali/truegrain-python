"""Tool definitions for agent frameworks.

Most frameworks, whatever else they disagree about, accept a list of function
schemas in the shape OpenAI popularised, and call back with a name and a
dictionary of arguments. This module produces that list and dispatches those
calls, which is enough to wire the engine into LangChain, CrewAI, the OpenAI
Agents SDK, Pydantic AI or something hand-rolled, without this library growing
an adapter per framework. Adapters age badly; a schema does not.

If your client speaks the Model Context Protocol, use the engine's MCP server
directly instead. It is the same four tools with the same guarantees.

Example:
    >>> from truegrain import Client, tools
    >>> client = Client.from_env()
    >>> specs = tools.tool_specs()                     # hand to the framework
    >>> tools.dispatch(client, "query", {"metrics": ["sales.order_revenue"]})
"""

from __future__ import annotations

from typing import Any, Mapping

from .client import Client
from .errors import Refused

__all__ = ["tool_specs", "dispatch", "TOOL_NAMES"]

TOOL_NAMES = ("list_metrics", "describe_metric", "list_dimensions", "query")

_FILTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "A structured predicate. This is not SQL and no SQL fragment is accepted."
    ),
    "required": ["dimension", "op"],
    "properties": {
        "dimension": {"type": "string", "description": "dataset.field or namespace.dataset.field"},
        "op": {
            "type": "string",
            "enum": ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte",
                     "between", "is_null", "is_not_null"],
        },
        "values": {
            "type": "array",
            "description": (
                "One value for the comparisons, two for between, one or more for "
                "in and not_in, none for the null checks."
            ),
            "items": {},
        },
    },
}


def tool_specs() -> list[dict[str, Any]]:
    """Return the engine's tools as OpenAI-style function schemas.

    The descriptions are written for a model rather than a human: they say when
    to use a tool and when not to, because that is what decides whether an agent
    picks the right metric or invents one.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "list_metrics",
                "description": (
                    "List the metrics this semantic layer can answer, with a "
                    "plain-language description of each. Call this first when you "
                    "do not already know the exact metric name. Do not guess a "
                    "metric name: a name not in this list does not exist and the "
                    "query will be refused."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search": {
                            "type": "string",
                            "description": "Narrow by name, description or synonym.",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_metric",
                "description": (
                    "Return one metric's full definition and exactly which "
                    "dimensions it can be grouped by. Call this before query "
                    "whenever you are unsure a metric answers the question asked, "
                    "or which dimensions are legal for it."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Qualified as namespace.metric, or bare when unambiguous.",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dimensions",
                "description": (
                    "List the dimensions available for grouping and filtering. "
                    "Pass a metric to get only the dimensions valid for it, which "
                    "is almost always what you want."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "description": "Restrict to this metric."}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query",
                "description": (
                    "Answer a question by naming metrics, dimensions and filters. "
                    "This is the only way to get numbers, and it is not a SQL "
                    "interface. The response carries the rows, the SQL that was "
                    "compiled and the model version, so any number can be traced "
                    "back. If a request is refused, read the refusal: it says "
                    "whether to change the request, wait, or stop."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["metrics"],
                    "properties": {
                        "metrics": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Metric names from list_metrics.",
                        },
                        "dimensions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Dimension names from list_dimensions.",
                        },
                        "filters": {"type": "array", "items": _FILTER_SCHEMA},
                        "grain": {
                            "type": "string",
                            "enum": ["second", "minute", "hour", "day", "week",
                                     "month", "quarter", "year"],
                            "description": "Time bucket for the selected time dimension.",
                        },
                        "limit": {"type": "integer", "description": "Maximum rows."},
                        "order_by": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["field"],
                                "properties": {
                                    "field": {"type": "string"},
                                    "desc": {"type": "boolean"},
                                },
                            },
                        },
                    },
                },
            },
        },
    ]


def dispatch(client: Client, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Run one tool call and return a JSON-serializable result.

    A refusal is returned rather than raised, because a framework feeding this
    back to a model wants the text, not a traceback. The shape carries the
    ``retry`` class so the model learns whether to change the request, wait, or
    stop, instead of looping on a denial.
    """
    args = dict(arguments)
    try:
        if name == "list_metrics":
            metrics = client.metrics(search=args.get("search"))
            return {"metrics": [m.raw for m in metrics]}
        if name == "describe_metric":
            if "name" not in args:
                raise ValueError("describe_metric needs a `name`")
            return client.metric(args["name"]).raw
        if name == "list_dimensions":
            dims = client.dimensions(metric=args.get("metric"))
            return {"dimensions": [d.raw for d in dims]}
        if name == "query":
            result = client.query(
                metrics=args.get("metrics", []),
                dimensions=args.get("dimensions"),
                filters=args.get("filters"),
                grain=args.get("grain"),
                limit=args.get("limit"),
                order_by=args.get("order_by"),
            )
            return result.raw
    except Refused as refusal:
        return {
            "refused": True,
            "code": refusal.code,
            "reason": refusal.reason,
            "hint": refusal.hint,
            "retry": refusal.retry,
            "note": (
                "This is a refusal, not an empty result. Do not report a number "
                "for this question."
            ),
        }
    except ValueError as bad_argument:
        return {"refused": True, "code": "invalid_arguments",
                "reason": str(bad_argument), "retry": "modify"}

    raise ValueError(f"unknown tool {name!r}; expected one of {', '.join(TOOL_NAMES)}")
