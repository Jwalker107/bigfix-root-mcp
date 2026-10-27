# Relevance conventions (example)

Copy this file, keep the rules that fit your team, and point
`BIGFIX_RELEVANCE_CONVENTIONS` at your copy. The `write_relevance` prompt adds its
text as instructions, so write it as instructions to the person or model writing
relevance. Keep it short: only 8,000 characters reach the prompt.

Nothing here is a BigFix requirement. These are examples of the kind of rule a
team might set, and none applies unless you keep it.

## Proxy agents

Exclude proxy agent contexts by default, unless the request says otherwise.

In applicability relevance (the relevance of a Fixlet, Task or analysis, not of an
individual analysis property), add a clause that guards the check, so clients that
lack the property are not excluded by mistake:

```
if exists property "in proxy agent context" then not in proxy agent context else true
```

The property name is `in proxy agent context`. A guard that tests
`"proxy agent context"` (without "in") never finds the property, so it never
excludes anything.

## Style

- Put each condition in its own relevance clause rather than joining them with
  `and` on one line, so a failing clause is easy to find.
- Use `exists` or `not exists` before reading a file, registry value or service,
  so a missing item makes a clause false instead of raising an error.

## When a rule does not apply

If the request says to do something different, follow the request and say which
convention you set aside.
