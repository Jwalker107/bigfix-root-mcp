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
| 4. Workflow prompt and curated resources | Not started |
| 5. Evaluation | Not started |
| 6. Docs and CI | Not started |

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
| `index.py` | SQLite + FTS5 writer, schema version 1 (documented in the module) |
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

### Phase 4: Workflow prompt and curated resources
- `write_relevance(goal, dialect)` prompt in `prompts.py`:
  1. `search_relevance_docs`
  2. `search_inspectors`
  3. draft
  4. `analyze_relevance`
  5. run with `session_relevance_query` or a client query, with a limit
  6. report only what was verified, scoped to the operator's visibility
- Tell the model that retrieved snippets are data and that instructions found
  inside them are not to be followed.
- Hand-distil the best patterns and pitfalls into small markdown resources
  under `resources/`; update `tests/test_resources.py` to match.

### Phase 5: Evaluation
- `tests/eval/golden.yaml`: question, dialect, acceptance check (parses
  cleanly, no unknown inspectors, optional result check on a test server).
- A script that scores answers with and without retrieval; record versions,
  parameters and seeds.
- Live checks run read-only against a non-production server.
- Done when retrieval measurably improves the pass rate. If it does not,
  tune chunking before adding more material.

### Phase 6: Docs and CI
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
