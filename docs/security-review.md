# Security review

Review of the code as of `0.0.2` against the threat model below. Findings are
ordered by expected impact. Nothing here is a report of an exploited
deployment - it is a design/code review.

> **Status note.** Findings 3 (path traversal) and part of 5 (unbounded
> responses) have since been addressed; see "Changes since this review" at the
> end, which also covers the gated write surface added afterwards. The
> remaining findings - TLS default, request timeouts, shared session,
> credential-at-rest handling - are open.

Existing safety notes live in [README.md](../README.md#safety-and-design-notes)
and [design-decisions.md](design-decisions.md); this document is the fuller
treatment and should be the place new findings land.

## Threat model

Assets: BigFix operator credentials, the root server's availability, and the
data readable through the operator's scope - including file contents on every
managed endpoint.

Adversaries considered:

1. **A confused or manipulated LLM client.** The server takes instructions from
   a model. Anything the model can be talked into calling, it will call. This
   includes indirect prompt injection: tool results (computer names, file
   contents pulled off endpoints, dashboard variables) are attacker-influenced
   text that flows straight back into the model's context.
2. **A network attacker** between the server and the root server.
3. **A local user** on the machine running the MCP server.

Explicitly *not* in scope: BigFix's own authorization model (the operator scope
is trusted to be correct), and the MCP client's own transport, since the server
runs over stdio (`fastmcp.json` -> `"transport": "stdio"`, `mcp.run()` default)
and inherits the client process's trust boundary.

## Findings

### 1. Client fast query is arbitrary read of every managed endpoint - HIGH

`client_query` / `client_query_submit`
([server.py](../src/bigfix_root_mcp/server.py),
[server.py](../src/bigfix_root_mcp/server.py)) take arbitrary client
relevance and `target_all`. Client relevance is side-effect-free, so the
"read-only" framing holds - but it can read file contents, registry values,
environment variables, and process lists on every agent, and the BigFix agent
runs as SYSTEM/root. `target_all=true` with a relevance like
`lines of files "/etc/shadow"` is a fleet-wide credential sweep expressed as
one tool call, and the results come back through the MCP response.

This is the single largest capability the server grants, and it is far beyond
what "read-only MCP server" suggests to someone wiring it up. It is currently
undocumented as a risk.

Recommendations, roughly in order of value:

- Document it prominently in the README, next to the read-only claim.
- Consider an opt-in gate for `target_all` (env flag), so the blast radius of a
  single misdirected tool call is a machine rather than the fleet.
- Note in the tool description that results are returned verbatim to the model
  and should not be used to pull secrets.
- Deployments should give the MCP server a **non-master operator** scoped to
  the smallest useful computer set. `whoami.is_main_operator` already surfaces
  which one is configured; the docs should recommend the scoped one.

Related: `session_relevance_query` can read action scripts and other server
objects that sometimes carry embedded secrets, and `get_dashboard_variable`
reads datastore variables that some deployments use for configuration secrets.
Lower impact than the fleet sweep, same category.

### 2. TLS verification defaults to off, with credentials on every request - HIGH

`BESConfig.verify` defaults to `False` and `_parse_verify("")` returns `False`
([connection.py](../src/bigfix_root_mcp/connection.py),
[connection.py](../src/bigfix_root_mcp/connection.py)), so an unset
`BES_SSL_VERIFY` means no certificate validation. besapi sets
`session.auth = (username, password)`, i.e. HTTP Basic - the operator password
is sent, base64-encoded and otherwise in the clear, on *every* request. With
verification off, any on-path attacker gets the credential and can rewrite
responses (which are then read by the model as fact).

besapi also calls `urllib3.disable_warnings()` process-wide when `verify` is
falsy, so there is no runtime signal that this is happening.

The README documents the default and says to set `BES_SSL_VERIFY=true` "for
anything beyond a lab", which is honest but weak for a credential-bearing
default. Recommendation: **flip the default to `True`** and require an explicit
`BES_SSL_VERIFY=false` for lab use; log a `logging.warning` at startup whenever
verification is disabled. Matching besapi's default is a compatibility argument,
not a security one, and this package already reimplements config loading
precisely because besapi's helper hardcodes `verify=False`.

