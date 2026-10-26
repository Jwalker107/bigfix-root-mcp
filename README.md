# bigfix-root-mcp

A minimal, read-only MCP server around the [besapi](https://github.com/jgstew/besapi)
library, exposing the HCL BigFix root server REST API to MCP clients. Built on
[FastMCP](https://gofastmcp.com) 4 (stateless). Session relevance is evaluated
with [bigfix-remote-session-relevance](https://github.com/jgstew/bigfix-remote-session-relevance)
(see [Session relevance](#session-relevance)); every other REST call goes
through besapi.

Focus: **session relevance** queries (data the server already has) and
**client fast query** (live questions answered by BigFix agents), plus a few
read-only helpers.

Strongly recommend using the official BigFix Platform MCP server instead: https://help.hcl-software.com/bigfix/11.0/platform/Platform/MCP/c_introduction.html

The capabilities of this MCP server are intentionally limited, where as the official one is not.

## Tools

| Tool | Purpose |
| --- | --- |
| `session_relevance_query` | Evaluate session relevance on the root server; returns the JSON envelope (`result`, `count`, `plural`, `type`, `evaltime_ms`). |
| `analyze_relevance` | Statically analyze a relevance expression offline - parse, types, dialect, unknown inspectors with suggestions, complexity. Never evaluates it. |
| `search_inspectors` | Fuzzy-search the offline inspector reference by name or phrase. |
| `search_relevance_docs` | Search a local, offline index of relevance documentation and worked examples (needs `BIGFIX_KNOWLEDGE_INDEX`; answers `available: false` with how to enable it when unset). Never contacts BigFix. |
| `search_reference_fixlets` | Search a local index of your own existing fixlets and analyses (built from a directory or a git repository: relevance, analysis properties and action script templates), with the habits counted across them (needs `BIGFIX_FIXLET_INDEX`; answers `available: false` when unset; how to build it: [docs/reference-fixlets.md](docs/reference-fixlets.md)). Company content: read-only, bounded, and every result names its source and when it was read. Never contacts BigFix. |
| `client_query_submit` | Submit a client fast query, return its `query_id` immediately. |
| `client_query_results` | Fetch current (cumulative) results for a query ID; safe to call repeatedly. |
| `client_query` | Submit + poll in one call with progress notifications; stops on expected count reached, results stable, or timeout. |
| `get_server_info` | Root server version info (`/api/serverinfo`). |
| `list_sites` | Sites visible to the configured operator. |
| `get_computer_group` | Look up a group by name - requires an explicit `site_path`. |
| `get_operator` | Look up a console operator by name. |
| `get_dashboard_variable` | Read a dashboard datastore variable. |
| `whoami` | Configured user/root server, main-operator status, write-gate state; connectivity smoke test. |
| `api_get` | Read-only escape hatch: GET any `/api/` path (try `help` for discovery). |
| `get_computer` | One computer's full record (`/api/computer/{id}`). |
| `find_computers` | Find computers by case-insensitive name substring. |
| `applicable_fixlets` | Content currently relevant to one computer. |
| `get_action` / `get_action_status` | An action's definition, and its per-computer execution state. |
| `list_actions` | Actions visible to the configured operator. |
| `find_content` | Search fixlets/tasks/analyses/baselines by name across sites; resolves the `site_path` needed by `get_content`. |
| `get_content` | One fixlet, task, analysis or baseline by site path and ID. |
| `list_operators` / `list_roles` | Console operators and roles (master operator only). |
| `validate_bes_xml` | Validate BES XML against the BigFix schemas. No server call. |

### Relevance analysis (advisory)

The relevance-taking tools statically analyze their relevance with
[bigfix-relevance-analyzer](https://github.com/jgstew/bigfix-relevance-analyzer)
before sending it (dialect pinned per tool). The analysis is **advisory
only**: even a query the analyzer rejects still goes to the server, because
the analyzer's inspector table is a snapshot - an unknown inspector there may
still exist on the live server. When there are findings, the response carries
an `analysis` key (findings, suggestions, dialect mismatch); a clean query
adds nothing. Server-side relevance errors gain a `Static analysis:` appendix
with the findings and did-you-mean suggestions, so a typo'd inspector is a
one-round-trip fix.

### qna fast evaluation

Registered by default; absent when every target source is disabled - see
[qna evaluation](#qna-evaluation).

| Tool | Purpose |
| --- | --- |
| `qna_eval_client_relevance` | Evaluate client relevance with the `qna` binary on containers or inventory hosts (every inventory host when none is named) - about a second per warm run. Never touches the BigFix deployment. |
| `qna_list_targets` | List the inventory targets, which inventory file is in use, whether auto-discovery may run, and whether container images are allowed. |
| `qna_discover_targets` | Find qna targets that work on the server's host and add them to `~/.bigfix/remote_clients.toml`, like the CLI's `--auto-discovery`. Registered only with the default inventory. |

### Write tools (opt-in)

Absent unless `BIGFIX_ALLOW_WRITES` is set - see [Writes](#writes).

| Tool | Purpose |
| --- | --- |
| `stop_action` | Stop an in-flight action (`POST /api/action/{id}/stop`). |
| `set_dashboard_variable` | Set a dashboard datastore variable. |
| `import_bes_content` | Create/update custom content in a site. Does **not** deploy it. |

### Result bounding

Every tool that can return an unbounded payload is windowed and says so.
List-shaped tools take `limit`/`offset` and report `returned`,
`total_available` and `truncated`; blob-shaped tools report `truncated` and
`total_chars`, and drop an oversized payload rather than cut it into something
that looks complete.

This is not optional politeness: BigFix relevance has **no row-limiting
operator** (`first`, `firsts`, `items`, `elements` are all undefined), so
bounding the response is the only way to bound a result. `find_content` on the
reference deployment matches 12,395 fixlets.

## Resources and prompts

Relevance is the hard part, so the server ships reference material clients can
pull on demand rather than repeating it in every tool description:

| Resource | Contents |
| --- | --- |
| `bigfix://relevance/session-cookbook` | Session relevance that works - every expression verified against a live root server - plus the operators that don't exist. |
| `bigfix://relevance/client-cookbook` | Client (fast query) relevance, targeting forms, reading cumulative results. |
| `bigfix://relevance/authoring-guide` | Rules that prevent most relevance errors, the common error messages with their usual causes, and how applicability clauses combine. Every example is checked with the analyzer. |
| `bigfix://relevance/client-idioms` | Documented client-relevance patterns for files, registry, applications, services, OS, versions, text and WMI. Every example is checked with the analyzer. |
| `bigfix://guide/tools` | Which tool answers which question, how to read bounded responses, what operator scope means. |
| `bigfix://relevance/reference/dialects` | Client versus session relevance: where each is evaluated, which one a tool expects. |
| `bigfix://relevance/reference/client-relevance` | Client relevance language reference (from bigfix-relevance-analyzer). |
| `bigfix://relevance/reference/session-relevance` | Session relevance language reference (from bigfix-relevance-analyzer). |

Prompts: `diagnose_computer`, `patch_status`, `find_stale_agents`,
`troubleshoot_relevance`, `write_relevance`, `write_actionscript`, `write_fixlet`.

`write_relevance(goal, dialect)` walks a model through writing relevance: look it
up with `search_relevance_docs` (and `search_reference_fixlets`, for similar existing
content), check names with `search_inspectors`, draft,
`analyze_relevance`, evaluate with a bounded query, and report only what was
verified. If `BIGFIX_RELEVANCE_CONVENTIONS` names a markdown file, its text is
added as house conventions (how your team treats proxy agents, guard idioms, and
so on); a starting point is in
[docs/relevance-conventions.example.md](docs/relevance-conventions.example.md).

`write_actionscript(goal, platform)` does the same for an action script: look up the
commands in the indexed action script documentation, find a similar script as a template
with `search_reference_fixlets` (`role='actionscript'`), draft, check every `{...}`
relevance substitution with `analyze_relevance`, and report what was checked. A script
can be checked but not run, so it never says one works, never invents a download URL or
checksum, and never puts a credential in a script. `write_fixlet(goal, platform)` joins
the two: applicability relevance and an action script that agree with each other,
assembled as BES XML and checked offline with `validate_bes_xml`. It does not import or
deploy anything.

Relevance errors also carry a cause hint: the server recognizes the common
failure shapes (a non-existent limiting operator, client relevance in a
session query, singular-vs-plural) and appends what to do instead, so a bad
expression is a retry rather than a dead end.

### Client fast query semantics

Client queries are answered by live agents: results accumulate at
`/api/clientqueryresults/{id}` over seconds to minutes as clients report in,
and there is **no completion flag**. The `client_query` tool polls with three
termination heuristics (reported in `stop_reason`):

1. `expected_count_reached` - as many distinct computers reported as targeted;
2. `results_stable` - no new computers for `stable_polls` consecutive polls;
3. `timeout` - partial results at timeout are a *normal* outcome (offline
   agents never report), not an error.

For long waits, use `client_query_submit` then `client_query_results`
repeatedly instead of a single blocking call.

## The knowledge base: adding sources and regenerating the indexes

The server can search two local indexes. Both are optional, offline and read-only, and
both are built by tools in this repository, not downloaded.

| Index | Built from | Tool that reads it | Variable | File |
| --- | --- | --- | --- | --- |
| **Documentation** | Web pages, PDFs and git repositories of public documentation | `search_relevance_docs` | `BIGFIX_KNOWLEDGE_INDEX` | `corpus/index/relevance.sqlite` |
| **Reference fixlets** | `.bes` files in a directory or a git repository: your own existing content | `search_reference_fixlets` | `BIGFIX_FIXLET_INDEX` | `corpus/index/fixlets.sqlite` |

Everything the tools download, convert or build lives under `corpus/` and is ignored by git
(see [corpus/README.md](corpus/README.md)). Public availability is not permission to
redistribute: only add sources you are cleared to use, and note that the reference
fixlet index holds your own company content, so never commit, package or share it.

Run every command from the repository root. `uv sync` installs the development group,
which includes `pypdf`, needed only to read PDFs. Use `uv run python -m ...`, or plain
`python -m ...` with the project environment active. Every step has a `--dry-run` that
changes nothing: use it first.

### Add to the documentation index

| To add | Edit | Then run |
| --- | --- | --- |
| A web page (a URL) | a row in `corpus/MANIFEST.csv` | `fetch`, then `build` |
| A PDF (a URL) | a row in `corpus/MANIFEST.csv` | `fetch`, then `build` |
| A git repository of documentation | a row in `corpus/GIT_SOURCES.csv` | `gitsource`, then `build` |

**A URL (web page or PDF).** Add one row to `corpus/MANIFEST.csv`. The columns are
`id, path, source_url, title, retrieved_on, license, bigfix_version, dialect, sha256, notes`:

```
my-guide,,https://example.com/docs/my-guide.pdf,My Guide,,Public; use covered by sign-off,11.0,client,,added 2026-10-24
```

- `id` is a short name (letters, digits, `.`, `_`, `-`). `dialect` is `session`, `client`
  or `both`; blank means unknown. Leave `path`, `retrieved_on` and `sha256` blank: the
  fetch step fills them in and records the file's SHA-256, so a later change upstream
  shows as a manifest diff.
- Supported types are `.pdf`, `.html`/`.htm` and `.md`/`.markdown`/`.txt`. The stored file
  name takes its extension from the URL. A page whose URL has no `.html` extension would be
  stored as `.bin` and skipped, so put `my-page.html` in the `path` column yourself.
- Fetching honours `robots.txt`, waits between requests to one host and caps response
  size. It only fetches `http` and `https`.

```
python -m tools.ingest.fetch --dry-run     # download and report, write nothing
python -m tools.ingest.fetch               # fetch, record hashes in MANIFEST.csv
python -m tools.ingest.fetch --only my-guide
```

**A git repository.** Add one row to `corpus/GIT_SOURCES.csv`. The columns are
`id, repo_url, branch, commit, subpath, license, pinned_on, notes`. The `commit` must be a
full 40-character SHA and `subpath` must name the folder to read, so every build uses the
same bytes. Find the commit with `git ls-remote <repo_url> <branch>`:

```
my-docs,https://github.com/example/docs.git,main,0123456789abcdef0123456789abcdef01234567,site/pages,Apache-2.0,2026-10-24,pinned today
```

To read two folders of one repository, add two rows with different ids and the same commit.
To update a source, change its `commit` and run the steps again.

```
python -m tools.ingest.gitsource --dry-run
python -m tools.ingest.gitsource
```

**Regenerate the documentation index.** The index is rebuilt from scratch from every row, so
this is also how you pick up a change or remove a source (delete its row first):

```
python -m tools.ingest.build --dry-run     # converts and scans, writes nothing
python -m tools.ingest.build               # writes corpus/index/relevance.sqlite
```

The report lists each source's files, chunks and examples. It also scans for credentials and
refuses to write the index if it finds a high-confidence one (exit code 3); a known false
positive can be allowed in `corpus/SCAN_ALLOW.csv` with a reason. Point
`BIGFIX_KNOWLEDGE_INDEX` at the file. It is read on every call, so a rebuilt index is used
without restarting the server.

### Add to the reference fixlet index

| To add | Edit | Then run |
| --- | --- | --- |
| A directory of `.bes` files (a fixlet path) | a row in `corpus/fixlet-sources.csv` | `fixlets`, then `fixletbuild` |
| A git repository of `.bes` files | a row in `corpus/fixlet-sources.csv` | `fixlets`, then `fixletbuild` |

The source list is `corpus/fixlet-sources.csv`, which is ignored by git because a company
path or an internal repository URL is itself internal. Copy the template and edit it:

```
cp docs/fixlet-sources.example.csv corpus/fixlet-sources.csv
```

The columns are `id, kind, location, branch, commit, subpath, group, notes`:

```
team-fixlets,dir,C:/path/to/bes-files,,,,team,a working tree
team-repo,git,https://git.example.com/org/repo.git,main,,content/fixlets,team,internal repository
public-repo,git,https://github.com/example/public-content,,,,reference,a public repository
```

- A **directory** is read in place and never modified. Every `.bes` file under it is found,
  in any letter case. Links and junctions are not followed.
- A **git repository** is checked out under `corpus/raw/fixlets/<id>/`. A blank `commit` means
  the branch tip at the moment it is read; the commit is recorded. Put a full SHA there to
  read exactly the same content again. Use `https` (git must be able to sign in without a
  prompt; a password in a URL is refused) or, with `--allow-scheme ssh`, an `ssh` URL.
- `group` decides what is counted together. The habits the index reports are counted per
  group and never pooled, so give your own sources one group and other people's content
  another. A source with no group counts alone.

```
python -m tools.ingest.fixlets --source team-fixlets --dry-run   # check one source
python -m tools.ingest.fixlets --source team-fixlets             # read it, record when
python -m tools.ingest.fixletbuild --all --dry-run               # analyse every listed source
python -m tools.ingest.fixletbuild --all                         # write corpus/index/fixlets.sqlite
```

For a one-off, skip the list: `python -m tools.ingest.fixlets --dir PATH` or
`--git URL [--branch B] [--commit SHA] [--subpath P]`.

The index is built from **all** the sources you name at once and rebuilt from scratch, so
always build with `--all` (or repeat `--source`). It holds three kinds of row: applicability
relevance of fixlets and tasks, analysis properties, and **action scripts**, each stored as a
template with its relevance substitutions listed separately. It also counts the habits that
recur, per group and platform. A credential found in relevance, a description or a script is
left out and reported by file and rule, never by value. Point `BIGFIX_FIXLET_INDEX` at the
file. The step-by-step guide, with a worked example for each kind of source, is
[docs/reference-fixlets.md](docs/reference-fixlets.md).

### Check and keep it current

- Ask for something the sources cover. `search_relevance_docs` answers with `chunks` and
  `examples`; `search_reference_fixlets` answers with `results` and `conventions`. Either
  answers `available: false` with the reason and how to enable it when its variable is unset,
  its file is missing, or the index was built by a different version. A version mismatch means
  rebuild.
- Rebuild whenever a source changes. Every real read of a reference source appends to
  `corpus/index/fixlet-reads.jsonl`: when it was read (UTC), the commit (git), a fingerprint of
  the files and counts.
- To judge answers fairly, set `BIGFIX_FIXLET_HOLDOUT` to the item ids a question came from;
  they are removed, with their action scripts, from every search.
- The write prompts that use these indexes are `write_relevance`, `write_actionscript` and
  `write_fixlet` (above). Team rules belong in `BIGFIX_RELEVANCE_CONVENTIONS`, not in the
  indexes.

## Configuration

Environment variables win over config files:

| Setting | Env var / `[besapi]` config key | Default |
| --- | --- | --- |
| Root server URL | `BES_ROOT_SERVER` (e.g. `https://bes.example.com:52311`) | - |
| REST operator | `BES_USER_NAME` | - |
| Password | `BES_PASSWORD` | - |
| Write tools | `BIGFIX_ALLOW_WRITES`: `true` to register them | off |
| TLS verification | `BES_SSL_VERIFY`: `tofu`, `false`, `true`, or a CA bundle path. `tofu` (or unset) pins self-signed certs on first use for session relevance only; besapi treats it as `false`. See [Safety and design notes](#safety-and-design-notes) | unset (`tofu`) |
| qna inventory | `BIGFIX_QNA_INVENTORY`: path to a remote_clients.toml, or `0` for none (see [qna evaluation](#qna-evaluation)) | `~/.bigfix/remote_clients.toml`, then the all-users config dir |
| qna auto-discovery | `BIGFIX_QNA_AUTO_DISCOVERY`: `0` to never discover hosts, on first run or when asked | on |
| qna containers | `BIGFIX_QNA_CONTAINERS`: `0` to forbid container-image targets | on |
| Knowledge index | `BIGFIX_KNOWLEDGE_INDEX`: path to the `relevance.sqlite` built by `python -m tools.ingest.build` (see [docs/knowledge-retrieval-plan.md](docs/knowledge-retrieval-plan.md)). Read-only; read on every call, so a rebuilt index is picked up without a restart. An index and a server must agree on the schema version, and a mismatch is reported (rebuild the index, or restart after upgrading the server) | unset (`search_relevance_docs` reports it is unavailable) |
| Reference fixlet index | `BIGFIX_FIXLET_INDEX`: path to the `fixlets.sqlite` built by `python -m tools.ingest.fixletbuild` from the sources in the untracked `corpus/fixlet-sources.csv` (template: [docs/fixlet-sources.example.csv](docs/fixlet-sources.example.csv)). Step-by-step build instructions for a directory, a backup export and a git repository (the public `jgstew/bigfix-content`, as the example of reading an internal one) are in [docs/reference-fixlets.md](docs/reference-fixlets.md). Company content, so it is never tracked or packaged. Read-only; read on every call. Optional `BIGFIX_FIXLET_HOLDOUT` (comma-separated item ids) removes those items from every search, for evaluations. |
| Relevance conventions | `BIGFIX_RELEVANCE_CONVENTIONS`: path to a markdown file of your team's relevance conventions, added to the `write_relevance` prompt as instructions. Read on every call; capped at 64 KB (8,000 characters reach the prompt); set but unreadable is reported in the prompt, not ignored | unset (no conventions) |
| Transport | `BIGFIX_MCP_TRANSPORT`: `stdio` or `http` (streamable HTTP on 127.0.0.1:8000/mcp, banner on). No auth layer of its own - put authentication in front before binding beyond localhost. | `stdio` |

Config files are searched in besapi's order: `/etc/besapi.conf`,
`~/besapi.conf`, `~/.besapi.conf`, `./besapi.conf` - same
`[besapi]` section format as besapi/bescli, so an existing config just works.
Prefer keeping credentials in `~/besapi.conf` over MCP client config files.

Example MCP client config (see [.mcp.json](.mcp.json)):

```json
{
  "mcpServers": {
    "bigfix-root": {
      "command": "uvx",
      "args": ["bigfix-root-mcp"]
    }
  }
}
```

## Install / run

Published on [PyPI](https://pypi.org/project/bigfix-root-mcp/). Requires Python 3.11+.

### With uv (preferred)

No install step - `uvx` fetches and runs it in a throwaway environment, which is
also what the MCP client configuration above uses:

```bash
uvx bigfix-root-mcp
```

To keep it on `PATH` instead, as a managed tool:

```bash
uv tool install bigfix-root-mcp
bigfix-root-mcp
```

### With pip

```bash
pip install bigfix-root-mcp
bigfix-root-mcp               # or: python -m bigfix_root_mcp
```

### From a checkout

[uv](https://docs.astral.sh/uv/) 0.12+ required:

```bash
uv sync
uv run bigfix-root-mcp        # or: uv run python -m bigfix_root_mcp
```

### Verifying a download

Every [release](https://github.com/jgstew/bigfix-root-mcp/releases) since v0.1.3
also attaches the wheel and sdist with a `SHA256SUMS.txt`, checkable with
`sha256sum -c SHA256SUMS.txt`.

Smoke test against a live root server with MCP Inspector:

```bash
npx @modelcontextprotocol/inspector bigfix-root-mcp
```

then call `whoami`, `session_relevance_query` with `number of bes computers`,
and `client_query` targeting a known computer ID.

### Operator scope

Every result is limited to what the configured REST operator can see. Only a
**master operator** has full visibility; a regular operator can never be
certain its view is complete, and cannot distinguish "does not exist" from
"outside my scope". So `number of bes computers` returning 35 means *35
computers visible to this operator* - a lower bound, not the BigFix total.

`whoami` reports `is_main_operator` for exactly this reason: check it before
treating any result as the full state of BigFix. The tool descriptions carry
this caveat so LLM clients don't overstate scoped results.

## Safety and design notes

- **Read-only by default**: with `BIGFIX_ALLOW_WRITES` unset, only read tools
  are registered - the write tools do not exist as far as any client can tell.
  One nuance: submitting a client query does create a query object
  server-side, but agents only *evaluate* relevance against it - no
  managed-endpoint state changes.
- **Client fast query is a powerful read.** `client_query` with `target_all`
  evaluates arbitrary client relevance on every agent the operator can see,
  and the BigFix agent runs as SYSTEM/root. That can read file contents,
  registry values and process lists fleet-wide, and the results come back in
  the tool response. Scope the configured operator to the smallest useful set
  of computers; `whoami.is_main_operator` tells you which you have.
- **Explicit site paths**: this server never uses besapi's mutable
  "current site path" connection state (`set_current_site_path` /
  `get_current_site_path` - a bescli convenience); tools that need a site take
  a required `site_path` parameter.
- **Stdout hygiene**: stdout belongs to the MCP stdio transport; all logging
  goes to stderr, and config loading avoids besapi helpers that print.
- **TLS**: with `BES_SSL_VERIFY` unset (or `tofu`), session relevance
  trusts on first use: a CA-valid certificate is used as is, a self-signed
  one (the BigFix default) is pinned on first contact in
  `~/.bigfix/.remote_session_known_certs.toml` - shared with the
  bigfix-remote-session-relevance CLI - and a different certificate later is
  refused before any credentials are sent. Remove the entry (or run
  `bigfix-remote-session-relevance --forget-cert HOST:PORT`) once a
  legitimately changed certificate is verified out of band. The besapi tools
  have no pinning and stay unverified in that mode, matching besapi. An
  explicit `false` turns verification off for both; `true` (honouring
  `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`) or a CA bundle path verifies both
  and pins nothing - use one of those for anything beyond a lab.
  `HTTPS_PROXY` / `NO_PROXY` are honoured by the besapi tools only; session
  relevance connects directly.
- Generic BigFix logic here is written to be upstreamed into besapi - see
  [docs/besapi-proposals.md](docs/besapi-proposals.md).

## Session relevance

`session_relevance_query`, `find_computers` and `find_content` evaluate
through the REST backend of
[bigfix-remote-session-relevance](https://github.com/jgstew/bigfix-remote-session-relevance),
using the same connection settings as everything else (env vars or
`besapi.conf`); its own `~/.bigfix` config, certificate pinning and
deployment discovery are not used. The reason is the root server's
behaviour when it can reach a BigFix Explorer: it forwards `/api/query` to
Explorer, which rejects besapi's double-encoded form post with "This
expression contained a character which is not allowed" - in practice any
query containing a space. The library sends a JSON body first and falls back
to besapi's form encoding only when the server answers "no reachable BigFix
Explorer", so both setups evaluate the query exactly as written.

## qna evaluation

[bigfix-remote-client-relevance](https://github.com/jgstew/bigfix-remote-client-relevance)
is a base dependency, so `qna_eval_client_relevance` / `qna_list_targets`
are registered by default; they are absent only when both target sources are
off (`BIGFIX_QNA_INVENTORY=0` and `BIGFIX_QNA_CONTAINERS=0`). The `[qna]`
extra still exists so older install commands keep working, but adds nothing.
It evaluates **client** relevance with
the BigFix `qna` binary on lab targets in about a second per warm run - the
fast half of the feedback loop next to the advisory analysis.

**What it is not:** qna runs on the target machine, *not* through the BigFix
deployment. No operator scope, no site subscriptions, no client settings -
inspectors that depend on deployment state will differ from `client_query`.
Iterate here, confirm on real agents with `client_query`. (The package's
Fast Query transport is an upstream stub; this server does not expose it.)

Targets are deliberately constrained:

- **Container images** (`ubuntu:22.04`, ...) may be passed by the caller;
  needs docker or podman on the server host. First use of an image is slow
  (agent artifact download + derived image build); later runs are ~1s. Set
  `BIGFIX_QNA_CONTAINERS=0` to forbid these.
- **Inventory hosts** come only from a remote_clients.toml on the server (the
  package's inventory format: ssh/local/container/online_evaluator entries).
  `BIGFIX_QNA_INVENTORY` names one; unset, the server uses the same default
  as the package's CLI: `~/.bigfix/remote_clients.toml`, then the all-users
  config directory (`/Library/Application Support/bigfix`, `/etc/xdg/bigfix`,
  `C:\ProgramData\bigfix`). Unlike the CLI it never reads
  `./remote_clients.toml`, since an MCP server's working directory is wherever
  its client launched it. `BIGFIX_QNA_INVENTORY=0` turns the inventory off.
  The MCP client can pick entries by name but can never supply ssh hosts,
  users, or `become` flags of its own. `qna_list_targets` shows the file in
  use.
- **No targets named** means every inventory host, as with the CLI (qna only
  reads), up to 20 per run. An `online_evaluator` entry sends the client
  relevance to developer.bigfix.com, so name targets explicitly to keep a
  query off it.
- **Auto-discovery:** the package's non-interactive discovery probes local
  qna, a downloaded qna (cached by the package), the container engine's
  images and developer.bigfix.com, then adds what works to
  `~/.bigfix/remote_clients.toml`; it can take minutes. Runs are one at a
  time. It is refused while the inventory in use is another file (the
  all-users one, say), because the new `~/.bigfix` file would hide it. As with the CLI, it
  runs by itself only on a first run: one that names no targets when no
  inventory file exists. Once the file exists, even with no hosts, it runs
  only when the `qna_discover_targets` tool is called (the CLI's
  `--auto-discovery`). With a `BIGFIX_QNA_INVENTORY` path (that file is the
  admin's) or `BIGFIX_QNA_AUTO_DISCOVERY=0` it never runs and that tool is
  not registered. It never uses the interactive SSH discovery and never
  prompts. When a `qna_eval_client_relevance` run finds no targets (an empty
  inventory, a name missing from an empty one) or returns no result rows at
  all, the error or the payload's `hint` suggests `qna_discover_targets`.
  Error rows count as results. If discovery ran in the last 15 minutes, it
  says what that run added and where to add hosts by hand instead.

Security notes: qna executes on the targets; the controller downloads BigFix
agent artifacts from support.bigfix.com (SHA256-verified against the release
site's sums) on first use of a version, so the server host makes outbound
requests; every run, and every auto-discovery, leaves a `BIGFIX QNA` audit
line on stderr.

## Writes

Set `BIGFIX_ALLOW_WRITES=true` to register the three write tools. The flag
controls *registration*, so with it off there is nothing to call.

Two guardrails apply to all of them:

- **`dry_run` defaults to true.** The response describes the call that would
  be made and nothing is sent. A write only happens on an explicit
  `dry_run=false`.
- **Every attempt is audit-logged** to stderr as one `BIGFIX WRITE` line with
  the operator, target, dry-run flag and outcome.

The set is limited on purpose to operations whose blast radius is reversible
or nil. `import_bes_content` *creates* content; it does not run it - a fixlet
imported this way does nothing until somebody deploys an action against it in
the console.

**Not implemented, and not to be added without their own design round:**
deploying actions (`POST /api/actions`), any `DELETE`, creating sites or
operators, and file upload. Deploying an action is arbitrary code execution as
root across the fleet, which is a different category of risk from anything
here.

## Documentation

| Doc | Contents |
| --- | --- |
| [client-query.md](docs/client-query.md) | Client fast query protocol reference: endpoints, payloads, live-captured result schema, termination heuristics and their tradeoffs. |
| [besapi-notes.md](docs/besapi-notes.md) | besapi behaviors this wrapper depends on or works around (error surfacing, connection lifecycle, return shapes, site-path state). |
| [design-decisions.md](docs/design-decisions.md) | Why the server is shaped this way, plus FastMCP 4 beta specifics. |
| [besapi-proposals.md](docs/besapi-proposals.md) | Proposed upstream besapi changes that would let this project shrink. |
| [rest-endpoints.md](docs/rest-endpoints.md) | Live-verified REST paths, site-path rules, and relevance findings (including the operators that don't exist). |
| [security-review.md](docs/security-review.md) | Threat model and findings for the tool surface. |

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) 0.12+. `uv sync`
creates `.venv` from the committed `uv.lock` and installs the `dev` dependency
group; no manual venv or `pip install -e` step.

```bash
uv sync
uv run pytest
```

Linting, formatting, type checking and the packaging checks all run through
pre-commit (ruff, mypy, bandit, slyp, zizmor, and the uv lock/build checks).
Install both hook types - some checks are deliberately deferred to `pre-push`
because they are too slow to want on every commit:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

Useful invocations:

```bash
uv run pre-commit run --all-files                    # the commit-stage hooks
uv run pre-commit run --all-files --hook-stage manual  # everything, as CI runs it
```

Tests run entirely offline against a scripted fake `BESConnection`, including
in-memory end-to-end MCP calls via `fastmcp.Client`.
