"""Asynchronous execution: submit, poll, page, cancel.

These cover the client's side of the contract. The engine's side, including who
may read a finished job, is tested in internal/serve/rest/jobs_test.go.
"""

from __future__ import annotations

import pytest

from truegrain import Client, Refused, SemanticError

SQL = 'SELECT "region", SUM("order_total") AS "order_revenue" FROM orders GROUP BY 1'

ACCEPTED = {
    "job_id": "j-1",
    "state": "running",
    "compiled_sql": SQL,
    "columns": ["region", "order_revenue"],
    "model_version": "sha256:abc123",
    "namespace": "sales",
    "dialect": "duckdb",
}


def finished(rows: list[list], *, row_count: int | None = None, cursor: str = "") -> dict:
    body = {
        "job_id": "j-1",
        "state": "succeeded",
        "columns": ["region", "order_revenue"],
        "rows": rows,
        "row_count": row_count if row_count is not None else len(rows),
        "compiled_sql": SQL,
        "model_version": "sha256:abc123",
        "namespace": "sales",
        "dialect": "duckdb",
    }
    if cursor:
        body["next_cursor"] = cursor
    return body


def test_submit_returns_sql_before_the_query_finishes(stub, client: Client) -> None:
    stub.on("POST", "/v1/jobs", ACCEPTED, status=202)

    job = client.submit(metrics=["sales.order_revenue"], dimensions=["sales.customers.region"])

    assert job.job_id == "j-1"
    assert job.is_running
    assert not job.is_done
    # Compilation is synchronous, so the SQL is available immediately. An agent
    # can show its work without waiting for the warehouse.
    assert "SUM" in job.compiled_sql


def test_run_polls_until_done_and_returns_a_result(stub, client: Client) -> None:
    stub.on("POST", "/v1/jobs", ACCEPTED, status=202)

    polls = {"n": 0}

    def poll(_):
        polls["n"] += 1
        if polls["n"] < 3:
            return {"job_id": "j-1", "state": "running"}
        return finished([["NE", 750.0], ["MW", 135.5]])

    stub.on("GET", "/v1/jobs/j-1", poll)

    result = client.run(metrics=["sales.order_revenue"], dimensions=["sales.customers.region"])

    assert polls["n"] == 3, "run must keep polling while the job is running"
    assert result.row_count == 2
    assert result.dicts()[0] == {"region": "NE", "order_revenue": 750.0}
    # Provenance survives the asynchronous path exactly as it does the
    # synchronous one; that is the whole point of carrying it.
    assert result.model_version == "sha256:abc123"
    assert "SUM" in result.compiled_sql


def test_run_collects_every_page(stub, client: Client) -> None:
    """The reason the cursor exists: a result too big to return in one body."""
    stub.on("POST", "/v1/jobs", ACCEPTED, status=202)

    pages = {
        "": finished([["a", 1], ["b", 2]], row_count=5, cursor="p2"),
        "p2": finished([["c", 3], ["d", 4]], row_count=5, cursor="p3"),
        "p3": finished([["e", 5]], row_count=5),
    }

    def paged(_):
        return pages[stub.last_query().get("cursor", "")]

    stub.on("GET", "/v1/jobs/j-1", paged)

    result = client.run(metrics=["sales.order_revenue"])

    assert result.row_count == 5
    assert [r[0] for r in result.rows] == ["a", "b", "c", "d", "e"]


def test_run_stops_paging_once_every_row_is_collected(stub, client: Client) -> None:
    """A server that keeps handing out a cursor must not loop the client.

    row_count bounds the walk, so the loop does not depend on trusting the
    cursor to eventually be empty.
    """
    stub.on("POST", "/v1/jobs", ACCEPTED, status=202)
    stub.on("GET", "/v1/jobs/j-1", lambda _: finished([["a", 1]], row_count=1, cursor="never-ends"))

    result = client.run(metrics=["sales.order_revenue"])

    assert result.row_count == 1


