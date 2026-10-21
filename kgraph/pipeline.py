"""Scan pipeline: scan_frontend -> ... -> populate_graph."""
from __future__ import annotations

import re
import time
from collections import defaultdict

from . import backend as be
from . import frontend as fe
from . import rules as rl
from .graph import Graph
from .lex import norm_path
from .ontology import PIPELINE
from .source import Repo, is_test_path, load_repository
from .sqlscan import parse_sql

PERSIST_METHODS = {"save", "saveAll", "saveAndFlush", "saveAllAndFlush", "persist", "merge"}
GENERIC_SEGMENTS = re.compile(r"^(api|rest|v\d+|public|private|internal|service|services|ws|\{\})$", re.I)


class Context:
    def __init__(self, repo: Repo, log=lambda m: None):
        self.repo, self.log = repo, log
        self.fe = fe.FEModel()
        self.fe.reader = repo.read
        self.ts_files: set[str] = set()
        self.classes: list[be.ClassInfo] = []
        self.by_name: dict[str, list[be.ClassInfo]] = defaultdict(list)
        self.alias: dict[str, list[be.ClassInfo]] = defaultdict(list)
        self.consts: dict[str, str] = {}
        self.config: dict[str, str] = {}
        self.prefix = ""
        self.endpoints: list[dict] = []          # {ctrl, verb, path, norm, method, line}
        self.listeners: list[tuple] = []         # (topic, ClassInfo, Method)
        self.entities: dict[str, dict] = {}      # class name -> entity info
        self.entity_cls: dict[str, be.ClassInfo] = {}
        self.entity_by_ename: dict[str, str] = {}
        self.repo_info: dict[str, dict] = {}     # fqn -> {entity, methods}
        self.sql_tables: dict[str, dict] = {}
        self.sql_rules: list[dict] = []
        self.rules: list[dict] = []
        self.dto_rules: dict[str, list[dict]] = {}
        self.stats: dict[str, int] = {}
        self.llm = None   # optional llm.RuleEnricher
        self.rules_provenance: dict = {}
        self.rules_engine = "heuristic"   # heuristic | none (modernizer, when set, takes precedence)
        self.modernizer = None   # optional modernization.Modernizer (replaces static rule detection)
        self.graph = Graph()

    # -- class registry -----------------------------------------------------
    def resolve_class(self, name: str, frm: be.ClassInfo | None = None) -> be.ClassInfo | None:
        cands = self.by_name.get(name, [])
        concrete = [c for c in cands if c.kind != "interface" or c.role == "repository"]
        pool = concrete or self.alias.get(name, [])
        if not pool:
            pool = self.alias.get(name, [])
        if not pool:
            return None
        if frm is not None:
            same = [c for c in pool if c.pkg == frm.pkg]
            if same:
                return same[0]
        return pool[0]

    def methods_of(self, ci: be.ClassInfo, name: str) -> list[be.Method]:
        return [m for m in ci.methods if m.name == name and not m.is_ctor]


# ================================================================ stages
def scan_frontend(ctx: Context):
    repo = ctx.repo
    files = [f for f in repo.files(".ts", ".tsx", ".js", ".jsx", ".html") if not is_test_path(f)]
    ctx.ts_files = {f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))}
    fe.parse_env_files(ctx.fe, sorted(ctx.ts_files), repo.read)
    for rel in sorted(ctx.ts_files):
        text = repo.read(rel)
        if rel.endswith(".ts") and re.search(r"@(Component|Injectable)\s*\(", text):
            fe.parse_angular_file(ctx.fe, rel, text, ctx.ts_files)
        elif rel.endswith((".tsx", ".jsx")) or re.search(
                r"from\s*['\"](react[\w/-]*|axios|swr|@tanstack/[\w-]+)['\"]|\bfetch\s*\(", text):
            fe.parse_react_file(ctx.fe, rel, text, ctx.ts_files)
    ctx.stats["fe_units"] = len(ctx.fe.units)
    ctx.log(f"frontend: {len(ctx.fe.units)} units ({', '.join(sorted(ctx.fe.frameworks)) or 'none detected'})")


def extract_routes(ctx: Context):
    routes: list[fe.FERoute] = []
    for rel in sorted(ctx.ts_files):
        text = ctx.repo.read(rel)
        if re.search(r"<Route\b", text):
            routes += fe.parse_jsx_routes(rel, text)
        if re.search(r"createBrowserRouter|createHashRouter|createMemoryRouter|useRoutes|RouteObject", text):
            routes += _offset(fe.parse_route_objects(rel, text, "react"), len(routes))
        elif re.search(r"\bRoutes\b|RouterModule|loadChildren|loadComponent", text) and re.search(r"\bpath\s*:", text) \
                and "@angular" in text:
            routes += _offset(fe.parse_route_objects(rel, text, "angular"), len(routes))
    prefix: dict[str, str] = {}
    for _ in range(6):
        full = fe.full_route_paths(routes, prefix)
        changed = False
        for r, p in zip(routes, full):
            if r.lazy_children:
                tgt = fe.resolve_file(r.file, r.lazy_children, ctx.ts_files)
                if tgt and prefix.get(tgt) != p:
                    prefix[tgt] = p
                    changed = True
        if not changed:
            break
    for r, p in zip(routes, fe.full_route_paths(routes, prefix)):
        r.full = p
    ctx.fe.routes = routes
    ctx.log(f"routes: {len(routes)}")


def _offset(rs: list[fe.FERoute], n: int):
    for r in rs:
        if r.parent is not None:
            r.parent += n
    return rs