### 3. `api_get` path guard does not stop dot-segment escape - MEDIUM

[server.py](../src/bigfix_root_mcp/server.py) rejects a path only if it
contains `://`, *starts with* `..`, or is empty. `..` anywhere else passes:

    path = "computers/../../rd/x"  ->  https://host:52311/api/computers/../../rd/x

besapi's `url()` is plain string concatenation and `requests` does not
normalize dot segments, so the escape depends on whether the root server
normalizes the path before routing. **Unverified against a live root server** -
worth testing before deciding severity. If it normalizes, `api_get` reaches
non-`/api/` handlers on port 52311, which is outside the reviewed and
documented surface even though it remains GET-only and still authenticated as
the same operator.

The same pattern applies to `get_computer_group`, which interpolates
`site_path` into the path unescaped
([server.py](../src/bigfix_root_mcp/server.py)) - a `site_path` of
`master/../../x` or one carrying a `?` alters the request shape.

Fix: reject any path whose segments include `.` or `..` (not just a prefix
check), and percent-encode `site_path` segments. Cheap, no behavior change for
legitimate input.

Redirects are followed by default. `requests` strips the `Authorization` header
on a cross-host redirect (`Session.rebuild_auth`), so a redirect off-host does
not leak the credential - but the server will still follow it and hand the body
back to the model. Consider `allow_redirects=False` if besapi grows the option.

### 4. No request timeout on tool HTTP calls - MEDIUM (availability)

besapi sets a timeout only on `login()` (`timeout=(3, 20)`); `get()` / `post()`
pass no `timeout`, so a hung or slow root server blocks a tool call
indefinitely. `MAX_TIMEOUT_SECONDS` bounds the *polling loop*, not the
individual HTTP request, so `client_query`'s timeout is not actually an upper
bound on how long the call takes.

Compounding this: `poll_client_query` is `async` but calls the synchronous
`fetch_client_query_results` directly
([clientquery.py](../src/bigfix_root_mcp/clientquery.py)), so each poll
blocks the event loop for the duration of the HTTP request. One stuck query
stalls the whole server.

Fix: pass a `timeout=` through besapi's kwargs on every call, and run the
synchronous fetch via `asyncio.to_thread`.

### 5. Unbounded response size in memory - MEDIUM (availability)

`RESTResult.__init__` eagerly reads `request.text`, so the entire response is in
memory before any limit applies. `api_get` truncates *after* the fact
(`API_GET_MAX_TEXT = 50_000`), and `session_relevance_query`,
`list_sites`, and `client_query_results` do not truncate at all - the
session-relevance docstring pushes the bound into the user's relevance
(`firsts 100 of ...`), which is a convention, not an enforcement.

A large result set is both a memory problem for the server and a context-window
problem for the client. Recommend a response-size cap with an explicit
`truncated` flag, applied uniformly rather than only in `api_get`.

### 6. Shared `requests.Session` across concurrent tool calls - LOW/MEDIUM

`connection.get_connection()` returns a module-level singleton
([connection.py](../src/bigfix_root_mcp/connection.py)) whose
`requests.Session` is shared. FastMCP dispatches synchronous tools to a worker
thread pool, so two concurrent tool calls can use the same `Session` from
different threads; `requests.Session` is not documented as thread-safe. Failure
mode is corrupted/interleaved requests rather than a privilege issue, but it is
an availability and correctness risk under a parallel-tool-calling client.

The singleton is also never invalidated on auth failure - `reset_connection()`
exists but nothing calls it outside tests, so an expired session persists until
restart.

### 7. Credential handling at rest and in logs - LOW

- `load_config()` reads `besapi.conf` without checking file permissions. A
  world-readable `~/besapi.conf` holding an operator password is a common
  footgun; a `logging.warning` when the mode is group/other-readable would be
  cheap. Confirmed gap: `.gitignore` does **not** list `besapi.conf`, and the
  config search order includes `besapi.conf` in the current working directory -
  so a developer who follows the README's advice inside the repo can commit
  their operator password. Add `besapi.conf` and `.besapi.conf` to
  `.gitignore`.
