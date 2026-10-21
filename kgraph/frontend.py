"""Angular / React frontend extraction: components, routes, HTTP calls."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from .backend import _kotlin_expr_end
from .lex import Src, flatten, match_close, norm_path, parse_anns, split_top

TS_QUOTES = "\"'`"
HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head")
HTTP_TYPES = {"HttpClient", "Http", "AxiosInstance", "Axios", "RestService", "ApiService"}
HTTP_NAME = re.compile(r"^(http|https|axios|client|api|request|requests|ajax|rest|httpClient|\w*Http\w*|\w*Api)$", re.I)
INFRA_URL = re.compile(r"url|base|api|host|endpoint|server|origin", re.I)
_IMPORT_RE = re.compile(r"import\s+(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\}\s*)?(?:\*\s+as\s+(\w+)\s*)?from\s*['\"]([^'\"]+)['\"]")


@dataclass
class ApiCall:
    verb: str
    path: str
    raw: str
    file: str
    line: int


@dataclass
class FEMethod:
    name: str
    calls: set = field(default_factory=set)      # {(unit_key, method)}
    api: list = field(default_factory=list)      # [ApiCall]
    line: int = 0


@dataclass
class FEUnit:
    name: str
    kind: str                 # component | service | hook | module
    framework: str
    file: str
    line: int
    selector: str | None = None
    template: str = ""
    methods: dict = field(default_factory=dict)
    deps: dict = field(default_factory=dict)       # variable -> type name
    children: set = field(default_factory=set)     # unit keys used in template / JSX
    nav: set = field(default_factory=set)          # raw navigation targets
    pending: list = field(default_factory=list)    # unresolved cross-file call refs (React)
    jsx_tags: set = field(default_factory=set)
    default_export: bool = False

    @property
    def key(self):
        return f"{self.file}#{self.name}"


@dataclass
class FERoute:
    path: str
    component: str | None
    file: str
    line: int
    framework: str
    guards: list = field(default_factory=list)
    lazy_file: str | None = None
    lazy_children: str | None = None
    parent: int | None = None
    redirect: str | None = None
    full: str = ""


class FEModel:
    def __init__(self):
        self.units: dict[str, FEUnit] = {}
        self.by_name: dict[str, list[FEUnit]] = {}
        self.by_file: dict[str, list[FEUnit]] = {}
        self.routes: list[FERoute] = []
        self.imports: dict[str, dict] = {}
        self.env_consts: dict[str, str] = {}
        self.frameworks: set[str] = set()
        self.reader = lambda rel: ""

    def add(self, u: FEUnit):
        self.units[u.key] = u
        self.by_name.setdefault(u.name, []).append(u)
        self.by_file.setdefault(u.file, []).append(u)

    def lookup(self, name: str, prefer_file: str | None = None) -> FEUnit | None:
        cands = self.by_name.get(name, [])
        for c in cands:
            if c.file == prefer_file:
                return c
        return cands[0] if cands else None

    def reach(self, unit: FEUnit):
        """(unit keys reached, [(unit, ApiCall)]) starting from every method of `unit`.

        Template/JSX children are entered as a whole (template-driven entry
        points); injected services/modules are followed at method granularity.
        """
        units: set[str] = set()
        seen: set[tuple] = set()
        calls: list[tuple[FEUnit, ApiCall]] = []
        work: list[tuple] = [(unit.key, None)]
        while work:
            k, mn = work.pop()
            u = self.units.get(k)
            if u is None:
                continue
            units.add(k)
            if mn is None:
                if (k, None) in seen:
                    continue
                seen.add((k, None))
                work.extend((k, m) for m in u.methods)
                work.extend((c, None) for c in u.children)
                continue
            if (k, mn) in seen:
                continue
            seen.add((k, mn))
            meth = u.methods.get(mn)
            if meth:
                calls.extend((u, ac) for ac in meth.api)
                work.extend(meth.calls)
        return units, calls


def resolve_file(base_file: str, spec: str, files: set[str]) -> str | None:
    if spec.startswith("."):
        cand = posixpath.normpath(posixpath.join(posixpath.dirname(base_file), spec))
    else:
        spec2 = re.sub(r"^(@/|~/|src/|@app/|app/)", "", spec)
        cands = [f for f in files if re.search(r"(^|/)" + re.escape(spec2) + r"\.(tsx?|jsx?)$", f)
                 or re.search(r"(^|/)" + re.escape(spec2) + r"/index\.(tsx?|jsx?)$", f)]
        return cands[0] if cands else None
    for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"):
        if cand + ext in files:
            return cand + ext
    return None


# ---------------------------------------------------------------- URL evaluation
def eval_expr(expr: str, env: dict) -> str | None:
    """Evaluate a string/template/concat expression; unknown parts become `{}`."""
    e = expr.strip().rstrip(";")
    if not e:
        return None
    if e.startswith("`") and e.endswith("`") and len(e) > 1:
        def sub(m):
            inner = m.group(1).strip()
            v = env.get(inner.split(".")[-1])
            if v is not None:
                return v
            return "" if INFRA_URL.search(inner) and not re.search(r"id|param|slug", inner, re.I) else "{}"
        return re.sub(r"\$\{([^}]*)\}", sub, e[1:-1])
    parts = split_top(e, "+")
    if len(parts) > 1:
        vals = [eval_expr(p, env) for p in parts]
        if vals[0] is None:
            vals[0] = ""
        return "".join(v if v is not None else "{}" for v in vals)
    if (e[0] in "'\"") and e[-1] == e[0]:
        return e[1:-1]
    m = re.match(r"^(?:this\.)?([\w.]+)$", e)
    if m:
        name = m.group(1).split(".")[-1]
        if name in env:
            return env[name]
        return "" if INFRA_URL.search(name) and not re.search(r"id|param|slug", name, re.I) else "{}"
    if e.startswith("new URL("):
        return eval_expr(split_top(e[8:-1])[0], env)
    return None


def _strip_origin(p: str) -> str:
    p = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", p)
    return re.sub(r"^\{\}(?=/)", "", p)


# ---------------------------------------------------------------- route parsing
def _spans(skel: str):
    stack, spans = [], []
    for i, ch in enumerate(skel):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            spans.append((stack.pop(), i))
    spans.sort()
    return spans


def parse_route_objects(rel: str, text: str, framework: str) -> list[FERoute]:
    src = Src(text, TS_QUOTES)
    spans = _spans(src.skel)
    owned = []
    for (s, e) in spans:
        chars = list(text[s + 1:e])
        for (s2, e2) in spans:
            if s < s2 and e2 < e:
                for k in range(s2 - s - 1, e2 - s):
                    chars[k] = " "
        owned.append("".join(chars))
    routes: list[FERoute] = []
    idx_of: dict[int, int] = {}
    for n, ((s, e), ot) in enumerate(zip(spans, owned)):
        pm = re.search(r"\bpath\s*:\s*(['\"`])(.*?)\1", ot)
        comp = re.search(r"\bcomponent\s*:\s*(\w+)", ot) or re.search(r"\belement\s*:\s*<\s*(\w+)", ot) \
            or re.search(r"\bComponent\s*:\s*(\w+)", ot)
        lazy_c = re.search(r"\bloadComponent\s*:\s*\(\)\s*=>\s*import\(\s*['\"]([^'\"]+)['\"]\s*\)(?:\s*\.then\(\s*\w+\s*=>\s*\w+\.(\w+))?", ot)
        lazy_ch = re.search(r"\bloadChildren\s*:\s*\(\)\s*=>\s*import\(\s*['\"]([^'\"]+)['\"]", ot)
        redirect = re.search(r"\bredirectTo\s*:\s*['\"]([^'\"]*)['\"]", ot)
        is_index = re.search(r"\bindex\s*:\s*true", ot)
        has_children = re.search(r"\bchildren\b", text[s:e])
        if not (pm or is_index or ((comp or lazy_c) and has_children)):
            continue
        guards = re.search(r"\bcanActivate\s*:\s*\[([^\]]*)\]", ot)
        parent = None
        for pi in range(len(routes) - 1, -1, -1):
            ps, pe = spans[idx_of[pi]]
            if ps < s and e < pe:
                parent = pi
                break
        r = FERoute(pm.group(2) if pm else "", (comp.group(1) if comp else None) or (lazy_c.group(2) if lazy_c else None),
                    rel, src.line(s), framework,
                    [g.strip() for g in guards.group(1).split(",") if g.strip()] if guards else [],
                    lazy_c.group(1) if lazy_c else None, lazy_ch.group(1) if lazy_ch else None, parent,
                    redirect.group(1) if redirect else None)
        idx_of[len(routes)] = n
        routes.append(r)
    return routes


_JSX_ROUTE = re.compile(r"<Route\b((?:[^<>{}]|\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})*?)(/?)>|</Route>")


def parse_jsx_routes(rel: str, text: str) -> list[FERoute]:
    routes: list[FERoute] = []
    stack: list[int] = []
    src = Src(text, TS_QUOTES)
    for m in _JSX_ROUTE.finditer(text):
        if m.group(0) == "</Route>":
            if stack:
                stack.pop()
            continue
        attrs = m.group(1)
        pm = re.search(r"\bpath\s*=\s*(?:\{\s*)?['\"`]([^'\"`]*)['\"`]", attrs)
        el = re.search(r"\belement\s*=\s*\{\s*<\s*(\w+)", attrs) or re.search(r"\b[Cc]omponent\s*=\s*\{\s*(\w+)", attrs)
        index = re.search(r"\bindex\b", attrs)
        r = FERoute(pm.group(1) if pm else "", el.group(1) if el else None, rel, src.line(m.start()), "react",
                    parent=stack[-1] if stack else None)
        if pm or index or el:
            routes.append(r)
            if not m.group(2):
                stack.append(len(routes) - 1)
        elif not m.group(2):
            stack.append(stack[-1] if stack else -1)  # pathless layout wrapper
    for r in routes:
        if r.parent == -1:
            r.parent = None
    return routes


def full_route_paths(routes: list[FERoute], base: dict[str, str] | None = None) -> list[str]:
    base = base or {}
    res = []
    for r in routes:
        parts, cur = [], r
        while True:
            parts.append(cur.path)
            if cur.parent is None:
                break
            cur = routes[cur.parent]
        parts.append(base.get(r.file, ""))
        p = "/".join(x.strip("/") for x in reversed(parts) if x.strip("/"))
        res.append("/" + p)
    return res


# ---------------------------------------------------------------- Angular
def _ts_class_regions(src: Src):
    flat = flatten(src.skel)
    out = []
    prev_end = 0
    for m in re.finditer(r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)", flat):
        if m.start() < prev_end:
            continue
        b = flat.find("{", m.end())
        if b < 0:
            continue
        e = match_close(src.skel, b)
        hdr = max(prev_end, flat.rfind(";", 0, m.start()) + 1, flat.rfind("}", 0, m.start()) + 1)
        anns = parse_anns(src, hdr, m.start(), stop_at_paren=False)
        out.append((m.group(1), m.start(), b + 1, e, anns, "default" in flat[m.start():m.start(1)]))
        prev_end = e + 1
    return out


def _method_regions(src: Src, s: int, e: int):
    """(name, body_start, body_end, pos) for methods/arrow properties of a TS class body."""
    sk = src.skel
    flat = flatten(sk, s, e)
    res = []
    meth = re.compile(r"(?:^|[;}\n])\s*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
                      r"(?P<n>[\w$]+)\s*(?:<[^>(]*>)?\s*\(")
    for m in meth.finditer(flat):
        name = m.group("n")
        if name in ("if", "for", "while", "switch", "catch", "return", "constructor", "function"):
            continue
        po = s + m.end() - 1
        pc = match_close(sk, po, "(", ")")
        j = pc + 1
        while j < e and sk[j] not in "{;=":
            j += 1
        if j < e and sk[j] == "{":
            res.append((name, j, match_close(sk, j), s + m.start("n")))
    arrow = re.compile(r"(?:^|[;}\n])\s*(?:(?:public|private|protected|static|readonly)\s+)*(?P<n>[\w$]+)\s*(?::[^=;\n]+)?=\s*"
                       r"(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*(?::\s*[^=\n]+?)?=>")
    for m in arrow.finditer(flat):
        j = s + m.end()
        while j < e and sk[j] in " \t\n":
            j += 1
        end = match_close(sk, j) if j < e and sk[j] == "{" else _kotlin_expr_end(sk, j)
        res.append((m.group("n"), j, end, s + m.start("n")))
    return res


_NAV_RES = [re.compile(p) for p in (
    r"""routerLink\s*=\s*"(/[^"]*)\"""",
    r"""\[routerLink\]\s*=\s*"\s*\[\s*['"`](/[^'"`]*)""",
    r"""navigate(?:ByUrl)?\(\s*\[?\s*['"`](/[^'"`]*)""",
    r"""\bto\s*=\s*\{?\s*['"`](/[^'"`]*)""",
)]


def extract_nav(text: str) -> set[str]:
    return {m.group(1).replace("${", "{") for rx in _NAV_RES for m in rx.finditer(text)}


def _analyze_body(model: FEModel, unit: FEUnit, meth: FEMethod, src: Src, a: int, b: int, env: dict, kind_http: set):
    sk, text = src.skel, src.text
    body_sk, body = sk[a:b + 1], text[a:b + 1]
    for m in re.finditer(r"(?:this\s*\.\s*)?([\w$]+)\s*\.\s*(get|post|put|patch|delete|head|request)\s*(?:<[^()]*>)?\s*\(", body_sk):
        recv, verb = m.group(1), m.group(2)
        rtype = unit.deps.get(recv, "")
        if not (rtype in HTTP_TYPES or recv in kind_http or HTTP_NAME.match(recv)):
            continue
        po = a + m.end() - 1
        args = text[po + 1:match_close(sk, po, "(", ")")]
        parts = split_top(args)
        if not parts:
            continue
        if verb == "request":
            if len(parts) < 2:
                continue
            verb_s, url_expr = parts[0].strip("'\" ").lower(), parts[1]
        else:
            verb_s, url_expr = verb, parts[0]
        url = eval_expr(url_expr, env)
        if url is None or ("/" not in url and "://" not in url):
            continue
        meth.api.append(ApiCall(verb_s.upper(), norm_path(_strip_origin(url)), url, unit.file, src.line(po)))
    for m in re.finditer(r"(?<![\w.])fetch\s*\(", body_sk):
        po = a + m.end() - 1
        args = text[po + 1:match_close(sk, po, "(", ")")]
        parts = split_top(args)
        if not parts:
            continue
        url = eval_expr(parts[0], env)
        if url is None:
            continue
        vm = re.search(r"\bmethod\s*:\s*['\"](\w+)['\"]", args)
        meth.api.append(ApiCall((vm.group(1) if vm else "GET").upper(), norm_path(_strip_origin(url)), url,
                                unit.file, src.line(po)))
    for m in re.finditer(r"(?<![\w.])axios\s*\(\s*\{([^}]*)\}", body):
        um = re.search(r"\burl\s*:\s*([^,}]+)", m.group(1))
        vm = re.search(r"\bmethod\s*:\s*['\"](\w+)['\"]", m.group(1))
        url = eval_expr(um.group(1), env) if um else None
        if url is not None:
            meth.api.append(ApiCall((vm.group(1) if vm else "GET").upper(), norm_path(_strip_origin(url)), url,
                                    unit.file, src.line(a)))
    unit.nav |= extract_nav(body)


def parse_angular_file(model: FEModel, rel: str, text: str, files: set[str]):
    src = Src(text, TS_QUOTES)
    file_env = {k: v for k, v in model.env_consts.items()}
    for name, cstart, bs, be, anns, is_default in _ts_class_regions(src):
        comp = next((a for a in anns if a.name == "Component"), None)
        inj = next((a for a in anns if a.name == "Injectable"), None)
        if not (comp or inj):
            body_sk = src.skel[bs:be]
            if not re.search(r"\bHttpClient\b|\.(get|post|put|delete)\s*[<(]", body_sk):
                continue
        kind = "component" if comp else "service"
        u = FEUnit(name, kind, "angular", rel, src.line(cstart))
        if comp:
            sel = re.search(r"selector\s*:\s*['\"]([^'\"]+)['\"]", comp.args)
            u.selector = sel.group(1) if sel else None
            tu = re.search(r"templateUrl\s*:\s*['\"]([^'\"]+)['\"]", comp.args)
            it = re.search(r"template\s*:\s*`([^`]*)`", comp.args)
            if tu:
                tpath = posixpath.normpath(posixpath.join(posixpath.dirname(rel), tu.group(1)))
                u.template = model.reader(tpath)
            elif it:
                u.template = it.group(1)
            imp = re.search(r"imports\s*:\s*\[([^\]]*)\]", comp.args)
            if imp:
                u.pending.append(("imports", [x.strip() for x in imp.group(1).split(",") if x.strip()]))
        body = src.text[bs:be]
        for m in re.finditer(r"(?:private|public|protected|readonly)\s+(?:readonly\s+)?([\w$]+)\s*:\s*([\w.]+)", body):
            u.deps[m.group(1)] = m.group(2).split(".")[-1]
        for m in re.finditer(r"([\w$]+)\s*(?::\s*[\w.<>]+)?\s*=\s*inject\(\s*([\w.]+)", body):
            u.deps[m.group(1)] = m.group(2).split(".")[-1]
        flat = flatten(src.skel, bs, be)
        env = dict(file_env)
        for m in re.finditer(r"(?:^|[;}\n])\s*(?:(?:private|public|protected|readonly|static)\s+)*([\w$]+)\s*(?::\s*string)?\s*=\s*([^;\n]+)", flat):
            val = eval_expr(src.text[bs + m.start(2):bs + m.end(2)], env)
            if val is not None and not val.startswith("("):
                env[m.group(1)] = val
        http_vars = {v for v, t in u.deps.items() if t in HTTP_TYPES}
        for (mname, a, b, pos) in _method_regions(src, bs, be):
            meth = u.methods.setdefault(mname, FEMethod(mname, line=src.line(pos)))
            _analyze_body(model, u, meth, src, a, b, env, http_vars)
            body_sk = src.skel[a:b + 1]
            for cm in re.finditer(r"(?<![\w.])this\s*\.\s*([\w$]+)\s*\.\s*([\w$]+)\s*\(", body_sk):
                t = u.deps.get(cm.group(1))
                if t:
                    meth.calls.add((("name", t), cm.group(2)))
            for cm in re.finditer(r"(?<![\w.])this\s*\.\s*([\w$]+)\s*\(", body_sk):
                meth.calls.add((("name", name), cm.group(1)))
        if u.methods or comp:
            u.default_export = is_default
            model.add(u)
    model.frameworks.add("angular")


# ---------------------------------------------------------------- React
def _parse_imports(text: str):
    imps = {}
    for m in _IMPORT_RE.finditer(text):
        default, named, ns, spec = m.groups()
        if default:
            imps[default] = (spec, "default")
        if ns:
            imps[ns] = (spec, "*")
        for part in (named or "").split(","):
            part = part.strip()
            if not part:
                continue
            nm = re.match(r"(?:type\s+)?(\w+)(?:\s+as\s+(\w+))?", part)
            if nm:
                imps[nm.group(2) or nm.group(1)] = (spec, nm.group(1))
    return imps


_FN_DECL = re.compile(r"(?:^|[;}\n])\s*(?P<exp>export\s+(?:default\s+)?)?(?:async\s+)?function\s+(?P<n>[\w$]+)\s*(?:<[^>(]*>)?\s*\(")
_ARROW_DECL = re.compile(
    r"(?:^|[;}\n])\s*(?P<exp>export\s+(?:default\s+)?)?(?:const|let|var)\s+(?P<n>[\w$]+)\s*(?::\s*[^=\n]+?)?=\s*"
    r"(?:(?:React\.)?(?:memo|forwardRef|observer)\s*\(\s*)?(?:async\s*)?(?:function\s*[\w$]*\s*)?"
    r"(?:\((?:[^()]|\([^()]*\))*\)|[\w$]+)\s*(?::\s*[^=\n]+?)?\s*(?:=>|\{)")
_OBJ_DECL = re.compile(r"(?:^|[;}\n])\s*(?P<exp>export\s+(?:default\s+)?)?(?:const|let|var)\s+(?P<n>[\w$]+)\s*(?::\s*[^=\n]+?)?=\s*\{")


def parse_react_file(model: FEModel, rel: str, text: str, files: set[str]):
    src = Src(text, TS_QUOTES)
    sk = src.skel
    flat = flatten(sk)
    imps = _parse_imports(text)
    model.imports[rel] = imps
    env = dict(model.env_consts)
    for m in re.finditer(r"(?:^|[;\n])\s*(?:export\s+)?const\s+([\w$]+)\s*(?::\s*string)?\s*=\s*([^;\n]+)", flat):
        v = eval_expr(text[m.start(2):m.end(2)], env)
        if v is not None and not v.startswith("("):
            env[m.group(1)] = v
    http_names = {k for k, (spec, _) in imps.items() if re.search(r"axios|http|api|client", spec, re.I)}
    found: list[tuple[str, int, int, int, bool]] = []   # name, body_a, body_b, pos, exported_default
    for m in _FN_DECL.finditer(flat):
        po = m.end() - 1
        pc = match_close(sk, po, "(", ")")
        j = pc + 1
        while j < len(sk) and sk[j] != "{":
            j += 1
        if j < len(sk):
            found.append((m.group("n"), j, match_close(sk, j), m.start("n"), bool(m.group("exp") and "default" in m.group("exp"))))
    for m in _ARROW_DECL.finditer(flat):
        j = m.end()
        if flat[m.end() - 1] != "{":
            while j < len(sk) and sk[j] in " \t\n":
                j += 1
            end = match_close(sk, j) if j < len(sk) and sk[j] == "{" else _kotlin_expr_end(sk, j)
        else:
            j = m.end() - 1
            end = match_close(sk, j)
        # `const x = {` object literals are handled below
        found.append((m.group("n"), j, end, m.start("n"), bool(m.group("exp") and "default" in m.group("exp"))))
    plain: dict[str, tuple[int, int, int]] = {}
    for name, a, b, pos, is_def in found:
        body = text[a:b + 1]
        jsx = bool(re.search(r"<\s*[A-Za-z][\w.]*[^>]*>|<>", sk[a:b + 1]))
        if name[0].isupper() and jsx:
            kind = "component"
        elif re.match(r"use[A-Z]", name):
            kind = "hook"
        else:
            plain[name] = (a, b, pos)
            continue
        u = FEUnit(name, kind, "react", rel, src.line(pos), default_export=is_def)
        meth = u.methods.setdefault("*", FEMethod("*", line=src.line(pos)))
        _analyze_body(model, u, meth, src, a, b, env, http_names)
        for tm in re.finditer(r"<\s*([A-Z][\w]*)", sk[a:b + 1]):
            u.jsx_tags.add(tm.group(1))
        _react_calls(u, meth, sk[a:b + 1], imps, plain_names=set(plain))
        model.add(u)
    for m in _OBJ_DECL.finditer(flat):
        b0 = m.end() - 1
        e0 = match_close(sk, b0)
        name = m.group("n")
        if name[0].isupper() or re.match(r"use[A-Z]", name):
            continue
        members = split_top(text[b0 + 1:e0])
        mod = FEUnit(name, "module", "react", rel, src.line(m.start("n")))
        for mem in members:
            mm = re.match(r"(?:async\s+)?([\w$]+)\s*(?::|\()", mem)
            if not mm:
                continue
            off = text.find(mem, b0)
            meth = mod.methods.setdefault(mm.group(1), FEMethod(mm.group(1), line=src.line(off)))
            _analyze_body(model, mod, meth, src, off, off + len(mem) - 1, env, http_names)
        if any(mt.api for mt in mod.methods.values()):
            model.add(mod)
    stem = posixpath.basename(rel).rsplit(".", 1)[0]
    if plain:
        mod = FEUnit(stem, "module", "react", rel, 1)
        for name, (a, b, pos) in plain.items():
            meth = mod.methods.setdefault(name, FEMethod(name, line=src.line(pos)))
            _analyze_body(model, mod, meth, src, a, b, env, http_names)
            _react_calls(mod, meth, sk[a:b + 1], imps, plain_names=set(plain))
        if any(mt.api for mt in mod.methods.values()):
            model.add(mod)
    for m in re.finditer(r"export\s+default\s+(?:React\.)?(?:memo\(|observer\()?\s*([A-Za-z_$][\w$]*)", flat):
        for u in model.by_file.get(rel, []):
            if u.name == m.group(1):
                u.default_export = True
    model.frameworks.add("react")


def _react_calls(u: FEUnit, meth: FEMethod, body_sk: str, imps: dict, plain_names: set):
    for m in re.finditer(r"(?<![\w.])([\w$]+)\s*\(", body_sk):
        n = m.group(1)
        if n in imps and imps[n][1] not in ("*",):
            u.pending.append(("call", n, imps[n], meth.name))
        elif n in plain_names:
            u.pending.append(("local", n, None, meth.name))
    for m in re.finditer(r"(?<![\w.])([\w$]+)\s*\.\s*([\w$]+)\s*\(", body_sk):
        if m.group(1) in imps:
            u.pending.append(("member", m.group(2), imps[m.group(1)], meth.name, m.group(1)))


def resolve_pending(model: FEModel, files: set[str]):
    """Second pass: cross-file call / usage resolution for both frameworks."""
    selectors = {u.selector: u for u in model.units.values()
                 if u.selector and re.match(r"^[a-z][\w-]*$", u.selector)}
    for u in list(model.units.values()):
        # Angular: DI type placeholders -> unit keys
        for meth in u.methods.values():
            resolved = set()
            for (uk, mn) in meth.calls:
                if isinstance(uk, tuple):
                    t = model.lookup(uk[1], u.file)
                    if t is not None and mn in t.methods:
                        resolved.add((t.key, mn))
                else:
                    resolved.add((uk, mn))
            meth.calls = resolved
        # Angular templates: child components (selectors) and navigation links
        for sel, child in selectors.items():
            if u.template and child.key != u.key and re.search(r"<%s[\s>/]" % re.escape(sel), u.template):
                u.children.add(child.key)
        if u.template:
            u.nav |= extract_nav(u.template)
        for p in u.pending:
            if p[0] == "imports":
                for nm in p[1]:
                    t = model.lookup(nm, u.file)
                    if t and t.key != u.key and t.kind == "component":
                        u.children.add(t.key)
            elif p[0] in ("call", "member", "local"):
                caller = u.methods.get(p[3]) or next(iter(u.methods.values()), None)
                tgt = _resolve_call(model, u, p, files)
                if caller is not None and tgt and tgt[0] != u.key:
                    caller.calls.add(tgt)
        if u.framework == "react":
            imps = model.imports.get(u.file, {})
            for tag in u.jsx_tags:
                t = None
                if tag in imps:
                    spec, imported = imps[tag]
                    f = resolve_file(u.file, spec, files)
                    in_file = model.by_file.get(f or "", [])
                    for c in in_file:
                        if c.name == imported or (imported == "default" and (c.default_export or c.name == tag)):
                            t = c
                    if not t:
                        comps = [c for c in in_file if c.kind == "component"]
                        t = comps[0] if len(comps) == 1 else None
                else:
                    t = next((c for c in model.by_file.get(u.file, []) if c.name == tag), None)
                if t and t.key != u.key:
                    u.children.add(t.key)


def _resolve_call(model: FEModel, u: FEUnit, p: tuple, files: set[str]):
    kind = p[0]
    if kind == "local":
        for c in model.by_file.get(u.file, []):
            if p[1] in c.methods:
                return (c.key, p[1])
        return None
    spec, imported = p[2]
    in_file = model.by_file.get(resolve_file(u.file, spec, files) or "", [])
    for c in in_file:
        if kind == "call":
            if imported in c.methods:
                return (c.key, imported)
            if c.kind in ("component", "hook") and (c.name == imported or (imported == "default" and c.default_export)):
                return (c.key, "*")
        elif p[1] in c.methods and (imported in ("*", "default") or c.name == imported):
            return (c.key, p[1])
    return None


def parse_env_files(model: FEModel, repo_files: list[str], read):
    for rel in repo_files:
        if re.search(r"(^|/)environments?/[^/]*\.ts$", rel) or re.search(r"(^|/)(config|constants?)\.(ts|js)$", rel):
            for m in re.finditer(r"([\w$]+)\s*[:=]\s*['\"]([^'\"]*)['\"]", read(rel)):
                model.env_consts.setdefault(m.group(1), m.group(2))