def detect_api_calls(ctx: Context):
    fe.resolve_pending(ctx.fe, ctx.ts_files)
    n = sum(len(m.api) for u in ctx.fe.units.values() for m in u.methods.values())
    ctx.stats["fe_api_calls"] = n
    ctx.log(f"frontend HTTP calls: {n}")


def scan_backend(ctx: Context):
    repo = ctx.repo
    files = [f for f in repo.files(".java", ".kt") if not is_test_path(f) and "src/test" not in f]
    for rel in files:
        ctx.consts.update(be.collect_consts(repo.read(rel)))
    for rel in repo.files(".properties", ".yml", ".yaml"):
        base = rel.rsplit("/", 1)[-1]
        if not base.startswith("application") and not base.startswith("bootstrap") or "src/test" in rel:
            continue
        txt = repo.read(rel)
        ctx.config.update(be.parse_properties(txt) if rel.endswith(".properties") else be.flatten_yaml(txt))
    for key in ("server.servlet.context-path", "spring.mvc.servlet.path", "spring.webflux.base-path", "quarkus.http.root-path"):
        if ctx.config.get(key):
            ctx.prefix = "/" + ctx.config[key].strip("/")
            break
    for rel in files:
        try:
            for ci in be.parse_jvm_file(rel, repo.read(rel)):
                ctx.classes.append(ci)
                ctx.by_name[ci.name].append(ci)
        except Exception as ex:   # one unparsable file must not stop the scan
            ctx.log(f"warning: cannot parse {rel}: {ex}")
    for ci in ctx.classes:
        be.analyze_methods(ci, ctx.consts, ctx.config)
    ctx.stats["classes"] = len(ctx.classes)
    ctx.log(f"backend: {len(ctx.classes)} classes in {len(files)} files")


def discover_controllers(ctx: Context):
    for ci in ctx.classes:
        ci.role = be.classify(ci)
    for ci in ctx.classes:
        if ci.role == "controller":
            for ep in be.endpoints(ci, ctx.consts, ctx.config, ctx.prefix):
                ep["ctrl"] = ci
                ctx.endpoints.append(ep)
    ctx.stats["controllers"] = sum(1 for c in ctx.classes if c.role == "controller")
    ctx.stats["apis"] = len(ctx.endpoints)
    ctx.log(f"controllers: {ctx.stats['controllers']}, endpoints: {len(ctx.endpoints)}")


def discover_services(ctx: Context):
    for ci in ctx.classes:
        for b in ci.base_names():
            if ci.kind != "interface":
                ctx.alias[b].append(ci)
    # helper promotion: classes called from services/controllers are part of the logic
    changed = True
    while changed:
        changed = False
        for ci in ctx.classes:
            if ci.role not in ("service", "controller"):
                continue
            for m in ci.methods:
                for t, _ in m.calls:
                    tc = ctx.resolve_class(t, ci)
                    if tc and tc.role is None and tc.kind in ("class", "object") and tc.methods \
                            and not any(f.ann(*be.VALIDATION_ANNS) for f in tc.ctor + tc.fields):
                        tc.role = "helper"
                        changed = True
    for ci in ctx.classes:
        if ci.role in ("service", "helper", "controller"):
            for m in ci.methods:
                for topic in be.listener_topics(m, ctx.consts, ctx.config):
                    ctx.listeners.append((topic, ci, m))
    ctx.stats["services"] = sum(1 for c in ctx.classes if c.role in ("service", "helper"))
    ctx.log(f"services: {ctx.stats['services']}, listeners: {len(ctx.listeners)}")


def discover_repositories(ctx: Context):
    for ci in ctx.classes:
        if ci.role != "repository":
            continue
        entity = None
        for b, gen in ci.bases:
            g = re.match(r"<\s*([\w.]+)", gen or "")
            if g and re.search(r"Repository|Executor", b):
                entity = be.base_type(g.group(1))
                break
        methods = {}
        for m in ci.methods:
            q = m.ann("Query", "NamedQuery")
            sql = None
            if q:
                strs = be.ann_values(q.args, "value", "nativeQuery") or []
                native = bool(re.search(r"nativeQuery\s*=\s*true", q.args))
                sqls = [s for s in strs if re.search(r"(?i)\b(select|insert|update|delete|from)\b", s)]
                if sqls:
                    r, w = be.sql_tables(" ".join(sqls))
                    sql = (r, w, native)
            for r, w, native in m.sql:
                sql = (r, w, True)
            set_fields = []
            if q:
                for qs in be.ann_values(q.args, "value") or []:
                    sm = re.search(r"(?i)\bset\s+(.*?)(?:\bwhere\b|$)", qs)
                    if sm:
                        set_fields += [x.split(".")[-1] for x in re.findall(r"([\w.]+)\s*=", sm.group(1))]
            methods[m.name] = {"mode": be.classify_method(m.name, m, sql[:2] if sql else None), "sql": sql,
                               "fields": be.derived_query_fields(m.name), "set_fields": set_fields}
        ctx.repo_info[ci.fqn] = {"entity": entity, "methods": methods}
    ctx.stats["repositories"] = len(ctx.repo_info)
    ctx.log(f"repositories: {len(ctx.repo_info)}")


def discover_entities(ctx: Context):
    registry = {c.name: c for c in ctx.classes}
    for ci in ctx.classes:
        if ci.role == "entity":
            info = be.entity_info(ci, registry)
            ctx.entities[ci.name] = info
            ctx.entity_cls[ci.name] = ci
            ctx.entity_by_ename[info["entity_name"]] = ci.name
    for rel in ctx.repo.files(".sql"):
        if is_test_path(rel):
            continue
        parsed = parse_sql(rel, ctx.repo.read(rel))
        for name, t in parsed["tables"].items():
            cur = ctx.sql_tables.get(name)
            if cur is None or (cur.get("partial") and not t.get("partial")):
                ctx.sql_tables[name] = t
            else:
                cur["columns"].extend(c for c in t["columns"] if c["name"] not in {x["name"] for x in cur["columns"]})
                cur["fks"].extend(t["fks"])
        ctx.sql_rules += parsed["rules"]
    ctx.stats["entities"] = len(ctx.entities)
    ctx.stats["sql_tables"] = len(ctx.sql_tables)
    ctx.log(f"entities: {len(ctx.entities)}, SQL tables: {len(ctx.sql_tables)}")


