"""Business-rule detection on Java / Kotlin classes.

Heuristic, evidence-based: every rule carries `kind`, source location and a
`confidence` so that consumers can filter. Detected sources:

* explicit markers (`// BR-12: ...`, `@BusinessRule("...")`)  -> declared  (1.0)
* guard clauses (`if (c) throw ...`, `require(c)`, `Assert.isTrue`)  -> guard (0.9)
* missing-entity checks (`orElseThrow`, `?: throw`)             -> existence (0.8)
* bean-validation annotations on entities / DTOs               -> validation (0.9)
* authorization annotations (`@PreAuthorize`, `@Secured`)       -> authorization (0.9)
* entity lifecycle callbacks (`@PrePersist`, ...)                -> lifecycle (0.7)
* threshold / status branching (`if (amount > 1000)`)           -> decision (0.5)
"""
from __future__ import annotations

import re

from .backend import (VALIDATION_ANNS, ClassInfo, Method, ann_named, ann_strings, _kotlin_expr_end)
from .lex import STR_RE, match_close, split_top

_DECISION_COND = re.compile(
    r"[<>]=?\s*-?\d{2,}|[=!]=\s*-?\d{2,}|\.equals\(\s*\"[^\"]+\"\s*\)|[=!]=\s*\"[^\"]+\"|"
    r"\b[A-Z]\w*(?:Status|State|Type|Level)\.[A-Z_]+\b|\.compareTo\(|\.(?:isAfter|isBefore)\(")
_MARKER = re.compile(r"(?://|\*)\s*(?:@)?(?:BR|RULE|BUSINESS[ _-]?RULE)[-_ ]?([\w.-]*)\s*[:\-–]\s*(.+)", re.I)
_ASSERTS = re.compile(r"(?<![\w.])(requireNotNull|checkNotNull|require|check|Assert\s*\.\s*(?:isTrue|state|notNull|hasText|notEmpty)|"
                      r"Preconditions\s*\.\s*(?:checkArgument|checkState|checkNotNull)|Objects\s*\.\s*requireNonNull)\s*\(")
_ORELSE = re.compile(r"\.orElseThrow\s*\(\s*(?:\(\s*\)\s*->\s*)?(?:new\s+)?([A-Z]\w*)\s*\(([^)]*)\)|\?:\s*throw\s+([A-Z]\w*)\s*\(([^)]*)\)")
AUTH_ANNS = ("PreAuthorize", "PostAuthorize", "Secured", "RolesAllowed")
LIFECYCLE_ANNS = ("PrePersist", "PreUpdate", "PreRemove", "PostLoad", "PostPersist", "PostUpdate")


def _squash(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _humanize(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"Exception$|Error$", "", name)).lower()


def _first_string(s: str) -> str | None:
    m = STR_RE.search(s)
    return m.group(1) if m else None


def _message(expr: str) -> str | None:
    """Readable message of a (possibly concatenated) string expression."""
    parts = split_top(expr.strip(), "+")
    if not parts or not any(p.lstrip().startswith('"') for p in parts):
        return None
    out = []
    for p in parts:
        p = p.strip()
        m = STR_RE.fullmatch(p)
        out.append(m.group(1) if m else "{" + re.sub(r"[^\w.]+", "", p.split(".")[-1].split("(")[0]) + "}")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _throw_message(stmt: str, stmt_sk: str) -> str | None:
    t = stmt_sk.find("throw")
    if t < 0:
        return _first_string(stmt)
    po = stmt_sk.find("(", t)
    if po < 0:
        return None
    args = split_top(stmt[po + 1:match_close(stmt_sk, po, "(", ")")])
    return _message(args[0]) if args else None


