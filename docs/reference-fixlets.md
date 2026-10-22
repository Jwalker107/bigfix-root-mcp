# Building the reference fixlet index

This guide builds the local index behind the `search_reference_fixlets` tool: a
searchable copy of how existing fixlets, tasks and analyses were written, from `.bes`
files in a local directory or a git repository. It also counts the habits that recur
across them.

It is **optional**. Without it the tool answers `available: false` and everything
else works. The design and its reasons are in
[knowledge-retrieval-plan.md](knowledge-retrieval-plan.md), Phase 6.

## Read this first: it is company content

The index holds your own fixlets, so treat it like your source code.

- Nothing it builds is tracked or packaged. The source list, the checkouts, the read
  log and the index are all under `corpus/` and ignored by git.
- The index lives in its own file, apart from the documentation index
  (`relevance.sqlite`), so the two can never be distributed together by mistake.
- Reading is local and read-only. A directory is never modified, and a git source is
  checked out under `corpus/raw/fixlets/`.
- Search results go into the context of the model your MCP client uses. Make sure
  that is acceptable for the content you index.
- A scan runs before anything is indexed and leaves out text that looks like a
  credential. It is a net, not a guarantee, so do not index content that must not
  leave your machine.

## What you need

- This repository checked out, with its environment set up (`uv sync`).
- `git` on your path, for git sources.
- One or more sources of `.bes` files: a directory, a repository, or both.

Run every command below from the repository root. `uv run` uses the project's
environment; drop it if that environment is already active.

## The three kinds of source used so far

| Source | Kind | Group | What it is |
| --- | --- | --- | --- |
| A working tree of your own `.bes` files | `dir` | `team` | Content you maintain |
| A backup export of a root server's custom site | `dir` | `team` | Content you already own, as exported `.bes` files |
| `https://github.com/jgstew/bigfix-content` | `git` | `reference` | A public repository, used here as the worked example of reading a git repository |

The **group** decides what is counted together. Habits are counted for each group on
its own and never pooled, so a large public repository cannot be mistaken for your
team's conventions. Put your own sources in one group (`team` here) and other
people's content in another (`reference`). A source with no group counts alone.

## Step 1: list your sources

Copy the template and edit the copy:

```
copy docs\fixlet-sources.example.csv corpus\fixlet-sources.csv
```

`corpus/fixlet-sources.csv` is ignored by git, because a company path or an internal
repository URL is itself internal. The columns:

| Column | Meaning |
| --- | --- |
| `id` | A short name you choose: letters, digits, `.`, `_`, `-` |
| `kind` | `dir` or `git` |
| `location` | A local path (`dir`) or a repository URL (`git`) |
| `branch` | `git` only. Blank means the remote's default branch |
| `commit` | `git` only. Blank means the branch tip at the moment it is read; a full 40-character SHA pins it |
| `subpath` | Optional. Only read below this folder |
| `group` | Which sources are counted together. Blank means this source alone |
| `notes` | Free text |

With the three sources above, the file looks like this (replace the placeholders):

```
id,kind,location,branch,commit,subpath,group,notes
team-fixlets,dir,<LOCAL_DIRECTORY_OF_BES_FILES>,,,,team,working tree
custom-site-backup,dir,<LOCAL_DIRECTORY_OF_EXPORTED_BES_FILES>,,,,team,backup export
jgstew-bigfix-content,git,https://github.com/jgstew/bigfix-content,,,,reference,public repository
```

Use forward slashes in Windows paths (`C:/path/to/folder`).

## Step 2: read each source and check it

Reading a source checks that it can be reached and parsed, and records when it was
read. It does not build the index. Do this once per source, and look at the counts.

Every read command takes `--dry-run`, which fetches nothing and records nothing. For a
directory a dry run still scans the files. For a git source it only asks the remote
which commit it would read.

### A. A working tree of your own `.bes` files

```
python -m tools.ingest.fixlets --source team-fixlets --dry-run
python -m tools.ingest.fixlets --source team-fixlets
```

Expected: `read`, the UTC time, a fingerprint, the number of files (identical copies
are counted once), the number not parseable, and the number of items with relevance.
`.bes`, `.BES` and `.Bes` are all found. Links and junctions are not followed.