- `.mcp.json` ships `BES_PASSWORD` as a client-config env var. The README
  already recommends `~/besapi.conf` instead; keep that recommendation, since
  MCP client configs are frequently committed or synced.
- The password is held in a frozen dataclass and in `session.auth` for process
  lifetime. Unavoidable with Basic auth; noted for completeness.
- Logging hygiene is good: `bes_errors` never formats the config or connection
  object, `main()` pins logging to stderr, and besapi logs URLs but not the
  `Authorization` header. At `DEBUG`, urllib3 and besapi log full request URLs
  - including any query string passed to `api_get` - so `DEBUG` should not be
  the documented default.

### 8. Error messages return up to 500 bytes of server response - LOW

`_snippet` caps error text at 500 characters
([errors.py](../src/bigfix_root_mcp/errors.py)) and `check_rest_result`
includes the response body in the `ToolError`. The 403 `PermissionError` raised
inside besapi's `RESTResult` also embeds the full request URL. This is
deliberate and useful for debugging; it does mean BigFix error bodies reach the
model. No credential path was found into these messages. Accepted risk - record
it rather than change it.

### 9. Broad exception catching in `bes_errors` - LOW (not a security issue)

`_handled` includes `AttributeError` and `KeyError`
([errors.py](../src/bigfix_root_mcp/errors.py)), so genuine coding bugs
are reported to the model as "unexpected error" rather than surfacing. This
hides defects, including any that would otherwise reveal a security-relevant
failure. Consider narrowing, or at least `logger.exception`-ing the original
before translating.

## Things checked and found sound

- **Read-only surface.** No mutating besapi call (`put`, `delete`, `upload`,
  `create_*`, `set_dashboard_variable_value`, `export_*`) appears in the
  package; `post` is used only for `/api/clientquery`. Registration really is
  the boundary, as [design-decisions.md](design-decisions.md) claims.

  As reviewed, at `0.0.2`. Three `post`-based write tools have since been added
  behind `BIGFIX_ALLOW_WRITES`, off by default - see "Newly relevant: the write
  surface" below. Registration is still the boundary; there is simply more than
  one thing it can register now.
- **XML injection.** `build_target_xml` and `build_client_query_xml` escape
  query text, computer names, and targeting relevance with
  `xml.sax.saxutils.escape`, and computer IDs go through `int()`. This is
  stricter than besapi's own `get_target_xml`, which does not escape names.
  No XML is parsed from untrusted input by this package beyond besapi's own
  handling.
- **SSRF via `api_get`.** besapi's `url()` is string concatenation, not
  `urljoin`, so a path like `//evil.example/x` becomes
  `https://host:52311/api//evil.example/x` and stays on the root server. The
  `://` check blocks the `path.startswith(self.rootserver)` passthrough branch.
  (Dot segments are the remaining gap - finding 3.)
- **Stdout hygiene.** `main()` routes all logging to stderr, and config loading
  deliberately avoids besapi's printing helper, so the stdio transport cannot be
  corrupted into desynchronizing the client.
- **Polling limits.** `MAX_TIMEOUT_SECONDS` and `MIN_POLL_INTERVAL_SECONDS`
  ([server.py](../src/bigfix_root_mcp/server.py)) clamp the caller's
  values and prevent a tight polling loop against the root server.
- **Operator-scope honesty.** Tool descriptions and `whoami.is_main_operator`
  correctly prevent the model from reporting a scoped view as complete. This is
  a real integrity control, not just documentation.

## Open questions

- Does the BigFix root server normalize `..` in the request path before
  routing? Determines what finding 3's severity *was*; the traversal is now
  blocked regardless, so this is no longer blocking.
- Is a scoped (non-master) operator sufficient for the intended use cases? If
  yes, the docs should recommend it as the default deployment posture.

## Changes since this review

### Fixed