def humanize_constraint(a) -> str:
    args = a.args
    msg = ann_named(args, "message")
    if msg:
        return msg.strip('"')
    n = a.name
    mn, mx = ann_named(args, "min"), ann_named(args, "max")
    val = (re.search(r"[-\d.]+", args) or [None])[0] if args.strip() else None
    return {
        "NotNull": "must not be null", "NotBlank": "must not be blank", "NotEmpty": "must not be empty",
        "Email": "must be a valid e-mail address", "Positive": "must be > 0", "PositiveOrZero": "must be >= 0",
        "Negative": "must be < 0", "NegativeOrZero": "must be <= 0", "Future": "must be in the future",
        "FutureOrPresent": "must not be in the past", "Past": "must be in the past",
        "PastOrPresent": "must not be in the future", "AssertTrue": "must be true", "AssertFalse": "must be false",
        "Min": f"must be >= {val}", "Max": f"must be <= {val}", "DecimalMin": f"must be >= {val}",
        "DecimalMax": f"must be <= {val}",
        "Size": f"size must be between {mn or 0} and {mx or '∞'}", "Length": f"length must be between {mn or 0} and {mx or '∞'}",
        "Range": f"must be between {mn or 0} and {mx or '∞'}",
        "Pattern": f"must match {ann_named(args, 'regexp') or (ann_strings(args) or ['pattern'])[0]}",
        "Digits": "must have limited digits",
    }.get(n, f"@{n}")


def _rule(kind, desc, ci, m, line, confidence, **extra) -> dict:
    return {"kind": kind, "description": _squash(desc), "owner": ci.name, "method": m.name if m else None,
            "file": ci.file, "line": line, "confidence": confidence, "condition": None, "message": None,
            "exception": None, **extra}


def _stmt_after(sk: str, text: str, j: int):
    while j < len(sk) and sk[j] in " \t\r\n":
        j += 1
    if j < len(sk) and sk[j] == "{":
        e = match_close(sk, j)
        return text[j:e + 1], sk[j:e + 1], e
    e = sk.find(";", j)
    k = _kotlin_expr_end(sk, j)
    e = k if (e < 0 or k < e) else e
    return text[j:e], sk[j:e], e


def method_rules(ci: ClassInfo, m: Method) -> list[dict]:
    out: list[dict] = []
    if m.open is None:
        return out
    sk, text = m.body_skel, m.body
    base_line = ci.src.line(m.open)

    def line_at(off):
        return base_line + text.count("\n", 0, off)

    for im in re.finditer(r"(?<![\w.])if\s*\(", sk):
        po = im.end() - 1
        pc = match_close(sk, po, "(", ")")
        cond = _squash(text[po + 1:pc])
        stmt, stmt_sk, _ = _stmt_after(sk, text, pc + 1)
        th = re.search(r"\bthrow\s+(?:new\s+)?([A-Z]\w*)\s*(?:\(|\b)", stmt_sk)
        err_ret = re.search(r"ResponseStatusException|badRequest\(|HttpStatus\.(?:BAD_REQUEST|CONFLICT|FORBIDDEN|UNPROCESSABLE)|"
                            r"Result\.failure|Either\.Left|\.error\(", stmt_sk)
        if th or err_ret:
            exc = th.group(1) if th else "error response"
            msg = _throw_message(stmt, stmt_sk) if th else _first_string(stmt)
            kind = "existence" if th and "NotFound" in exc else "guard"
            desc = f"{msg}" if msg else f"Reject ({_humanize(exc)}) when {cond}"
            out.append(_rule(kind, desc, ci, m, line_at(im.start()), 0.9,
                             condition=cond, message=msg, exception=exc, scope=cond + " " + stmt))
        elif _DECISION_COND.search(cond) and stmt.strip("{} \n"):
            out.append(_rule("decision", f"When {cond} → {_squash(stmt.strip('{} \n'), 90)}", ci, m, line_at(im.start()),
                             0.5, condition=cond, scope=cond + " " + stmt))
    for am in _ASSERTS.finditer(sk):
        po = am.end() - 1
        pc = match_close(sk, po, "(", ")")
        args = split_top(text[po + 1:pc])
        if not args:
            continue
        fn = re.sub(r"\s+", "", am.group(1))
        cond = _squash(args[0])
        msg = None
        if len(args) > 1:
            msg = _first_string(args[1])
        trailing = text[pc + 1:pc + 200]
        tm = re.match(r"\s*\{\s*\"((?:[^\"\\]|\\.)*)\"", trailing)
        if tm:
            msg = tm.group(1)
        if fn.endswith("NotNull") or fn.endswith("notNull"):
            cond = f"{cond} != null"
        out.append(_rule("guard", msg or f"Requires {cond}", ci, m, line_at(am.start()), 0.85,
                         condition=cond, message=msg, exception=fn, scope=cond))
    for om in _ORELSE.finditer(text):
        exc = om.group(1) or om.group(3)
        args = om.group(2) if om.group(1) else om.group(4)
        parts0 = split_top(args or "")
        msg = _message(parts0[0]) if parts0 else None
        before = sk[max(0, om.start() - 160):om.start()]
        subj = re.findall(r"(\w+)\s*\.\s*(?:find|get|read|load)\w*\s*\(", before)
        out.append(_rule("existence", msg or f"{_humanize(exc)}", ci, m, line_at(om.start()), 0.8,
                         exception=exc, message=msg, condition=None,
                         scope=(subj[-1] if subj else "") + " " + (args or ""), via_var=subj[-1] if subj else None))
    return out