def discover_business_rules(ctx: Context):
    if ctx.modernizer is None and ctx.rules_engine == "none":
        ctx.dto_rules, ctx.sql_rules, ctx.rules = {}, [], []
        ctx.stats["rules"] = 0
        ctx.log("business rules: skipped (use the plugin analysis to add them)")
        return
    if ctx.modernizer is not None:
        # business rules come exclusively from the code-modernization plugin: no heuristic detection
        ctx.dto_rules, ctx.sql_rules = {}, []
        ctx.rules = ctx.modernizer.collect(ctx)
        ctx.stats["rules"] = len(ctx.rules)
        return
    entity_names = set(ctx.entities)
    out: list[dict] = []
    for ci in ctx.classes:
        if ci.role in ("service", "helper", "controller", "entity"):
            for m in ci.methods:
                out += rl.method_rules(ci, m)
                if ci.role == "controller":
                    out += rl.authorization_rules(ci, m)
            out += rl.declared_rules(ci)
        if ci.role == "entity":
            out += rl.validation_rules(ci)
            out += rl.lifecycle_rules(ci)
        elif ci.role in (None, "helper") and any(f.ann(*be.VALIDATION_ANNS) for f in ci.ctor + ci.fields):
            dto = rl.validation_rules(ci)
            ctx.dto_rules[ci.name] = dto
            out += dto
    seen = set()
    for i, r in enumerate(out):
        r["role"] = next((c.role for c in ctx.by_name.get(r["owner"], [])), None)
        r["entities"], r["fields"] = _rule_entities(ctx, r, entity_names)
        key = (r["file"], r["line"], r["kind"], r["description"])
        if key in seen:
            r["dup"] = True
        seen.add(key)
        r["key"] = f"{r['file']}:{r['line']}:{r['kind']}:{i}"
    ctx.rules = [r for r in out if not r.get("dup")]
    if ctx.llm is not None:
        ls = ctx.llm.enrich(ctx)
        ctx.stats.update({f"llm_{k}": v for k, v in ls.items()})
        ctx.log(f"claude enrichment: {ls['refined']} refined, {ls['added']} added, {ls['rejected']} rejected (unverifiable), "
                f"{ls['errors']} error(s); {ctx.llm.calls} call(s), {ctx.llm.cached} cached")
    for r in ctx.sql_rules:
        r["key"] = f"{r['file']}:{r['line']}:{r['kind']}:{r['description'][:40]}"
    ctx.stats["rules"] = len(ctx.rules) + len(ctx.sql_rules)
    ctx.log(f"business rules: {ctx.stats['rules']}")


def _entity_pattern(name: str, prose: bool) -> str:
    if not prose:
        return r"\b%s\b" % re.escape(name)
    words = re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", name)
    return r"\b%ss?\b" % r"[\s_-]?".join(re.escape(w) for w in words)


def _rule_entities(ctx: Context, r: dict, entity_names: set[str]):
    scope = (r.get("scope") or "") + " " + (r.get("condition") or "") + " " + (r.get("description") or "")
    prose = r.get("source") == "modernization"      # business prose: "stock item", "orders" — not CamelCase identifiers
    ents = {n for n in entity_names if re.search(_entity_pattern(n, prose), scope, re.I if prose else 0)}
    if r["kind"] in ("validation", "lifecycle") and r["owner"] in entity_names:
        ents.add(r["owner"])
    if r["kind"] == "validation" and r["owner"] in ctx.dto_rules:
        ents |= {n for n in entity_names if n in r["owner"]}
    ci = next((c for c in ctx.by_name.get(r["owner"], [])), None)
    if not ents and r.get("via_var") and ci is not None:
        t = ci.deps.get(r["via_var"])
        rc = ctx.resolve_class(t, ci) if t else None
        if rc and rc.fqn in ctx.repo_info and ctx.repo_info[rc.fqn]["entity"] in entity_names:
            ents.add(ctx.repo_info[rc.fqn]["entity"])
    if not ents and ci is not None and (prose or r["kind"] in ("guard", "decision", "existence", "declared")) and r.get("method"):
        for m in ctx.methods_of(ci, r["method"]):
            for t, _ in m.calls:
                rc = ctx.resolve_class(t, ci)
                if rc and rc.fqn in ctx.repo_info and ctx.repo_info[rc.fqn]["entity"] in entity_names:
                    ents.add(ctx.repo_info[rc.fqn]["entity"])
            for f in m.param_list:
                if f.type in entity_names:
                    ents.add(f.type)
    fields = set()
    for e in ents:
        for col in ctx.entities[e]["columns"]:
            f = col["field"]
            cap = f[0].upper() + f[1:]
            if re.search(r"\b(get|is|set)%s\b|\.%s\b|\b%s\b" % (cap, f, f), scope):
                fields.add(f"{e}.{f}")
    return sorted(ents), sorted(fields)


