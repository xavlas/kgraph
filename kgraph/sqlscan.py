"""DDL extraction for PostgreSQL / Oracle / MySQL / generic SQL scripts."""
from __future__ import annotations

import re

from .backend import clean_ident, sql_tables
from .lex import match_close, skeleton, split_top

_IDENT = r"(?:[`\"\[]?[\w$#]+[`\"\]]?\.)?[`\"\[]?([\w$#]+)[`\"\]]?"
_CONSTRAINT_START = re.compile(r"(?i)^(constraint|primary\s+key|foreign\s+key|unique|check|key|index|fulltext|period|exclude)\b")


def detect_dialect(text: str) -> str:
    low = text.lower()
    if re.search(r"\bvarchar2\b|\bnumber\s*\(|\bnvarchar2\b|\bsequence\b.*\bnextval\b|\bpls_integer\b|\bbegin\b.*\bend;\s*/", low, re.S):
        return "oracle"
    if re.search(r"auto_increment|engine\s*=|\bunsigned\b|`\w+`|\btinyint\b", low):
        return "mysql"
    if re.search(r"\bserial\b|\bbigserial\b|\bjsonb\b|::\w+|\btimestamptz\b|\bplpgsql\b|\bgenerated\s+always\s+as\s+identity", low):
        return "postgresql"
    return "sql"


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _strip_comments(text: str) -> str:
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*[\s\S]*?\*/", blank, text)
    return re.sub(r"--[^\n]*", blank, text)


def _parse_column(item: str) -> dict | None:
    m = re.match(r"^[`\"\[]?([\w$#]+)[`\"\]]?\s+([\w$.]+(?:\s*\([^)]*\))?(?:\s+(?:varying|precision|with(?:out)?\s+time\s+zone))*)(.*)$", item, re.S)
    if not m:
        return None
    name, typ, rest = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip(), m.group(3)
    low = rest.lower()
    col = {"name": name.lower(), "type": typ.lower(),
           "nullable": not re.search(r"\bnot\s+null\b", low) and not re.search(r"\bprimary\s+key\b", low),
           "pk": bool(re.search(r"\bprimary\s+key\b", low)),
           "unique": bool(re.search(r"\bunique\b", low)), "fk": None, "check": None}
    rm = re.search(rf"(?i)\breferences\s+{_IDENT}\s*(?:\(\s*[`\"\[]?([\w$#]+))?", rest)
    if rm:
        col["fk"] = (rm.group(1).lower(), (rm.group(2) or "id").lower())
    ck = re.search(r"(?i)\bcheck\s*\(", rest)
    if ck:
        po = ck.end() - 1
        col["check"] = re.sub(r"\s+", " ", rest[po + 1:match_close(skeleton(rest, "'"), po, "(", ")")]).strip()
    return col


