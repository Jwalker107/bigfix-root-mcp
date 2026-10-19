# besapi behaviors this wrapper depends on

Notes on besapi 4.1.5 behaviors that shaped this implementation, verified
against the source. Changes we would like *upstream* live in
[besapi-proposals.md](besapi-proposals.md); this file covers what we simply
work around or rely on, so a future maintainer doesn't "simplify" a workaround
back into a bug.

## Error surfacing is uneven

Only two paths raise:

- **HTTP 403** raises `PermissionError`, and it happens inside
  `RESTResult.__init__` (`besapi.py:1358`) - so it propagates out of
  `conn.get()`/`post()` rather than being returned.
- **`login()`** calls `raise_for_status()` (`besapi.py:798`).

Every other non-2xx status comes back as a `RESTResult` with `valid=False` and
the error body in `.text`. Hence `errors.check_rest_result()` and
`clientquery._check_status()`: **do not assume a returned RESTResult means
success.**

## Connection lifecycle gotchas

| Behavior | Source | Consequence |
| --- | --- | --- |
| `__enter__ = login` with **no `__exit__`** | `besapi.py:1342` | `with BESConnection(...)` raises `AttributeError`. Never use the context-manager form. |
| `__bool__` calls `login()` | `besapi.py:613` | A casual `if conn:` can issue an HTTP request. Avoid truthiness checks on connections. |
| `__del__` calls `logout()` and clears auth | `besapi.py:608` | The connection must be kept referenced for the process lifetime; a garbage-collected connection closes its session. This is why `connection.py` caches a module-level singleton. |
| `logout()` clears cookies and closes the session but does **not** reset `last_connected` | `besapi.py:812` | After `logout()`, `login()` still short-circuits and `bool(conn)` still returns `True` against a dead session. To genuinely reconnect, construct a new `BESConnection` (`connection.reset_connection()`). |
| `__init__` performs the login round-trip | - | Constructing the object is a network call that can raise `requests.HTTPError`. Construction is therefore deferred to the first tool call, not done at server startup. |

Auth is HTTP Basic re-sent on every request, so there is no session expiry to
manage - the absence of refresh logic is fine, not an oversight.

## Method return shapes are inconsistent

Verified by reading each implementation; assuming a uniform `RESTResult` return
would break at runtime:

| Method | Returns |
| --- | --- |
| `get`/`post`/`put`/`delete` | `RESTResult` |
| `session_relevance_json` | `dict` (parsed JSON envelope) |
| `session_relevance_array` | `list[str]` |
| `get_user(name)` | `RESTResult`, or `None` when not found |
| `get_computergroup(name, site_path)` | lxml objectified element, or `None` |
| `get_dashboard_variable_value(dash, var)` | plain `str` |
| `am_i_main_operator()` | `bool`, or `None` on unexpected errors |

## Session relevance: prefer the JSON variants

- `session_relevance_json` -> `{"result": [...], "plural": bool, "type": str,
  "evaltime_ms": int}`. This is the one to use.
- `session_relevance_array` / `session_relevance_string` report **errors
  in-band as list elements**: a relevance error becomes the string
  `"ERROR: ..."` and an empty result becomes
  `"<Nothing> Nothing returned, but no error."`. Callers that don't
  string-match those sentinels silently treat failures as data.
- `session_relevance_string` additionally rewrites the caller's relevance as
  `(it as string) of ( ... )`, which changes semantics for some tuple and
  plural expressions.

Relevance **errors are returned by the server with HTTP 200** and an `error`
key in the JSON envelope, which is why `errors.check_relevance_envelope()`
exists - status-code checking alone will not catch a bad query.

This server no longer calls `session_relevance_json`: session relevance
goes through bigfix-remote-session-relevance (`sessionrelevance.py`).

`session_relevance_json` percent-encodes the relevance with
`urllib.parse.quote()` and then passes it as a form-dict value, which
percent-encodes again. That round-trips correctly on a root server **without**
a reachable BigFix Explorer, including relevance containing spaces, `+`, and
`%25`. A root server **with** a reachable Explorer forwards the query to it,
which decodes only once, so every query containing a space fails with "This
expression contained a character which is not allowed." (seen live on a
11.0.7 lab server: 16 of 17 test queries failed this way through besapi;
through the library 16 answered and the 17th, `"100%"`, got its real
%-sequence error). (Note that a literal `%` in
a relevance *string constant* must be written `%25` in relevance syntax itself
- an unescaped `%` produces the server-side error "A string constant had an
improper %-sequence", which is a relevance authoring error, not an encoding
bug.)

## Helpers that are unsafe in a server context

- `get_bes_conn_using_config_file()` **prints to stdout** (`besapi.py:407`).
  On an MCP stdio transport, stray stdout corrupts the JSON-RPC stream. This is
  the reason `connection.py` re-implements config reading instead of calling
  it. It also hardcodes `verify=False` with no override.
- `get_bes_conn_interactive()` prompts via `input()`/`getpass()` - blocks
  forever with no TTY.
- `plugin_utilities.get_besapi_connection(args)` falls back to
  `getpass.getpass()`, and on Windows reads credentials from the registry
  **before** consulting passed arguments. Neither behavior is appropriate here,
  so this server constructs `BESConnection` directly.

The general rule for this package: **no `print()` anywhere**, all logging to
stderr. There is a test enforcing it
(`tests/test_connection.py::test_source_has_no_print_calls`).

## Site path state

`set_current_site_path()` / `get_current_site_path()` maintain a mutable
"current site" on the connection, defaulting to `master`. Several methods take
an optional `site_path` and silently fall back to it - notably
`get_computergroup`, `import_bes_to_site`, and `export_site_contents`.

This is a bescli REPL convenience and is wrong for an MCP server, where tool
calls are independent and order-dependent hidden state would make results
irreproducible. This package never touches it, and `get_computer_group` is
implemented against `/api/computergroups/{site_path}` directly *because*
besapi's own `get_computergroup` routes through that state internally.

Note that `validate_site_path` requires the path to contain one of
`external/`, `custom/`, `operator/`, or to be exactly `master`, and raises
`ValueError` otherwise regardless of its `raise_error` argument.
