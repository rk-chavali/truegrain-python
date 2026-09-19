"""Tool schemas and dispatch, which is how any agent framework wires in."""

from __future__ import annotations

from truegrain import Client, tools


def test_every_tool_has_a_schema_a_framework_can_use() -> None:
    specs = tools.tool_specs()
    names = {spec["function"]["name"] for spec in specs}

    assert names == set(tools.TOOL_NAMES)
    for spec in specs:
        function = spec["function"]
        assert spec["type"] == "function"
        assert function["description"], f"{function['name']} has no description"
        assert function["parameters"]["type"] == "object"
        # Descriptions are read by a model, not a person. A bare restatement of
        # the name teaches it nothing about when to pick the tool.
        assert len(function["description"]) > 80


def test_no_tool_accepts_sql() -> None:
    """The guarantee that makes routing an agent through this worth anything.

    An agent cannot express an ungoverned query because the vocabulary does not
    contain one, and that holds only while no tool takes SQL.
    """
    for spec in tools.tool_specs():
        function = spec["function"]
        properties = function["parameters"].get("properties", {})
        for banned in ("sql", "query_string", "statement", "raw", "where"):
            assert banned not in properties, f"{function['name']} accepts {banned}"
        assert "sql" not in function["name"].lower()


def test_query_tool_requires_metrics() -> None:
    query = next(s for s in tools.tool_specs() if s["function"]["name"] == "query")
    assert query["function"]["parameters"]["required"] == ["metrics"]


def test_dispatch_runs_a_query(stub, client: Client) -> None:
    stub.on("POST", "/v1/query", {
        "columns": ["region", "order_revenue"],
        "rows": [["NE", 750.0]],
        "compiled_sql": "SELECT ...",
        "model_version": "sha256:abc",
    })

    out = tools.dispatch(client, "query", {"metrics": ["sales.order_revenue"]})

    assert out["rows"] == [["NE", 750.0]]
    assert out["compiled_sql"] == "SELECT ..."


def test_dispatch_returns_refusals_rather_than_raising(stub, client: Client) -> None:
    """A framework feeding this back to a model wants the text, not a traceback,
    and the model needs the retry class to know whether to try again."""
    stub.on("POST", "/v1/query", {
        "code": "access_denied",
        "reason": "this identity may not read metric order_revenue",
        "hint": "ask the owner for access",
        "retry": "never",
    }, status=403)

    out = tools.dispatch(client, "query", {"metrics": ["sales.order_revenue"]})

    assert out["refused"] is True
    assert out["code"] == "access_denied"
    assert out["retry"] == "never"
    assert "not an empty result" in out["note"]


def test_dispatch_reports_bad_arguments_as_modifiable(stub, client: Client) -> None:
    out = tools.dispatch(client, "describe_metric", {})
    assert out["refused"] is True
    assert out["retry"] == "modify"
    assert stub.requests == [], "a malformed call should not reach the engine"


def test_dispatch_rejects_an_unknown_tool(stub, client: Client) -> None:
    try:
        tools.dispatch(client, "run_sql", {})
    except ValueError as err:
        assert "run_sql" in str(err)
    else:  # pragma: no cover
        raise AssertionError("an unknown tool must raise")
