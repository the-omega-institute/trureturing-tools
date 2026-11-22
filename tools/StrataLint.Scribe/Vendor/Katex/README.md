# Vendored KaTeX

`katex.min.js` is the renderer the published site runs on. It is kept in-tree and embedded
into StrataLint.Scribe so the Blueprint markdown gate parses formulas with the real parser
instead of a hand-maintained approximation of it — offline, at a version pinned by these
bytes, and without the gate reading anything from the tree to do it.

| | |
| --- | --- |
| Upstream | https://github.com/KaTeX/KaTeX |
| Version | 0.16.22 |
| Source | `https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.js` |
| sha256 | `e8d885505949f3a5f4abdd5dd0d53696bd1371ad26ffbf4f310dcd77c8cdae89` |
| Licence | MIT, reproduced verbatim in `LICENSE` |

Only the parser is exercised: the gate calls `katex.renderToString` with
`throwOnError` and keeps the verdict, never the HTML. Fonts, stylesheets and the
browser bundle are therefore not vendored.

The scoped `content-check` validates selected Markdown formulas. Its NUL-separated
path manifest selects the documents, and its target manifest identifies the Lean
inputs associated with the report.

The bundle is a program this assembly executes, not declarative data, which is why it
lives inside the project that runs it rather than in a data residence.

When raising the KaTeX version, run the scoped gate on the Blueprint documents
that use the vendored renderer.