### B. A backup export of a custom site

```
python -m tools.ingest.fixlets --source custom-site-backup --dry-run
python -m tools.ingest.fixlets --source custom-site-backup
```

An export often holds files other than `.bes`; they are ignored. If you only want one
part of a large export, set `subpath` in the list.

### C. The public repository `jgstew/bigfix-content`

This is a real public repository, and any customer can use it
(its `LICENSE` file, at the commit read here, dedicates it to the public domain). Here
it is also the **worked example of a git source**: the same row, with a private
repository's URL in place of this one, is how an internal repository is read.

```
python -m tools.ingest.fixlets --source jgstew-bigfix-content --dry-run
python -m tools.ingest.fixlets --source jgstew-bigfix-content
```

- The dry run names the commit it would read and says it "would be cloned".
- The real read clones that one commit (no history) into
  `corpus/raw/fixlets/jgstew-bigfix-content/` (about 36 MB) and prints the commit, its
  date and the counts. The clone is untracked.
- Re-running updates the checkout to the branch tip when `commit` is blank. To read
  exactly the same content again later, put the printed commit SHA in the `commit`
  column.
- A checkout you have edited is refused, never overwritten. Delete the directory to
  clone again.

A repository can check files out in a different encoding from the one it stores
(this one writes some `.reg` files as UTF-16). That is the repository's own
configuration and is not treated as a local change.

You can also read it without the list, which is useful to try a repository once:

```
python -m tools.ingest.fixlets --git https://github.com/jgstew/bigfix-content --dry-run
```

#### Using the same recipe for a private internal repository

Add a row exactly like the one above, with the private URL and `group` `team`:

```
internal-fixlets,git,https://git.example.com/<ORG>/<REPO>.git,main,,content/fixlets,team,internal repository
```

- **Authentication has to work without a prompt.** Git is run with prompts turned off,
  so a private repository is read only if git can sign in on its own: a credential
  helper for https, or an SSH key loaded in your agent.
- **Never put a password or token in the URL or the list.** A URL containing one is
  refused. A user name is accepted only for `ssh` URLs, where it is the login.
- **For ssh,** use `ssh://git@host/org/repo.git` and allow the scheme on each command,
  because only `https` is allowed by default:

  ```
  python -m tools.ingest.fixlets --source internal-fixlets --allow-scheme ssh
  ```

- Check your first read of a private repository carefully. This path has been tested
  against local repositories and the public one above, but not yet against a private
  server.

## Step 3: build the index

The index is built from **all** the sources you name at once, and rebuilt from
scratch each time, so build with the whole list:

```
python -m tools.ingest.fixletbuild --all --dry-run
python -m tools.ingest.fixletbuild --all
```

The dry run reads and analyses everything and writes nothing. Look at it before the
real build:

| Line in the report | What to check |
| --- | --- |
| `read` per source | Each source is the one you meant, with a sensible file count |
| `clauses` | How many pieces of relevance were found, split into applicability and property |
| `parse cleanly with the analyzer` | Normally close to all of them |
| `mention an inspector the analyzer lacks` | A few is normal (newer inspectors than the analyzer knows) |
| `left out for a credential` | Anything here names the file, the field and the rule, never the text. Read it. |
| `warning kept` | Credential-like text that was kept, for example `password = value` in a script. Review it. |

To build from a subset of the list, repeat `--source`:

```
python -m tools.ingest.fixletbuild --source team-fixlets --source custom-site-backup
```

To see the counted habits as the build finishes, add `--show-conventions 5`. That
prints company content to your terminal, so only use it where that is fine.

For reference, the first real build on 2026-10-22 read three sources (47, 580 and
1,112 files) and produced 8,917 clauses, all of which parsed, with nothing blocked.
Your numbers will differ.

### If something in a clause looks like a credential

- A credential in the relevance itself: the whole clause is left out.
- A credential in a title, description or action script: only that field is left out,
  and the result later says `left_out`.
- A false positive can be allowed in `corpus/SCAN_ALLOW.csv` (columns `source_id`,
  `path`, `rule`, `reason`; `*` matches any path). A reason is required. This file is
  shared with the documentation build.