# ================================================================ graph
def _method_closure(ctx: Context, ci: be.ClassInfo, m: be.Method):
    """Everything reachable from one method: methods, repository calls, topics, SQL tables."""
    seen: set[tuple] = set()
    visited: list[tuple] = []
    stack = [(ci, m)]
    repo_calls: dict[str, set] = defaultdict(set)
    sql: list[tuple] = []
    topics: set[str] = set()
    while stack:
        c, mt = stack.pop()
        key = (c.fqn, mt.name, mt.sig_start)
        if key in seen:
            continue
        seen.add(key)
        visited.append((c, mt))
        sql += [(r, w, c, mt) for r, w, _ in mt.sql]
        topics |= mt.publishes
        for t, mn in mt.calls:
            tc = ctx.resolve_class(t, c)
            if tc is None:
                continue
            if tc.role == "repository":
                repo_calls[tc.fqn].add(mn)
            else:
                stack.extend((tc, x) for x in ctx.methods_of(tc, mn))
    return seen, repo_calls, sql, topics, visited


def _method_field_access(ctx: Context, ci: be.ClassInfo, m: be.Method) -> dict[str, str]:
    """Heuristic: which entity fields a method reads ('r') or writes ('w')."""
    if m.open is None:
        return {}
    body = m.body_skel
    ents = {e for e in ctx.entities if re.search(r"\b%s\b" % re.escape(e), body)}
    ents |= {f.type for f in m.param_list if f.type in ctx.entities}
    for t, _ in m.calls:
        rc = ctx.resolve_class(t, ci)
        if rc and rc.fqn in ctx.repo_info and ctx.repo_info[rc.fqn]["entity"] in ctx.entities:
            ents.add(ctx.repo_info[rc.fqn]["entity"])
    out: dict[str, str] = {}
    for e in ents:
        for col in ctx.entities[e]["columns"]:
            f = col["field"]
            cap = f[0].upper() + f[1:]
            w = re.search(r"\.set%s\s*\(|\.%s\s*(?:[-+*]?=)(?!=)|\bcopy\([^)]*\b%s\s*=" % (cap, f, f), body)
            r = re.search(r"(?:\.|::)(?:get|is)%s\b|\.%s\b(?!\s*[-+*]?=(?!=))" % (cap, f), body)
            if w or r:
                out[f"{e}.{f}"] = ("r" if r else "") + ("w" if w else "")
    return out


def _table_name(ctx: Context, name: str) -> str | None:
    if name in ctx.entities:
        return ctx.entities[name]["table"].lower()
    if name in ctx.entity_by_ename:
        return ctx.entities[ctx.entity_by_ename[name]]["table"].lower()
    return name.lower()


def _repo_effects(ctx: Context, repo_fqn: str, mname: str) -> list[tuple[str, str]]:
    info = ctx.repo_info.get(repo_fqn, {})
    mi = info.get("methods", {}).get(mname)
    ent = info.get("entity")
    mode = (mi or {}).get("mode") or be.classify_method(mname)
    if mi and mi.get("sql"):
        r, w, _ = mi["sql"]
        res = [(_table_name(ctx, x), "R") for x in r] + [(_table_name(ctx, x), "W") for x in w]
        if res:
            return res
    if ent:
        return [(_table_name(ctx, ent), mode)]
    return []