def declared_rules(ci: ClassInfo) -> list[dict]:
    out = []
    text = ci.src.text
    spans = sorted((m.sig_start, m) for m in ci.methods if m.open is not None)
    for cm in _MARKER.finditer(text):
        pos = cm.start()
        owner = None
        for start, m in spans:
            if m.sig_start <= pos <= (m.close or m.sig_start):
                owner = m
                break
            if start > pos and start - pos < 600:
                owner = m
                break
        out.append(_rule("declared", f"{cm.group(1)+': ' if cm.group(1) else ''}{cm.group(2).strip()}", ci, owner,
                         ci.src.line(pos), 1.0, scope=cm.group(2), rule_id=cm.group(1) or None))
    for holder, m in [(ci, None)] + [(mm, mm) for mm in ci.methods]:
        for a in holder.anns:
            if a.name in ("BusinessRule", "Rule"):
                vals = ann_strings(a.args)
                out.append(_rule("declared", vals[-1] if vals else a.args, ci, m, ci.src.line(a.pos), 1.0,
                                 scope=a.args, rule_id=ann_named(a.args, "id")))
    return out


def authorization_rules(ci: ClassInfo, m: Method) -> list[dict]:
    out = []
    for holder in (ci, m):
        for a in holder.anns:
            if a.name in AUTH_ANNS:
                expr = a.args.strip().strip('"') or "authenticated"
                out.append(_rule("authorization", f"Access restricted: {_squash(expr, 100)}", ci, m, ci.src.line(a.pos),
                                 0.9, condition=expr, scope=""))
    return out


def validation_rules(ci: ClassInfo) -> list[dict]:
    out = []
    for f in ci.ctor + ci.fields:
        for a in f.anns:
            if a.name in VALIDATION_ANNS and a.name != "Valid":
                msg = ann_named(a.args, "message")
                out.append(_rule("validation", f"{ci.name}.{f.name} {humanize_constraint(a)}", ci, None, f.line, 0.9,
                                 message=msg.strip('"') if msg else None, constraint=a.name,
                                 field=f.name, scope=f.name))
    return out


def lifecycle_rules(ci: ClassInfo) -> list[dict]:
    return [_rule("lifecycle", f"{ci.name}: {a.name} callback {m.name}()", ci, m, m.line, 0.7, scope=m.body)
            for m in ci.methods for a in m.anns if a.name in LIFECYCLE_ANNS]
