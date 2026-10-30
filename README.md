# Decisions for Hermes

A standalone, predicate-only [Hermes Agent](https://github.com/NousResearch/hermes-agent)
plugin for the [OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions).
Submit text or an operator-scoped UTF-8 file and yes/no questions, get probabilities,
and retrieve session-owned status. One call preserves the original decoded JSON;
intentional repetitions return compact stability summaries. Background execution is the default; foreground
execution uses the same bounded transport. MIT licensed; not an official OpenAI or Nous plugin.

## Requirements and installation

- Hermes **0.22.5 or newer**, Python within Hermes's supported range (3.11–3.14).
  Integration tests exercised Hermes v0.22.5, revision
  `bf0203a794a07aac3ff764972ab9087baa4e4f4e`, with Python 3.14.7.
  Older Hermes releases and other Python versions have not been tested here.
- An API key authorized for Decisions. The public defaults are
  `https://api.openai.com/v1/decisions`, model `gpt-6-luna`, scoped secret
  `OPENAI_API_KEY`. A ChatGPT/Codex subscription sign-in is not an API key.
- Runtime dependency: `httpx>=0.23,<0.29`, declared in `plugin.yaml`.
  The HTTPX 0.23 API supports the async streaming/raw-byte client used here;
  tests used 0.28.1, compatible with Hermes's 0.28.1 core pin. Hermes's package
  manager owns dependency admission. No OpenAI SDK or other runtime package is needed.

Obtain a source checkout or downloaded source directory from this repository and
review it before installing. From your existing Hermes environment:

```sh
hermes plugins install /absolute/path/to/hermes-decisions-plugin --yes-deps
hermes plugins enable decisions
```

Installation may prepare dependencies through Hermes's package manager; `--yes-deps`
is explicit consent. Use the installer's native API-key prompt or Hermes Desktop's
plugin settings secret field to configure `OPENAI_API_KEY` for the active profile.
Do not put the key in `config.yaml`, tool arguments, or source files. Restart your
Hermes session through your normal operator procedure to pick up the enabled tools.
No catalog listing is assumed: installation by catalog name is available only after
maintainer admission. Current manifest version is 0.1.0, a pre-release candidate.

## Configuration

Merge into your existing Hermes configuration, preserving other plugins/settings:

```yaml
plugins:
  enabled: [decisions]
  entries:
    decisions:
      allow_gateway_injection: false
      settings:
        endpoint: https://api.openai.com/v1/decisions
        decision_model: gpt-6-luna
        credential_ref: OPENAI_API_KEY
        credential_endpoint: https://api.openai.com/v1/decisions
        timeout: 120
        input_roots: []  # Default deny; operator may approve narrow absolute directories.
```

Settings are read anew for each submission. `decision_model` avoids Hermes's reserved
`model` setting. `credential_ref` is a scoped secret **name**, never its value.
The manifest's `api_key` secret field maps to `OPENAI_API_KEY` through Hermes's native
credential route, not an ordinary setting. The plugin calls
`agent.secret_scope.get_secret` inside each submission's preserved context; it never
reads credential files, caches a startup key, or reads credentials directly from the
environment. Missing scoped credentials fail closed under multiplexing.

Alternate operator-approved HTTP(S) endpoints and model identifiers remain supported.
Set **both** `endpoint` and `credential_endpoint` to the exact same URL after reviewing
where the credential may be sent. Changing only the endpoint fails before secret
retrieval or network I/O. This equality check is not a network allowlist or egress
approval. URLs cannot include credentials, queries, or fragments. Use HTTPS except
on an explicitly trusted local/private transport: HTTP exposes the bearer credential
and request contents to the network. Configure an alternate `credential_ref` through
Hermes's native credential management; the default secret UI/prompt still names
`OPENAI_API_KEY` and does not dynamically provision alternate secret names.

Timeout is total wall-clock request seconds, greater than zero and at most 600
(default 120). No proxy or environment authentication is inherited; TLS verification
is on. No retries, redirects (even same-host), fallback endpoint or fallback model.

### File-input authorization

`input_path` requires an **operator-configured** `input_roots` list, read through the
public plugin-relative `ctx.get_config` API. Empty/missing roots disable file reads.
For example, after independently approving the data for the configured provider:

```yaml
# Under plugins.entries.decisions.settings:
input_roots:
  - /absolute/path/to/approved-task-inputs
```

Use narrow, trusted directories containing only inputs authorized for transmission.
**Never allow `/`, the Hermes runtime home, configuration, credential/state directories,
or other users' data.** This setting authorizes any file within its roots for callers
of this plugin; it is not an individual-file permission prompt or a substitute for
host/session filesystem isolation. No root comes from tool arguments or inferred
runtime-home/workspace access. The plugin does not modify this configuration.

Supply an absolute `input_path`. It must resolve within an approved directory after
symlink resolution; escaping symlinks/traversal, missing/unreadable files, directories
and special files are rejected. The read uses POSIX nonblocking/no-follow open flags
and checks the opened regular file. Trusted roots must not be concurrently rearranged
by adversarial filesystem writers; no hostile multi-writer filesystem sandbox is
claimed. The file is read once before admission, strictly decoded as UTF-8, and frozen
into the request bytes. No section extraction, truncation, plugin disk exports or
saved results. File size may not exceed 262144 bytes, and the existing **serialized
ASCII JSON** limit below still applies (escaping/model/questions consume space).
Shorten the input on a size error; the plugin never sends a truncated prefix.

## Tools and wire contract

Call `decisions_submit`:

```json
{"input":"The package arrived.","questions":[{"type":"predicate","instructions":"Did the package arrive?"}]}
```

The default `background:true` returns promptly:

```json
{"job_id":"…","state":"running"}
```

Call `decisions_status` with `{"job_id":"…"}` to retrieve the owned snapshot:

```json
{"job_id":"…","state":"completed","result":{"answers":[{"type":"predicate","name":null,"probability":0.95}]},"notification_accepted":false}
```

`background:false` waits and returns the status snapshot directly, for at most
`min(repetitions * timeout + 5, 240)` seconds. This stays below the released native
running-loop bridge's 300-second deadline. If still running, the response contains
an explicit foreground timeout `error` and `job_id`: poll `decisions_status`.
**The group continues; foreground wait expiry does not cancel it.** Failed single calls
have `state:"failed"` and a safe `error`, never the remote response body; groups put
safe per-attempt errors in `result.attempts`. Admission,
configuration and ownership errors return `{"error":"…"}`. A foreground wait error
may include a job ID for polling; do not resubmit automatically.

Supply exactly one of `input` or `input_path`, never both. For example:

```json
{"input_path":"/absolute/path/to/approved-task-inputs/report.txt","questions":[{"type":"predicate","instructions":"Does the report support the claim?"}],"repetitions":3}
```

`repetitions` defaults to **1** and must be a strict integer **1–20** (not boolean,
string or fractional). Each repetition is a deliberate, potentially **paid HTTP
request**, not a transport retry. Requests run sequentially within one job, using
the exact same frozen request bytes, questions, model and settings. The captured API
reference has no native simulation parameter: no repetition/simulation keys are sent.
No automatic retries, new queue or pool is introduced.

For `repetitions:1`, the existing `result` is unchanged. For more than one, `result`
contains only counts, essential attempt states/errors and per-question probabilities
in original question order, not full copies of each remote response. Example:

```json
{"job_id":"…","state":"partial","result":{"requested":3,"completed":3,"success":2,"failed":1,"not_started":0,"attempts":[{"state":"completed"},{"state":"failed","error":"Decisions HTTP 401; not retried"},{"state":"completed"}],"questions":[{"probabilities":[0.6,0.8],"mean":0.7,"min":0.6,"max":0.8}]},"notification_accepted":false}
```

`completed` counts finished attempts, including failures; `success`/`failed` split
those attempts. `not_started` excludes any in-flight attempt. Successful probabilities
are ordered by attempt; pair `attempts` with that list to identify failed positions.
A running attempt is `running`; cancellation marks it `cancelled`, with uncertain
remote outcome, not a completed failure or a not-started request. Completed results
and counts are preserved on cancellation while the in-memory record exists; unload
then drops all records. All successes => `completed`, mixed outcomes => `partial`,
all failures => `failed`; cancellation => `cancelled`. All-failed summaries have empty
probability lists and null mean/min/max—**no fabricated probability zero**. Expected
per-attempt failures do not prevent the remaining intentional calls. An unexpected
internal error instead stops the group as `failed`, preserving completed counts and
probabilities and leaving the remaining attempts `not_started`.

Repeats measure **stability, not calibration or accuracy**. Same-model outputs may
be correlated; their mean is not an independent-vote confidence guarantee. Choose
thresholds against independent labeled evidence, not repeat agreement alone.

The request body remains `{model,input,questions}`: string input and **1–64 unnamed predicate
questions**, each with string instructions. This is intentionally a subset of the
upstream API: no choice, score, images, structured input, named questions, or extra
request controls. Responses must contain one predicate answer per question in order,
with null `name` and numeric probability in [0,1]. The official
[create-decision reference](https://developers.openai.com/api/reference/resources/decisions/methods/create)
defines optional question names and nullable answer names, so unnamed predicates keep
this contract. Upstream refusals are rejected as unsupported answers, not interpreted
as probability zero or a successful partial result. Invalid/nonfinite JSON also fails.
For a single call, additional response fields are retained unchanged in `result`: decoded JSON fidelity,
not raw byte/format fidelity. Probabilities are model estimates, not guarantees;
choose and validate application thresholds separately.

A captured session key or session ID is required; tool arguments cannot choose an
origin. Jobs belong to the canonical Hermes home plus session key, or a separately
namespaced session ID when no key exists. Foreign, unknown and evicted jobs produce
the same not-found error.

## Background notices, safety and lifecycle

Completion attempts a concise `ctx.inject_message` notice at the captured origin
when the originating transport supports async delivery. Foreground calls use the
same completion path. Gateway/TUI injection requires **separate native operator
consent** (`allow_gateway_injection`) and a live injector. The default above does not
grant that consent. `notification_accepted:true` means the host accepted the call,
**not proof of chat delivery or a completed turn**. Denial/error leaves status usable.
The notice includes a job ID and state, not the input, result or credential. A host
may process an accepted notice as another agent turn. One-shot/stateless sessions
cannot be relied on to receive later notices or retain a background worker.

**Installation disclosures:**

- Network: each accepted job sends the full supplied text/questions, model identifier
  and bearer credential to the configured endpoint (OpenAI by default). Usage may be
  billable (N intentional requests for repetitions N) and is subject to that provider's terms and data policy. Nothing is sent
  until a tool submission; import/registration makes no inference request.
- Background: one context-preserving continuing asyncio loop on a daemon thread per
  loaded plugin, with native supervised task leases; no shell children or service.
- State: volatile in-memory jobs only. No persistence, scheduler, database, telemetry,
  credential storage, automatic updater or core overrides. Filesystem content reads
  occur only for explicitly supplied input_path within operator-approved input_roots.
  Hermes itself owns configuration and credential retrieval.
- Bounds: at most **4 active jobs**, foreground included, each with at most one HTTP
  request in flight (thus still at most **4 concurrent HTTP requests**), no waiting queue;
  retain the latest **32 terminal results** plus active jobs. These conservative local caps
  permit interactive overlap/recent polling, not endpoint quotas.
- Requests are limited to **262144 serialized ASCII JSON bytes**, responses to
  **1048576 raw JSON bytes**, streamed in chunks of at most 65536 bytes. Only identity
  content encoding is accepted; oversized data fails without retaining a result.
  Single-call retained response wire content is at most 32 MiB plus 4 MiB active content.
  Groups discard arbitrary response fields and keep only up to 20 probabilities per
  question, counts and essential attempt statuses/errors; they do not retain N raw results. Decoded
  Python objects, parsing and serialization add overhead: this is not an exact RSS cap.
- Unload cancels/drains requests on their owning loop, closes clients, joins the
  worker and drops results. Restart/process loss loses jobs. A timeout, transport
  failure, unload or process loss can leave the remote outcome uncertain. No durable,
  exactly-once or recovery guarantee. **Do not automatically retry/resubmit**.

### Host API compatibility and admission warnings

The plugin uses native `register_tool`, `spawn_task`, `on_unload` and `inject_message`
surfaces, plus `agent.memory_provider.spawn_context_thread`,
`agent.secret_scope.get_secret`, `hermes_constants.get_hermes_home` and
`gateway.session_context` helpers. The public
[threading contract](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin#threading-contract)
requires context-preserving threads; the
[plugin guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
documents session identity through `get_session_env` and message injection.
The released source supplies `async_delivery_supported`, `spawn_task` and `on_unload`;
not all helpers have a dedicated public documentation section. The released native
validator reports a private-core-import warning for
`worker.py:11 gateway.session_context.async_delivery_supported` and
`worker.py:11 gateway.session_context.get_session_env`, even though `get_session_env`
is documented. Its security scan reports `safe`, with no runtime core rebinds found.
Keep these warnings visible for review rather than replacing context-safe native APIs
with environment reads or hiding imports. Plugin-host subprocess isolation is not
claimed; Hermes's validator reports its own host-readiness assessment.

## Reproduce isolated tests

Use an **existing** Hermes v0.22.5 source checkout and its compatible interpreter with
`httpx`, `pytest` and `PyYAML` already available. Tested versions: Python 3.14.7,
httpx 0.28.1, pytest 9.1.1, PyYAML 6.0.3. No installation or live credentials are
needed by these tests. From this plugin source directory, set real paths:

```sh
HERMES=/absolute/path/to/hermes-agent
PYTHON=/absolute/path/to/compatible/python
SCRATCH="$PWD/outputs/test-runtime"
mkdir -p "$SCRATCH/home" "$SCRATCH/empty-plugins"
env -i PATH=/usr/bin:/bin HOME="$SCRATCH/home" HERMES_HOME="$SCRATCH/home" \
  DECISIONS_TEST_SCRATCH="$SCRATCH" HERMES_BUNDLED_PLUGINS="$SCRATCH/empty-plugins" \
  HERMES_ENABLE_PROJECT_PLUGINS=false PYTHONPATH="$HERMES" PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m pytest -q -p no:cacheprovider --confcutdir=tests \
  -o log_file="$SCRATCH/pytest.log" --basetemp="$SCRATCH/pytest" tests
```

Choose an owned disposable scratch path. `--confcutdir=tests` prevents pytest from
importing the plugin root as a test package when its checkout name contains hyphens;
it does not replace native plugin discovery. The harness discovers only its synthetic-home
copy of this plugin, disables installed plugin entrypoints and suppresses automatic
discovery while importing `model_tools`. An audit hook rejects SQLite opens,
non-loopback socket connections/binds and file writes outside scratch. It exercises
real native discovery/registry/async bridges and deterministic loopback HTTP with
synthetic scoped credentials: background liveness, JSON results, ownership, context,
consent, foreground (including bounded-wait polling fallback), failures, limits,
retention, capacity and unload; scoped UTF-8 rejection/frozen input; sequential repetitions,
partial/all failures and compact summaries. It does not
prove live OpenAI inference, installation dependency resolution, or actual chat delivery.

For catalog admission, separately run the official command in a **disposable Hermes
development environment**, after reviewing its effects:

```sh
hermes plugins validate /absolute/path/to/hermes-decisions-plugin --install-deps
```

`--install-deps` can create and select a dependency environment generation; a synthetic
`HOME` alone does not isolate Hermes's package manager. Do not run it against a managed
runtime without operator approval. A non-installing `hermes plugins validate <path>`
checks source/capabilities with existing dependencies but is **not** proof of that
exact install-dependencies gate. Read every security/private-import warning. Catalog
publication also requires a public cloneable repository and a reviewed exact SHA pin;
local tests do not establish those conditions.
