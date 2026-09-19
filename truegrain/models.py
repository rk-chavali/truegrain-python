"""Typed views over the API's responses.

Every model keeps the raw payload alongside the parsed fields. The engine is a
young project and the spec will gain fields; a caller should never have to wait
for an SDK release to read one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

# Values the engine can return in a result cell.
Cell = Any


def _get(payload: dict[str, Any], key: str, default: Any = None) -> Any:
    value = payload.get(key)
    return default if value is None else value


@dataclass(frozen=True)
class Namespace:
    """One team's independently owned set of definitions."""

    name: str
    available: bool
    metric_count: int
    digest: str = ""
    owners: tuple[str, ...] = ()
    #: Present only when the namespace failed to load. An unavailable namespace
    #: is reported rather than omitted, so absence is never mistaken for a
    #: model that simply had no such metrics.
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Namespace":
        return cls(
            name=_get(payload, "name", ""),
            available=bool(_get(payload, "available", False)),
            metric_count=int(_get(payload, "metric_count", 0)),
            digest=_get(payload, "digest", ""),
            owners=tuple(_get(payload, "owners", [])),
            error=_get(payload, "error", ""),
            raw=payload,
        )


@dataclass(frozen=True)
class Metric:
    """A metric, named `namespace.metric`."""

    name: str
    namespace: str
    description: str
    datatype: str = ""
    synonyms: tuple[str, ...] = ()
    #: Only the dimensions this identity may group the metric by.
    dimensions: tuple[str, ...] = ()
    #: The Ossie expression, present on describe but not on list.
    definition: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Metric":
        dims = _get(payload, "dimensions", [])
        # describe_metric returns dimension objects; list_metrics returns names.
        names = tuple(d["name"] if isinstance(d, dict) else d for d in dims)
        return cls(
            name=_get(payload, "name", ""),
            namespace=_get(payload, "namespace", ""),
            description=_get(payload, "description", ""),
            datatype=_get(payload, "datatype", ""),
            synonyms=tuple(_get(payload, "synonyms", [])),
            dimensions=names,
            definition=_get(payload, "definition", ""),
            raw=payload,
        )


@dataclass(frozen=True)
class Dimension:
    """A field usable for grouping and filtering, named `namespace.dataset.field`."""

    name: str
    is_time: bool
    namespace: str = ""
    description: str = ""
    datatype: str = ""
    synonyms: tuple[str, ...] = ()
    #: Time buckets available, present only for a time dimension.
    grains: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Dimension":
        return cls(
            name=_get(payload, "name", ""),
            is_time=bool(_get(payload, "is_time", False)),
            namespace=_get(payload, "namespace", ""),
            description=_get(payload, "description", ""),
            datatype=_get(payload, "datatype", ""),
            synonyms=tuple(_get(payload, "synonyms", [])),
            grains=tuple(_get(payload, "grains", [])),
            raw=payload,
        )


@dataclass(frozen=True)
class Health:
    """What a deployment enforces, including what it does not."""

    workspace: str
    workspace_digest: str
    executor: str = ""
    metric_count: int = 0
    dimension_count: int = 0
    namespaces: tuple[Namespace, ...] = ()
    #: Plain-language statements of what this configuration does NOT enforce.
    #: Read them before trusting the layer with anything sensitive.
    enforcement_notes: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Health":
        return cls(
            workspace=_get(payload, "workspace", ""),
            workspace_digest=_get(payload, "workspace_digest", ""),
            executor=_get(payload, "executor", ""),
            metric_count=int(_get(payload, "metric_count", 0)),
            dimension_count=int(_get(payload, "dimension_count", 0)),
            namespaces=tuple(Namespace.parse(n) for n in _get(payload, "namespaces", [])),
            enforcement_notes=tuple(_get(payload, "enforcement_notes", [])),
            raw=payload,
        )

    @property
    def governs_columns(self) -> bool:
        """Whether column-level access control is actually configured."""
        return bool(self.raw.get("governance", {}).get("column_level", False))


@dataclass(frozen=True)
class Compiled:
    """The SQL a request compiles to, from a dry run."""

    compiled_sql: str
    columns: tuple[str, ...]
    dialect: str
    model_version: str
    namespace: str = ""
    #: How many facts were aggregated separately and joined. More than one means
    #: the query spanned grains or namespaces.
    parts: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Compiled":
        return cls(
            compiled_sql=_get(payload, "compiled_sql", ""),
            columns=tuple(_get(payload, "columns", [])),
            dialect=_get(payload, "dialect", ""),
            model_version=_get(payload, "model_version", ""),
            namespace=_get(payload, "namespace", ""),
            parts=int(_get(payload, "parts", 1)),
            raw=payload,
        )


