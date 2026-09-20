"""HTTP client for the semantic engine.

Deliberately dependency-free. It uses only the standard library so that
installing it into an agent's environment cannot conflict with anything already
there. pandas is optional and used only by :meth:`Result.to_dataframe`.

There is no method that sends SQL, because there is no endpoint that accepts it.
The only expressible request is a semantic one.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping

from .errors import Refused, SemanticError, TransportError, Unauthorized
from .models import (
    AuditPage,
    Compiled,
    Diagnosis,
    Diff,
    Dimension,
    DoctorHistory,
    Health,
    Job,
    Metric,
    Namespace,
    Policy,
    PolicyExplanation,
    Result,
    TestReport,
)

__all__ = ["Client"]

#: Maps each OpenAPI operationId to the method that implements it.
#:
#: tests/test_covers_spec.py asserts this covers every operation in
#: api/openapi.yaml, so an endpoint added to the engine cannot quietly go
#: unsupported here.
OPERATIONS: dict[str, str] = {
    "getHealth": "health",
    "getModelVersion": "model_version",
    "listNamespaces": "namespaces",
    "listMetrics": "metrics",
    "describeMetric": "metric",
    "listDimensions": "dimensions",
    "query": "query",
    "compile": "compile",
    "submitJob": "submit",
    "getJob": "job",
    "cancelJob": "cancel_job",
    "listAudit": "audit",
    "doctor": "doctor",
    "doctorHistory": "doctor_history",
    "runTests": "run_tests",
    "policy": "policy",
    "explainPolicy": "explain_policy",
    "diff": "diff",
    "reload": "reload",
}

DEFAULT_TIMEOUT = 60.0


class Client:
    """A connection to one semantic engine.

    Args:
        base_url: Where the engine is served, for example
            ``http://127.0.0.1:8080``.
        token: Bearer token identifying the workload. Read it from the
            environment; never hard-code one. Omit it only against a local
            engine running without authentication.
        timeout: Seconds to wait for a response.
        user_agent: Overrides the identifying header, which is useful when you
            want the audit log to distinguish one agent from another.

    Example:
        >>> from truegrain import Client, filters
        >>> c = Client("http://127.0.0.1:8080", token=os.environ["SEMANTIC_TOKEN"])
        >>> result = c.query(
        ...     metrics=["sales.order_revenue"],
        ...     dimensions=["sales.customers.region"],
        ...     filters=[filters.eq("sales.orders.status", "shipped")],
        ... )
        >>> result.to_dataframe()
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = "truegrain-python/1.0",
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.user_agent = user_agent

    @classmethod
    def from_env(
        cls,
        url_var: str = "SEMANTIC_URL",
        token_var: str = "SEMANTIC_TOKEN",
        **kwargs: Any,
    ) -> "Client":
        """Build a client from environment variables.

        Keeps the token out of source and out of the process list, which is
        where a token passed as a command-line flag ends up.
        """
        url = os.environ.get(url_var)
        if not url:
            raise SemanticError(f"{url_var} is not set")
        return cls(url, token=os.environ.get(token_var), **kwargs)

    # ---------- metadata ----------

    def health(self) -> Health:
        """What this deployment enforces, including what it does not.

        Worth reading before trusting the layer with anything sensitive:
        ``health().enforcement_notes`` states the gaps in plain language.
        """
        return Health.parse(self._get("/v1/health"))

    def model_version(self) -> str:
        """The workspace digest currently served.

        Every query result carries this too. Two results with the same digest
        were produced by exactly the same definitions.
        """
        return str(self._get("/v1/model/version").get("model_version", ""))

    def namespaces(self) -> list[Namespace]:
        """The namespaces in the workspace, their owners and availability."""
        payload = self._get("/v1/namespaces")
        return [Namespace.parse(n) for n in payload.get("namespaces", [])]

    def metrics(self, search: str | None = None) -> list[Metric]:
        """Every metric this identity may read.

        Args:
            search: Case-insensitive substring over name and description.
        """
        params = {"search": search} if search else None
        payload = self._get("/v1/metrics", params)
        return [Metric.parse(m) for m in payload.get("metrics", [])]

    def metric(self, name: str) -> Metric:
        """One metric's definition and the dimensions legal for it.

        Args:
            name: Qualified as ``namespace.metric``, or bare when unambiguous
                across the workspace.
        """
        if not name:
            raise ValueError("metric name is required")
        return Metric.parse(self._get(f"/v1/metrics/{urllib.parse.quote(name, safe='')}"))

    def dimensions(self, metric: str | None = None) -> list[Dimension]:
        """Dimensions available for grouping and filtering.

        Args:
            metric: Restrict to dimensions valid for this metric, which is
                almost always what you want.
        """
        params = {"metric": metric} if metric else None
        payload = self._get("/v1/dimensions", params)
        return [Dimension.parse(d) for d in payload.get("dimensions", [])]

    # ---------- query ----------

    def query(
        self,
        metrics: Iterable[str],
        dimensions: Iterable[str] | None = None,
        filters: Iterable[Mapping[str, Any]] | None = None,
        grain: str | None = None,
        limit: int | None = None,
        order_by: Iterable[Mapping[str, Any]] | None = None,
    ) -> Result:
        """Answer a question and return rows.

        Args:
            metrics: Metric names. Metrics at different grains, or in different
                namespaces, are aggregated separately and joined on the shared
                dimensions.
            dimensions: ``dataset.field`` or ``namespace.dataset.field``.
            filters: Structured filters. Use :mod:`truegrain.filters` to
                build them.
            grain: Time bucket for the selected time dimension.
            limit: Maximum rows. Defaults to the server's limit.
            order_by: ``[{"field": "order_revenue", "desc": True}]``.

        Raises:
            Refused: The engine declined. Check ``err.retry`` before retrying.
        """
        return Result.parse(
            self._post("/v1/query", self._request_body(metrics, dimensions, filters, grain, limit, order_by))
        )

    def compile(
        self,
        metrics: Iterable[str],
        dimensions: Iterable[str] | None = None,
        filters: Iterable[Mapping[str, Any]] | None = None,
        grain: str | None = None,
        limit: int | None = None,
        order_by: Iterable[Mapping[str, Any]] | None = None,
    ) -> Compiled:
        """Return the SQL a request compiles to, without running it.

        A dry run is still governed: a request you may not run returns a refusal
        and no SQL, and the inspection is audited. It is a way to see what a
        query would do, not a way around the gate.
        """
        return Compiled.parse(
            self._post("/v1/compile", self._request_body(metrics, dimensions, filters, grain, limit, order_by))
        )

    # ---------- asynchronous execution ----------

    def run(
        self,
        metrics: Iterable[str],
        dimensions: Iterable[str] | None = None,
        filters: Iterable[Mapping[str, Any]] | None = None,
        grain: str | None = None,
        limit: int | None = None,
        order_by: Iterable[Mapping[str, Any]] | None = None,
        max_wait: float = 900.0,
        page_size: int | None = None,
    ) -> Result:
        """Run a query that may take longer than an HTTP request should.

        Submits the query, polls until it finishes, collects every page, and
        returns one :class:`Result`. Use this instead of :meth:`query` whenever
        the warehouse might be slow: a large BigQuery scan routinely outlives
        the default timeout of an agent framework, and a synchronous call that
        times out leaves the query running and billable with nobody reading it.

        The governance gate runs during submission, so a request this identity
        may not make raises :class:`Refused` immediately rather than after a
        wait.

        Args:
            max_wait: Seconds to wait before giving up. On expiry the job is
                cancelled rather than left running, so an abandoned wait does
                not keep spending warehouse time.
            page_size: Rows per page while collecting. The default suits most
                results; lower it when rows are wide.

        Raises:
            Refused: The engine declined, or the query failed. Check
                ``err.retry`` before retrying.
            SemanticError: The wait expired, or the job was cancelled.
        """
        job = self.submit(metrics, dimensions, filters, grain, limit, order_by)
        job = self.wait(job.job_id, max_wait=max_wait, page_size=page_size)
        job.raise_for_state()

        # Walk the remaining pages. row_count is the size of the whole result,
        # so it bounds the walk without the loop having to trust the cursor to
        # eventually come back empty.
        rows = list(job.rows)
        cursor = job.next_cursor
        total = job.row_count or None
        while cursor and (total is None or len(rows) < total):
            page = self.job(job.job_id, cursor=cursor, page_size=page_size)
            if not page.rows:
                break
            rows.extend(page.rows)
            cursor = page.next_cursor
        return job.as_result(tuple(rows))

    def submit(
        self,
        metrics: Iterable[str],
        dimensions: Iterable[str] | None = None,
        filters: Iterable[Mapping[str, Any]] | None = None,
        grain: str | None = None,
        limit: int | None = None,
        order_by: Iterable[Mapping[str, Any]] | None = None,
    ) -> Job:
        """Start a query in the background and return immediately.

        The returned job already carries ``compiled_sql``, because compilation
        happened synchronously. A job coming back at all means the query was
        authorized, not merely accepted.

        Prefer :meth:`run` unless you genuinely need to do something else while
        the query runs.
        """
        return Job.parse(
            self._post(
                "/v1/jobs",
                self._request_body(metrics, dimensions, filters, grain, limit, order_by),
            )
        )

    def job(self, job_id: str, cursor: str | None = None, page_size: int | None = None) -> Job:
        """Poll a job and read one page of its rows.

        Args:
            cursor: From a previous page's ``next_cursor``. Omit for the first.
            page_size: Rows in this page.
        """
        if not job_id:
            raise ValueError("job_id is required")
        params: dict[str, str] = {}
        if cursor:
            params["cursor"] = cursor
        if page_size is not None:
            params["page_size"] = str(page_size)
        return Job.parse(
            self._get(f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}", params or None)
        )

    def cancel_job(self, job_id: str) -> Job:
        """Stop a running query. Idempotent."""
        if not job_id:
            raise ValueError("job_id is required")
        return Job.parse(
            self._delete(f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}")
        )

    def wait(
        self,
        job_id: str,
        max_wait: float = 900.0,
        page_size: int | None = None,
    ) -> Job:
        """Poll until a job reaches a terminal state.

        Backs off from a fast first poll to a two second ceiling, so a query
        that finishes quickly is not delayed and a slow one is not hammered.

        On expiry the job is cancelled before raising, so giving up here also
        stops the warehouse work.
        """
        deadline = time.monotonic() + max_wait
        interval = 0.25
        while True:
            job = self.job(job_id, page_size=page_size)
            if job.is_done:
                return job
            if time.monotonic() >= deadline:
                # Leaving it running would keep spending warehouse time for an
                # answer this caller has already stopped waiting for.
                try:
                    self.cancel_job(job_id)
                except SemanticError:
                    pass
                raise SemanticError(
                    f"job {job_id} did not finish within {max_wait:g}s and was cancelled"
                )
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 1.5, 2.0)

    # ---------- audit ----------

    def audit(self, limit: int | None = None, decision: str | None = None) -> AuditPage:
        """Read the decisions the engine recently made, newest first.

        Not served unless an operator started the engine with ``-audit-readers``
        naming who may read it, because the record carries every caller's
        activity. An engine with nobody named answers 404 and a caller who is
        authenticated but not named gets 403, both as :class:`Refused`. Handle
        both: this is one of the few calls that can fail for reasons that have
        nothing to do with the request.

        It is a bounded window rather than the whole record. An engine writing
        an audit file keeps everything there.

        Args:
            limit: Most recent decisions to return, up to 200.
            decision: Return only this decision. ``refused`` is the useful one.
        """
        params: dict[str, str] = {}
        if limit is not None:
            params["limit"] = str(limit)
        if decision:
            params["decision"] = decision
        return AuditPage.parse(self._get("/v1/audit", params))

    # ---------- what the engine says about itself ----------
    #
    # Everything here is metadata. None of it reads a row, which is why all but
    # reload need only read:model, and why a pipeline that checks a model does
    # not also need a credential that can read the warehouse.
    #
    # Four of them answer 404 rather than an empty success, and the difference
    # matters enough to say once: an engine with no test suite is not an engine
    # whose tests pass, an engine that has never reloaded is not an engine in
    # which nothing changed, and an engine checking nothing on a timer has no
    # history rather than a clean one. Each arrives as :class:`Refused`.

    def doctor(self) -> Diagnosis:
        """Ask the warehouse whether the model is still true.

        Everything else validates the model against itself: the YAML parses,
        the joins resolve, the expressions compile. None of that notices that
        somebody dropped a column last Tuesday, and the first sign of that is
        usually a caller getting an error, which is the most expensive place to
        find it.

        Reports rather than refuses, so an exception here means the check could
        not run. Read :attr:`Diagnosis.ok` for the verdict and
        :attr:`Diagnosis.skipped` for the case where nothing could be checked.
        """
        return Diagnosis.parse(self._get("/v1/doctor"))

    def doctor_history(self) -> DoctorHistory:
        """What the scheduled warehouse check has seen, oldest first.

        Drift is found by looking regularly, not by looking once, which is how
        "when did this start" stays answerable. An engine started without
        ``-doctor-every`` has nothing scheduled and answers 404 as
        :class:`Refused`.
        """
        return DoctorHistory.parse(self._get("/v1/doctor/history"))

    def run_tests(self) -> TestReport:
        """Assert what this model answers.

        ``validate`` says the model holds together and :meth:`diff` says a
        number changed. Neither says a number was ever right.

        Check :attr:`TestReport.withheld` as well as ``ok``. A credential
        without ``run:query`` cannot cause warehouse execution, so cases that
        would are withheld and counted rather than run or silently dropped, and
        a caller reading only ``ok`` would conclude a suite passed when half of
        it never ran.
        """
        return TestReport.parse(self._post("/v1/tests", {}))

    def policy(self) -> Policy:
        """What this engine enforces, and what it does not.

        Says nothing about who is allowed what. For that, and only about
        yourself, use :meth:`explain_policy`.
        """
        return Policy.parse(self._get("/v1/policy"))

    def explain_policy(self, metric: str) -> PolicyExplanation:
        """What you may read of a metric, and why.

        Answers "why can I not group by that column" without running a query
        and being denied. For the calling identity only, which is a security
        property rather than a limitation: an endpoint that reported another
        identity's access would publish the policy it was configured to
        enforce.

        Args:
            metric: The qualified name, as :meth:`metrics` reports it.
        """
        if not metric:
            # Refused here rather than sent, so a caller who forgot the
            # argument reads that instead of a 400 naming a field they did not
            # write.
            raise ValueError("name the metric to explain")
        return PolicyExplanation.parse(self._post("/v1/policy/explain", {"metric": metric}))

    def diff(self) -> Diff:
        """Whether the last model reload moved a number.

        This compares the model being served against the one served before it,
        which is the comparison nobody can make from outside the process. An
        engine that has served only one model answers 404 as :class:`Refused`,
        because that is a different answer from nothing having changed and only
        one of them is reassuring.
        """
        return Diff.parse(self._get("/v1/diff"))

    def reload(self) -> str:
        """Tell the engine to re-read its model source.

        Returns ``reading`` or ``already running``. Both mean the caller got
        what they asked for; treating the second as a failure would retry a
        sync that is already under way.

        It carries no model, deliberately: this means "look now", not "install
        this". The engine already follows git, so the only thing this changes
        is the wait. A method that accepted a model would be a second way into
        production, one that skips the pull request, the checks and the diff
        that reports which numbers move.

        Needs the ``deploy:model`` scope, which ``read:model`` and
        ``run:query`` never imply.

        Returning does not mean the new model is serving. A sync is not
        instant, and reporting a commit before the swap happened would be a
        claim a pipeline then asserts as fact. Poll :meth:`health` and read
        ``origin.commit`` to know when the new model is the one answering.

        An engine reading from a path has nothing to re-read and answers 404. A
        model that fails to load is not a failure of this call either: the
        engine keeps serving the previous one and says so in a 502. Both are
        :class:`Refused`.
        """
        payload = self._post("/v1/reload", {})
        return str(payload.get("status", ""))

    # ---------- internals ----------

    @staticmethod
    def _request_body(
        metrics: Iterable[str],
        dimensions: Iterable[str] | None,
        filters: Iterable[Mapping[str, Any]] | None,
        grain: str | None,
        limit: int | None,
        order_by: Iterable[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        names = list(metrics)
        if not names:
            raise ValueError("at least one metric is required")
        body: dict[str, Any] = {"metrics": names}
        # Omitted rather than sent empty: the server rejects unknown and
        # malformed fields, and an empty list is not the same as absent.
        if dimensions:
            body["dimensions"] = list(dimensions)
        if filters:
            body["filters"] = [dict(f) for f in filters]
        if grain:
            body["grain"] = grain
        if limit is not None:
            body["limit"] = limit
        if order_by:
            body["order_by"] = [dict(o) for o in order_by]
        return body

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
        return self._send(urllib.request.Request(url, headers=self._headers(), method="GET"))

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=payload, headers=headers, method="POST"
        )
        return self._send(request)

    def _delete(self, path: str) -> dict[str, Any]:
        return self._send(
            urllib.request.Request(self.base_url + path, headers=self._headers(), method="DELETE")
        )

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return self._decode(response.read(), response.status)
        except urllib.error.HTTPError as exc:
            # A refusal arrives as an error status with a JSON body. It is an
            # answer, not a transport failure, so it is raised as Refused with
            # the code and retry class intact.
            raw = exc.read()
            if exc.code == 401:
                raise Unauthorized(self._message(raw, "credentials were not accepted")) from None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise TransportError(
                    f"HTTP {exc.code} from {request.full_url}: {self._message(raw, exc.reason)}"
                ) from None
            if isinstance(payload, dict) and "code" in payload:
                raise Refused.from_payload(payload, exc.code) from None
            raise TransportError(f"HTTP {exc.code} from {request.full_url}: {payload}") from None
        except urllib.error.URLError as exc:
            raise TransportError(f"cannot reach {request.full_url}: {exc.reason}") from None
        except TimeoutError:
            raise TransportError(
                f"{request.full_url} did not respond within {self.timeout:g}s"
            ) from None

    @staticmethod
    def _decode(raw: bytes, status: int) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError(f"HTTP {status} body was not JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise TransportError(f"HTTP {status} body was not a JSON object")
        return payload

    @staticmethod
    def _message(raw: bytes, fallback: Any) -> str:
        text = raw.decode("utf-8", errors="replace").strip()
        return text or str(fallback)

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r}, authenticated={bool(self._token)})"