- **Finding 3 (path traversal).** `content.validate_path_segment` now splits a
  site path on `/`, **decodes each segment, validates, then re-encodes**, and
  rejects `.`/`..`/empty segments. Applied to every tool that interpolates a
  site path, including the pre-existing `get_computer_group`.

  Writing it surfaced a second bypass the original review missed: checking the
  raw segment lets `%2e%2e` through, because it only becomes `..` at the
  server. Validation therefore has to happen *after* decoding. There is a
  regression test per encoded form.

  A related correctness trap: site names may contain a literal `/`
  (`custom/Public%2fWindows` is a real deployment's site named
  "Public/Windows"), so naive re-encoding corrupts valid paths. The
  decode-validate-encode cycle is idempotent, which lets a path taken from a
  REST `Resource` URL be passed straight back in.

- **Finding 5 (unbounded responses), partly.** `response.py` bounds every
  tool's payload: list tools window with `limit`/`offset` and report
  `total_available`/`truncated`; blob tools report `total_chars` and drop an
  oversized payload rather than truncating it into something that looks
  complete. Still true that `RESTResult` reads the whole body into memory
  first, so this is a context-window fix, not a memory fix.

- **XML parsing.** All XML this package parses (`besxml.parse_xml`,
  `content.validate_bes_xml`) now uses a parser with `resolve_entities=False`,
  `no_network=True`, `huge_tree=False`, rather than lxml's defaults. This
  matters most for `validate_bes_xml`, which accepts XML supplied by the model.

### Newly relevant: the write surface

Three write tools now exist behind `BIGFIX_ALLOW_WRITES` (off by default):
`stop_action`, `set_dashboard_variable`, `import_bes_content`. Controls:

- The gate governs **registration**, so with it off the tools are absent from
  `list_tools()` - the same "registration is the boundary" property the
  read-only surface relies on. Tested in both directions.
- `dry_run` defaults to true; nothing is sent without an explicit
  `dry_run=false`.
- Every attempt emits a `BIGFIX WRITE` audit line (operator, target, dry-run
  flag, outcome) to stderr.
- `import_bes_content` schema-validates before any POST, in dry run too.
- `set_dashboard_variable` builds its own escaped payload rather than calling
  besapi's `set_dashboard_variable_value`, which interpolates all three fields
  into XML unescaped.

Deploying actions, every `DELETE`, operator/site creation and upload remain
unimplemented, and are documented as requiring their own design round.

### Newly relevant: qna inventory and auto-discovery

The qna tools (`qna_eval_client_relevance`, `qna_list_targets`,
`qna_discover_targets`) now read the bigfix-remote-client-relevance CLI's
default inventory (`~/.bigfix/remote_clients.toml`, then the all-users config
dir) when `BIGFIX_QNA_INVENTORY` is unset. A run naming no targets uses every
inventory host. Controls and residual exposure:

- Callers still name inventory entries only; no caller-supplied ssh hosts,
  users or become flags. Per-run `BIGFIX QNA` audit lines list every target.
- **File write:** auto-discovery writes `~/.bigfix/remote_clients.toml`. It
  runs by itself only when no inventory file exists, and otherwise only
  through `qna_discover_targets`. That tool is not registered with a
  `BIGFIX_QNA_INVENTORY` path or `BIGFIX_QNA_AUTO_DISCOVERY=0`. It is refused
  while another file (site config dir, sudo invoker's home) is in use, since
  the new file would hide it. Runs are serialized, and the audit lines are
  written before probing and on failure.
- **Egress:** discovery probes developer.bigfix.com, downloads a qna build
  into the package's cache and checks container images. It may add an
  `online_evaluator` entry, after which all-hosts runs send the caller's
  client relevance to developer.bigfix.com unless targets are named. This
  was accepted as matching the CLI; the README and tool docstrings say so.
- Never interactive: the package's SSH discovery is never called.

### Still open

Findings 1 (client query as a fleet-wide read), 2 (TLS default off with Basic
auth), 4 (no request timeouts), 6 (shared `requests.Session`), 7 (config file
permissions and `besapi.conf` missing from `.gitignore`), 8 and 9 are
unchanged. Finding 1 is now at least documented in the README rather than
implicit. Finding 4 is worth raising in priority now that writes exist: a write
with no timeout has an ambiguous outcome if it hangs.

Finding 9 stopped being hypothetical during this work - a real
`AttributeError` from besapi surfaced to the client as
`get_computer: unexpected error: 'str' object has no attribute 'copy'`, which
reads like a tool bug rather than the upstream library defect it was.