def populate_graph(ctx: Context):
    g = ctx.graph
    repo = ctx.repo
    g.meta = {"repository": repo.name, "origin": repo.origin, "commit": repo.commit,
              "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "frameworks": sorted(ctx.fe.frameworks), "stats": ctx.stats,
              "rules_source": ctx.rules_provenance or {"engine": "none" if ctx.rules_engine == "none" else "built-in heuristics" + (" + Claude enrichment" if ctx.llm else "")}}
    role_nodes: dict[str, str] = {}   # class fqn -> node id

    # ---- tables & entities -------------------------------------------------
    table_ids: dict[str, str] = {}

    def table_node(name: str) -> str:
        name = name.lower()
        if name not in table_ids:
            t = ctx.sql_tables.get(name)
            ent = next((e for e, i in ctx.entities.items() if i["table"].lower() == name), None)
            cols = (t or {}).get("columns") or [
                {"name": c["column"], "type": c["type"], "pk": c["pk"], "nullable": c["nullable"], "fk": None}
                for c in (ctx.entities[ent]["columns"] if ent else []) if c["column"]]
            table_ids[name] = g.add_node("Table", name, key=name, columns=cols, dialect=(t or {}).get("dialect"),
                                         file=(t or {}).get("file"), line=(t or {}).get("line"), entity=ent,
                                         declared=bool(t), schema=ctx.entities[ent]["schema"] if ent else None)
        return table_ids[name]

    for name, info in ctx.entities.items():
        ci = ctx.entity_cls[name]
        if ci.has_ann("MappedSuperclass"):
            continue
        eid = g.add_node("Entity", name, key=ci.fqn, file=ci.file, line=ci.line, table=info["table"],
                         columns=info["columns"], lang=ci.lang)
        role_nodes[ci.fqn] = eid
        g.add_edge(eid, table_node(info["table"]), "USES", kind="mapping")
    for name, t in ctx.sql_tables.items():
        table_node(name)
    for name, info in ctx.entities.items():
        eid = role_nodes.get(ctx.entity_cls[name].fqn)
        for col in info["columns"]:
            tgt = col["target"]
            if eid and tgt in ctx.entities and role_nodes.get(ctx.entity_cls[tgt].fqn):
                g.add_edge(eid, role_nodes[ctx.entity_cls[tgt].fqn], "DEPENDS_ON", field=[col["field"]], relation=col["relation"])
    for name, t in ctx.sql_tables.items():
        for col, rt, rc in t["fks"]:
            if rt in ctx.sql_tables or rt in table_ids:
                g.add_edge(table_node(name), table_node(rt), "DEPENDS_ON", columns=[f"{col}->{rt}.{rc}"], kind="foreign-key")

    # ---- controllers, APIs -------------------------------------------------
    api_index: dict[tuple, str] = {}
    handler_api: dict[tuple, list[str]] = defaultdict(list)
    for ci in ctx.classes:
        if ci.role == "controller":
            role_nodes[ci.fqn] = g.add_node("Controller", ci.name, key=ci.fqn, file=ci.file, line=ci.line, lang=ci.lang)
    for ep in ctx.endpoints:
        ci = ep["ctrl"]
        aid = g.add_node("API", f"{ep['verb']} {ep['path']}", key=f"{ep['verb']} {ep['norm']}", verb=ep["verb"],
                         path=ep["path"], norm=ep["norm"], handler=f"{ci.name}.{ep['method'].name}", file=ci.file,
                         line=ep["line"], controller=ci.name)
        g.add_edge(role_nodes[ci.fqn], aid, "EXPOSES", handler=ep["method"].name)
        api_index[(ep["verb"], ep["norm"])] = aid
        handler_api[(ci.fqn, ep["method"].name)].append(aid)

    # ---- services, repositories -------------------------------------------
    for ci in ctx.classes:
        if ci.role in ("service", "helper"):
            role_nodes[ci.fqn] = g.add_node("Service", ci.name, key=ci.fqn, file=ci.file, line=ci.line, lang=ci.lang,
                                            helper=(ci.role == "helper"),
                                            methods=[m.name for m in ci.methods if not m.is_ctor])
        elif ci.role == "repository":
            role_nodes[ci.fqn] = g.add_node("Repository", ci.name, key=ci.fqn, file=ci.file, line=ci.line, lang=ci.lang,
                                            entity=ctx.repo_info[ci.fqn]["entity"],
                                            methods=sorted(ctx.repo_info[ci.fqn]["methods"]))
    # repository -> entity / table
    called_repo_methods: dict[str, set] = defaultdict(set)
    for ci in ctx.classes:
        for m in ci.methods:
            for t, mn in m.calls:
                rc = ctx.resolve_class(t, ci)
                if rc and rc.role == "repository":
                    called_repo_methods[rc.fqn].add(mn)
    for fqn, info in ctx.repo_info.items():
        rid = role_nodes[fqn]
        ent = info["entity"]
        if ent in ctx.entities and ctx.entity_cls[ent].fqn in role_nodes:
            g.add_edge(rid, role_nodes[ctx.entity_cls[ent].fqn], "USES")
        for mn in set(info["methods"]) | called_repo_methods[fqn]:
            for tname, mode in _repo_effects(ctx, fqn, mn):
                g.add_edge(rid, table_node(tname), "READS" if mode == "R" else "WRITES", methods=[mn],
                           fields=info["methods"].get(mn, {}).get("fields") or None)

    # ---- topics ------------------------------------------------------------
    topic_ids: dict[str, str] = {}

    def topic_node(name):
        if name not in topic_ids:
            topic_ids[name] = g.add_node("Topic", name, key=name)
        return topic_ids[name]

    for topic, ci, m in ctx.listeners:
        if ci.fqn in role_nodes:
            g.add_edge(topic_node(topic), role_nodes[ci.fqn], "CALLS", method=[m.name], kind="consume")

    # ---- call edges (class-level) + exact API closures --------------------
    def add_call_edges(src_id, ci, mt_list, derived=False, classlevel=True):
        for m in mt_list:
            for t, mn in sorted(m.calls):
                tc = ctx.resolve_class(t, ci)
                if tc is None or tc.fqn == ci.fqn or tc.fqn not in role_nodes:
                    continue
                dst = role_nodes[tc.fqn]
                if g.nodes[dst]["type"] == "Repository":
                    g.add_edge(src_id, dst, "USES", via=[f"{m.name}->{tc.name}.{mn}"], derived=derived or None)
                elif g.nodes[dst]["type"] in ("Service", "Controller"):
                    g.add_edge(src_id, dst, "CALLS", via=[f"{m.name}->{tc.name}.{mn}"], derived=derived or None)
            for sql_r, sql_w, _n in m.sql:
                for tname in sql_r:
                    g.add_edge(src_id, table_node(_table_name(ctx, tname)), "READS", via=[f"{ci.name}.{m.name} (SQL)"])
                for tname in sql_w:
                    g.add_edge(src_id, table_node(_table_name(ctx, tname)), "WRITES", via=[f"{ci.name}.{m.name} (SQL)"])
            for topic in m.publishes:
                g.add_edge(src_id, topic_node(topic), "WRITES", via=[f"{ci.name}.{m.name}"], kind="publish")

    for ci in ctx.classes:
        if ci.role in ("service", "helper"):
            add_call_edges(role_nodes[ci.fqn], ci, ci.methods)
        elif ci.role == "controller":
            add_call_edges(role_nodes[ci.fqn], ci, ci.methods, derived=True)

    # ---- business rules ----------------------------------------------------
    rule_ids: dict[str, str] = {}
    rules_by_owner_method: dict[tuple, list[str]] = defaultdict(list)
    class_level_rules: dict[str, list[str]] = defaultdict(list)
    sql_rule_by_table: dict[str, list[tuple]] = defaultdict(list)

    def tables_of_sql_sources(r: dict) -> set[str]:
        """Tables whose CREATE statement contains a cited `file.sql:line` (plugin cards cite DDL lines)."""
        found = set()
        for x in r.get("sources") or []:
            f, _, spec = x.rpartition(":")
            if not f.endswith(".sql"):
                continue
            for a, b in re.findall(r"(\d+)(?:\s*-\s*(\d+))?", spec):
                lo, hi = int(a), int(b or a)
                for name, t in ctx.sql_tables.items():
                    if t.get("file") == f and t.get("line") and t["line"] <= hi and lo <= t.get("end_line", t["line"]):
                        found.add(name)
        return found

    for r in ctx.rules:
        label = f"{r['rule_id']} · {r['description']}" if r.get("source") == "modernization" and r.get("rule_id") else r["description"]
        rid = g.add_node("BusinessRule", label[:90], key=r["key"], kind=r["kind"], description=r["description"],
                         condition=r["condition"], message=r["message"], exception=r["exception"], file=r["file"],
                         line=r["line"], owner=r["owner"], method=r["method"], confidence=r["confidence"],
                         layer=r.get("role"), fields=r["fields"], rule_id=r.get("rule_id"),
                         source=r.get("source", "static"), business_description=r.get("business_description"),
                         evidence=r.get("evidence"), llm_confirmed=r.get("llm_confirmed"), priority=r.get("priority"),
                         specification=r.get("specification"), parameters=r.get("parameters") or None,
                         edge_cases=r.get("edge_cases") or None, suspected_defect=r.get("suspected_defect") or None,
                         sme_question=r.get("sme_question") or None, notes=r.get("notes") or None, sources=r.get("sources") or None,
                         source_verified=r.get("source_verified"))
        if r.get("source") == "modernization" and any(x.rpartition(":")[0].endswith(".sql") for x in r.get("sources") or []):
            named = {t for t in table_ids if re.search(r"\b%s\b" % re.escape(t), r.get("scope") or "", re.I)}
            for tname in named | tables_of_sql_sources(r):
                if tname in table_ids:
                    g.add_edge(rid, table_ids[tname], "IMPACTS")
                    g.add_edge(table_ids[tname], rid, "EXECUTES")
                    sql_rule_by_table[tname].append((rid, r["kind"]))
        rule_ids[r["key"]] = rid
        for e in r["entities"]:
            if e in ctx.entity_cls and ctx.entity_cls[e].fqn in role_nodes:
                g.add_edge(rid, role_nodes[ctx.entity_cls[e].fqn], "IMPACTS", fields=[f for f in r["fields"] if f.startswith(e + ".")] or None)
        owner = next(iter(ctx.by_name.get(r["owner"], [])), None)
        if owner is None or owner.fqn not in role_nodes:
            continue
        if r["method"]:
            rules_by_owner_method[(owner.fqn, r["method"])].append(rid)
        else:
            class_level_rules[owner.fqn].append(rid)
        if owner.role == "controller" and r["method"] and handler_api.get((owner.fqn, r["method"])):
            for aid in handler_api[(owner.fqn, r["method"])]:
                g.add_edge(aid, rid, "EXECUTES")
        elif owner.role == "controller":
            g.add_edge(role_nodes[owner.fqn], rid, "EXECUTES")
        elif owner.role in ("service", "helper", "entity", "repository"):
            g.add_edge(role_nodes[owner.fqn], rid, "EXECUTES")
    for r in ctx.sql_rules:
        rid = g.add_node("BusinessRule", r["description"][:90], key=r["key"], kind=r["kind"], description=r["description"],
                         condition=r["condition"], file=r["file"], line=r["line"], owner=r["table"], confidence=1.0,
                         layer="database", source="static")
        rule_ids[r["key"]] = rid
        tid = table_node(r["table"])
        g.add_edge(rid, tid, "IMPACTS")
        g.add_edge(tid, rid, "EXECUTES")
        sql_rule_by_table[r["table"]].append((rid, r["kind"]))
        for w in r.get("also_writes", []):
            g.add_edge(rid, table_node(w), "IMPACTS")
            g.add_edge(rid, table_node(w), "WRITES", kind="trigger")

    # ---- API closures: exact (derived) edges -------------------------------
    entity_rules: dict[str, list[str]] = defaultdict(list)
    for r in ctx.rules:
        if r["owner"] in ctx.entity_cls and r["kind"] in ("validation", "lifecycle"):
            entity_rules[r["owner"]].append(rule_ids[r["key"]])
    for ep in ctx.endpoints:
        ci, hm = ep["ctrl"], ep["method"]
        aid = api_index[(ep["verb"], ep["norm"])]
        # direct (first-level) calls, following helper methods inside the controller only
        local, stack, seen_local = [], [hm], set()
        while stack:
            cur = stack.pop()
            if id(cur) in seen_local:
                continue
            seen_local.add(id(cur))
            local.append(cur)
            for t, mn in cur.calls:
                if t == ci.name:
                    stack.extend(ctx.methods_of(ci, mn))
        add_call_edges(aid, ci, local)
        seen, repo_calls, sql, topics, visited = _method_closure(ctx, ci, hm)
        for (fq, mname, _s) in seen:
            for rid in rules_by_owner_method.get((fq, mname), []):
                g.add_edge(aid, rid, "EXECUTES", derived=True)
        access: dict[str, str] = {}
        for c_, m_ in visited:
            for fld, mode in _method_field_access(ctx, c_, m_).items():
                access[fld] = "".join(sorted(set(access.get(fld, "") + mode)))
        for fqn, mns in repo_calls.items():
            ent = ctx.repo_info[fqn]["entity"]
            for mn in mns:
                mi = ctx.repo_info[fqn]["methods"].get(mn, {})
                for fld in mi.get("fields") or []:
                    access[f"{ent}.{fld}"] = "".join(sorted(set(access.get(f"{ent}.{fld}", "") + "r")))
                for fld in mi.get("set_fields") or []:
                    access[f"{ent}.{fld}"] = "".join(sorted(set(access.get(f"{ent}.{fld}", "") + "w")))
        if access:
            g.nodes[aid]["props"]["field_access"] = access
        for f in hm.param_list:
            if f.ann("Valid", "Validated") and f.type in ctx.dto_rules:
                for rr in ctx.dto_rules[f.type]:
                    g.add_edge(aid, rule_ids[rr["key"]], "EXECUTES", derived=True)
        for fqn, mns in repo_calls.items():
            ent = ctx.repo_info[fqn]["entity"]
            for mn in mns:
                for rid in rules_by_owner_method.get((fqn, mn), []):      # rule attached to a repository method
                    g.add_edge(aid, rid, "EXECUTES", derived=True)
                for tname, mode in _repo_effects(ctx, fqn, mn):
                    g.add_edge(aid, table_node(tname), "READS" if mode == "R" else "WRITES",
                               via=[f"{ctx.by_name[fqn.rsplit('.', 1)[-1]][0].name}.{mn}"], derived=True,
                               fields=ctx.repo_info[fqn]["methods"].get(mn, {}).get("fields") or None)
                    for rid in (entity_rules.get(ent, []) if mn in PERSIST_METHODS else []):
                        g.add_edge(aid, rid, "EXECUTES", derived=True)
                    if mode == "W":
                        for rid, kind in sql_rule_by_table.get(tname, []):
                            g.add_edge(aid, rid, "EXECUTES", derived=True)
        for r_, w_, c_, m_ in sql:
            for tname in r_:
                g.add_edge(aid, table_node(_table_name(ctx, tname)), "READS", via=[f"{c_.name}.{m_.name} (SQL)"], derived=True)
            for tname in w_:
                tn = _table_name(ctx, tname)
                g.add_edge(aid, table_node(tn), "WRITES", via=[f"{c_.name}.{m_.name} (SQL)"], derived=True)
                for rid, kind in sql_rule_by_table.get(tn, []):
                    g.add_edge(aid, rid, "EXECUTES", derived=True)
        for topic in topics:
            g.add_edge(aid, topic_node(topic), "WRITES", derived=True, kind="publish")
    # service-level exact table access (listeners, jobs): derived Service -> Table
    for ci in ctx.classes:
        if ci.role in ("service", "helper"):
            sid = role_nodes[ci.fqn]
            for m in ci.methods:
                _, repo_calls, sql, topics, _v = _method_closure(ctx, ci, m)
                for fqn, mns in repo_calls.items():
                    for mn in mns:
                        for tname, mode in _repo_effects(ctx, fqn, mn):
                            g.add_edge(sid, table_node(tname), "READS" if mode == "R" else "WRITES",
                                       via=[f"{ctx.by_name[fqn.rsplit('.', 1)[-1]][0].name}.{mn}"], derived=True)

    # ---- frontend ----------------------------------------------------------
    routed: dict[str, fe.FERoute] = {}
    route_by_comp: dict[str, list[fe.FERoute]] = defaultdict(list)
    for r in ctx.fe.routes:
        comp = None
        if r.component:
            comp = ctx.fe.lookup(r.component, r.file)
        if comp is None and r.lazy_file:
            tf = fe.resolve_file(r.file, r.lazy_file, ctx.ts_files)
            comp = next((u for u in ctx.fe.by_file.get(tf or "", []) if u.kind == "component"), None)
        if comp is not None:
            route_by_comp[comp.key].append(r)
    unit_ids: dict[str, str] = {}
    for key, u in ctx.fe.units.items():
        ntype = "Screen" if key in route_by_comp else "Component"
        unit_ids[key] = g.add_node(ntype, u.name, key=key, file=u.file, line=u.line, framework=u.framework,
                                   kind=u.kind, selector=u.selector)
    route_ids: dict[str, str] = {}
    for key, rs in route_by_comp.items():
        for r in rs:
            rn = norm_path(r.full)
            route_ids[rn] = g.add_node("Route", r.full or "/", key=rn, file=r.file, line=r.line, framework=r.framework,
                                       guards=r.guards or None)
            g.add_edge(route_ids[rn], unit_ids[key], "EXPOSES")
    backend_apis = [(g.nodes[a]["props"]["verb"], g.nodes[a]["props"]["norm"], a) for a in api_index.values()]

    def match_api(verb: str, path: str) -> str:
        hit = api_index.get((verb, path)) or api_index.get(("ANY", path))
        if hit:
            return hit
        segs = [s for s in path.split("/") if s]
        best, best_score = None, 0
        for v, bp, aid in backend_apis:
            if v not in (verb, "ANY"):
                continue
            bsegs = [s for s in bp.split("/") if s]
            n = min(len(segs), len(bsegs))
            if n == 0 or not any(s != "{}" for s in (segs if len(segs) <= len(bsegs) else bsegs)):
                continue
            a_, b_ = (segs[-n:], bsegs[-n:])
            if all(x == y or "{}" in (x, y) for x, y in zip(a_, b_)) and (len(segs) == n or len(bsegs) == n):
                score = n * 10 - abs(len(segs) - len(bsegs))
                if score > best_score:
                    best, best_score = aid, score
        if best:
            return best
        return g.add_node("API", f"{verb} {path}", key=f"{verb} {path}", verb=verb, path=path, norm=path, unresolved=True)

    for key, u in ctx.fe.units.items():
        uid = unit_ids[key]
        for m in u.methods.values():
            for ac in m.api:
                aid = match_api(ac.verb, ac.path)
                g.add_edge(uid, aid, "CALLS", via=[f"{u.name}.{m.name}" if m.name != "*" else u.name],
                           call_site=[f"{ac.file}:{ac.line}"])
        for ck in u.children:
            if ck in unit_ids:
                g.add_edge(uid, unit_ids[ck], "USES")
        for m in u.methods.values():
            for (uk, mn) in m.calls:
                if uk in unit_ids and uk != key:
                    g.add_edge(uid, unit_ids[uk], "USES", via=[f"{mn}"] if mn != "*" else None)
        for nv in u.nav:
            rn = norm_path(nv)
            if rn in route_ids:
                g.add_edge(uid, route_ids[rn], "USES", kind="navigation")
    for key in route_by_comp:
        u = ctx.fe.units[key]
        _, calls = ctx.fe.reach(u)
        for cu, ac in calls:
            aid = match_api(ac.verb, ac.path)
            g.add_edge(unit_ids[key], aid, "CALLS", derived=True, via=[f"{cu.name}"], call_site=[f"{ac.file}:{ac.line}"])

    # ---- capabilities -------------------------------------------------------
    _build_capabilities(ctx, role_nodes, unit_ids, route_by_comp)
    _attach_orphans(g)
    g.meta["stats"] = {**ctx.stats, **g.stats()}
    ctx.log(f"graph: {len(g.nodes)} nodes, {len(g.edges)} edges")