@dataclass(frozen=True)
class Result:
    """Rows, plus the provenance needed to defend the numbers in them."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]
    #: The exact statement executed. Carried on every response on purpose: it is
    #: how a disagreement about a number gets settled.
    compiled_sql: str
    model_version: str
    dialect: str = ""
    namespace: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Result":
        return cls(
            columns=tuple(_get(payload, "columns", [])),
            rows=tuple(tuple(r) for r in _get(payload, "rows", [])),
            compiled_sql=_get(payload, "compiled_sql", ""),
            model_version=_get(payload, "model_version", ""),
            dialect=_get(payload, "dialect", ""),
            namespace=_get(payload, "namespace", ""),
            raw=payload,
        )

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[dict[str, Cell]]:
        """Iterate rows as dictionaries keyed by column name."""
        for row in self.rows:
            yield dict(zip(self.columns, row))

    def dicts(self) -> list[dict[str, Cell]]:
        """Every row as a dictionary keyed by column name."""
        return list(self)

    def to_dataframe(self):  # type: ignore[no-untyped-def]
        """Return a pandas DataFrame.

        pandas is an optional dependency: the client itself needs nothing beyond
        the standard library, because an SDK that drags dependencies into an
        agent's environment causes conflicts nobody asked for.

        The compiled SQL and the model version travel with the frame in
        ``df.attrs``, so provenance survives into a notebook instead of being
        dropped at the client boundary.
        """
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
            raise ModuleNotFoundError(
                "to_dataframe() needs pandas. Install it with "
                "`pip install truegrain[pandas]` or `pip install pandas`."
            ) from exc

        frame = pd.DataFrame(list(self.rows), columns=list(self.columns))
        frame.attrs["compiled_sql"] = self.compiled_sql
        frame.attrs["model_version"] = self.model_version
        frame.attrs["namespace"] = self.namespace
        return frame


@dataclass(frozen=True)
class Job:
    """One asynchronously executing query.

    Exists because a warehouse query can run longer than an agent's HTTP
    timeout. ``state`` is the field to branch on: ``running`` is the only
    non-terminal one, and ``cancelled`` is distinct from ``failed`` because the
    caller stopped it rather than the warehouse failing.

    A job is visible only to the identity that submitted it.
    """

    job_id: str
    state: str
    #: Available from the moment of submission: compilation, and therefore the
    #: governance gate, runs synchronously before the job exists.
    compiled_sql: str = ""
    columns: tuple[str, ...] = ()
    #: One page of rows. Use :meth:`Client.run` to collect every page.
    rows: tuple[tuple[Cell, ...], ...] = ()
    #: Rows in the whole result, not in this page.
    row_count: int = 0
    #: Present when more rows remain. Pass it back as ``cursor``.
    next_cursor: str = ""
    model_version: str = ""
    namespace: str = ""
    dialect: str = ""
    #: Set only when the job failed. Same vocabulary as a synchronous refusal.
    code: str = ""
    reason: str = ""
    hint: str = ""
    retry: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Job":
        return cls(
            job_id=_get(payload, "job_id", ""),
            state=_get(payload, "state", ""),
            compiled_sql=_get(payload, "compiled_sql", ""),
            columns=tuple(_get(payload, "columns", [])),
            rows=tuple(tuple(r) for r in _get(payload, "rows", [])),
            row_count=int(_get(payload, "row_count", 0)),
            next_cursor=_get(payload, "next_cursor", ""),
            model_version=_get(payload, "model_version", ""),
            namespace=_get(payload, "namespace", ""),
            dialect=_get(payload, "dialect", ""),
            code=_get(payload, "code", ""),
            reason=_get(payload, "reason", ""),
            hint=_get(payload, "hint", ""),
            retry=_get(payload, "retry", ""),
            raw=payload,
        )

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_done(self) -> bool:
        """True once the job reached any terminal state, including failure."""
        return self.state in ("succeeded", "failed", "cancelled")

    @property
    def succeeded(self) -> bool:
        return self.state == "succeeded"

    def raise_for_state(self) -> None:
        """Raise if the job did not succeed.

        A failure carries the same code, hint and retry class a synchronous
        refusal would have, so an agent branches on it identically.
        """
        from .errors import Refused, SemanticError

        if self.succeeded or self.is_running:
            return
        if self.state == "cancelled":
            raise SemanticError(f"job {self.job_id} was cancelled")
        raise Refused(
            code=self.code or "execution_failed",
            reason=self.reason,
            hint=self.hint,
            retry=self.retry or "later",  # type: ignore[arg-type]
        )

    def as_result(self, rows: tuple[tuple[Cell, ...], ...] | None = None) -> Result:
        """Render this job's rows as a :class:`Result`.

        Args:
            rows: Overrides the single page held here, which is how
                :meth:`Client.run` returns every page as one result.
        """
        collected = self.rows if rows is None else rows
        payload = dict(self.raw)
        payload["rows"] = [list(r) for r in collected]
        return Result(
            columns=self.columns,
            rows=collected,
            compiled_sql=self.compiled_sql,
            model_version=self.model_version,
            dialect=self.dialect,
            namespace=self.namespace,
            raw=payload,
        )


@dataclass(frozen=True)
class AuditEvent:
    """One decision the engine recorded.

    ``refused`` and ``denied`` are deliberately separate. Denied is access:
    this caller may not read something. Refused is correctness: nobody can be
    told this accurately, and ``hint`` names a question that can be. Counting
    them together would report every fan-out as an access incident.

    Carries no filter values, no compiled SQL and no result rows. ``sql_hash``
    identifies the statement without disclosing it.
    """

    #: RFC 3339.
    time: str
    #: One of ``allowed``, ``refused``, ``denied``, ``compiled``, ``error``.
    decision: str
    #: Who asked. ``anonymous`` on an engine without authentication.
    identity: str = "anonymous"
    #: Which refusal, when ``decision`` is ``refused``.
    code: str = ""
    #: What the caller should do about a refusal: ``modify``, ``later`` or
    #: ``never``. Empty for anything that is not a refusal.
    retry: str = ""
    reason: str = ""
    #: What would answer instead. The actionable half of a refusal.
    hint: str = ""
    #: What was asked for. Names only, never values.
    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    namespace: str = ""
    model_name: str = ""
    #: The workspace digest that answered, tying a number to a model revision.
    model_version: str = ""
    dialect: str = ""
    #: Semantic fields refused on a denial. Never returned to the denied caller.
    denied_fields: tuple[str, ...] = ()
    sql_hash: str = ""
    #: The warehouse's own identifier, so two logs can be joined.
    job_id: str = ""
    row_count: int = 0
    #: What the warehouse says the query cost. Zero means not reported rather
    #: than free: DuckDB bills nobody and reports nothing, and the two are
    #: indistinguishable here on purpose.
    bytes_billed: int = 0
    duration_ms: int = 0
    #: The failure, when ``decision`` is ``error``.
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "AuditEvent":
        retry = _get(payload, "retry", "")
        return cls(
            time=_get(payload, "time", ""),
            # An unrecognised decision reads as ``error`` rather than as an
            # empty string, so a caller branching on it cannot silently treat
            # something the engine meant as significant as nothing at all.
            decision=_get(payload, "decision", "") or "error",
            identity=_get(payload, "identity", "") or "anonymous",
            code=_get(payload, "refusal_code", ""),
            retry=retry if retry in ("modify", "later", "never") else "",
            reason=_get(payload, "reason", ""),
            hint=_get(payload, "hint", ""),
            metrics=tuple(_get(payload, "metrics", [])),
            dimensions=tuple(_get(payload, "dimensions", [])),
            namespace=_get(payload, "namespace", ""),
            model_name=_get(payload, "model_name", ""),
            model_version=_get(payload, "model_version", ""),
            dialect=_get(payload, "dialect", ""),
            denied_fields=tuple(_get(payload, "denied_fields", [])),
            sql_hash=_get(payload, "sql_hash", ""),
            job_id=_get(payload, "job_id", ""),
            row_count=int(_get(payload, "row_count", 0)),
            bytes_billed=int(_get(payload, "bytes_billed", 0)),
            duration_ms=int(_get(payload, "duration_ms", 0)),
            error=_get(payload, "error", ""),
            raw=payload,
        )

    def is_refusal(self) -> bool:
        """True when the engine declined for correctness rather than access."""
        return self.decision == "refused"

    def is_denial(self) -> bool:
        """True when the engine declined because this caller may not read something."""
        return self.decision == "denied"


@dataclass(frozen=True)
class AuditPage:
    """A window of recorded decisions, newest first.

    A window rather than the whole record: the engine holds a bounded buffer in
    memory. An engine started with an audit file keeps everything there, and
    ``note`` says so.
    """

    events: tuple[AuditEvent, ...] = ()
    #: How many are in this page, as the engine counted them.
    count: int = 0
    note: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "AuditPage":
        events = tuple(AuditEvent.parse(e) for e in _get(payload, "events", []))
        return cls(
            events=events,
            # The engine's own count, falling back to what arrived. They agree,
            # and preferring the engine's means a disagreement is visible rather
            # than papered over by recounting.
            count=int(_get(payload, "count", len(events))),
            note=_get(payload, "note", ""),
            raw=payload,
        )

    def __iter__(self) -> Iterator[AuditEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def refusals(self) -> tuple[AuditEvent, ...]:
        """Only the refusals, which is what an operator opens this to read."""
        return tuple(e for e in self.events if e.is_refusal())
