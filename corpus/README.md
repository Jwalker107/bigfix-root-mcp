# corpus

Source material for the relevance knowledge index. See
[docs/knowledge-retrieval-plan.md](../docs/knowledge-retrieval-plan.md).

| Path | Tracked | Purpose |
|---|---|---|
| `MANIFEST.csv` | yes | One row per URL source: URL, license, version, dialect, SHA-256 |
| `GIT_SOURCES.csv` | yes | One row per git source: repo, branch, pinned commit, subpath, license |
| `raw/` | **no** | Downloaded originals (or `$BIGFIX_CORPUS_DIR/raw`) |
| `md/` | **no** | Converted markdown, derived |
| `index/` | **no** | Search index, derived |
| `eval/` | **no** | Golden set of examples, derived from the index (documentation text, so not committed) |

Do not commit anything under `raw/`, `md/` or `index/`. Public availability is
not a licence to redistribute; check each source's terms first. Keep internal
or confidential material out of this repository altogether.

`SCAN_ALLOW.csv` (tracked) lists known false positives from the secret scan:
`source_id,path,rule,reason`, with `*` for any path. Every row needs a reason.

Build from scratch (all from the repo root):

    python -m tools.ingest.gitsource          # git sources at their pinned commits
    python -m tools.ingest.fetch              # URL sources, hashes into MANIFEST.csv
    python -m tools.ingest.build --dry-run    # report only
    python -m tools.ingest.build              # writes index/relevance.sqlite

Add `--dry-run` to the first two to see what they would do.

`MANIFEST.csv` also lists every page of the HCL BigFix wiki (`wiki-*` ids). Those URLs are
the wiki's anonymous content endpoint, addressed by page id, and the page list comes from
its public feed; see "Source additions" in the plan for how to list it again.

`GIT_SOURCES.csv` may hold two rows for one repository at the same commit with different
subpaths and ids: the pinned `developer.bigfix.com` checkout is read for both its
relevance documentation (`site/pages/relevance`) and its action script documentation
(`site/pages/action-script`).

Measure and select, once the index is built:

    python -m tools.eval.analyze_examples     # offline analyzer pass over the examples
    python -m tools.eval.golden               # writes eval/golden.jsonl
    python -m tools.eval.retrieval            # search mechanics (not a relevance score)

Real questions (company content, so everything stays under `eval/questions/`):

    python -m tools.eval.questions extract --source DIR   # .bes files -> candidates
    python -m tools.eval.questions review                  # readable sheet to review
    python -m tools.eval.questions summary                 # counts only

Reference fixlets (company content: the sources, checkouts, read log and index are all
untracked; the steps are in `docs/reference-fixlets.md`):

    python -m tools.ingest.fixlets --source ID           # read one source, record when
    python -m tools.ingest.fixletbuild --all --dry-run    # analyse every listed source
    python -m tools.ingest.fixletbuild --all              # write index/fixlets.sqlite

The index is rebuilt, not migrated, when its schema version changes: a server
that reads a different version says so and asks for a rebuild.