def test_denied_request_is_refused_at_submit_not_after_a_wait(stub, client: Client) -> None:
    stub.on(
        "POST",
        "/v1/jobs",
        {
            "code": "access_denied",
            "reason": "column sales.orders.order_total is not readable by this identity",
            "retry": "never",
        },
        status=403,
    )

    with pytest.raises(Refused) as caught:
        client.run(metrics=["sales.order_revenue"])

    assert caught.value.is_final()
    assert caught.value.code == "access_denied"


def test_a_failed_job_raises_with_its_retry_class(stub, client: Client) -> None:
    stub.on("POST", "/v1/jobs", ACCEPTED, status=202)
    # A failed job polls as 200: the poll succeeded, the query did not. The
    # failure is described in the body with the refusal vocabulary.
    stub.on(
        "GET",
        "/v1/jobs/j-1",
        {
            "job_id": "j-1",
            "state": "failed",
            "code": "execution_failed",
            "reason": "warehouse unavailable",
            "retry": "later",
        },
    )

    with pytest.raises(Refused) as caught:
        client.run(metrics=["sales.order_revenue"])

    assert caught.value.should_wait(), "an executor failure is worth retrying unchanged"


def test_backpressure_is_retryable(stub, client: Client) -> None:
    """Hitting the concurrency cap must not look like a permanent failure."""
    stub.on(
        "POST",
        "/v1/jobs",
        {
            "code": "too_many_jobs",
            "reason": "this caller already has 4 queries running, which is the limit",
            "hint": "wait for one to finish, or cancel one",
            "retry": "later",
        },
        status=400,
    )

    with pytest.raises(Refused) as caught:
        client.submit(metrics=["sales.order_revenue"])

    assert caught.value.should_wait()
    assert not caught.value.is_final()


def test_a_cancelled_job_is_not_reported_as_a_result(stub, client: Client) -> None:
    stub.on("POST", "/v1/jobs", ACCEPTED, status=202)
    stub.on("GET", "/v1/jobs/j-1", {"job_id": "j-1", "state": "cancelled"})

    with pytest.raises(SemanticError, match="cancelled"):
        client.run(metrics=["sales.order_revenue"])


def test_cancel_sends_delete(stub, client: Client) -> None:
    stub.on("DELETE", "/v1/jobs/j-1", {"job_id": "j-1", "state": "cancelled"})

    job = client.cancel_job("j-1")

    assert job.state == "cancelled"
    assert stub.last_request()["method"] == "DELETE"


def test_expired_wait_cancels_the_job(stub, client: Client) -> None:
    """Giving up on a wait must stop the warehouse work, not just stop looking.

    Leaving it running would keep spending on an answer nobody will read.
    """
    stub.on("POST", "/v1/jobs", ACCEPTED, status=202)
    stub.on("GET", "/v1/jobs/j-1", {"job_id": "j-1", "state": "running"})
    stub.on("DELETE", "/v1/jobs/j-1", {"job_id": "j-1", "state": "cancelled"})

    with pytest.raises(SemanticError, match="did not finish"):
        client.run(metrics=["sales.order_revenue"], max_wait=0.3)

    assert any(r["method"] == "DELETE" for r in stub.requests), (
        "an abandoned wait must cancel the job it abandoned"
    )


def test_page_size_reaches_the_server(stub, client: Client) -> None:
    stub.on("GET", "/v1/jobs/j-1", finished([["a", 1]]))

    client.job("j-1", cursor="p2", page_size=250)

    assert stub.last_query() == {"cursor": "p2", "page_size": "250"}


def test_no_job_method_accepts_sql(client: Client) -> None:
    """The raw-SQL guarantee applied to the newest surface."""
    for name in ("run", "submit", "job", "cancel_job", "wait"):
        params = client.__class__.__dict__[name].__code__.co_varnames
        assert "sql" not in params, f"{name} accepts a sql argument"
