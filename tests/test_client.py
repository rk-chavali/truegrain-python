"""Transport, parsing and error mapping."""

from __future__ import annotations

import pytest

from truegrain import Client, Refused, TransportError, Unauthorized, filters

QUERY_RESPONSE = {
    "columns": ["region", "order_revenue"],
    "rows": [["NE", 750.0], ["MW", 135.5]],
    "row_count": 2,
    "compiled_sql": 'SELECT "region", SUM("order_total") AS "order_revenue" FROM orders',
    "model_version": "sha256:abc123",
    "dialect": "duckdb",
    "namespace": "sales",
}


def test_query_parses_rows_and_provenance(stub, client: Client) -> None:
    stub.on("POST", "/v1/query", QUERY_RESPONSE)

    result = client.query(metrics=["sales.order_revenue"], dimensions=["sales.customers.region"])

    assert result.columns == ("region", "order_revenue")
    assert result.row_count == 2
    assert len(result) == 2
    # Provenance travels with every result. It is how a disputed number gets
    # settled, so losing it at the client boundary would defeat the point.
    assert result.model_version == "sha256:abc123"
    assert "SUM" in result.compiled_sql


def test_rows_iterate_as_dicts(stub, client: Client) -> None:
    stub.on("POST", "/v1/query", QUERY_RESPONSE)
    rows = client.query(metrics=["sales.order_revenue"]).dicts()
    assert rows[0] == {"region": "NE", "order_revenue": 750.0}


def test_dataframe_carries_provenance(stub, client: Client) -> None:
    pd = pytest.importorskip("pandas")
    stub.on("POST", "/v1/query", QUERY_RESPONSE)

    frame = client.query(metrics=["sales.order_revenue"]).to_dataframe()

    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == ["region", "order_revenue"]
    assert frame.attrs["model_version"] == "sha256:abc123"
    assert frame.attrs["compiled_sql"].startswith("SELECT")


def test_request_omits_empty_fields(stub, client: Client) -> None:
    """The server rejects unknown and malformed fields, and an empty list is not
    the same as an absent one. Sending `"dimensions": []` would be a different
    request from sending nothing."""
    stub.on("POST", "/v1/query", QUERY_RESPONSE)

    client.query(metrics=["sales.order_revenue"])

    body = stub.last_request()["body"]
    assert body == {"metrics": ["sales.order_revenue"]}
    for absent in ("dimensions", "filters", "grain", "limit", "order_by"):
        assert absent not in body


def test_request_carries_everything_given(stub, client: Client) -> None:
    stub.on("POST", "/v1/query", QUERY_RESPONSE)

    client.query(
        metrics=["sales.order_revenue"],
        dimensions=["sales.orders.order_date"],
        filters=[filters.in_("sales.orders.status", "shipped", "delivered")],
        grain="month",
        limit=10,
        order_by=[{"field": "order_revenue", "desc": True}],
    )

    body = stub.last_request()["body"]
    assert body["grain"] == "month"
    assert body["limit"] == 10
    assert body["filters"] == [
        {"dimension": "sales.orders.status", "op": "in", "values": ["shipped", "delivered"]}
    ]
    assert body["order_by"] == [{"field": "order_revenue", "desc": True}]


def test_bearer_token_is_sent(stub, client: Client) -> None:
    stub.on("POST", "/v1/query", QUERY_RESPONSE)
    client.query(metrics=["sales.order_revenue"])
    assert stub.last_request()["authorization"] == "Bearer test-token"


def test_empty_metrics_fails_before_the_network(stub, client: Client) -> None:
    """Caught locally so an obvious mistake does not cost a round trip."""
    with pytest.raises(ValueError):
        client.query(metrics=[])
    assert stub.requests == []


# ---------- refusals ----------


def test_refusal_carries_code_hint_and_retry(stub, client: Client) -> None:
    stub.on("POST", "/v1/query", {
        "code": "fan_out_would_inflate",
        "reason": "metric order_revenue aggregates SUM(...) over orders",
        "hint": "Metrics defined at the order_lines grain answer this correctly: line_revenue.",
        "retry": "modify",
    }, status=422)

    with pytest.raises(Refused) as caught:
        client.query(metrics=["sales.order_revenue"], dimensions=["sales.products.category"])

    refusal = caught.value
    assert refusal.code == "fan_out_would_inflate"
    assert refusal.status == 422
    assert "line_revenue" in refusal.hint
    assert refusal.should_modify()
    assert not refusal.is_final()


