"""Lightweight lexical helpers shared by the Java/Kotlin and TS/JS extractors.

The extractors do not build a full AST. Instead they work on a "skeleton" of
the source (comments and string contents blanked, same offsets) so that brace
and parenthesis matching is reliable, and read the original text at the same
offsets when they need literal values.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass


def skeleton(text: str, quotes: str = "\"'") -> str:
    out = list(text)
    n = len(text)

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    i = 0
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif text.startswith('"""', i) and '"' in quotes:
            j = text.find('"""', i + 3)
            j = n if j < 0 else j
            blank(i + 3, j)
            i = j + 3
        elif c in quotes:
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and c != "`":
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def match_close(skel: str, i: int, open_c: str = "{", close_c: str = "}") -> int:
    """Index of the bracket closing the one at skel[i] (len-1 if unbalanced)."""
    depth = 0
    for k in range(i, len(skel)):
        ch = skel[k]
        if ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                return k
    return len(skel) - 1


def flatten(skel: str, s: int = 0, e: int | None = None) -> str:
    """Copy of skel[s:e] where the content of nested `{}` blocks is blanked.

    Braces opened inside parentheses (annotation arrays, lambdas) are blanked
    entirely so they never look like statement boundaries.
    """
    e = len(skel) if e is None else e
    out = list(skel[s:e])
    bdepth = 0
    pdepth = 0
    inparen = False
    for k, ch in enumerate(out):
        if bdepth == 0:
            if ch == "(":
                pdepth += 1
            elif ch == ")":
                pdepth = max(0, pdepth - 1)
            elif ch == "{":
                bdepth = 1
                inparen = pdepth > 0
                if inparen:
                    out[k] = " "
        else:
            if ch == "{":
                bdepth += 1
            elif ch == "}":
                bdepth -= 1
                if bdepth == 0:
                    if inparen:
                        out[k] = " "
                    continue
            if ch != "\n":
                out[k] = " "
    return "".join(out)


class Src:
    def __init__(self, text: str, quotes: str = "\"'"):
        self.text = text
        self.skel = skeleton(text, quotes)
        self._ls = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self._ls, pos)


@dataclass
class Ann:
    name: str
    args: str
    pos: int
    end: int


_ANN_RE = re.compile(r"@(?:(?:field|get|set|param|property|file|delegate|receiver|setparam):)?([A-Za-z_][\w.]*)")


def parse_anns(src: Src, s: int, e: int, stop_at_paren: bool = True) -> list[Ann]:
    """Annotations/decorators found in src[s:e].

    With stop_at_paren, scanning stops at the first '(' that is not an
    annotation's argument list (i.e. the start of a method signature) so that
    parameter annotations are not attributed to the method.
    """
    res: list[Ann] = []
    i = s
    sk = src.skel
    while i < e:
        m = _ANN_RE.search(sk, i, e)
        if not m:
            break
        if stop_at_paren and "(" in sk[i:m.start()]:
            break
        j = m.end()
        k = j
        while k < e and sk[k] in " \t":
            k += 1
        args = ""
        if k < e and sk[k] == "(":
            c = match_close(sk, k, "(", ")")
            args = src.text[k + 1:c]
            j = c + 1
        res.append(Ann(m.group(1).split(".")[-1], args, m.start(), j))
        i = j
    return res


STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def split_top(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` outside (), [], {}, <> and string literals."""
    sk = skeleton(text)
    parts, depth, last = [], 0, 0
    for i, ch in enumerate(sk):
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            if ch == ">" and i > 0 and sk[i - 1] == "-":
                continue
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(text[last:i])
            last = i + 1
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def base_type(t: str) -> str:
    m = re.match(r"[\w.]+", t.strip())
    return m.group(0).split(".")[-1] if m else ""


def snake_case(name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def norm_path(p: str) -> str:
    """Normalise an URL path: strip host/query, unify path params to `{}`."""
    p = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", p.strip())
    p = p.split("?")[0].split("#")[0]
    p = re.sub(r"\{[^}]*\}", "{}", p)
    p = re.sub(r"/:[A-Za-z_]\w*", "/{}", p)
    p = re.sub(r"/+", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p if p.startswith("/") else "/" + p