## Step 4: check the result

The file is `corpus/index/fixlets.sqlite`. A quick look at what is in it:

```
python -c "import sqlite3; c = sqlite3.connect('corpus/index/fixlets.sqlite'); print(list(c.execute('select source_id, source_group, read_at, files, items from reads')))"
```

You should see one row per source, each with its read time.

## Step 5: point the server at it

Set `BIGFIX_FIXLET_INDEX` to the full path of that file in the environment of the
MCP server entry, then restart or reconnect the server. For Claude Code, that is the
`env` block of the server's entry:

```
"env": {
  "BIGFIX_FIXLET_INDEX": "C:/path/to/bigfix-root-mcp/corpus/index/fixlets.sqlite"
}
```

The file is read on every call, so a rebuilt index is picked up without another
restart. Then ask for something the sources cover, for example "find the fixlet that
adds an antivirus exclusion". The answer should have `available: true`, results with
a group and a read time, and `conventions` for the group.

If it answers `available: false`, the `reason` says why. The common causes are in
the table at the end.

## Keeping it current

- Re-run steps 2 and 3 when the content changes. `--all` rebuilds everything, so
  nothing is left stale.
- Every real read appends a record to `corpus/index/fixlet-reads.jsonl`: when it was
  read (UTC), the commit and its date (git), a fingerprint of the files, and counts. A
  directory has no commit, so its fingerprint is how you tell whether it changed: it
  covers each file's path and content, so a rename or an edit changes it.
- To make a git source reproducible, set its `commit`. To follow the branch, leave it
  blank and the commit read is recorded each time.
- The schema is versioned. If the server and the index disagree, the tool reports the
  versions and says to rebuild; it never guesses.

## For evaluations

`BIGFIX_FIXLET_HOLDOUT` takes a comma-separated list of item ids and removes those
items from every search, so a question's own source cannot be returned as its answer.
It is read from the environment, never from a tool argument. A malformed list makes
the index unavailable rather than being ignored. The ids are the `id` field in
results, and the same ids the question collector writes.

## When something goes wrong

| Message or symptom | Cause | Fix |
| --- | --- | --- |
| `... is not a directory` | The path in the list is wrong, or the drive is not available | Correct `location` |
| `checkout has local modifications; refusing to touch it` | Someone edited a file in `corpus/raw/fixlets/<id>/` | Delete that directory and read again |
| `URL scheme 'ssh' is not allowed` | Only `https` is allowed by default | Add `--allow-scheme ssh` |
| A git read fails with an authentication error | Git could not sign in without a prompt | Set up a credential helper or an SSH agent; never put a secret in the URL |
| `a password in the URL is not accepted` | The URL contains a password or token | Remove it and use a credential helper |
| `not built: a source could not be read` | One source failed; no index is written | Fix that source, or leave it out with `--source` |
| `nothing to index` | No `.bes` content with relevance was found | Check the path and `subpath` |
| `available: false`, "no reference fixlet index is configured" | `BIGFIX_FIXLET_INDEX` is not set in the server's environment | Step 5 |
| `available: false`, "no index file at ..." | The path in the variable is wrong | Correct it |
| `available: false`, "index schema version ..." | The index was built by a different version | Rebuild with `--all` |
| `available: false`, "BIGFIX_FIXLET_HOLDOUT ..." | The hold-out list is malformed | Fix or unset it |
| Results for the wrong platform | The platform is a heuristic from the item's own operating-system test | Use the `platform` filter, which keeps unclear items too |
| Habits look like another team's | A public source shares a group with your own | Give each its own `group` and rebuild |

## Where everything lives

| Path | Tracked? | Purpose |
| --- | --- | --- |
| `docs/fixlet-sources.example.csv` | yes | The template |
| `corpus/fixlet-sources.csv` | no | Your sources |
| `corpus/raw/fixlets/<id>/` | no | Checkouts of git sources |
| `corpus/index/fixlets.sqlite` | no | The index |
| `corpus/index/fixlet-reads.jsonl` | no | The record of every read |
| `corpus/SCAN_ALLOW.csv` | yes | Allowed scan false positives (no secrets) |
