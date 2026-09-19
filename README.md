# truegrain (Python)

The Python client for [truegrain](https://github.com/rk-chavali/truegrain):
governed metrics for agents, notebooks and applications.

There is no method that sends SQL, because there is no endpoint that accepts it.
The only expressible request is a semantic one.

```bash
pip install truegrain            # no dependencies
pip install truegrain[pandas]    # adds to_dataframe()
```

The client uses only the standard library. That is deliberate: this package goes
into agent environments where a dependency conflict is a real cost, and thirteen
endpoints do not justify dragging one in.

## Use it

```python
import os
from truegrain import Client, filters

client = Client.from_env()          # SEMANTIC_URL, SEMANTIC_TOKEN

for metric in client.metrics(search="revenue"):
    print(metric.name, "-", metric.description)

result = client.query(
    metrics=["sales.order_revenue", "marketing.campaign_spend"],
    dimensions=["sales.orders.order_date"],
    grain="month",
    filters=[filters.in_("sales.orders.status", "shipped", "delivered")],
    order_by=[{"field": "order_date"}],
)

result.to_dataframe()
```

Those two metrics live in different namespaces and aggregate different facts.
The engine computes each separately and joins them on the shared month, so both
numbers are correct rather than one being inflated by the join.

Every result carries the SQL that produced it and the exact version of the
definitions used:

```python
frame = result.to_dataframe()
frame.attrs["compiled_sql"]
frame.attrs["model_version"]      # sha256:eebdad0d19bb
```

That is not a debug feature. It is how a disagreement about a number gets
settled, which is why it survives into the frame rather than being dropped at
the client boundary.

## Refusals are answers

The engine declines questions it cannot answer correctly, and says what to do
instead. Catch `Refused` and branch on the retry class rather than on the text.

```python
from truegrain import Refused

try:
    client.query(metrics=["sales.order_revenue"],
                 dimensions=["sales.products.category"])
except Refused as refusal:
    if refusal.should_modify():
        print("try instead:", refusal.hint)
    elif refusal.should_wait():
        ...        # the same request may work shortly
    elif refusal.is_final():
        ...        # stop; say so rather than substituting another metric
```

That example refuses because an order contains several products, so revenue per
category has no answer without an allocation rule. The hint names the metrics
defined at the grain where the question *is* well defined:

```
fan_out_would_inflate: metric "order_revenue" aggregates SUM(...) over orders,
but this query repeats orders rows
  hint: ... Metrics defined at the order_lines grain answer this correctly:
        line_revenue, units_sold.
  retry: modify
```

The three classes matter most to an agent. Without them it either gives up on a
typo or loops forever on a denial.

| `retry` | Meaning |
|---|---|
| `modify` | Answerable, but not as written. Change the arguments. |
| `later` | Nothing is wrong with the request. Something outside it failed. |
| `never` | No version of this will succeed for you. Stop. |

## Wiring into an agent framework

Most frameworks accept a list of function schemas and call back with a name and
arguments. `tools` produces that list and dispatches those calls, which covers
LangChain, CrewAI, the OpenAI Agents SDK, Pydantic AI and anything hand-rolled
without this library growing an adapter per framework.

```python
from truegrain import Client, tools

client = Client.from_env()
specs = tools.tool_specs()          # hand these to the framework

# when the model calls back:
result = tools.dispatch(client, name, arguments)
```

`dispatch` returns refusals rather than raising them, because a framework
feeding the result back to a model wants the text and the retry class, not a
traceback.

If your client speaks the Model Context Protocol, skip this and point it at the
engine's MCP server instead. Same four tools, same guarantees, no code.

```bash
claude mcp add truegrain -- /path/to/truegrain serve mcp
```

## Dry runs

`compile` returns the SQL a request would produce without running it, which is
useful before committing an agent to an expensive query.

```python
compiled = client.compile(metrics=["sales.order_revenue"],
                          dimensions=["sales.customers.region"])
print(compiled.compiled_sql)
print(compiled.parts)      # >1 means facts were aggregated separately and joined
```

A dry run is still governed. A request you may not run returns a refusal and no
SQL, and the inspection is recorded in the audit log as `compiled` rather than
`allowed`. It shows you what a query would do; it is not a way around the gate.

## Know what you are trusting

```python
health = client.health()
health.governs_columns          # is column-level access control configured at all
health.enforcement_notes        # plain-language list of what this deployment does NOT enforce
```

A deployment that enforces nothing says so here. Overstating a governance
guarantee is worse than not offering one.

## Reading the decisions

`audit` returns the decisions the engine recently made, newest first. It is
the only place a refusal survives the request that caused it, which makes it
the answer to "why did that agent give up".

```python
for refusal in client.audit(decision="refused").refusals():
    print(refusal.time, refusal.code, "->", refusal.hint)
```

`refused` and `denied` stay separate. Denied is access, meaning this caller may
not read something. Refused is correctness, meaning nobody can be told this
accurately and `hint` names a question that can be. Counting them together
would report every fan-out as a security incident.

This call fails for reasons that have nothing to do with the request, so handle
both: an engine started without `-audit-readers` answers 404, because the
record carries every caller's activity and the capability is absent rather than
withheld, and a caller who is authenticated but not named gets 403. Both arrive
as a final `Refused`.

It is a bounded window rather than the whole record. An engine writing an audit
file keeps everything there.

## Errors

| Exception | When |
|---|---|
| `Refused` | The engine understood and declined. Carries `code`, `reason`, `hint`, `retry`. |
| `Unauthorized` | Credentials missing or not recognised. |
| `TransportError` | The engine could not be reached, or answered with something unparseable. Never a statement about the request. |
| `SemanticError` | Base class for all of the above. |

## Development

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_covers_spec.py` checks this client against the vendored contract;
see below for what that guarantees.

## How this stays in step with the engine

The engine's OpenAPI contract is vendored at `spec/openapi.yaml`, pinned to an
engine commit in `spec/PINNED_AT`.

`tests/test_covers_spec.py` checks this client against it in both directions: an
operation the engine exposes with no method here fails the build, and a method
claiming an operation the spec does not define fails it too. A scheduled job
compares the pin against the engine daily and opens a pull request when the
contract moves, so a change arrives as a reviewable diff with the contract tests
already run against it.

The client is hand-written rather than generated. A generator produces a
transport and a flat error type; the parts worth having are the ones it cannot
produce: `Refused.should_modify()`, and `run()` submitting a job and polling it
so no caller writes that loop.

## Licence

Apache 2.0, matching the engine.
