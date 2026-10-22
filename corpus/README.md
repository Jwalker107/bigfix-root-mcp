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
