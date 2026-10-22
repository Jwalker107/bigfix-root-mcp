# Plan: relevance knowledge retrieval

Goal: help an MCP client write correct BigFix Relevance by letting the server
search a local knowledge base built from public training material (web pages
and PDFs), and by wrapping that search in a workflow that verifies every draft
with the analyzer and a live evaluation.

Approach: **retrieval and validation, not training.** Distil the material into
searchable, dialect-tagged chunks, expose a read-only search tool, and use a
prompt that forces `analyze_relevance` and a bounded live run before anything
is reported.

## Status

| Phase | State |
|---|---|
| 0. Scaffolding | Done |
| 1. Fetch | Done (unit-tested with a fake HTTP layer; not yet run against real sources) |
| 2. Convert and chunk | Done (real index built from the pinned developer.bigfix.com checkout) |
| 3. Retrieval tool | Done (`search_relevance_docs`, read-only, tested against fixture and real index) |
| 4. Workflow prompt and curated resources | Done (`write_relevance`, conventions file, two analyzer-checked resources) |
| 5. Evaluation | In progress: analyzer pass, the four index changes, golden-set selection and retrieval mechanics done (Phase 5a). Live answer checks deferred by the user (2026-10-22). Phase 5b has started: 25 questions from the team's own BES content are approved (more wanted) |
| 6. Reference fixlets | Planned (written 2026-10-22); not started. Needs the decisions listed in its section before 6e |
| 7. Docs and CI | Not started |

## Decisions

| # | Decision | State |
|---|---|---|
| 1 | Build in this repo or as a separate Claude skill | Confirmed: this repo |
| 2 | Corpus location | Confirmed: raw files under `BIGFIX_CORPUS_DIR`, else `corpus/raw/`; the manifest always lives in `corpus/` |
| 3 | Source list (URLs, PDF count and size) | Confirmed. Two PDFs in `corpus/MANIFEST.csv` and one git source in `corpus/GIT_SOURCES.csv` |
| 4 | Legal sign-off on fetching and storing the sources | Confirmed (2026-10-21) for the URLs and, per the user, for the git repos as well. Only sources that sign-off covers may go in either manifest; record each one's license or terms in the `license` column. Publicly viewable is not the same as redistributable, so the downloaded sources, converted text and index stay untracked and nothing is redistributed |

## Source strategy: git repos first (decided 2026-10-21)

Where a source's git repo is available, ingest the repo at a pinned commit
instead of crawling the rendered website: cleaner content (no navigation
boilerplate), complete (no link discovery), no load on the site, and a commit
SHA is a stronger provenance record than a page hash. Pinned repos are listed
in `corpus/GIT_SOURCES.csv` (id, repo_url, branch, commit, subpath, license,
pinned_on, notes). The raw checkout goes under `raw/<id>/` like any other
source. Plain URLs (PDFs, pages with no repo) stay in `corpus/MANIFEST.csv`.
The crawler idea was dropped; the two `developer.bigfix.com` page rows were
removed from the manifest.

**Legal:** the sign-off was first given for the website URLs; on 2026-10-21 the
user confirmed it also covers the git repos. The repo is Apache-2.0 per its
`LICENSE`. This records that confirmation; it is not legal advice, and it does
not extend to redistributing the sources.

### First pinned source: developer.bigfix.com

- Repo `https://github.com/bigfix/developer.bigfix.com.git`, default branch
  `master`, pinned at `89435d306d01041767ae82b83057b443c5edb552`
  ("Merge changes for 11.0.7 publication (#454)"). Area of interest:
  `site/pages/relevance` (609 files, 604 markdown).
- The README says the rendered site is built from markdown in `site/pages`,
  **except** the inspector reference, which is built from
  `site/pages/relevance/_reference/docs` (535 files) by a custom build because
  one inspector can appear on several pages.
- **Runnable examples.** 1,111 `{% qna %}...{% endqna %}` blocks in 134 files
  (guide, tutorial, examples and the inspector reference). Inside a block:
  `Q:` query (client relevance for the QnA evaluator), `A:` expected answer
  (repeated for multi-row results), `I:` expected type (only 5 files), `E:`
  expected error message (58 lines: useful as "this is wrong because" negative
  examples) and `T:` evaluation time (noise, drop). Counts are from the pinned
  commit.
- Other template tags to handle or strip: `{% evaluator %}` (18), `{% collapse %}`
  (11), `{% section %}` (3), `{% for %}` (2), `{% note %}` (1). Only 69 of 604
  markdown files have YAML front matter. Inspector docs use `# heading` per
  signature, e.g. `# bit <integer> of <bit set> : boolean`, with HTML-escaped
  angle brackets (`&lt;`), so unescape before indexing.
