"""The SDK is checked against the OpenAPI contract.

This client is hand-written rather than generated, because the value is in the
parts no generator emits: `Refused.should_modify()`, and `run()` submitting and
polling a job so no caller writes that loop. The cost of hand-writing is that it
can silently fall behind the engine. These tests are what pay it.

The spec is vendored at `spec/openapi.yaml` and pinned to an engine commit in
`spec/PINNED_AT`. Vendoring keeps this repository buildable offline and makes a
contract change a reviewable diff rather than a build that breaks one morning
because something moved in another repository. A scheduled job compares the pin
against the engine and opens a pull request when they diverge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from truegrain import OPERATIONS, Client

yaml = pytest.importorskip("yaml", reason="pyyaml is needed to read the spec")

SPEC = Path(__file__).resolve().parents[1] / "spec" / "openapi.yaml"


def spec_operations() -> dict[str, str]:
    """Every operationId in the spec, mapped to its 'METHOD /path'."""
    # Never skipped. The vendored spec is committed, so its absence is a broken
    # repository rather than an environment without the engine checked out, and
    # a skipped contract test is a contract test nobody notices is gone.
    assert SPEC.exists(), f"the vendored spec is missing from {SPEC}"
    document = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            found[operation["operationId"]] = f"{method.upper()} {path}"
    return found


def test_every_operation_has_a_client_method() -> None:
    missing = {
        operation_id: route
        for operation_id, route in spec_operations().items()
        if operation_id not in OPERATIONS
    }
    assert not missing, (
        "the engine exposes operations this client cannot call: "
        f"{missing}. Add a method and register it in client.OPERATIONS."
    )


def test_no_client_method_claims_a_missing_operation() -> None:
    known = spec_operations()
    stale = {op: name for op, name in OPERATIONS.items() if op not in known}
    assert not stale, (
        f"OPERATIONS names operations the spec does not define: {stale}. "
        "Either the spec lost an endpoint or the mapping is wrong."
    )


def test_every_mapped_method_exists_and_is_documented() -> None:
    for operation_id, method_name in OPERATIONS.items():
        method = getattr(Client, method_name, None)
        assert method is not None, (
            f"OPERATIONS maps {operation_id} to Client.{method_name}, which does not exist"
        )
        assert callable(method)
        assert method.__doc__, (
            f"Client.{method_name} has no docstring; it is the only documentation "
            "most callers will read"
        )