def _capability_name(ci: be.ClassInfo, eps: list[dict]) -> str:
    segs = []
    for ep in eps:
        parts = [p for p in ep["path"].split("/") if p and not GENERIC_SEGMENTS.match(p) and not p.startswith("{")]
        if parts:
            segs.append(parts[0])
    if segs:
        name = max(set(segs), key=segs.count)
    else:
        name = re.sub(r"(Rest)?Controller$", "", ci.name) or ci.name
    name = re.sub(r"[-_]+", " ", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name.strip().title()


def _build_capabilities(ctx: Context, role_nodes, unit_ids, route_by_comp):
    g = ctx.graph
    groups: dict[str, list[be.ClassInfo]] = defaultdict(list)
    for ci in ctx.classes:
        if ci.role == "controller":
            eps = [e for e in ctx.endpoints if e["ctrl"] is ci]
            groups[_capability_name(ci, eps)].append(ci)
    for name, ctrls in groups.items():
        cid = g.add_node("Capability", name, key=name.lower())
        apis = set()
        for ci in ctrls:
            for e, _ in [(e, 0) for e in g.out_edges(role_nodes[ci.fqn]) if e["rel"] == "EXPOSES"]:
                apis.add(e["dst"])
            g.add_edge(role_nodes[ci.fqn], cid, "IMPLEMENTS")
        members = set()
        frontier = list(apis)
        while frontier:
            n = frontier.pop()
            if g.nodes[n]["type"] == "Topic":
                continue   # asynchronous consumers belong to their own capability
            for nxt, e in g.flow_succ(n):
                if nxt in members or g.nodes[nxt]["type"] in ("Screen", "Component", "Route", "Capability"):
                    continue
                members.add(nxt)
                frontier.append(nxt)
        for a in apis:
            g.add_edge(cid, a, "EXPOSES")
        for mid in members:
            if g.nodes[mid]["type"] != "API":
                g.add_edge(mid, cid, "IMPLEMENTS")
        for a in apis:
            for src, e in g.flow_pred(a, derived=True):
                if g.nodes[src]["type"] == "Screen":
                    g.add_edge(cid, src, "EXPOSES")
                    g.add_edge(src, cid, "IMPLEMENTS")


def _attach_orphans(g: Graph):
    caps_of: dict[str, set] = defaultdict(set)
    for e in g.edges.values():
        if e["rel"] == "IMPLEMENTS" and g.nodes[e["dst"]]["type"] == "Capability":
            caps_of[e["src"]].add(e["dst"])
    for n in g.of_type("Service"):
        if caps_of.get(n["id"]):
            continue
        cands = {c for nxt, _ in g.flow_succ(n["id"]) for c in caps_of.get(nxt, ())}
        if len(cands) == 1:
            cid = next(iter(cands))
            g.add_edge(n["id"], cid, "IMPLEMENTS")
            for p, e in g.flow_pred(n["id"]):
                if g.nodes[p]["type"] == "Topic":
                    g.add_edge(p, cid, "IMPLEMENTS")


STAGES = {
    "scan_frontend": scan_frontend, "extract_routes": extract_routes, "detect_api_calls": detect_api_calls,
    "scan_backend": scan_backend, "discover_controllers": discover_controllers,
    "discover_services": discover_services, "discover_repositories": discover_repositories,
    "discover_entities": discover_entities, "discover_business_rules": discover_business_rules,
    "populate_graph": populate_graph,
}


def scan_repository(source: str, workdir="workspaces", branch=None, on_progress=lambda stage, msg: None, llm=None,
                    modernizer=None, rules_engine: str = "heuristic") -> Graph:
    """Load a repository (path or git URL) and run the whole pipeline.

    `llm`: optional llm.RuleEnricher; when given, source of business classes is sent to the model.
    `modernizer`: optional modernization.Modernizer; business rules then come ONLY from the plugin (no heuristics).
    """
    repo = load_repository(source, workdir, branch, log=lambda m: on_progress("scan_repository", m))
    if llm is not None and modernizer is not None:
        raise ValueError("choose one business-rule source: Claude enrichment (llm) or the modernization plugin")
    ctx = Context(repo)
    ctx.llm = llm
    ctx.modernizer = modernizer
    ctx.rules_engine = rules_engine
    if modernizer is not None:
        modernizer.log = lambda m: ctx.log(m)
    if llm is not None:
        llm.log = lambda m: ctx.log(m)
    for stage in PIPELINE:
        ctx.log = (lambda s: lambda m: on_progress(s, m))(stage)
        on_progress(stage, "started")
        STAGES[stage](ctx)
    return ctx.graph
