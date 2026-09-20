"""What the engine says about itself, and where the served model came from.

Two kinds of test here, because the seven metadata calls fail in two ways.

They can drop a field on the way in, which the parsing tests cover. And a 404
meaning "nobody configured this" can arrive as something a caller cannot branch
on, which is the difference between an engine whose tests all pass and an
engine that was never given any.

The origin tests exist because the Go client shipped without that field while
its own reload documentation told callers to read it. The operation coverage
test did not catch it: it checks that every operationId has a method, not that
every documented field has somewhere to land. So these read the vendored spec
rather than trusting this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from truegrain import Client, Diagnosis, Diff, Refused

yaml = pytest.importorskip("yaml", reason="pyyaml is needed to read the spec")

SPEC = Path(__file__).resolve().parents[1] / "spec" / "openapi.yaml"

DOCTOR_RESPONSE = {
    "ok": False,
    "tables_checked": 12,
    "findings": [
        {
            "severity": "error",
            "dataset": "orders",
            "field": "region",
            "source": "analytics.fct_orders",
            "message": "the model names a column the warehouse does not have",
            "hint": "drop the field, or point it at the column that replaced it",
        },
        {"severity": "warning", "dataset": "customers", "message": "the warehouse type widened"},
    ],
}


def test_doctor_keeps_the_verdict_separate_from_the_findings(stub, client: Client) -> None:
    stub.on("GET", "/v1/doctor", DOCTOR_RESPONSE)

    report = client.doctor()

    assert isinstance(report, Diagnosis)
    assert report.ok is False
    assert report.tables_checked == 12
    # Severity is the whole point: a warning and an error read the same to
    # anybody who only counts findings.
    assert [f.severity for f in report.findings] == ["error", "warning"]
    assert len(report.errors()) == 1
    # `field` on the wire, field_name here, and the rename must not lose it.
    assert report.findings[0].field_name == "region"
    assert report.findings[0].hint


def test_doctor_reports_why_nothing_was_checked(stub, client: Client) -> None:
    """An executor that cannot introspect produces no findings, which is what a
    healthy model produces. Without `skipped` the two are identical, and the
    wrong one is reassuring."""
    stub.on(
        "GET",
        "/v1/doctor",
        {"ok": True, "tables_checked": 0, "findings": [], "skipped": "this executor cannot describe the warehouse"},
    )

    assert client.doctor().skipped


@pytest.mark.parametrize(
    ("method", "path", "http_method"),
    [
        ("run_tests", "/v1/tests", "POST"),
        ("diff", "/v1/diff", "GET"),
        ("doctor_history", "/v1/doctor/history", "GET"),
        ("reload", "/v1/reload", "POST"),
    ],
)
def test_not_configured_arrives_as_a_refusal(stub, client: Client, method, path, http_method) -> None:
    """Four endpoints answer 404 for "nobody set this up", and all four would
    otherwise be reported as a transport failure a caller cannot branch on."""
    stub.on(
        http_method,
        path,
        {"code": "not_served", "reason": "nobody configured this", "retry": "never"},
        status=404,
    )

    with pytest.raises(Refused) as caught:
        getattr(client, method)()
    assert caught.value.is_final(), "a caller would retry something nobody configured"


def test_run_tests_reports_what_it_did_not_run(stub, client: Client) -> None:
    """The failure this guards is a green suite that never executed half its
    cases, which is worse than a red one."""
    stub.on(
        "POST",
        "/v1/tests",
        {
            "ok": True,
            "passed": 3,
            "failed": 0,
            "skipped": 0,
            "withheld": 4,
            "results": [
                {"name": "a fan-out is refused", "passed": True, "duration_ms": 12},
                {
                    "name": "revenue by region",
                    "passed": False,
                    "skipped": True,
                    "reason": "this credential may not run a query",
                },
            ],
        },
    )

    report = client.run_tests()

    assert report.ok
    assert report.withheld == 4, "withheld was dropped, so ok reads as a full pass"
    assert report.results[0].duration_ms == 12
    assert report.results[1].reason
    # A skipped case is not a failure, and counting it as one would make a
    # withheld suite look broken rather than incomplete.
    assert report.failures() == ()


def test_policy_carries_the_notes_that_say_what_is_not_enforced(stub, client: Client) -> None:
    stub.on(
        "GET",
        "/v1/policy",
        {
            "governance": {"resolver": "allow-all", "column_level": False, "note": "every caller reads every column"},
            "enforcement_notes": ["no column-level access control is configured"],
        },
    )

    policy = client.policy()

    # An engine running allow-all has to say so, or a reader assumes a gate
    # exists because the product has one.
    assert policy.governs_columns is False
    assert policy.enforcement_notes, "the notes stating the gaps were dropped"


def test_explain_policy_sends_the_metric_and_refuses_an_empty_one(stub, client: Client) -> None:
    stub.on(
        "POST",
        "/v1/policy/explain",
        {
            "metric": "retail.order_revenue",
            "identity": "analyst@acme.com",
            "readable": ["customers.region", "orders.placed_at"],
        },
    )

    explained = client.explain_policy("retail.order_revenue")
    assert explained.identity == "analyst@acme.com"
    assert len(explained.readable) == 2
    assert stub.requests[-1]["body"] == {"metric": "retail.order_revenue"}

    sent = len(stub.requests)
    with pytest.raises(ValueError):
        client.explain_policy("")
    assert len(stub.requests) == sent, "an empty metric reached the engine"


def test_diff_carries_the_before_and_after(stub, client: Client) -> None:
    """Added and removed name things; altered is the one that says a number
    moved, and it is useless without both sides."""
    stub.on(
        "GET",
        "/v1/diff",
        {
            "changed": True,
            "from": "sha256:aaa",
            "to": "sha256:bbb",
            "added": ["retail.refund_total"],
            "removed": [],
            "altered": {
                "order_revenue by region": {
                    "before": "SELECT sum(amount) ...",
                    "after": "SELECT sum(amount_net) ...",
                }
            },
        },
    )

    diff = client.diff()

    assert isinstance(diff, Diff)
    assert diff.changed
    # `from` is a keyword, so the rename must not lose either side.
    assert diff.from_version == "sha256:aaa"
    assert diff.to_version == "sha256:bbb"
    change = diff.altered["order_revenue by region"]
    assert change.before and change.after and change.before != change.after


@pytest.mark.parametrize("status", ["reading", "already running"])
def test_reload_says_which_of_the_two_happened(stub, client: Client, status: str) -> None:
    """A pipeline that treats "already running" as a failure retries a sync
    that is already under way."""
    stub.on("POST", "/v1/reload", {"status": status}, status=202)

    assert client.reload() == status
    # It means "look now", not "install this". A body would be a second way
    # into production.
    assert stub.requests[-1]["body"] == {}


def test_reload_reports_a_failed_load_as_a_refusal(stub, client: Client) -> None:
    """The engine keeps serving the previous model and answers 502. A pipeline
    must be able to tell that from the network being down, because only one of
    them means the deploy did not land."""
    stub.on(
        "POST",
        "/v1/reload",
        {
            "code": "model_did_not_load",
            "reason": "the previously loaded model is still being served",
            "retry": "modify",
        },
        status=502,
    )

    with pytest.raises(Refused) as caught:
        client.reload()
    assert caught.value.should_modify()


# ---------- where the served model came from ----------


def spec_origin_properties() -> list[str]:
    """The property names the spec documents under health.origin."""
    assert SPEC.exists(), f"the vendored spec is missing from {SPEC}"
    document = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    health = document["components"]["schemas"]["Health"]["properties"]
    assert "origin" in health, (
        "the spec no longer describes health.origin; if the engine dropped it, "
        "drop Health.origin too rather than leaving a field nothing fills"
    )
    return sorted(health["origin"]["properties"])


def test_origin_carries_every_field_the_spec_documents() -> None:
    """The test that was missing when the Go client shipped without Origin."""
    from truegrain import Origin

    carried = set(Origin.__dataclass_fields__)
    for name in spec_origin_properties():
        assert name in carried, f"the engine reports origin.{name} and Origin has no field for it"


def test_health_carries_the_commit_a_deploy_is_confirmed_by(stub, client: Client) -> None:
    """Named for the thing a pipeline does. Reload deliberately names no commit,
    so the only way to know a deploy landed is to read it here."""
    stub.on(
        "GET",
        "/v1/health",
        {
            "workspace": "retail",
            "workspace_digest": "sha256:abc",
            "origin": {
                "repository": "https://github.com/acme/models.git",
                "ref": "main",
                "commit": "23523843c4f5b92df4f8628a96a79bb5cbbfa9ee",
                "subdirectory": "models",
            },
        },
    )

    health = client.health()

    assert health.origin is not None, "an engine following a repository reported no origin"
    assert health.origin.commit == "23523843c4f5b92df4f8628a96a79bb5cbbfa9ee"
    assert health.origin.ref == "main"
    assert health.origin.subdirectory == "models"
    # The URL must arrive as given. A credential in it is refused at startup,
    # so anything that looked like redaction here would be hiding a bug.
    assert "@" not in health.origin.repository


def test_an_engine_reading_from_a_path_reports_no_origin(stub, client: Client) -> None:
    """So "not under version control" is a None check rather than a record of
    empty strings that reads like a repository nobody named."""
    stub.on("GET", "/v1/health", {"workspace": "retail", "workspace_digest": "sha256:abc"})

    assert client.health().origin is None