- `examples/` holds only two pages (an index and a step-by-step "Creating a
  computer group" walkthrough with five qna blocks and console steps).

### Phase 2 implications

- Parse `qna` blocks into structured records `(query, answers, type, error)`
  linked to the nearest preceding heading or prose. These are the best records
  in the corpus: short, verified, and they carry their own expected output.
- They also seed Phase 5: the same blocks are a ready-made golden set, scored
  by running each `Q:` through `qna_eval_client_relevance` and comparing to
  `A:` (and to `E:` for the error cases).
- Chunk inspector docs per signature heading, not per file.

## Design

### Storage

```
corpus/                      # corpus/README.md and MANIFEST.csv are tracked
  MANIFEST.csv               # one row per source; the update changelog
  raw/                       # downloaded originals, untracked (or BIGFIX_CORPUS_DIR/raw)
  md/                        # converted markdown, untracked, derived
  index/relevance.sqlite     # FTS5 index, untracked, derived
tools/ingest/                # fetch.py now; build.py in Phase 2
```

- The raw files are never edited. `md/` and `index/` are rebuildable.
- Source material is **not committed**: copyright, binary bloat in git history,
  and silent staleness. The manifest (URL plus SHA-256) makes updates easy
  instead: a manifest diff in git shows what upstream changed.
- Only permissively licensed material or things we wrote ourselves belong in
  git (our own distilled cookbooks go under `src/bigfix_root_mcp/resources/`).
- Internal or confidential sources stay off the repo entirely.

### Manifest columns

`id, path, source_url, title, retrieved_on, license, bigfix_version, dialect, sha256, notes`

`id` is a short slug and the default file stem. `path` is relative to `raw/`
and is filled in by the fetch step if blank. `dialect` is `session`, `client`
or `both`.

### Retrieval

SQLite FTS5 (BM25), no new runtime dependency. Relevance is identifier-heavy
(`bes computers`, `name of it`, `whose`) and dialect-sensitive, which suits
exact-term search better than embeddings. Add local embeddings only if the
golden set shows missed paraphrases. Never send corpus content to an external
embedding or LLM service.

The useful unit is a (description, relevance, dialect) triple, so code blocks
are extracted as their own records alongside the surrounding explanation.

## Phases

### Phase 0: Scaffolding (done)
- `corpus/README.md`, an empty `corpus/MANIFEST.csv` (header only).
- `.gitignore` entries for `corpus/raw/`, `corpus/md/`, `corpus/index/`.
- `tools/` and `tools/ingest/` packages.
- Done when: `git status` shows no corpus files and pre-commit passes.

### Phase 1: Fetch, `tools/ingest/fetch.py` (done)
- Reads the manifest and downloads each `source_url` into `raw/`.
- Compares SHA-256 to the manifest and reports `new`, `changed`,
  `unchanged`, `gone` (404/410), `blocked` (robots.txt) and `error`.
- Safety: http(s) only, robots.txt (unreachable or 5xx counts as disallow, per
  RFC 9309), per-host rate limit, retries with backoff on 429/5xx and network
  errors, request timeout, response size cap, atomic writes, `--dry-run`
  (downloads into memory and reports, writes nothing), `--only <id>`.
- Fetched content is untrusted data: stored, hashed, never executed. The
  `path` column is checked so a manifest row cannot write outside `raw/`.
- Idempotent: an unchanged source with a missing local copy (fresh checkout)
  restores the file without touching the manifest.
- Usage: `python -m tools.ingest.fetch [--dry-run] [--only ID] [--delay S]`
- Tests: `tests/test_ingest_fetch.py`.

### Phase 2: Convert and chunk (done)

Modules in `tools/ingest/`:

| Module | Job |
|---|---|
| `gitsource.py` | `python -m tools.ingest.gitsource`: bring each `GIT_SOURCES.csv` checkout to its pinned commit |
| `chunking.py` | markdown plus the site's template tags -> chunks and examples (pure functions) |
| `convert.py` | HTML -> markdown (stdlib); PDF -> markdown via `pypdf` if installed |
| `scan.py` | secret/PII heuristics; allowlist in `corpus/SCAN_ALLOW.csv` |
| `index.py` | SQLite + FTS5 writer, schema version 2 (documented in the module; version 1 had no analyzer verdict) |
| `verdict.py` | the offline analyzer's verdict on one query, stored with each example |
| `build.py` | `python -m tools.ingest.build`: orchestrates all of the above |

Build behaviour worth knowing:
- Exit codes: 0 built, 1 error / nothing to index / skipped under `--strict`,
  2 bad arguments or manifests, 3 secret found (no index written).
- **error** (stops the build): checkout not at its pinned commit, modified
  checkout, fetched file not matching its manifest hash, invalid UTF-8, a
  conversion that produced no text.
- **skipped** (reported, build continues unless `--strict`): never fetched, raw
  file missing, `pypdf` not installed, no converter for the file type.
- `--only ID` is only allowed with `--dry-run`, so a partial build can never
  overwrite a complete index.
- The checkout check is byte-level: git is run with `core.autocrlf=false`, and
  a checkout whose line endings were converted counts as modified (git's own
  `status` can say "clean" after a stat-cache refresh under a different
  setting; `git ls-files --eol` is also checked).
- Example text is never HTML-unescaped; prose is cleaned. Examples that repeat
  an earlier (dialect, query, answers, errors) are dropped and counted.
- Template cleanup: `{% ... %}` tags are dropped and `{{ 'x' | linkType }}`
  (the site's type-link filter, 35 uses in 6 files) becomes `x`. Anything else
  with `{{` or `{%` left in indexed prose or headings is not guessed at; it is
  counted and shown as `template-leftovers=N` per source in the build report, so
  a new construct is visible at build time instead of in search results. Code
  and examples are verbatim and not counted. The first index had 35 such
  leftovers, found by a live search, because only `{% %}` had been checked.
- Dialect and kind come from per-source rules in `build.SOURCE_RULES`.
  developer.bigfix.com: `guide/session` -> session, `guide/client` and
  `tutorial/client` -> client, the generated inspector reference
  (`_reference/`) -> `unknown` (the source does not say), everything else ->
  both. `unknown` means "not stated", so a reader filtering by dialect should
  treat it as matching either.

Result of the first real build (commit `89435d3`, 2026-10-21): 604 markdown
files -> 5,483 chunks and 1,198 examples (37 duplicates dropped, 1,529 "No
documentation exists." sections skipped, 2 empty sections), 3.6 MB. Examples by
form: 1,121 qna and 15 fenced Q/A cases found, 99 relevance-only fences; 876
examples carry dialect `unknown` because they come from the inspector
reference; 40 examples expect an error; 2 are marked `incomplete` (an elided
answer list). These counts were cross-checked against separate counts made
with different code (every file agreed on the number of `Q:` lines).

Known limits:
- PDF conversion uses `pypdf`, a **dev-only** dependency (`[dependency-groups]
  dev`, `>=6.20.1`, locked at 6.20.1, uploaded 2026-10-12 so inside the 7-day
  cooldown). The server never imports it, and without it PDF sources are
  skipped with a message. Tested against real `pypdf` with small generated
  PDFs (`tests/ingest_pdf.py`).
- Both vendor PDFs were fetched on 2026-10-21 and indexed: core-inspectors-guide
  (239 pages, 237 with text, 256 chunks) and custom-authoring-training-guide
  (83 pages, 86 chunks). The second is from 2005. The converter normalises PDF
  text: typographic quotes become straight quotes (the 2005 guide's example
  relevance used curly quotes, which are not valid relevance), C1 control
  characters (Wingdings bullets arrive as U+0084) are removed, and a line
  printed on >= 90% of pages (its copyright footer is on 100%) or equal to the
  page's own number is dropped. Repeated table headings and platform lists are
  real content and are kept.
- PDFs yield no structured examples (`examples=0`): they have no Q:/A: form.
  The 2005 guide's example lines are indented lines of prose; extracting them
  as examples is possible later but not done. The core guide is a table
  (key phrase, form, description) flattened to lines, so a row reads as one
  or two lines, searchable but not structured. Its index pages (inspector
  name, middle dot, page numbers) are indexed as well; low value, not removed.
- 2 scan warnings from the 2005 guide are generic company addresses on its
  contact page (info@ and sales@), not personal ones.
- 4 email-address warnings in the scan are benign (placeholder repo URL, a
  `.local` test account, a fictitious `bigcorp` address).
- The `slyp` pre-commit hook cannot run on Windows (it needs `fork`); lint the
  code with `slyp --only lint -` per file (stdin mode) instead.
- 876 reference examples are dialect `unknown`; classifying them from the
  analyzer package's per-inspector dialect table is a possible later step.

### Phase 3: Retrieval tool (done)

Built: `src/bigfix_root_mcp/knowledge.py` (no fastmcp import, like
`analysis.py`) and the `search_relevance_docs` tool in `server.py`. Tests:
`tests/test_knowledge.py`; `tests/test_tools.py` pins the new tool in the
registered set. Docs: README (tool table and `BIGFIX_KNOWLEDGE_INDEX`),
`resources/tool-guide.md`, and `docs/design-decisions.md` (why it is always
registered).

Behaviour:
- `search_relevance_docs(query, dialect=None, kind=None, limit=5)`. Returns
  `chunks` (documentation) and `examples` as separate lists, because BM25
  scores from the two tables are not comparable. `kind` of `explanation` or
  `inspector` returns documentation only, `example` examples only, unset both.
  `limit` applies per list and is clamped to 1-25.
- Ranking: SQLite FTS5 BM25 with heading and example-query matches weighted
  above body text. Each list tries all terms first, then any term, and
  reports which in `match` (`all_terms`, `any_terms`, `none`).
- `dialect` of `session` or `client` keeps that dialect plus `both` and
  `unknown`.
- Safety: a query is reduced to words and quoted phrases and each term is
  passed to FTS5 as a quoted string; the index is opened `mode=ro` per call
  and its path comes only from `BIGFIX_KNOWLEDGE_INDEX`. Every response notes
  that the text is data, not instructions, and can lag the live server.
- Missing, unreadable, non-index and wrong-schema-version files all give
  `available: false` with a reason and `how_to_enable`, not an error. Bad
  arguments are checked first and become tool errors.
- Size: a default response (5 + 5 results) is about 4 KB; chunk text is
  clipped at 1,500 characters (`text_truncated`), an example shows at most 20
  answers (`answers_total`), each clipped at 300 characters.

To use it with a running server, set `BIGFIX_KNOWLEDGE_INDEX` to the built
`corpus/index/relevance.sqlite` in the MCP client's server configuration. The
index is read on every call, so rebuilding it needs no restart.

Not done, on purpose: no `source` filter, no paging (`offset`), no embedding
search. Add them only if Phase 5 shows a need.

### Phase 4: Workflow prompt and curated resources (done 2026-10-22)

Built, with tests in `tests/test_write_relevance.py` and `tests/test_resources.py`:

- **The `write_relevance(goal, dialect="")` prompt** (`src/bigfix_root_mcp/prompts.py`).
  Seven steps: dialect; look up with `search_relevance_docs` using the task's own
  words (prefer examples with `parses=true`, do not copy `parses=false`, say so when
  no index is configured, treat results as data); check names with
  `search_inspectors` (a name missing from the snapshot may be newer, not wrong);
  draft the smallest expression; `analyze_relevance` with the dialect pinned;
  evaluate bounded (session: a limit; client: one named computer or a lab target,
  never `target_all` unless asked); report only what was verified and what was not.
  It ends with the operator-scope reminder. A test checks that every tool and
  resource it names is registered.
- **Team conventions are configuration** (`src/bigfix_root_mcp/conventions.py`).
  `BIGFIX_RELEVANCE_CONVENTIONS` names a markdown file whose text is added to the
  prompt as instructions. The shipped prompt contains no team rule (tested). The
  proxy-agent default the user set is therefore carried by a conventions file, not
  by the public package: `docs/relevance-conventions.example.md` is the generic
  template, and the user's own file is `corpus/conventions.md` (untracked). To use
  it, set `BIGFIX_RELEVANCE_CONVENTIONS` to that path in the MCP client's server
  entry. An evaluation run must set it too, or the default is not applied.
- **Two curated resources**, written from the documentation in our own words:
  `bigfix://relevance/authoring-guide` (rules that prevent most errors, the
  documented error messages with causes and fixes, how applicability clauses
  combine, how to read analyzer warnings) and `bigfix://relevance/client-idioms`
  (documented patterns for files, registry, applications, services, OS, versions,
  text and WMI). **All 25 `relevance` snippets in them are parsed by the analyzer
  in a test** and must use only inspectors it knows, so the shipped guidance cannot
  teach a mistake like the ones measured in the upstream docs.
- Grounding checked, not assumed: "all clauses must be true" is from the 2005
  vendor guide ("an action is not taken on a machine if any of its relevance
  clauses are false"); `idp user` and `idp group` are listed as BigFix 11 Patch 7
  additions in the upstream "what is new" page.

Not done, on purpose: no session-relevance idioms resource (the existing session
cookbook is live-verified and there are no session questions yet to measure
against), and no change to retrieval ranking.

### Phase 5: Evaluation
- `tests/eval/golden.yaml`: question, dialect, acceptance check (parses
  cleanly, no unknown inspectors, optional result check on a test server).
- A script that scores answers with and without retrieval; record versions,
  parameters and seeds.
- Live checks run read-only against a non-production server.
- Done when retrieval measurably improves the pass rate. If it does not,
  tune chunking before adding more material.

#### Phase 5a, step 1: analyzer pass over the examples (done 2026-10-21)

`python -m tools.eval.analyze_examples [--csv PATH]` (code in `tools/eval/`,
tests in `tests/test_eval_analyze_examples.py`). Offline; reads the index
read-only; a measurement, not a gate (exit 0 unless the index cannot be read).
Analyzer: bigfix-relevance-analyzer 1.38.1. Index: commit `89435d3` plus the two
PDFs (which contribute no examples).

Result over 1,198 examples: **1,163 parse cleanly (97.1%)**, 35 do not (4 of
those document an error themselves, 31 do not), 0 analyzer crashes.

| Form | Parse cleanly | What it is |
|---|---|---|
| `qna` (Q:/A: blocks) | 1,075 / 1,087 (98.9%) | has documented answers |
| `fence-qa` (Q:/A: in a fence) | 15 / 15 (100%) | has documented answers |
| `fence-relevance` (bare fence) | 73 / 96 (**76.0%**) | a query with no answer |

What it found:
- **Bare `relevance` fences are unreliable, the Q:/A: forms are not.** 21 of
  the 92 session-guide examples fail, all of them bare fences: 13 in
  `guide/session/stataggregation.md`, plus typos such as an extra `)`
  (`links (name of it & ... ")")) of bes computers`) and a stray `".` ending. An
  example that does not parse is a bad model of syntax, so it should not be
  presented as equal to a documented, answered one.
- **The analyzer can label about two thirds of the `unknown`-dialect examples.**
  **Correction:** the first pass reported "866 of 869 classified definitively
  (744 client, 125 session)". That counted a resolution of `both` (the query is
  valid in either dialect, e.g. `bit 0 of bit set "10"`) as definite, which it is
  not. The measured figure, after the labelling rule was made strict, is 569 of
  869: 443 client and 126 session. 300 resolve to `both` and stay unknown, and 7
  do not parse. Even so, that makes the dialect filter useful for the
  inspector reference's examples.
- **Analyzer snapshot gaps, not doc errors.** 49 examples (34 distinct names)
  use inspectors the analyzer does not know, mostly BigFix 11 additions: `idp
  user` (14 examples), `idp groups` (8), `ips image`, `repository site flag`,
  `fips_140_2 mode`. One name is a misspelling of `signal strength` in the source
  docs themselves, so that one is a doc defect. This is
  useful feedback for the analyzer's maintainers, and a reminder that an
  `unknown-inspector` warning is not proof an inspector does not exist.
- **Authoring artifacts in the source docs.** Four queries contain HTML entities
  (`&lt;`, `&gt;=`) in `date.md` and `ipv4-address.md`; two fail to parse because
  of it. An en dash stands in for a minus in `sets.md`; one tutorial example
  (`(exists relay service) OR ()`) is a deliberate half-finished step; the
  `json-value.md` examples use `<Example>` placeholders. None of these are
  extraction errors; the text is kept verbatim.
- **One extraction limit:** a bare fence was read as one query, but
  `guide/basics/introspectors.md` has **8** blank-line-separated queries in a
  single fence (the first report said 2; the output had been cut short). It is
  the only such fence. Fixed below.
- Lint findings are mostly style: `non-unique-risk` (79), `plural-preferred`
  (52), `unknown-inspector` (50), `type-error` (9), `error-token` (5).

#### Phase 5a, step 2: the four changes (done 2026-10-21)

1. **The analyzer verdict is stored and shown.** Index schema is now **version 2**
   (`tools/ingest/index.py`, `src/bigfix_root_mcp/knowledge.py`). Each example
   carries `parses` (1, 0, or NULL when not analysed), `parse_error`,
   `unknown_inspectors` and `dialect_source`; `meta` records `analyzer_version`.
   `search_relevance_docs` returns them: `parses: false` (with `parse_error`)
   flags an example that does not parse, and its note says what that means (for
   an example with documented `errors` the failure can be the point). The
   verdict only labels; results are not re-ranked by it. A server that reads
   version 1 reports `available: false` for a version 2 index and says to
   rebuild, and the reverse; restart the MCP server after an upgrade.
   The shared code is `tools/ingest/verdict.py`, used by the build and by
   `tools/eval/analyze_examples.py`.
2. **Dialects are inferred only when the analyzer is definite.** An example
   whose source states no dialect is labelled `session` or `client` when the
   analyzer's text classifier and inspector table name exactly one of them. A
   resolution of `both`, a conflict, or a query that does not parse leaves it
   `unknown`, and a dialect the source states is never overridden. The label is
   marked `dialect_source: analyzer`. The eval tool ignores such labels when it
   pins dialects, so its mismatch check stays independent.
3. **The golden set is defined and generated, not committed.**
   `python -m tools.eval.golden` writes `corpus/eval/golden.jsonl` (+
   `golden.meta.json`, untracked) from the index: documented `qna` / `fence-qa`
   examples that parse, document an answer, document no error and are not
   `incomplete`. The examples are documentation text, so only the code and
   criteria are committed. First result: **1,031 of 1,204 examples**; excluded:
   103 bare fences, 12 that do not parse, 2 incomplete, 36 documenting an error,
   20 with no documented answer. 44 golden examples use an inspector the
   analyzer lacks (kept and flagged).
4. **A bare `relevance` fence is split at blank lines**, one example per
   statement. It changed one fence (`introspectors.md`, 1 example to 8).

Result of the rebuilt index (docs commit `89435d3`, analyzer 1.38.1): 1,204
examples; 1,170 parse and 34 do not (22 bare fences, 12 `qna`); 569 examples
labelled by the analyzer (443 client, 126 session); 49 mention an inspector the
analyzer lacks.

#### Phase 5a, step 3: retrieval mechanics (done 2026-10-22)

`python -m tools.eval.retrieval [--index PATH]` (`tools/eval/retrieval.py`, tests
in `tests/test_eval_retrieval.py`). It searches through
`bigfix_root_mcp.knowledge`, read-only, in about 8 seconds, and checks mechanics.
**It is not a relevance score**: every query is derived from the index itself, so
a high score is expected and says nothing about whether a person's question is
answered. That needs human-written questions (Phase 5b).

On the index built from commit `89435d3` plus the two PDFs (5,825 chunks, 1,204
examples), top-k is 5:

| Check | First result | In top 5 | Not found |
|---|---|---|---|
| Find an example by its own text (1,204) | 86.0% | 98.5% | 18 |
| Find a chunk by its own heading (5,789 distinct) | 82.1% | 97.4% | 152 |

- **No tokenisation fault.** All 18 examples missing from the top 5 are found
  within the top 25: they are ambiguity between similar examples, not
  unfindable text. Punctuation is not searchable (`"foo" = "foo"` reduces to the
  single word `foo`), so symbol-heavy relevance cannot be told apart by its
  symbols.
- **Ranking is weakest for generic headings.** Not ranked first: `type: X`
  intro sections 30.5% (124 of 407), prose headings 31.5% (170 of 539),
  inspector signatures 15.3% (743 of 4,843). A question like "what is an access
  control entry" competes with every inspector whose heading contains those
  words. A boost for an exact or leading heading match is the obvious candidate
  fix; it should be tried against real questions, not these.
- **Duplicate pages are real but small.** 32 chunk texts appear on more than one
  page (57 redundant chunks of 5,825, about 1%). The biggest group is
  `tutorial.md` = `tutorial/client/index.md` (11 chunks); the numeric reference
  pages (`integer`, `floating-point`, `large-integer`, `uinteger`) share
  several. 151 of the 5,789 heading searches (2.6%) return a list holding two
  chunks with identical text. Collapsing identical texts in a result list, with
  an `also_in` list of pages, would give those slots back; not done.

#### Phase 5a, step 4: live answer checks (deferred by the user, 2026-10-22)

Decision: skip for now; pick it up when a target is usable. The golden set
(`python -m tools.eval.golden`, 1,031 examples) could not be checked live at the
time of writing. What was available on this machine:

| Target | Kind | State |
|---|---|---|
| `local` | local qna | **Unusable**: no qna binary ("no qna binary found; pass qna_path or install the BES client"); there is no BES client here |
| `ubuntu-22-04-x86_64`, `ubi10-x86_64` | containers | **Unusable now**: the Docker client is installed but its daemon is not running |
| `web-eval-rhel` | online evaluator | Usable, but it sends each query to developer.bigfix.com, an external service |

The docs' own answers were produced on RHEL, so the online evaluator and the UBI
container are the faithful environments; the Windows host is not (many examples
read `/etc/...`). Options: start Docker Desktop and use the containers (first
use downloads the agent artifact and builds an image; no traffic to
developer.bigfix.com beyond that), or run the golden client examples (about
900) through the online evaluator at a polite rate (roughly 1 request a
second, 15 to 20 minutes). The package batches many expressions per run
(`evaluate_many_stream`). Not built yet: how documented answers should be
compared with qna output (formatting, tuples, ordering) depends on real output,
so the comparison is best written against a first run.

#### Phase 5b, step 1: collecting real questions (started 2026-10-22)

Goal: a set of questions people would really ask, each with a reference answer,
so retrieval can be judged on relevance (what Phase 5a's mechanics checks cannot).

Source: the team's own BES content (`c:\github\repository_sites`): 47 `.bes`
files, 5 byte-identical copies skipped, 37 distinct Fixlet/Task/Analysis items,
**183 candidates** (37 applicability, 146 analysis properties). All 183 parse
cleanly with the analyzer, 3 mention an inspector it lacks, 0 held a secret.
The content is all client relevance.

Confidentiality (the rules, since this is company content in a fork of a public
project): everything derived from it lives in the gitignored
`corpus/eval/questions/` (`candidates.jsonl`, `questions.jsonl`, `review.md`);
only the tooling is committed. The XML parser resolves no entities and loads no
DTD or network resource (tested against external-entity and expansion attacks),
action scripts are never read, every candidate is secret-scanned, and `summary`
prints counts, never question text.

Tooling (`tools/eval/questions.py`, tests in `tests/test_eval_questions*.py`):

    python -m tools.eval.questions extract --source DIR   # .bes -> candidates.jsonl
    python -m tools.eval.questions review                  # readable sheet: question beside its reference
    python -m tools.eval.questions summary                 # counts and problems only
    python -m tools.eval.questions update --id ID --status approved [--question TEXT]
    python -m tools.eval.questions add --question TEXT --reference LINE [--reference LINE]

Workflow: 25 draft questions were written from a spread of the candidates (10
applicability, 15 property; 10 easy, 11 medium, 4 hard by estimate), phrased as
the task and not in inspector words, so retrieval is not handed the answer. A
person reviews them against their references, approves or rewrites, and adds
questions of their own (`add`, which records its own reference and counts as
approved). Statuses: draft, approved, rejected.

Status (2026-10-22): all 25 drafts are approved by the user, with question 2
replaced by the user's own wording. More questions, especially session
relevance and tasks beyond hardware inventory, are still wanted (`add`).

Conventions for expected answers (set by the user):
- **Proxy agents are excluded by default.** Every applicability answer is expected
  to exclude proxy agent contexts, using the team's guarded form, whether or not
  the question says so. An answer that adds the check is correct even when the
  reference lacks it (questions 3, 5 and 10 have no check in their reference).
  Questions that already mention proxy agents (1, 2, 4, 6, 7, 8, 9, 23) do not
  test the default, since they state it. It applies to applicability, not to
  individual analysis properties. The Phase 4 prompt must carry this default.
- The reference of question 9 spells the property `"proxy agent context"` (no
  "in"): probably a typo in the source, so its guard never fires. Two items in
  the team's content do this.

Observations so far:
- **Team conventions are not in the public docs.** Several items share the guard
  `if exists property "in proxy agent context" then not in proxy agent context
  else true`. A model that knows only the public documentation will write the
  unguarded form, which is valid on newer clients. This is the clearest case for
  adding the team's own content as a knowledge source.
- **A reference is one valid answer, not the only one.** Scoring must therefore
  accept equivalent relevance (analyzer parse and dialect, overlap of the
  inspectors used, and a judge or a live comparison), not exact text. Not decided.
- Some descriptions are placeholders (`<enter a description ...>`), so those items
  are only as informative as their titles.
- No session-relevance questions exist yet; this content has none.

### Phase 6: Reference fixlets (planned, written 2026-10-22)

Goal: learn from a tree of existing `.bes` files, so the server can show how this
team has already solved a problem and which habits recur. Apply the same shape
as the documentation work: build an index offline, search it read-only.

**The reference corpus may be confidential.** Everything below follows from that:
it is never tracked, never put in the wheel, and kept apart from the
documentation index so the index-distribution decision (still deferred) cannot
leak it.

#### Scope

- Applicability relevance: the `Relevance` lines of a Fixlet, Task or Analysis.
- Property relevance: the `Property` clauses of an Analysis (and Global
  Properties, which use the same form). Action-script conditions are out of scope.
- Purpose of each clause comes from the file name, the title, the description and
  the action script. The server never guesses a purpose from the relevance itself.
- Dialect is `client` for all of it. The analyzer verdict (`parses`,
  `unknown_inspectors`) is stored per clause as for the documentation examples.

#### Design decisions

| # | Decision | Reason |
|---|---|---|
| 1 | A separate function, in three parts: an offline ingest step, a separate index file, a separate read-only tool. Not a parameter on `write_relevance` or `search_relevance_docs` | The result shape differs (clause, role, purpose, observed conventions), and the confidentiality differs |
| 2 | The scanner is an operator command, never a tool argument | A tool that read a path chosen by the model could be pointed at any directory by injected text |
| 3 | Separate SQLite file, `corpus/index/fixlets.sqlite`, selected by `BIGFIX_FIXLET_INDEX` | Keeps company content out of the (possibly distributable) documentation index |
| 4 | Conventions are counted, not summarized by a model | Every claim is checkable and reproducible, with counts and example ids |
| 5 | Observed conventions never override declared ones | `corpus/conventions.md` says what the team wants; the index says what the team has written, mistakes included |
| 6 | Optional everywhere: no index configured means the tool answers `available: false` and the prompt skips its step | Same degradation as the documentation index |

#### Sources: a directory or a git repository

Two source kinds, one list. The list is **untracked** (`corpus/fixlet-sources.csv`,
gitignored), because a company repository's URL or path is itself internal. A
tracked template, `docs/fixlet-sources.example.csv`, shows the columns.

`id, kind, location, branch, commit, subpath, notes`

- `kind=dir`: `location` is a local directory. It is read-only; nothing is
  written into it. Every file whose extension is `.bes` in any case (`.BES`, `.Bes`)
  is found. Symbolic links are not followed out of the root.
- `kind=git`: `location` is a repository URL. It reuses the pinned-checkout
  machinery in `tools/ingest/gitsource.py`: scheme restriction, strict validation
  of every field, protocol locking, no credential prompts, and a check that HEAD
  equals the requested commit. A blank `commit` means "the tip of `branch` now":
  the tip is resolved once at read time and recorded, so the read is still
  reproducible afterwards. URLs carrying a user name or password are rejected.
- Command line, for one-off use: `python -m tools.ingest.fixlets --dir PATH` or
  `--git URL [--branch B] [--commit SHA] [--subpath P]`. Either form can also be
  run from the list with `--source ID`.

**When it was read.** Every read is recorded in a `sources` table in the index and
printed in the report:

| Field | Directory | Git repository |
|---|---|---|
| `read_at` | UTC time the scan started | UTC time of the fetch |
| What was read | file count, and a tree fingerprint (SHA-256 over each file's relative path and content hash, in path order, so a rename changes it too) | commit SHA, the commit's own date, branch |
| Counts | parsed, skipped (with reason), unparsed | same |

The fingerprint answers "did anything change since last time" for a directory,
the way a commit SHA does for a repository. The search tool reports `read_at`
and the commit or fingerprint with its results, so an answer can be traced to a
specific read.

Private repositories: `gitsource.py` never prompts. A private repository works
only if git can authenticate non-interactively (for example a credential
helper). Credentials are never placed in the URL or the list. This is an open
point to confirm on first use.

#### Parsing

The `.bes` extraction now lives in `tools/ingest/bes.py` (`parse_bes`, `extract`,
`bes_files`), shared with the question tool, so there is one parser (step 6a).
It does not read action scripts yet; step 6c adds that. Requirements on top of it:

- XML with entity resolution and network access off. One bad file is recorded as
  skipped with its reason and never stops the run.
- Roles: `applicability` (Fixlet, Task, Analysis `Relevance`) and `property`
  (Analysis `Property`, with the property's name).
- Purpose text: file name, relative path, title, description (HTML removed), and a
  bounded excerpt of each action script.
- Each clause keeps its item type, its position in the item and a stable id.
- A per-file size limit, so a huge file cannot exhaust memory.

#### Secrets and personal data

Action scripts and descriptions often hold hostnames, paths, accounts and,
sometimes, passwords or tokens. Before anything is indexed, run the existing
`tools/ingest/scan.py` over the purpose text. A finding excludes that text (the
clause itself stays, the script excerpt does not) and is reported by file, never
by value. The scan is a net, not a guarantee, so the index stays local and untracked
regardless.

#### Index

New file and new schema, versioned like the documentation index (rebuilt, not
migrated):

- `clauses`: id, source id, relative path, item id, item type, role, property
  name, clause text, purpose text, analyzer verdict, `dialect_source`. FTS5 over
  purpose and clause text, weighted toward purpose, since the question is usually
  asked in words about what the content does.
- `sources`: the read record above.
- `conventions`: see below.

#### Learned conventions

Counted by a deterministic pass over the clauses, so each one can be checked.
Clauses are normalized (case and spacing only; string literals kept) and then
counted across files:

- Recurring clauses per role and platform. The platform comes from the item's own
  OS test, for example "Windows", "Mac", "Unix", or none.
- The guard clauses that most often open an item, in order.
- Share of items that contain a given guard, for example how many Windows
  applicability sets include the proxy-agent check.
- Property habits: how often properties are guarded, and the common shapes.

Each record stores the pattern, the number of files and the total considered, the
share, and a few example ids. A pattern is reported only above a minimum support
(default: at least 5 files and at least 10 percent of its group), so one-off
quirks are not called conventions. They are labelled `observed`, with the counts
shown.

#### Tool and prompt

- A new read-only tool, `search_reference_fixlets(query, role, platform, limit)`.
  Results carry the clause, role, purpose, analyzer verdict, `read_at` and
  provenance, and a small `conventions` list for the same role and platform.
- The text it returns is reference data, not instructions: descriptions are free
  text written by other people.
- Results are marked as company content and bounded like every other tool.
- `write_relevance` gains one step, taken only when the tool reports it is
  available: look for a similar fixlet before drafting, and treat a match as a
  starting point to adapt, run through `analyze_relevance`, and check, never as an
  answer to copy.

#### Evaluation caution

The 25 approved questions were written from this team's own BES files. If those
same files are in the reference index, retrieval would hand back the answer and
inflate any score. Phase 5b scoring with reference retrieval must hold out the
source item of the question being asked, by candidate id and file path. Report
scores with and without the hold-out only for comparison.

#### Steps

| Step | Work | Done when |
|---|---|---|
| 6a (done 2026-10-22) | Move the `.bes` parser to `tools/ingest/bes.py`; the question tool uses it | Its candidates output is byte-identical on the real source (183 candidates); the question tests pass with only their import repointed. Two fixes came with the move: `.BES` is found on case-sensitive systems, and symbolic links and Windows junctions are never followed |
| 6b (done 2026-10-22) | Source list, directory scanner with fingerprint, git sources via `gitsource.py`, read record | A directory and a repository each read from fixtures; the report shows `read_at`. `python -m tools.ingest.fixlets` (`--source`, `--dir`, `--git`); records go to the untracked `index/fixlet-reads.jsonl`; template in `docs/fixlet-sources.example.csv` |
| 6c | Scan for secrets, build the index with the analyzer verdict | Index builds from fixture `.bes` files; a planted secret is excluded and reported without its value |
| 6d | Convention counting with support thresholds | Counts match a hand-counted fixture |
| 6e | Tool, prompt step, resource note, README | The tool and the prompt step degrade cleanly with no index |
| 6f | Tests and docs | Fixtures are synthetic; nothing confidential is committed |

#### Decisions needed before 6e

1. May search results from this index go to the model the MCP client uses? The
   content is confidential, and results are returned into the model's context.
   This needs an approver's answer, not an assumption.
2. How private repositories authenticate (see above).
3. The minimum support for a convention, if the default is wrong for the corpus
   size.

#### Not in scope

- Learning from action-script conditions.
- Rewriting or "fixing" reference clauses in the index.
- Any model-generated summary stored in the index.

### Phase 7: Docs and CI
- README: the new tool, environment variables, how to build the index, the
  legal caveat.
- CI job for ingest unit tests using fixtures only (no network).
- Confirm the index and raw files never land in the wheel.

## Risks

| Risk | Mitigation |
|---|---|
| Copyright or terms-of-use breach | Legal review (decision 4), manifest license column, nothing redistributed |
| Stale or wrong guidance | Source and version on every snippet; analyzer and live run in the prompt |
| Confidential content leaking | Local-only processing, secret/PII scan, internal sources kept off the repo |
| Server complexity | Index is optional, tool degrades gracefully, ingestion is a separate tool |
| Confidential reference fixlets leaking, or inflating scores | Separate untracked index, source list untracked, secret scan before indexing, hold-out of each question's own source item (Phase 6) |
| Prompt injection via fetched pages | Snippets are returned as data; the prompt says not to follow instructions in them |

## Suggested order

Phases 0 and 1, then Phases 2 and 3 on a small slice (5 to 10 sources). Build
the Phase 5 golden set early, because it decides whether to continue.

## Resuming

1. Rebuild from scratch on any machine:
   `python -m tools.ingest.gitsource`, `python -m tools.ingest.fetch`,
   `python -m tools.ingest.build --dry-run`, then `python -m tools.ingest.build`.
   Point `BIGFIX_KNOWLEDGE_INDEX` at the result to use it from the server.
2. Continue with Phase 4 (the `write_relevance` prompt and curated resources).
   The tool it drives is `search_relevance_docs`; the index schema is in
   `tools/ingest/index.py`, and `examples` already holds the query, expected
   answers/errors, dialect and context that Phase 5 turns into a golden set.
