# Client relevance idioms

Common client-relevance patterns, each taken from a documented example in the
BigFix developer documentation (developer.bigfix.com, Apache-2.0). Every snippet
parses with the offline analyzer. Results depend on the machine they run on, so
none are quoted: confirm with a bounded evaluation, and prefer a lab target
(qna) or one named computer to a fleet-wide query.

Client relevance is evaluated on each agent. For data the root server already
holds, use session relevance instead (see `bigfix://relevance/session-cookbook`).

## Files and folders

Does a file exist, and what version and path is it:

```relevance
exists file "mshtml.dll" of system folder
```

```relevance
version of file "mshtml.dll" of system folder
```

```relevance
pathname of file "mshtml.dll" of system folder
```

Does any file in a folder match, and how many (filter with `whose`):

```relevance
exists files whose (name of it starts with "b") of folder "c:"
```

```relevance
number of (files whose (name of it starts with "b") of folder "c:")
```

When the file was last changed, and the first line of a text file:

```relevance
modification time of file "mshtml.dll" of system folder
```

```relevance
first line of file "C:\Users\user\Desktop\text.txt"
```

## Registry

Read a value. Use `exists` first when the key may be absent:

```relevance
value "Version" of key "HKEY_LOCAL_MACHINE\SOFTWARE\Wow6432Node\BigFix\EnterpriseClient" of registry
```

## Applications and services

```relevance
exists regapp "chrome.exe"
```

```relevance
exists service "BESClient"
```

```relevance
exists running service "BESClient"
```

## Operating system

```relevance
name of operating system
```

Which family, from properties of the same object:

```relevance
(windows of it, name of it) of operating system
```

## Versions

Compare as versions, not as strings, so `6.0` sorts below `6.44`:

```relevance
"6.0" as version < "6.44" as version
```

## Choosing a value

```relevance
if (exists packages "libxml2" of rpm) then "Yes" else "No"
```

## Text and lists

```relevance
substrings separated by " " of "a short string"
```

```relevance
concatenation ", " of ("foo"; "bar"; "baz")
```

```relevance
unique values of names of processes of sockets of network
```

## WMI (Windows)

```relevance
(selects ("ProviderName from win32_LogicalDisk")of WMI)
```

## Still to check

If a name here is not recognised on your agent version, search for it with
`search_relevance_docs` and `search_inspectors`; inspectors differ by platform
and by BigFix version.
