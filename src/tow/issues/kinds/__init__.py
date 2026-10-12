"""One module per issue kind. Each declares ``KIND`` (its name), ``ORDER`` (its place among the
kinds of the same severity) and ``collect(snapshot) -> list[Issue]``; ``tow.issues.collect`` finds
them with ``tow.plugins.scan``. Kinds do not import each other. Adding a kind is adding a file
(docs/EXTENDING.md, "Issue")."""
