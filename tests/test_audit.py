"""The audit record, which is the only place a refusal survives the request.

A query that was declined returns a refusal to the caller and nothing to
anybody else. These tests cover the call that lets an operator read the
decisions afterwards, and in particular that refused and denied stay apart:
folding them together would report every fan-out as an access incident.
"""

from __future__ import annotations

import pytest

from truegrain import AuditPage, Client, Refused

# Two refusals, one denial and one allowed query, as the engine records them.
AUDIT_RESPONSE = {
    "events": [
        {
            "time": "2026-09-18T09:14:02Z",
            "identity": "sa-agent@acme.iam.gserviceaccount.com",
            "decision": "refused",
            "refusal_code": "fan_out_would_inflate",
            "retry": "modify",
            "reason": "order_revenue is defined at the orders grain",
            "hint": "Metrics at the order_lines grain answer this: line_revenue.",
            "metrics": ["sales.order_revenue"],
            "dimensions": ["sales.products.category"],
            "namespace": "sales",
            "model_version": "sha256:abc",
            "dialect": "duckdb",
        },
        {
            "time": "2026-09-18T09:13:55Z",
            "identity": "sa-agent@acme.iam.gserviceaccount.com",
            "decision": "denied",
            "denied_fields": ["sales.customers.email"],
            "reason": "this identity may not read customers.email",
            "retry": "never",
        },
        {
            "time": "2026-09-18T09:13:40Z",
            "identity": "anonymous",
            "decision": "allowed",
            "metrics": ["sales.order_revenue"],
            "sql_hash": "sha256:9f2c",
            "job_id": "job_017",
            "row_count": 2,
            "bytes_billed": 10485760,
            "duration_ms": 412,
        },
    ],
    "count": 3,
    "note": "A bounded window. An engine started with an audit file keeps everything there.",
}


def test_audit_parses_a_window_of_decisions(stub, client: Client) -> None:
    stub.on("GET", "/v1/audit", AUDIT_RESPONSE)

    page = client.audit()

    assert isinstance(page, AuditPage)
    assert page.count == 3
    assert len(page) == 3
    assert "bounded window" in page.note
    assert [e.decision for e in page] == ["refused", "denied", "allowed"]


def test_refusals_are_separate_from_denials(stub, client: Client) -> None:
    """Denied is access, refused is correctness. Counting them together would
    report every fan-out as a security incident."""
    stub.on("GET", "/v1/audit", AUDIT_RESPONSE)

    page = client.audit()
    refusals = page.refusals()

    assert len(refusals) == 1
    assert refusals[0].code == "fan_out_would_inflate"
    assert refusals[0].is_refusal()
    assert not refusals[0].is_denial()

    denial = page.events[1]
    assert denial.is_denial()
    assert not denial.is_refusal()
    assert denial.denied_fields == ("sales.customers.email",)


def test_a_refusal_carries_the_question_that_would_answer(stub, client: Client) -> None:
    """The hint is the actionable half. An operator reading the record needs to
    see what the caller should have asked."""
    stub.on("GET", "/v1/audit", AUDIT_RESPONSE)

    refusal = client.audit().refusals()[0]

    assert refusal.retry == "modify"
    assert "line_revenue" in refusal.hint
    assert refusal.metrics == ("sales.order_revenue",)
    assert refusal.dimensions == ("sales.products.category",)


def test_audit_carries_no_values_sql_or_rows(stub, client: Client) -> None:
    """The record identifies a statement without disclosing it, and never
    carries filter values or result rows. A model that grew a field for them
    would be a disclosure this SDK made easy to read."""
    stub.on("GET", "/v1/audit", AUDIT_RESPONSE)

    allowed = client.audit().events[2]

    assert allowed.sql_hash == "sha256:9f2c"
    assert not hasattr(allowed, "compiled_sql")
    assert not hasattr(allowed, "rows")
    assert not hasattr(allowed, "filters")
    assert allowed.bytes_billed == 10485760


def test_limit_and_decision_are_sent_as_query_parameters(stub, client: Client) -> None:
    stub.on("GET", "/v1/audit", {"events": [], "count": 0})

    client.audit(limit=50, decision="refused")

    assert stub.last_query() == {"limit": "50", "decision": "refused"}


def test_no_parameters_are_sent_when_none_are_asked_for(stub, client: Client) -> None:
    """Sending limit= empty would be a 400 from an engine validating its own
    contract, so the client must omit rather than blank them."""
    stub.on("GET", "/v1/audit", {"events": [], "count": 0})

    client.audit()

    assert stub.last_request()["query"] == ""


def test_zero_is_sent_rather_than_dropped(stub, client: Client) -> None:
    """limit=0 is out of the contract's range and must reach the engine to be
    refused there. Treating it as "unset" would silently return 200 events."""
    stub.on("GET", "/v1/audit", {"events": [], "count": 0})

    client.audit(limit=0)

    assert stub.last_query() == {"limit": "0"}


def test_no_reader_configured_is_a_refusal_not_a_crash(stub, client: Client) -> None:
    """404 means the capability is absent rather than withheld: the operator
    named nobody. It must not look like a missing engine."""
    stub.on("GET", "/v1/audit", {
        "code": "audit_not_served",
        "reason": "this engine was not started with -audit-readers",
        "retry": "never",
    }, status=404)

    with pytest.raises(Refused) as caught:
        client.audit()

    assert caught.value.status == 404
    assert caught.value.code == "audit_not_served"
    assert caught.value.is_final()


def test_not_a_named_reader_is_also_final(stub, client: Client) -> None:
    """403 means authenticated but not named. Retrying cannot help, and an
    agent that treats it as transient will loop."""
    stub.on("GET", "/v1/audit", {
        "code": "not_an_audit_reader",
        "reason": "this identity is not an audit reader",
        "retry": "never",
    }, status=403)

    with pytest.raises(Refused) as caught:
        client.audit()

    assert caught.value.status == 403
    assert caught.value.code == "not_an_audit_reader"
    assert caught.value.is_final()


def test_an_unrecognised_decision_reads_as_error(stub, client: Client) -> None:
    """A decision this SDK does not know must not read as the empty string, or
    a caller branching on it treats something significant as nothing."""
    stub.on("GET", "/v1/audit", {"events": [{"time": "2026-09-18T09:00:00Z"}], "count": 1})

    event = client.audit().events[0]

    assert event.decision == "error"
    assert event.identity == "anonymous"


def test_count_comes_from_the_engine_not_from_recounting(stub, client: Client) -> None:
    """A disagreement between the engine's count and what arrived should stay
    visible rather than be papered over."""
    stub.on("GET", "/v1/audit", {"events": [], "count": 7})

    page = client.audit()

    assert page.count == 7
    assert len(page) == 0
