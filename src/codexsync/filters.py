"""Which relative paths `filters.exclude_globs` keeps out of a sync.

`**` means "any number of path segments, including none". That is what the
shipped config's comments say and what every user writing `**/tmp/**` expects,
but it is *not* what `PurePath.match` does: there `**` behaves like a single
`*`, so `**/tmp/**` matched `a/tmp/b` and matched neither `tmp/arg0/lock` (no
segment before `tmp`) nor `a/tmp/b/c` (two segments after it). On this machine
`.codex/tmp/` sits at the top of the state root, so the exclusion that was
supposed to keep lock folders out of every plan excluded nothing at all, and
the same held for `**/cache/**` and for `x.log` beside `**/*.log`.

So the translation is done here, once, as a regex:

* a `**` segment matches zero or more segments;
* `*` and `?` match within one segment and never cross a `/`;
* a pattern with no `/` at all is matched against the file name anywhere in
  the tree, which is what `*.lock` in the shipped config has always meant.

Matching stays case-sensitive, as `PurePosixPath.match` was: a rule that
quietly started matching `TMP/` would be a second change hidden inside this
one.
"""
from __future__ import annotations

from functools import lru_cache
import re

__all__ = ["PathFilter"]


def _segment_regex(segment: str) -> str:
    """One path segment's worth of glob, as a regex that cannot cross a `/`."""
    out = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = segment.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = segment[index + 1:end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    normalised = pattern.replace("\\", "/").strip()
    if "/" not in normalised:
        # `*.lock`: a name, wherever it sits.
        return re.compile(rf"(?:.*/)?{_segment_regex(normalised)}\Z")

    segments = normalised.strip("/").split("/")
    out = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            out.append("(?:.*)?" if last else "(?:[^/]+/)*")
        else:
            out.append(_segment_regex(segment) + ("" if last else "/"))
    return re.compile("".join(out) + r"\Z")


class PathFilter:
    def __init__(self, exclude_globs: list[str]) -> None:
        self._exclude_globs = exclude_globs

    def is_excluded(self, rel_path: str) -> bool:
        path = rel_path.replace("\\", "/").lstrip("/")
        return any(_compile(pattern).match(path) for pattern in self._exclude_globs if pattern)