def parse_sql(rel: str, text: str) -> dict:
    clean = _strip_comments(text)
    sk = skeleton(clean, "'")
    dialect = detect_dialect(clean)
    tables: dict[str, dict] = {}
    rules: list[dict] = []
    writes_by_trigger: list[dict] = []

    for m in re.finditer(rf"(?i)\bcreate\s+(?:global\s+temporary\s+|temporary\s+|unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?{_IDENT}\s*\(", sk):
        tname = m.group(1).lower()
        po = m.end() - 1
        pc = match_close(sk, po, "(", ")")
        body = clean[po + 1:pc]
        t = tables.setdefault(tname, {"name": tname, "columns": [], "fks": [], "file": rel,
                                      "line": _line(text, m.start()), "dialect": dialect})
        t["end_line"] = _line(text, pc)
        for item in split_top(body):
            if _CONSTRAINT_START.match(item):
                _table_constraint(t, item, rel, text, m.start(), rules)
                continue
            col = _parse_column(item)
            if col:
                t["columns"].append(col)
                if col["fk"]:
                    t["fks"].append((col["name"], *col["fk"]))
                if col["check"]:
                    rules.append(_rule("db-check", tname, f"{tname}.{col['name']} must satisfy: {col['check']}",
                                       col["check"], rel, _line(text, m.start()), col["name"]))
                if col["unique"] and not col["pk"]:
                    rules.append(_rule("db-unique", tname, f"{tname}.{col['name']} must be unique", None, rel,
                                       _line(text, m.start()), col["name"]))

    for m in re.finditer(rf"(?i)\balter\s+table\s+(?:only\s+)?(?:if\s+exists\s+)?{_IDENT}\s+(.*?)(?:;|$)", sk, re.S):
        tname = m.group(1).lower()
        stmt = clean[m.start(2):m.end(2)]
        t = tables.setdefault(tname, {"name": tname, "columns": [], "fks": [], "file": rel,
                                      "line": _line(text, m.start()), "dialect": dialect, "partial": True})
        for cm in re.finditer(r"(?i)\badd\s+(?:column\s+)?(?!constraint\b|primary\b|foreign\b|unique\b|check\b)([\w`\"]+\s+[^,;]+)", stmt):
            col = _parse_column(cm.group(1))
            if col:
                t["columns"].append(col)
        for cm in re.finditer(r"(?i)\badd\s+(constraint\b.*?|primary\s+key.*?|foreign\s+key.*?|unique.*?|check.*?)(?=,\s*add\b|$)", stmt, re.S):
            _table_constraint(t, cm.group(1), rel, text, m.start(), rules)

    functions: dict[str, str] = {}
    for m in re.finditer(rf"(?is)\bcreate\s+(?:or\s+replace\s+)?(?:function|procedure)\s+{_IDENT}\s*\(.*?(\$\w*\$)(.*?)\2", clean):
        functions[m.group(1).lower()] = m.group(3)

    for m in re.finditer(rf"(?i)\bcreate\s+(?:or\s+replace\s+)?trigger\s+{_IDENT}", sk):
        end = sk.find(";", m.end())
        end = len(sk) if end < 0 else end
        if re.search(r"(?i)\bbegin\b", sk[m.end():end]):
            em = re.search(r"(?i)\bend\b\s*\w*\s*;", sk[end:])
            end = end + em.end() if em else len(sk)
        body = clean[m.start():end]
        on = re.search(rf"(?i)\bon\s+{_IDENT}", body)
        if not on:
            continue
        target = on.group(1).lower()
        ev = re.search(r"(?i)\b(before|after|instead\s+of)\s+((?:insert|update|delete)(?:\s+or\s+(?:insert|update|delete))*)", body)
        fn = re.search(rf"(?i)\bexecute\s+(?:function|procedure)\s+{_IDENT}", body)
        logic = functions.get(fn.group(1).lower(), "") if fn else re.split(r"(?i)\bbegin\b", body, 1)[-1]
        r, w = sql_tables(logic)
        w.discard(target)
        desc = f"Trigger {m.group(1).lower()} on {target}" + (f" ({ev.group(1).lower()} {ev.group(2).lower()})" if ev else "")
        rules.append(_rule("db-trigger", target, desc, None, rel, _line(text, m.start()), None,
                           also_writes=sorted(x.lower() for x in w)))
    return {"tables": tables, "rules": rules, "dialect": dialect}


def _rule(kind, table, desc, cond, rel, line, column, **extra) -> dict:
    return {"kind": kind, "table": table, "description": desc, "condition": cond, "file": rel,
            "line": line, "column": column, **extra}


def _table_constraint(t: dict, item: str, rel: str, text: str, pos: int, rules: list):
    item = re.sub(r"(?i)^constraint\s+[`\"\[]?[\w$#]+[`\"\]]?\s+", "", item.strip())
    low = item.lower()
    line = t["line"]
    fk = re.match(rf"(?is)foreign\s+key\s*\(([^)]*)\)\s*references\s+{_IDENT}\s*(?:\(([^)]*)\))?", item)
    if fk:
        cols = [clean_ident(c) for c in fk.group(1).split(",")]
        refcols = [clean_ident(c) for c in (fk.group(3) or "id").split(",")]
        for c, rc in zip(cols, refcols * len(cols)):
            t["fks"].append((c.lower(), fk.group(2).lower(), rc.lower()))
    elif low.startswith("primary key"):
        for c in re.findall(r"[\w$#]+", item[item.find("(") + 1:item.rfind(")")]):
            for col in t["columns"]:
                if col["name"] == c.lower():
                    col["pk"] = True
                    col["nullable"] = False
    elif low.startswith("unique"):
        cols = re.sub(r"\s+", " ", item[item.find("(") + 1:item.rfind(")")]).strip()
        rules.append(_rule("db-unique", t["name"], f"{t['name']}({cols}) must be unique", None, rel, line, None))
    elif low.startswith("check"):
        cond = re.sub(r"\s+", " ", item[item.find("(") + 1:item.rfind(")")]).strip()
        rules.append(_rule("db-check", t["name"], f"{t['name']} must satisfy: {cond}", cond, rel, line, None))
