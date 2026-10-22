# Relevance authoring guide

Rules and fixes distilled from the BigFix developer documentation
(developer.bigfix.com, Apache-2.0). Every `relevance` snippet below parses with
the offline analyzer. Answers quoted here are what the documentation says they
return, not something run against your server: write, check with
`analyze_relevance`, then evaluate with a bounded query.

## Rules that prevent most errors

**Prefer plurals, and test before you read.** A singular expression must find
exactly one object. None gives "non-existent object", several give "non-unique
object". A plural finds zero or more and never errors, so it is the safe way to
look for something that may not be there. This returns `False`, with no error,
for a drive that does not exist:

```relevance
exists files of folders "z:"
```

The singular `file of folder "z:"` raises an error instead.

**Test existence before you read a property.** Ask whether the object is there,
then read it, and make both branches of an `if` the same type:

```relevance
if exists regapp "besclient.exe" then version of regapp "besclient.exe" as string else "N/A"
```

**`if` and `whose` need a Boolean.** `names of files whose (version of it)` fails
because a version is not a Boolean; compare it, as in `version of it = "5"`. The
`then` and `else` branches must also agree in type, so convert with `as string`
(a version in one branch and text in the other is "Incompatible types").

**Compare singular values.** Comparing two lists, or a list to a value, is an
error even when the list has one element. Filter with `whose`, then test
existence:

```relevance
exists files whose (name of it starts with "b") of folder "c:"
```

**`it` has two homes: `<list> whose (it ...)` and `(it ...) of <object>`.** It
refers to the list immediately to its left, so parentheses decide what it means:

```relevance
(name of it, size of it) of files of folder "c:"
```

```relevance
(files of folder "c:") whose (name of it contains "a")
```

Without the parentheses around `files of folder "c:"` the second form fails.
`it` with no object to refer to gives `"It" used outside of "whose" clause`; the
fix is always `(... of it) of <object>`.

**Strings use double quotes and may not contain one.** Write a quote as `%22`
and a percent sign as `%25`. A bare `%` must be followed by two letters or
digits, or you get "improper % sequence":

```relevance
"I'm telling the %22truth%22"
```

**No curly braces.** `{...}` substitutes values in an action script; it is not
valid relevance and gives "contained a character which is not allowed".

**Keep `;` lists to one type**, as in `(1;2;3)`. A list of applications and
strings is "Incompatible types".

## Common errors

| Message | Usual cause | Fix |
| --- | --- | --- |
| Singular expression refers to non-existent object | A singular inspector found nothing (missing folder, file or property) | Test with `exists`, or use the plural |
| Singular expression refers to non-unique object | A singular inspector matched several | Use the plural, or narrow with `whose` |
| A singular expression is required | Comparing a list to a list or to a value | Filter with `whose`; compare singular values |
| "It" used outside of "whose" clause | `it` with no object to refer to | Write `(... of it) of <object>` |
| The operator "x" is not defined | A word the engine does not know, or a property that object does not have | Check the name with `search_inspectors`; check the dialect and the object |
| Operator "string" is not defined | The expression ends in an object, not a value | Ask for a property, or use `exists` |
| This expression could not be parsed | Unmatched parentheses, or reserved words (`of`, `and`, `or`) misused | Balance parentheses; reread the clause |
| A boolean expression is required | `if` or `whose` got a non-Boolean | Add a comparison or `exists` |
| Incompatible types | `then`/`else` or a `;` list of different types | Convert with `as string` |
| A string constant had no ending quotation mark | Unbalanced `"` | Balance the quotes |
| This expression contained a character which is not allowed | A stray character such as `{` | Remove it |
| No inspector context | The inspector can only be evaluated by the client itself, so qna cannot | Define it as a retrieved property and read it there |

## Applicability relevance (Fixlets and Tasks)

Each relevance clause must be true for content to apply: per the vendor
documentation, an action is not taken on a machine if any of its clauses is
false. So write each condition as its own clause, and use `exists` or
`not exists` so a missing file or key makes a clause false instead of raising an
error. Clauses that read the machine (files, registry) are client relevance.

Teams often have conventions beyond this (how proxy agents are treated, guard
idioms). The `write_relevance` prompt includes them when the server is
configured with `BIGFIX_RELEVANCE_CONVENTIONS`.

## When the analyzer disagrees

`analyze_relevance` is advisory. An `unknown-inspector` warning can mean the
documentation is newer than the analyzer's inspector snapshot (BigFix 11 Patch 7
added `idp user` and `idp group`, for example), not that the name is wrong. A parse error is
firmer: fix it, then confirm with a bounded evaluation.
