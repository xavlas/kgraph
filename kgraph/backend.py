"""Java / Kotlin backend extraction (Spring MVC, Spring Data/JPA, messaging)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .lex import (Ann, Src, STR_RE, base_type, flatten, match_close, parse_anns,
                  snake_case, split_top, norm_path)
from .ontology import WRITE_PREFIXES

KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized", "super",
            "this", "when", "try", "else", "throw", "fun", "assert", "do", "in", "is", "as"}
MODIFIERS = {"private", "public", "protected", "static", "final", "transient", "volatile",
             "abstract", "default", "synchronized", "override", "open", "internal", "lateinit",
             "const", "vararg", "suspend", "inline", "native"}


@dataclass
class Field:
    name: str
    type: str
    type_raw: str
    anns: list[Ann]
    line: int
    is_prop: bool = True
    static: bool = False

    def ann(self, *names) -> Ann | None:
        return next((a for a in self.anns if a.name in names), None)


@dataclass
class Method:
    name: str
    line: int
    anns: list[Ann]
    params: str
    sig_start: int
    open: int | None
    close: int | None
    is_ctor: bool = False
    body: str = ""
    body_skel: str = ""
    calls: set = field(default_factory=set)          # {(type, method)}
    sql: list = field(default_factory=list)          # [(reads, writes, native)]
    publishes: set = field(default_factory=set)      # {topic}
    param_list: list = field(default_factory=list)   # [Field]

    def ann(self, *names) -> Ann | None:
        return next((a for a in self.anns if a.name in names), None)


@dataclass
class ClassInfo:
    name: str
    pkg: str
    kind: str
    file: str
    line: int
    lang: str
    anns: list[Ann]
    bases: list[tuple[str, str]]
    src: Src
    ctor: list[Field] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    methods: list[Method] = field(default_factory=list)
    body_span: tuple[int, int] | None = None
    role: str | None = None
    deps: dict = field(default_factory=dict)         # variable -> type

    @property
    def fqn(self):
        return f"{self.pkg}.{self.name}" if self.pkg else self.name

    def ann(self, *names) -> Ann | None:
        return next((a for a in self.anns if a.name in names), None)

    def has_ann(self, *names) -> bool:
        return self.ann(*names) is not None

    def base_names(self):
        return [b for b, _ in self.bases]


# ---------------------------------------------------------------- annotation values
def ann_strings(args: str) -> list[str]:
    return STR_RE.findall(args)


_STRING = r'"(?:[^"\\]|\\.)*"'
_BRACE = r'\{(?:%s|[^}"])*\}' % _STRING     # string-aware: braces inside "/{id}" do not end the array
_BRACK = r'\[(?:%s|[^\]"])*\]' % _STRING


def ann_named(args: str, *names: str) -> str | None:
    for n in names:
        m = re.search(r"\b%s\s*=\s*(%s|%s|%s|[\w.]+)" % (n, _BRACE, _BRACK, _STRING), args)
        if m:
            return m.group(1)
    return None


def ann_values(args: str, *names: str, consts: dict | None = None) -> list[str]:
    """String values of a (value|path|...) attribute or the first positional argument."""
    raw = ann_named(args, *names)
    if raw is None:
        a = args.strip()
        if not a or re.match(r"\w+\s*=", a):
            return []
        raw = split_top(a)[0]
    vals = STR_RE.findall(raw)
    if vals or not consts:
        return vals
    out = []
    for ident in re.findall(r"[A-Za-z_][\w.]*", raw):
        v = consts.get(ident.split(".")[-1])
        if v is not None:
            out.append(v)
    return out


# ---------------------------------------------------------------- parsing
_CLASS_RE = re.compile(
    r"(?<![\w.:@])(?:(?:public|private|protected|open|abstract|final|data|sealed|internal|static|inline|value|enum|annotation|fun)\s+)*"
    r"(class|interface|object|record)\s+([A-Za-z_]\w*)")
_FUN_RE = re.compile(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.<>?]+\.)?(\w+)\s*\(")
_PROP_RE = re.compile(r"\b(val|var)\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(\w+)\s*(?::\s*([\w.<>?,\s*]+?))?\s*(?==|\n|$|\bby\b|\bget\b|\bset\b)")
_DECL_CUT = re.compile(r"\n\s*(?:@|(?:(?:public|private|protected|open|abstract|final|data|sealed|internal|enum)\s+)*"
                       r"(?:class|interface|object|fun|val|var|typealias)\b)")


def _skip_angle(flat: str, p: int) -> int:
    while p < len(flat) and flat[p] in " \t\n":
        p += 1
    if p < len(flat) and flat[p] == "<":
        depth = 0
        for k in range(p, len(flat)):
            if flat[k] == "<":
                depth += 1
            elif flat[k] == ">":
                depth -= 1
                if depth == 0:
                    return k + 1
    return p


def _parse_bases(heritage: str) -> list[tuple[str, str]]:
    h = re.sub(r"\b(extends|implements|permits)\b", ",", heritage)
    h = h.replace(":", ",", 1) if ":" in h else h
    out = []
    for part in split_top(h):
        m = re.match(r"([\w.]+)\s*(<.*>)?", part.strip(), re.S)
        if m and m.group(1) not in KEYWORDS:
            out.append((m.group(1).split(".")[-1], m.group(2) or ""))
    return out


def _strip_anns(src: Src, anns: list[Ann], s: int, e: int) -> str:
    chars = list(src.skel[s:e])
    for a in anns:
        for k in range(max(a.pos, s), min(a.end, e)):
            chars[k - s] = " "
    return "".join(chars)


def _field_from_decl(src: Src, s: int, e: int, is_prop=True, kotlin=False) -> Field | None:
    """Parse a `Type name` (Java) or `[val|var] name: Type` (Kotlin) declaration."""
    anns = parse_anns(src, s, e, stop_at_paren=False)
    sig = _strip_anns(src, anns, s, e)
    sig = sig.split("=")[0] if "=" in sig else sig
    sig = re.sub(r"\s+", " ", sig).strip()
    if not sig:
        return None
    static = bool(re.search(r"\bstatic\b|\bconst\b", sig))
    if kotlin:
        m = re.search(r"(?:\b(val|var)\s+)?(\w+)\s*:\s*(.+)$", sig)
        if not m:
            return None
        name, traw, prop = m.group(2), m.group(3).strip(), bool(m.group(1))
    else:
        toks = [t for t in re.split(r"\s+(?![^<]*>)", sig) if t and t not in MODIFIERS]
        if len(toks) < 2:
            return None
        name, traw, prop = toks[-1], " ".join(toks[:-1]), True
        if not re.match(r"^\w+$", name):
            return None
    return Field(name, base_type(traw.replace("?", "")), traw, anns, src.line(s), prop and is_prop, static)


def _kotlin_expr_end(skel: str, start: int) -> int:
    depth, i = 0, start
    while i < len(skel):
        ch = skel[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif ch == "\n" and depth == 0:
            nxt = skel[i + 1:].lstrip()
            if not nxt or not re.match(r"(\.|\?\.|\?:|&&|\|\||\+|\*|/|\)|then\b|else\b)", nxt):
                return i
        i += 1
    return len(skel)


def _parse_params(params: str, kotlin: bool) -> list[Field]:
    src = Src(params)
    out = []
    for part in split_top(params):
        off = params.find(part)
        f = _field_from_decl(src, off, off + len(part), kotlin=kotlin)
        if f:
            out.append(f)
    return out


def _java_members(src: Src, ci: ClassInfo, s: int, e: int):
    sk = src.skel
    flat = flatten(sk, s, e)
    seg_start, i, paren = 0, 0, 0
    segs = []
    while i < len(flat):
        ch = flat[i]
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif paren == 0 and ch == ";":
            segs.append((seg_start, i, None))
            seg_start = i + 1
        elif paren == 0 and ch == "{":
            close = match_close(sk, s + i)
            segs.append((seg_start, i, (s + i, close)))
            i = close - s
            seg_start = i + 1
        i += 1
    for a, b, block in segs:
        sa, sb = s + a, s + b
        anns = parse_anns(src, sa, sb)
        sig = _strip_anns(src, anns, sa, sb)
        q = sig.find("(")
        if q >= 0:
            before = sig[:q]
            if "=" in before:
                continue
            names = re.findall(r"[\w$]+", before)
            if not names or names[-1] in KEYWORDS:
                continue
            name = names[-1]
            pclose = match_close(sk, sa + q, "(", ")")
            params = src.text[sa + q + 1:pclose]
            is_ctor = len(names) == 1 or all(n in MODIFIERS for n in names[:-1])
            m = Method(name, src.line(sa + before.rfind(name)), anns, params, sa,
                       block[0] if block else None, block[1] if block else None, is_ctor)
            m.param_list = _parse_params(params, kotlin=False)
            ci.methods.append(m)
        else:
            f = _field_from_decl(src, sa, sb)
            if f and "(" not in f.type_raw and not f.static:
                ci.fields.append(f)


def _kotlin_members(src: Src, ci: ClassInfo, s: int, e: int):
    sk = src.skel
    flat = flatten(sk, s, e)
    decls = []   # (start, kind, match)
    param_spans = []
    for m in _FUN_RE.finditer(flat):
        decls.append((s + m.start(), "fun", m))
        pc = match_close(sk, s + m.end() - 1, "(", ")")
        param_spans.append((s + m.end() - 1, pc))
    for m in _PROP_RE.finditer(flat):
        if any(a <= s + m.start() <= b for a, b in param_spans):
            continue
        decls.append((s + m.start(), "prop", m))
    decls.sort(key=lambda d: d[0])
    all_anns = [a for a in parse_anns(src, s, e, stop_at_paren=False)
                if flat[a.pos - s] == "@" and not any(x <= a.pos <= y for x, y in param_spans)]
    for idx, (pos, kind, m) in enumerate(decls):
        prev_pos = decls[idx - 1][0] if idx else s
        anns = [a for a in all_anns if a.pos >= prev_pos and a.end <= pos + 1 and
                (idx == 0 or a.pos >= decls[idx - 1][0]) and
                not any(d[0] < a.pos < pos for d in decls[:idx])]
        if kind == "fun":
            name = m.group(1)
            pstart = s + m.end() - 1
            pclose = match_close(sk, pstart, "(", ")")
            params = src.text[pstart + 1:pclose]
            j = pclose + 1
            while j < e and sk[j] not in "{=\n":
                j += 1
            open_i = close_i = None
            if j < e and sk[j] == "{":
                open_i, close_i = j, match_close(sk, j)
            elif j < e and sk[j] == "=":
                open_i, close_i = j, _kotlin_expr_end(sk, j + 1)
            mt = Method(name, src.line(pos), anns, params, pos, open_i, close_i)
            mt.param_list = _parse_params(params, kotlin=True)
            ci.methods.append(mt)
        else:
            traw = (m.group(3) or "").strip()
            ci.fields.append(Field(m.group(2), base_type(traw.replace("?", "")), traw, anns,
                                   src.line(pos), True, False))


def parse_jvm_file(rel: str, text: str) -> list[ClassInfo]:
    kotlin = rel.endswith(".kt")
    src = Src(text)
    sk = src.skel
    pm = re.search(r"^\s*package\s+([\w.]+)", sk, re.M)
    pkg = pm.group(1) if pm else ""
    flat = flatten(sk)
    flat = re.sub(r"(?m)^[ \t]*(?:import|package)\b[^\n;]*;?", lambda m: " " * len(m.group()), flat)
    out: list[ClassInfo] = []
    prev_end = 0
    for m in _CLASS_RE.finditer(flat):
        if m.start() < prev_end:
            continue
        kind, name = m.group(1), m.group(2)
        mods = flat[m.start():m.start(1)]
        p = _skip_angle(flat, m.end())
        ctor_text = ""
        if p < len(flat) and flat[p] == "(":
            pc = match_close(sk, p, "(", ")")
            ctor_text = text[p + 1:pc]
            p = pc + 1
        b = flat.find("{", p)
        head_end = len(flat) if b < 0 else b
        cut = _DECL_CUT.search(flat, p, head_end)
        has_body = b >= 0
        if cut:
            head_end, has_body = cut.start(), False
        elif ";" in flat[p:head_end]:
            head_end, has_body = flat.index(";", p), False
        heritage = text[p:head_end]
        hdr_start = max(prev_end, flat.rfind(";", 0, m.start()) + 1, flat.rfind("}", 0, m.start()) + 1)
        anns = parse_anns(src, hdr_start, m.start(), stop_at_paren=False)
        ci = ClassInfo(name, pkg, kind, rel, src.line(m.start(1)), "kotlin" if kotlin else "java",
                       anns, _parse_bases(heritage), src)
        if ctor_text:
            ci.ctor = _parse_params(ctor_text, kotlin or kind == "record")
        if has_body:
            bs, be = b + 1, match_close(sk, b)
            ci.body_span = (bs, be)
            prev_end = be + 1
            (_kotlin_members if kotlin else _java_members)(src, ci, bs, be)
        else:
            prev_end = head_end
        if "enum" in mods.split():
            continue
        # container variable -> type map (constructor injection / fields / lombok)
        for f in ci.ctor + ci.fields:
            if f.type and f.name not in ci.deps:
                ci.deps[f.name] = f.type
        for mt in ci.methods:
            if mt.is_ctor or (kotlin and mt.name == "constructor"):
                for f in mt.param_list:
                    ci.deps.setdefault(f.name, f.type)
        out.append(ci)
    return out


# ---------------------------------------------------------------- method analysis
_CALL_RE = re.compile(r"(?<![\w.])(?:this\s*\.\s*)?(\w+)\s*(?:\?\.|!!\.|\.)\s*(\w+)\s*\(")
_LOCAL_CALL_RE = re.compile(r"(?<![\w.])(?:this\s*\.\s*)?(\w+)\s*\(")
_STATIC_RE = re.compile(r"(?<![\w.])([A-Z]\w*)\s*\.\s*(\w+)\s*\(")
_LITERALS = re.compile(r'(?:"""[\s\S]*?"""|"(?:[^"\\\n]|\\.)*")(?:\s*\+?\s*(?:"""[\s\S]*?"""|"(?:[^"\\\n]|\\.)*"))*')
_LOCAL_DECL_RE = re.compile(r"(?<![\w.])(?:([A-Z]\w*)(?:<[^;=()]*?>)?\s+(\w+)\s*(?=[=;])|(?:val|var)\s+(\w+)\s*:\s*([A-Z]\w*))")
_PUBLISH_RE = re.compile(r"\b(\w+)\s*\.\s*(send\w*|convertAndSend\w*|publish\w*|produce|emit\w*)\s*\(\s*(\"(?:[^\"\\]|\\.)*\"|[\w.]+)")
_PUBLISHER_NAME = re.compile(r"template|producer|publisher|bridge|sender|kafka|rabbit|jms|sns|sqs|emitter|channel", re.I)
_SQL_START = re.compile(r"^\s*(select|insert|update|delete|merge|with|truncate)\b", re.I)
_IDENT = r"[`\"\[]?[\w$]+[`\"\]]?(?:\.[`\"\[]?[\w$]+[`\"\]]?)?"


def clean_ident(s: str) -> str:
    return s.strip('`"[]').split(".")[-1].strip('`"[]')


def sql_tables(sql: str) -> tuple[set, set]:
    """(read tables, written tables) of a SQL/JPQL string."""
    s = re.sub(r"\s+", " ", sql)
    writes = {clean_ident(x) for x in re.findall(rf"(?i)\binsert\s+(?:into\s+)?({_IDENT})", s)}
    writes |= {clean_ident(x) for x in re.findall(rf"(?i)\bupdate\s+({_IDENT})\s+(?:\w+\s+)?set\b", s)}
    writes |= {clean_ident(x) for x in re.findall(rf"(?i)\bdelete\s+(?:from\s+)?({_IDENT})", s)}
    writes |= {clean_ident(x) for x in re.findall(rf"(?i)\bmerge\s+into\s+({_IDENT})", s)}
    writes |= {clean_ident(x) for x in re.findall(rf"(?i)\btruncate\s+(?:table\s+)?({_IDENT})", s)}
    reads = {clean_ident(x) for x in re.findall(rf"(?i)\b(?:from|join)\s+({_IDENT})", s)}
    reads -= {"select", "dual", "set", "values"}
    if re.search(r"(?i)^\s*(insert|update|delete|merge)\b", s):
        reads = {r for r in reads if r not in writes} if re.search(r"(?i)\bselect\b", s) else set()
    return reads, writes


def sql_literals(text: str) -> list[str]:
    res = []
    for m in _LITERALS.finditer(text):
        joined = " ".join(STR_RE.findall(m.group(0))) or m.group(0).strip('"')
        if _SQL_START.match(joined) and (not re.match(r"(?i)\s*select", joined) or re.search(r"(?i)\bfrom\b", joined)):
            res.append(joined)
    return res


def analyze_methods(ci: ClassInfo, consts: dict, config: dict):
    names = {m.name for m in ci.methods if not m.is_ctor}
    for m in ci.methods:
        if m.open is None:
            continue
        m.body = ci.src.text[m.open:m.close + 1]
        m.body_skel = ci.src.skel[m.open:m.close + 1]
        sk = m.body_skel
        types = dict(ci.deps)
        types.update({f.name: f.type for f in m.param_list if f.type})
        for lm in _LOCAL_DECL_RE.finditer(sk):
            types[lm.group(2) or lm.group(3)] = lm.group(1) or lm.group(4)
        for cm in _CALL_RE.finditer(sk):
            var, meth = cm.group(1), cm.group(2)
            if var in types:
                m.calls.add((types[var], meth))
        for cm in _STATIC_RE.finditer(sk):
            m.calls.add((cm.group(1), cm.group(2)))
        for cm in _LOCAL_CALL_RE.finditer(sk):
            if cm.group(1) in names and cm.group(1) not in KEYWORDS:
                m.calls.add((ci.name, cm.group(1)))
        for sql in sql_literals(m.body):
            r, w = sql_tables(sql)
            m.sql.append((r, w, True))
        for pm in _PUBLISH_RE.finditer(m.body):
            if not _PUBLISHER_NAME.search(pm.group(1)):
                continue
            t = pm.group(3)
            val = t.strip('"') if t.startswith('"') else consts.get(t.split(".")[-1])
            if val:
                m.publishes.add(resolve_placeholders(val, config))


_PLACEHOLDER = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def resolve_placeholders(s: str, config: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: config.get(m.group(1).strip(), m.group(2) if m.group(2) is not None else m.group(1)), s)


# ---------------------------------------------------------------- config & constants
def flatten_yaml(text: str) -> dict:
    res, stack = {}, []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([\w.\-\"']+)\s*:\s*(.*)$", line)
        if not m:
            continue
        indent, key = len(m.group(1)), m.group(2).strip("\"'")
        val = m.group(3).split(" #")[0].strip().strip("\"'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if val:
            res[".".join(k for _, k in stack)] = val
    return res


def parse_properties(text: str) -> dict:
    res = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "!")) and "=" in line:
            k, v = line.split("=", 1)
            res[k.strip()] = v.strip()
    return res


_CONST_RES = [
    re.compile(r"(?:static\s+final|final\s+static)\s+String\s+(\w+)\s*=\s*\"([^\"]*)\""),
    re.compile(r"\bconst\s+val\s+(\w+)\s*(?::\s*String)?\s*=\s*\"([^\"]*)\""),
    re.compile(r"\bval\s+([A-Z][A-Z0-9_]*)\s*(?::\s*String)?\s*=\s*\"([^\"]*)\""),
]


def collect_consts(text: str) -> dict:
    res = {}
    for rx in _CONST_RES:
        for m in rx.finditer(text):
            res[m.group(1)] = m.group(2)
    return res


# ---------------------------------------------------------------- role classification
_REPO_BASE = re.compile(r"Repository|Executor|Dao$", re.I)
CONTROLLER_ANNS = ("RestController", "Controller", "Path")
HTTP_ANN = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
            "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
            "GET": "GET", "POST": "POST", "PUT": "PUT", "DELETE": "DELETE", "PATCH": "PATCH"}
LISTENER_ANNS = ("KafkaListener", "RabbitListener", "JmsListener", "SqsListener", "StreamListener")


def classify(ci: ClassInfo) -> str | None:
    if ci.has_ann("Entity", "MappedSuperclass") or (ci.has_ann("Table") and ci.kind == "class"):
        return "entity"
    if ci.has_ann(*CONTROLLER_ANNS) and ci.kind != "interface":
        return "controller"
    heritage = " ".join(ci.base_names())
    if ci.kind == "interface" and (_REPO_BASE.search(heritage) or ci.has_ann("Repository")):
        return "repository"
    if ci.has_ann("Repository"):
        return "repository"
    if ci.has_ann("Service", "Component") or re.search(r"(Service|UseCase|Facade|Handler|Manager)$", ci.name) \
            and ci.kind != "interface" and not ci.has_ann("Configuration"):
        return "service"
    if any(m.ann(*LISTENER_ANNS) for m in ci.methods):
        return "service"
    return None


def join_path(base: str, sub: str) -> str:
    parts = [p for p in (base + "/" + sub).split("/") if p]
    return "/" + "/".join(parts)


def endpoints(ci: ClassInfo, consts: dict, config: dict, prefix: str = "") -> list[dict]:
    """HTTP endpoints exposed by a controller class."""
    base_ann = ci.ann("RequestMapping", "Path")
    bases = ann_values(base_ann.args, "value", "path", consts=consts) if base_ann else []
    bases = [resolve_placeholders(b, config) for b in bases] or [""]
    eps = []
    for m in ci.methods:
        verbs, paths = [], []
        for a in m.anns:
            if a.name in HTTP_ANN:
                verbs.append(HTTP_ANN[a.name])
                if a.name.endswith("Mapping"):
                    paths += ann_values(a.args, "value", "path", consts=consts)
            elif a.name == "RequestMapping":
                vs = re.findall(r"RequestMethod\.(\w+)|\b(GET|POST|PUT|DELETE|PATCH)\b", a.args)
                verbs += [x or y for x, y in vs] or ["ANY"]
                paths += ann_values(a.args, "value", "path", consts=consts)
            elif a.name == "Path":
                paths += ann_values(a.args, "value", consts=consts)
        if not verbs:
            continue
        for verb in dict.fromkeys(verbs):
            for p in dict.fromkeys(paths or [""]):
                for b in bases:
                    full = join_path(prefix, join_path(b, resolve_placeholders(p, config)))
                    eps.append({"verb": verb, "path": full, "norm": norm_path(full),
                                "method": m, "line": m.line})
    return eps


def listener_topics(m: Method, consts: dict, config: dict) -> list[str]:
    a = m.ann(*LISTENER_ANNS)
    if not a:
        return []
    vals = ann_values(a.args, "topics", "queues", "destination", "value", "topicPattern", "queuesToDeclare",
                      consts=consts)
    return [resolve_placeholders(v, config) for v in vals]


def classify_method(name: str, m: Method | None = None, sql=None) -> str:
    """'R', 'W' for a repository/DAO method."""
    if m and m.ann("Modifying"):
        return "W"
    if sql:
        r, w = sql
        if w:
            return "W"
        if r:
            return "R"
    low = name.lower()
    if low.startswith(WRITE_PREFIXES):
        return "W"
    return "R"


def derived_query_fields(name: str) -> list[str]:
    m = re.match(r"^(?:find|read|get|query|stream|count|exists|delete|remove)\w*?By([A-Z]\w*)$", name)
    if not m:
        return []
    body = re.sub(r"(OrderBy\w+)$", "", m.group(1))
    out = []
    for part in re.split(r"And|Or", body):
        part = re.sub(r"(IgnoreCase|Between|LessThan|GreaterThan|IsNull|IsNotNull|NotNull|In|Not|Like|Containing|"
                      r"StartingWith|EndingWith|After|Before|True|False|Equals?)+$", "", part)
        if part:
            out.append(part[0].lower() + part[1:])
    return out


def entity_info(ci: ClassInfo, registry: dict) -> dict:
    ta = ci.ann("Table")
    ea = ci.ann("Entity")
    table = None
    schema = None
    if ta:
        t = ann_named(ta.args, "name")
        table = t.strip('"') if t else (STR_RE.findall(ta.args) or [None])[0]
        s = ann_named(ta.args, "schema")
        schema = s.strip('"') if s else None
    ename = ci.name
    if ea:
        n = ann_named(ea.args, "name") or (ea.args.strip() if ea.args.strip().startswith('"') else None)
        if n:
            ename = n.strip('"')
    cols = []
    seen = set()
    chain = [ci]
    for b in ci.base_names():
        p = registry.get(b)
        if p and p.has_ann("MappedSuperclass"):
            chain.append(p)
    for c in chain:
        for f in c.ctor + c.fields:
            if not f.is_prop or f.static or f.name in seen or f.ann("Transient"):
                continue
            seen.add(f.name)
            col_a = f.ann("Column", "JoinColumn")
            cname = None
            if col_a:
                v = ann_named(col_a.args, "name")
                cname = v.strip('"') if v else None
            rel = f.ann("ManyToOne", "OneToOne", "OneToMany", "ManyToMany", "Embedded")
            target = None
            if rel:
                g = re.search(r"<\s*([\w.]+)", f.type_raw)
                target = base_type(g.group(1)) if g else f.type
            if rel and rel.name in ("OneToMany", "ManyToMany"):
                cname = None
            elif not cname:
                cname = snake_case(f.name) + ("_id" if rel and rel.name in ("ManyToOne", "OneToOne") else "")
            nullable = not (f.ann("NotNull", "NotBlank", "NotEmpty") or
                            (col_a and re.search(r"nullable\s*=\s*false", col_a.args)) or f.ann("Id"))
            cols.append({"field": f.name, "column": cname, "type": f.type_raw, "pk": bool(f.ann("Id", "EmbeddedId")),
                         "nullable": nullable, "relation": rel.name if rel else None, "target": target,
                         "constraints": [a.name for a in f.anns if a.name in VALIDATION_ANNS]})
    return {"table": (table or snake_case(ci.name)), "schema": schema, "entity_name": ename, "columns": cols}


VALIDATION_ANNS = {"NotNull", "NotBlank", "NotEmpty", "Size", "Min", "Max", "Email", "Pattern", "Positive",
                   "PositiveOrZero", "Negative", "NegativeOrZero", "Future", "FutureOrPresent", "Past",
                   "PastOrPresent", "DecimalMin", "DecimalMax", "Digits", "AssertTrue", "AssertFalse", "Length",
                   "Range", "NotNull", "Valid"}