def test_denial_is_final(stub, client: Client) -> None:
    """A denial must not look retryable, or an agent will loop on it forever."""
    stub.on("POST", "/v1/query", {
        "code": "access_denied",
        "reason": "this identity may not read metric order_revenue",
        "retry": "never",
    }, status=403)

    with pytest.raises(Refused) as caught:
        client.query(metrics=["sales.order_revenue"])

    assert caught.value.is_final()
    assert not caught.value.should_modify()


def test_policy_outage_says_wait(stub, client: Client) -> None:
    stub.on("POST", "/v1/query", {
        "code": "policy_unavailable",
        "reason": "access could not be resolved, so the query was not run",
        "retry": "later",
    }, status=503)

    with pytest.raises(Refused) as caught:
        client.query(metrics=["sales.order_revenue"])

    assert caught.value.should_wait()


def test_unauthorized_is_its_own_error(stub, client: Client) -> None:
    stub.on("GET", "/v1/metrics", {"code": "unauthenticated", "reason": "missing bearer token"}, status=401)
    with pytest.raises(Unauthorized):
        client.metrics()


def test_unreachable_engine_is_a_transport_error() -> None:
    """Never a Refused: nothing was decided about the request."""
    client = Client("http://127.0.0.1:1", timeout=2)
    with pytest.raises(TransportError):
        client.health()


def test_non_json_body_is_a_transport_error(stub, client: Client) -> None:
    stub.on("GET", "/v1/health", b"<html>502 Bad Gateway</html>", status=502)
    with pytest.raises(TransportError):
        client.health()


# ---------- metadata ----------


def test_metrics_are_parsed(stub, client: Client) -> None:
    stub.on("GET", "/v1/metrics", {
        "model": "sales",
        "model_version": "sha256:abc",
        "metrics": [{
            "name": "sales.order_revenue",
            "namespace": "sales",
            "description": "Total value of all orders.",
            "synonyms": ["revenue", "top line"],
            "dimensions": ["sales.customers.region"],
        }],
    })

    metrics = client.metrics(search="revenue")

    assert len(metrics) == 1
    assert metrics[0].name == "sales.order_revenue"
    assert "revenue" in metrics[0].synonyms
    assert metrics[0].dimensions == ("sales.customers.region",)
    assert "search=revenue" in stub.last_request()["query"]


def test_describe_metric_handles_dimension_objects(stub, client: Client) -> None:
    """list_metrics returns dimension names; describe_metric returns objects.
    Both must land in the same field."""
    stub.on("GET", "/v1/metrics/sales.order_revenue", {
        "name": "sales.order_revenue",
        "namespace": "sales",
        "description": "Total value of all orders.",
        "definition": "SUM(orders.order_total)",
        "dimensions": [{"name": "sales.customers.region", "is_time": False}],
    })

    metric = client.metric("sales.order_revenue")

    assert metric.definition == "SUM(orders.order_total)"
    assert metric.dimensions == ("sales.customers.region",)


def test_namespaces_report_unavailability(stub, client: Client) -> None:
    """An unavailable namespace is reported rather than omitted, so absence is
    never mistaken for a model that simply had no such metrics."""
    stub.on("GET", "/v1/namespaces", {
        "workspace": "sales, broken",
        "workspace_digest": "sha256:abc",
        "namespaces": [
            {"name": "sales", "available": True, "metric_count": 8, "owners": ["@acme/sales"]},
            {"name": "broken", "available": False, "metric_count": 0, "error": "invalid YAML at line 3"},
        ],
    })

    namespaces = {n.name: n for n in client.namespaces()}

    assert namespaces["sales"].owners == ("@acme/sales",)
    assert not namespaces["broken"].available
    assert "invalid YAML" in namespaces["broken"].error


def test_health_surfaces_what_is_not_enforced(stub, client: Client) -> None:
    stub.on("GET", "/v1/health", {
        "workspace": "sales",
        "workspace_digest": "sha256:abc",
        "governance": {"resolver": "allow-all", "column_level": False},
        "enforcement_notes": ["No column-level access control is configured."],
    })

    health = client.health()

    assert not health.governs_columns
    assert health.enforcement_notes


def test_compile_returns_sql_and_no_rows(stub, client: Client) -> None:
    stub.on("POST", "/v1/compile", {
        "compiled_sql": "WITH fact_1 AS (SELECT ...)",
        "columns": ["order_date", "order_revenue"],
        "dialect": "duckdb",
        "namespace": "marketing, sales",
        "parts": 2,
        "model_version": "sha256:abc",
    })

    compiled = client.compile(metrics=["sales.order_revenue", "marketing.campaign_spend"])

    assert compiled.parts == 2
    assert compiled.compiled_sql.startswith("WITH")
    assert not hasattr(compiled, "rows")
