# House relevance conventions

These are this team's rules for writing relevance. Follow them unless the request
says otherwise, and say which one you set aside.

## Proxy agents

Exclude proxy agent contexts by default. The user does not want content to
evaluate in a proxy agent unless they ask for it.

In applicability relevance (a Fixlet, Task or analysis, not an individual analysis
property), add this clause as its own relevance line:

```relevance
if exists property "in proxy agent context" then not in proxy agent context else true
```

The property check protects older clients that lack it. The property is spelled
`in proxy agent context`; a guard testing `"proxy agent context"` (no "in") never
finds the property and excludes nothing.

Do this even when the request does not mention proxy agents.
