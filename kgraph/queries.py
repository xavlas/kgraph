"""The six knowledge-graph queries (explain_screen, show_call_chain, ...)."""
from __future__ import annotations

import re
from collections import deque

from .graph import Graph
from .ontology import NODE_TYPES, REL_TYPES

SKIP_IN_CHAINS = {"BusinessRule", "Entity", "Capability"}
STOP_AT = {"Table", "BusinessRule"}


class QueryError(Exception):
    def __init__(self, message, suggestions=None):
        super().__init__(message)
        self.suggestions = suggestions or []


def _ref(n: dict) -> dict:
    return {"id": n["id"], "type": n["type"], "name": n["name"]}


class QueryEngine:
    def __init__(self, graph: Graph):
        self.g = graph

    # ------------------------------------------------------------------ helpers
    def resolve(self, text: str, types=None) -> dict:
        g = self.g
        text = (text or "").strip()
        if text in g.nodes and (not types or g.nodes[text]["type"] in types):
            return g.nodes[text]
        hits = g.find(text, types, limit=25) if text else []
        if not hits:
            pool = sorted(n["name"] for n in g.of_type(*types))[:12] if types else []
            raise QueryError(f"No {'/'.join(types) if types else 'node'} matches '{text}'", pool)
        if types:
            hits.sort(key=lambda n: types.index(n["type"]))
        return hits[0]

    def _name(self, nid):
        return self.g.nodes[nid]["name"]

    def _out(self, nid, rel=None, dst_type=None, derived=None):
        res = []
        for e in self.g.out_edges(nid):
            if rel and e["rel"] != rel:
                continue
            if dst_type and self.g.nodes[e["dst"]]["type"] not in dst_type:
                continue
            if derived is not None and bool(e["props"].get("derived")) != derived:
                continue
            res.append(e)
        return res

    def _in(self, nid, rel=None, src_type=None, derived=None):
        res = []
        for e in self.g.in_edges(nid):
            if rel and e["rel"] != rel:
                continue
            if src_type and self.g.nodes[e["src"]]["type"] not in src_type:
                continue
            if derived is not None and bool(e["props"].get("derived")) != derived:
                continue
            res.append(e)
        return res

    def _routes(self, screen_id):
        return [self.g.nodes[e["src"]] for e in self._in(screen_id, "EXPOSES", {"Route"})]

    def _exact_apis(self, screen_id) -> list[str]:
        ex = [e["dst"] for e in self._out(screen_id, "CALLS", {"API"}, derived=True)]
        if ex:
            return ex
        seen, q, apis = {screen_id}, deque([screen_id]), []
        while q:
            n = q.popleft()
            for nxt, e in self.g.flow_succ(n):
                t = self.g.nodes[nxt]["type"]
                if t == "API":
                    apis.append(nxt)
                elif t in ("Component", "Screen") and nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        return list(dict.fromkeys(apis))

    def _shortest(self, src, dst, types=None, derived=False):
        prev = {src: None}
        q = deque([src])
        while q:
            n = q.popleft()
            if n == dst:
                break
            for nxt, e in self.g.flow_succ(n, derived=derived):
                if nxt not in prev and (not types or self.g.nodes[nxt]["type"] in types or nxt == dst):
                    prev[nxt] = (n, e)
                    q.append(nxt)
        if dst not in prev:
            return None
        path, cur = [], dst
        while cur is not None:
            p = prev[cur]
            path.append((cur, p[1] if p else None))
            cur = p[0] if p else None
        return list(reversed(path))

    def _path_dicts(self, path):
        return [{**_ref(self.g.nodes[nid]), "rel": e["rel"] if e else None,
                 "via": (e["props"].get("via") if e else None)} for nid, e in path]

    @staticmethod
    def _arrow(chain: list[dict]) -> str:
        out = []
        for i, n in enumerate(chain):
            if i:
                out.append(f" ─{n['rel']}→ ")
            out.append(f"{n['type']}:{n['name']}")
        return "".join(out)

    def _graph_payload(self, ids, focus=None, paths=None):
        ids = set(ids)
        edges = [[e["src"], e["dst"], e["rel"]] for e in self.g.edges.values()
                 if e["src"] in ids and e["dst"] in ids]
        return {"nodes": sorted(ids), "edges": edges, "focus": focus}

    # ------------------------------------------------------------ explain_screen
    def explain_screen(self, name: str) -> dict:
        g = self.g
        n = self.resolve(name, ["Screen", "Route", "Component"])
        if n["type"] == "Route":
            tgt = self._out(n["id"], "EXPOSES", {"Screen"})
            if not tgt:
                raise QueryError(f"Route {n['name']} has no screen")
            n = g.nodes[tgt[0]["dst"]]
        sid = n["id"]
        routes = self._routes(sid)
        ids = {sid}
        components = []
        seen, q = {sid}, deque([sid])
        while q:
            cur = q.popleft()
            for nxt, e in g.flow_succ(cur):
                nt = g.nodes[nxt]["type"]
                if nt in ("Component", "Screen") and nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
                    components.append(_ref(g.nodes[nxt]) | {"kind": g.nodes[nxt]["props"].get("kind")})
        nav = [g.nodes[e["dst"]] for e in self._out(sid, "USES", {"Route"})]
        apis = []
        for aid in self._exact_apis(sid):
            a = g.nodes[aid]
            ctrl = [g.nodes[e["src"]] for e in self._in(aid, "EXPOSES", {"Controller"})]
            reach = self._backend_reach(aid)
            tables = []
            for e in self._out(aid, None, {"Table"}, derived=True):
                if e["rel"] in ("READS", "WRITES"):
                    tables.append({"table": self._name(e["dst"]), "mode": "R" if e["rel"] == "READS" else "W",
                                   "via": e["props"].get("via")})
            rules = [_ref(g.nodes[e["dst"]]) | {"kind": g.nodes[e["dst"]]["props"].get("kind"),
                                                "confidence": g.nodes[e["dst"]]["props"].get("confidence")}
                     for e in self._out(aid, "EXECUTES", {"BusinessRule"})]
            topics = [self._name(e["dst"]) for e in self._out(aid, "WRITES", {"Topic"}, derived=True)]
            async_chain = []
            for tp in topics:
                tid = next((i for i, x in g.nodes.items() if x["type"] == "Topic" and x["name"] == tp), None)
                for nxt, _e in g.flow_succ(tid) if tid else []:
                    async_chain.append(f"{tp} → {self._name(nxt)}")
            apis.append({"api": _ref(a), "unresolved": bool(a["props"].get("unresolved")),
                         "controller": ctrl[0]["name"] if ctrl else None, "handler": a["props"].get("handler"),
                         "services": reach["services"], "repositories": reach["repos"],
                         "tables": sorted(tables, key=lambda t: (t["table"], t["mode"])),
                         "fields": a["props"].get("field_access") or {}, "rules": rules, "topics": topics,
                         "async": async_chain, "client_calls": [e["props"].get("via") for e in self._in(aid, "CALLS", {"Screen", "Component"})][:3]})
            ids |= {aid} | {x for x in (e["src"] for e in self._in(aid, "EXPOSES"))} | set(reach["ids"])
            ids |= {e["dst"] for e in self._out(aid, None, {"Table", "BusinessRule", "Topic"}, derived=True)}
        ids |= {c["id"] for c in components} | {r["id"] for r in routes}
        caps = [g.nodes[e["dst"]]["name"] for e in self._out(sid, "IMPLEMENTS", {"Capability"})]
        ids |= {e["dst"] for e in self._out(sid, "IMPLEMENTS", {"Capability"})}
        summary = self._screen_narrative(n, routes, components, apis, caps, nav)
        return {"query": "explain_screen", "target": _ref(n), "title": f"Screen {n['name']}", "summary": summary,
                "data": {"screen": _ref(n) | n["props"], "routes": [r["name"] for r in routes], "components": components,
                         "navigates_to": [r["name"] for r in nav], "apis": apis, "capabilities": caps},
                "graph": self._graph_payload(ids, sid)}

    def _backend_reach(self, aid):
        g = self.g
        services, repos, ids = [], [], set()
        seen, q = {aid}, deque([aid])
        while q:
            cur = q.popleft()
            for nxt, e in g.flow_succ(cur):
                t = g.nodes[nxt]["type"]
                if nxt in seen or t in ("Table", "BusinessRule", "Entity", "Controller", "Route", "Capability", "Topic"):
                    continue
                seen.add(nxt)
                q.append(nxt)
                ids.add(nxt)
                if t == "Service":
                    services.append(g.nodes[nxt]["name"])
                elif t == "Repository":
                    repos.append(g.nodes[nxt]["name"])
        return {"services": services, "repos": repos, "ids": ids}

    def _screen_narrative(self, n, routes, components, apis, caps, nav) -> str:
        fw = {"angular": "an Angular", "react": "a React"}.get(n["props"].get("framework", ""), "a")
        lines = [f"**{n['name']}** is {fw} screen" + (f" exposed on route {', '.join('`'+r['name']+'`' for r in routes)}" if routes else " (not routed)") + "."]
        if components:
            lines.append("It is composed of / relies on: " + ", ".join(f"{c['name']} ({c.get('kind')})" for c in components) + ".")
        if nav:
            lines.append("It links to: " + ", ".join(f"`{r['name']}`" for r in nav) + ".")
        if caps:
            lines.append("Business capability: " + ", ".join(caps) + ".")
        if not apis:
            lines.append("No backend API call was detected from this screen.")
        for a in apis:
            parts = [f"- `{a['api']['name']}`"]
            if a["unresolved"]:
                parts.append("(no matching backend endpoint found)")
            else:
                if a["controller"]:
                    parts.append(f"→ {a['handler']}")
                if a["services"]:
                    parts.append("→ " + " → ".join(a["services"]))
                if a["repositories"]:
                    parts.append("→ " + ", ".join(a["repositories"]))
            rd = [t["table"] for t in a["tables"] if t["mode"] == "R"]
            wr = [t["table"] for t in a["tables"] if t["mode"] == "W"]
            if rd:
                parts.append(f"; reads `{'`, `'.join(dict.fromkeys(rd))}`")
            if wr:
                parts.append(f"; writes `{'`, `'.join(dict.fromkeys(wr))}`")
            if a["topics"]:
                parts.append(f"; publishes `{'`, `'.join(a['topics'])}`")
            if a["rules"]:
                parts.append(f"; enforces {len(a['rules'])} business rule(s)")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    # ---------------------------------------------------------- show_call_chain
    def _chain_paths(self, s: dict, tgt: dict | None, allowed_apis: set | None, max_chains: int, max_depth: int) -> list[list[tuple]]:
        """Chains [(node id, edge)] from `s`, to `tgt` or down to the data layer. Overridden by the Neo4j engine."""
        g = self.g
        chains: list[list[tuple]] = []

        def ok(nid, e):
            t = g.nodes[nid]["type"]
            if tgt and nid == tgt["id"]:
                return True
            if t in SKIP_IN_CHAINS or (t == "Route"):
                return False
            if t == "API" and allowed_apis is not None and nid not in allowed_apis:
                return False
            return True

        def dfs(nid, path, visited, derived):
            if len(chains) >= max_chains:
                return
            t = g.nodes[nid]["type"]
            if tgt and nid == tgt["id"]:
                chains.append(path)
                return
            nxts = [(x, e) for x, e in g.flow_succ(nid, derived=derived) if x not in visited and ok(x, e)]
            if not tgt and (t in STOP_AT or not nxts) and len(path) > 1:
                chains.append(path)
                return
            if len(path) >= max_depth:
                return
            for x, e in nxts:
                dfs(x, path + [(x, e)], visited | {x}, derived)

        for derived in (False, True):
            dfs(s["id"], [(s["id"], None)], {s["id"]}, derived)
            if chains:
                break
        return chains

    def show_call_chain(self, source: str, target: str | None = None, max_chains: int = 60, max_depth: int = 14) -> dict:
        g = self.g
        s = self.resolve(source)
        allowed_apis = None
        if s["type"] == "Screen":
            allowed_apis = set(self._exact_apis(s["id"]))
        tgt = self.resolve(target) if target else None
        chains = self._chain_paths(s, tgt, allowed_apis, max_chains, max_depth)
        text = [self._arrow(self._path_dicts(c)) for c in chains]
        ids = {nid for c in chains for nid, _ in c}
        mermaid = self._mermaid(chains)
        if not chains:
            summary = f"No call chain found from {s['name']}" + (f" to {tgt['name']}." if tgt else ".")
        else:
            summary = f"{len(chains)} chain(s) from **{s['name']}**" + (f" to **{tgt['name']}**" if tgt else " down to the data layer") + ":\n\n" + "\n".join(f"- {t}" for t in text)
        return {"query": "show_call_chain", "target": _ref(s), "title": f"Call chains from {s['name']}", "summary": summary,
                "data": {"source": _ref(s), "target": _ref(tgt) if tgt else None,
                         "chains": [self._path_dicts(c) for c in chains], "mermaid": mermaid},
                "graph": self._graph_payload(ids, s["id"])}

    def _mermaid(self, chains) -> str:
        ids, lines = {}, ["flowchart LR"]

        def nid(x):
            if x not in ids:
                ids[x] = f"n{len(ids)}"
                n = self.g.nodes[x]
                label = n["name"].replace('"', "'")
                lines.append(f'  {ids[x]}["{n["type"]}<br/>{label}"]')
            return ids[x]
        seen = set()
        for c in chains:
            for (a, _), (b, e) in zip(c, c[1:]):
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                lines.append(f"  {nid(a)} -->|{e['rel']}| {nid(b)}")
        return "\n".join(lines)

    # ------------------------------------------------------- find_business_rules
    def _rules_of(self, n: dict) -> dict[str, set]:
        """rule id -> set of executor ids, in the scope of node `n`."""
        g = self.g
        t = n["type"]
        res: dict[str, set] = {}

        def add(rid, by):
            res.setdefault(rid, set()).add(by)

        def from_flow(start, derived=False):
            seen, q = {start}, deque([start])
            while q:
                cur = q.popleft()
                for nxt, e in g.flow_succ(cur, derived=derived):
                    nt = g.nodes[nxt]["type"]
                    if nt == "BusinessRule":
                        add(nxt, cur)
                    elif nxt not in seen and nt not in ("Route", "Capability"):
                        seen.add(nxt)
                        q.append(nxt)

        if t == "Route":
            for e in self._out(n["id"], "EXPOSES", {"Screen"}):
                for rid, by in self._rules_of(g.nodes[e["dst"]]).items():
                    res.setdefault(rid, set()).update(by)
        elif t == "BusinessRule":
            res[n["id"]] = {e["src"] for e in self._in(n["id"], "EXECUTES")}
        elif t == "API":
            for e in self._out(n["id"], "EXECUTES", {"BusinessRule"}):
                add(e["dst"], n["id"])
        elif t in ("Screen", "Component"):
            for aid in self._exact_apis(n["id"]):
                for rid, by in self._rules_of(g.nodes[aid]).items():
                    res.setdefault(rid, set()).update(by)
        elif t == "Capability":
            for e in self.g.in_edges(n["id"]):
                if e["rel"] == "IMPLEMENTS":
                    src = g.nodes[e["src"]]
                    if src["type"] == "BusinessRule":
                        add(src["id"], n["id"])
                    elif src["type"] in ("Service", "Controller", "Entity", "Table"):
                        for rid, by in self._rules_of(src).items():
                            res.setdefault(rid, set()).update(by)
            for e in self._out(n["id"], "EXPOSES", {"API"}):
                for rid, by in self._rules_of(g.nodes[e["dst"]]).items():
                    res.setdefault(rid, set()).update(by)
        elif t in ("Entity", "Table"):
            for r, e in g.assoc(n["id"], "IMPACTS"):
                if g.nodes[r]["type"] == "BusinessRule":
                    res.setdefault(r, set()).update(x["src"] for x in self._in(r, "EXECUTES"))
            for e in self._out(n["id"], "EXECUTES", {"BusinessRule"}):
                add(e["dst"], n["id"])
        else:
            from_flow(n["id"])
            for e in self._out(n["id"], "EXECUTES", {"BusinessRule"}):
                add(e["dst"], n["id"])
        return res

    def find_business_rules(self, text: str | None = None, scope: str | None = None, kind: str | None = None,
                            min_confidence: float = 0.0) -> dict:
        g = self.g
        scope_n = self.resolve(scope) if scope else None
        if scope_n:
            found = self._rules_of(scope_n)
            rules = [(g.nodes[r], ex) for r, ex in found.items()]
        else:
            rules = [(n, {e["src"] for e in self._in(n["id"], "EXECUTES")}) for n in g.of_type("BusinessRule")]
        terms = [w for w in re.split(r"\W+", (text or "").lower()) if len(w) > 2]
        out = []
        for n, ex in rules:
            p = n["props"]
            if kind and p.get("kind") != kind:
                continue
            if (p.get("confidence") or 0) < min_confidence:
                continue
            hay = " ".join(str(p.get(k) or "") for k in ("description", "condition", "message", "owner", "method", "fields", "kind")).lower()
            hay += " " + " ".join(self._name(i).lower() for i, _ in g.assoc(n["id"], "IMPACTS"))
            if terms and not all(t in hay for t in terms):
                continue
            out.append({"id": n["id"], "kind": p.get("kind"), "description": p.get("description"),
                        "condition": p.get("condition"), "message": p.get("message"), "location": f"{p.get('file')}:{p.get('line')}",
                        "owner": p.get("owner"), "method": p.get("method"), "confidence": p.get("confidence"),
                        "impacts": sorted(self._name(i) for i, _ in g.assoc(n["id"], "IMPACTS")),
                        "fields": p.get("fields") or [], "enforced_by": sorted({self._name(x) for x in ex}),
                        "source": p.get("source", "static"), "business_description": p.get("business_description"),
                        "evidence": p.get("evidence"), "priority": p.get("priority"), "rule_id": p.get("rule_id"),
                        "specification": p.get("specification"), "suspected_defect": p.get("suspected_defect"),
                        "sme_question": p.get("sme_question"), "sources": p.get("sources") or [],
                        "source_verified": p.get("source_verified")})
        out.sort(key=lambda r: (-(r["confidence"] or 0), r["kind"] or "", r["description"] or ""))
        by_kind: dict[str, int] = {}
        for r in out:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        head = f"{len(out)} business rule(s)" + (f" in scope of **{scope_n['name']}**" if scope_n else "")
        lines = [head + (" — " + ", ".join(f"{k}: {v}" for k, v in sorted(by_kind.items())) if by_kind else "") + "\n"]
        for r in out[:80]:
            lines.append(f"- **[{r['kind']}]** {(r['priority'] + ' ') if r['priority'] else ''}{r['business_description'] or r['description']} — `{r['location']}`"
                         + (f" (impacts {', '.join(r['impacts'])})" if r["impacts"] else "")
                         + (" · *Claude*" if r["source"] == "llm" else " · *modernization plugin*" if r["source"] == "modernization" else "")
                         + (" · ⚠ suspected defect" if r["suspected_defect"] else "")
                         + (" · ⚠ source not located in the repository" if r["source_verified"] is False else ""))
        ids = {r["id"] for r in out}
        for r in out:
            ids |= {i for i, _ in g.assoc(r["id"], "IMPACTS")} | {x["src"] for x in self._in(r["id"], "EXECUTES")}
        return {"query": "find_business_rules", "target": _ref(scope_n) if scope_n else None, "title": "Business rules",
                "summary": "\n".join(lines), "data": {"rules": out, "by_kind": by_kind},
                "graph": self._graph_payload(ids, scope_n["id"] if scope_n else None)}

    # --------------------------------------------------------- determine_impact
    def _impact_neighbors(self, nid):
        g = self.g
        res = list(g.flow_pred(nid, derived=True))
        if g.nodes[nid]["type"] in ("Entity", "Table"):
            res += [(r, e) for r, e in g.assoc(nid, "IMPACTS") if e["dst"] == nid and g.nodes[r]["type"] == "BusinessRule"]
        return res

    def _impact_reach(self, tid: str, direction: str, max_depth: int | None):
        """({node id: distance}, path_to(node id) -> ["Type:name", ...]).  Overridden by the Neo4j engine (one Cypher traversal)."""
        g = self.g
        nbrs = self._impact_neighbors if direction == "upstream" else (lambda x: g.flow_succ(x))
        prev = {tid: None}
        dist = {tid: 0}
        q = deque([tid])
        while q:
            cur = q.popleft()
            if max_depth is not None and dist[cur] >= max_depth:
                continue
            for nxt, e in nbrs(cur):
                if nxt not in prev:
                    prev[nxt] = (cur, e)
                    dist[nxt] = dist[cur] + 1
                    q.append(nxt)

        def path_to(nid):
            names, cur = [], nid
            while cur is not None:
                names.append(f"{g.nodes[cur]['type']}:{g.nodes[cur]['name']}")
                cur = prev[cur][0] if prev[cur] else None
            return names if direction == "upstream" else list(reversed(names))
        return dist, path_to

    def determine_impact(self, name: str, direction: str = "upstream", max_depth: int | None = None) -> dict:
        g = self.g
        t = self.resolve(name)
        tid = t["id"]
        dist, path_to = self._impact_reach(tid, direction, max_depth)

        groups: dict[str, list] = {}
        for nid, d in sorted(dist.items(), key=lambda x: (x[1], g.nodes[x[0]]["name"])):
            if nid == tid:
                continue
            n = g.nodes[nid]
            groups.setdefault(n["type"], []).append({"id": nid, "name": n["name"], "distance": d, "path": path_to(nid)})
        # controllers are implicit containers of impacted APIs
        for a in list(groups.get("API", [])):
            for e in self._in(a["id"], "EXPOSES", {"Controller"}):
                if e["src"] not in dist:
                    groups.setdefault("Controller", []).append({"id": e["src"], "name": self._name(e["src"]),
                                                                 "distance": a["distance"] + 1, "path": a["path"]})
                    dist[e["src"]] = a["distance"] + 1
        affects = []
        if t["type"] == "BusinessRule":
            affects = [{"id": i, "type": g.nodes[i]["type"], "name": self._name(i)} for i, _ in g.assoc(tid, "IMPACTS")]
        n_screens, n_apis = len(groups.get("Screen", [])), len(groups.get("API", []))
        score = 3 * n_screens + 2 * n_apis + len(groups.get("Service", [])) + 0.5 * len(groups.get("BusinessRule", [])) \
            + 2 * len(groups.get("Topic", []))
        risk = "high" if score >= 15 else "medium" if score >= 6 else "low"
        order = ["Route", "Screen", "Component", "Capability", "API", "Controller", "Service", "Topic", "BusinessRule",
                 "Repository", "Entity", "Table"]
        lines = [f"Changing **{t['type']}:{t['name']}** {'impacts' if direction == 'upstream' else 'depends on'} "
                 f"{sum(len(v) for v in groups.values())} element(s). Estimated risk: **{risk}** (score {score:g}).\n"]
        for typ in order:
            items = groups.get(typ)
            if items:
                lines.append(f"- **{typ}** ({len(items)}): " + ", ".join(f"{i['name']}" for i in items[:12]) + (" …" if len(items) > 12 else ""))
        if direction == "upstream" and groups.get("Screen"):
            lines.append("\nUser-facing paths (screen → … → change):")
            for s in groups["Screen"][:10]:
                lines.append("- " + " → ".join(s["path"]))
        ids = {tid} | {i["id"] for v in groups.values() for i in v}
        return {"query": "determine_impact", "target": _ref(t), "title": f"Impact of {t['name']}", "summary": "\n".join(lines),
                "data": {"direction": direction, "risk": risk, "score": score, "impacted": groups, "affects": affects,
                         "counts": {k: len(v) for k, v in groups.items()}},
                "graph": self._graph_payload(ids, tid)}

    # ---------------------------------------------------------- show_capability
    def show_capability(self, name: str | None = None) -> dict:
        g = self.g
        caps = g.of_type("Capability")
        if not name:
            rows = []
            for c in caps:
                m = self._capability_members(c["id"])
                rows.append({"capability": _ref(c), "screens": len(m["Screen"]), "apis": len(m["API"]),
                             "services": len(m["Service"]), "tables": len(m["Table"]), "rules": len(m["BusinessRule"])})
            lines = [f"{len(rows)} capabilities:"] + [
                f"- **{r['capability']['name']}**: {r['screens']} screen(s), {r['apis']} API(s), {r['services']} service(s), "
                f"{r['tables']} table(s), {r['rules']} rule(s)" for r in rows]
            return {"query": "show_capability", "target": None, "title": "Capabilities", "summary": "\n".join(lines),
                    "data": {"capabilities": rows}, "graph": self._graph_payload({c["id"] for c in caps})}
        c = self.resolve(name, ["Capability"])
        m = self._capability_members(c["id"])
        shared = []
        for other in caps:
            if other["id"] == c["id"]:
                continue
            om = self._capability_members(other["id"])
            common = [(t, x) for t in ("Table", "Service", "Entity", "Topic") for x in m[t] if x in om[t]]
            if common:
                shared.append({"capability": other["name"], "shared": [f"{t}:{self._name(x)}" for t, x in common]})
        lines = [f"Capability **{c['name']}**:"]
        for typ in ("Screen", "Route", "API", "Controller", "Service", "Repository", "Entity", "Table", "Topic", "BusinessRule"):
            if m[typ]:
                names = [self._name(i) for i in m[typ]]
                lines.append(f"- **{typ}** ({len(names)}): " + ", ".join(names[:10]) + (" …" if len(names) > 10 else ""))
        for s in shared:
            lines.append(f"- shares {', '.join(s['shared'][:6])} with capability **{s['capability']}**")
        ids = {c["id"]} | {i for v in m.values() for i in v}
        return {"query": "show_capability", "target": _ref(c), "title": f"Capability {c['name']}", "summary": "\n".join(lines),
                "data": {"capability": _ref(c), "members": {k: [_ref(g.nodes[i]) for i in v] for k, v in m.items()},
                         "shared_with": shared}, "graph": self._graph_payload(ids, c["id"])}

    def _capability_members(self, cid) -> dict[str, list]:
        g = self.g
        m: dict[str, list] = {t: [] for t in ("Screen", "Route", "API", "Controller", "Service", "Repository", "Entity",
                                              "Table", "Topic", "BusinessRule", "Component")}
        for e in g.in_edges(cid):
            if e["rel"] == "IMPLEMENTS" and g.nodes[e["src"]]["type"] in m:
                m[g.nodes[e["src"]]["type"]].append(e["src"])
        for e in g.out_edges(cid):
            if e["rel"] == "EXPOSES" and g.nodes[e["dst"]]["type"] in m:
                m[g.nodes[e["dst"]]["type"]].append(e["dst"])
        for s in m["Screen"]:
            for r in self._routes(s):
                m["Route"].append(r["id"])
        for k in m:
            m[k] = sorted(dict.fromkeys(m[k]), key=lambda i: (self.g.nodes[i]["name"], i))
        return m

    # --------------------------------------------------------- find_data_lineage
    def find_data_lineage(self, target: str, column: str | None = None) -> dict:
        g = self.g
        if column is None and "." in target and target not in g.nodes:
            head, _, col = target.partition(".")
            if g.find(head, ["Table", "Entity"]):
                target, column = head, col
        t = self.resolve(target, ["Table", "Entity", "Screen", "API", "Component"])
        if t["type"] == "Entity":
            tabs = self._out(t["id"], "USES", {"Table"})
            if not tabs:
                raise QueryError(f"Entity {t['name']} has no mapped table")
            t = g.nodes[tabs[0]["dst"]]
        if t["type"] == "Table":
            return self._lineage_of_table(t, column)
        return self._lineage_forward(t)

    def _field_for_column(self, table: dict, column: str):
        cols = table["props"].get("columns") or []
        ent = table["props"].get("entity")
        ent_cols = []
        if ent:
            for n in self.g.of_type("Entity"):
                if n["name"] == ent:
                    ent_cols = n["props"].get("columns") or []
        for c in ent_cols:
            if column.lower() in ((c.get("column") or "").lower(), c["field"].lower()):
                return f"{ent}.{c['field']}", c.get("column") or column
        if any(c["name"] == column.lower() for c in cols):
            return None, column.lower()
        raise QueryError(f"Column '{column}' not found in {table['name']}", [c["name"] for c in cols])

    def _lineage_of_table(self, t: dict, column: str | None) -> dict:
        g = self.g
        field = col = None
        if column:
            field, col = self._field_for_column(t, column)
        entries = []
        direct_services = []
        per_api: dict[str, dict] = {}
        for e in g.in_edges(t["id"]):
            if e["rel"] not in ("READS", "WRITES") or not e["props"].get("derived"):
                continue
            src = g.nodes[e["src"]]
            mode = "R" if e["rel"] == "READS" else "W"
            if src["type"] == "API":
                per_api.setdefault(e["src"], {})[mode] = e["props"].get("via") or []
            elif src["type"] == "Service":
                direct_services.append((src, mode, e))
        ent = t["props"].get("entity")
        for aid, per in per_api.items():
            src = g.nodes[aid]
            access = src["props"].get("field_access") or {}
            if column:
                if not field or field not in access:
                    continue
                modes = [m.upper() for m in access[field]]
            else:
                modes = list(per)
            screens = [{"screen": self._name(ce["src"]), "routes": [r["name"] for r in self._routes(ce["src"])]}
                       for ce in self._in(aid, "CALLS", {"Screen"}, derived=True)]
            for mode in modes:
                via = per.get(mode) or [v for vs in per.values() for v in vs]
                entries.append({"mode": mode, "api": src["name"], "screens": screens, "via": via,
                                "chain": self._chain_to_via(aid, via),
                                "fields": [field] if column else sorted(f for f in access if ent and f.startswith(ent + "."))})
        covered = {c for en in entries for c in en["chain"]}
        for src, mode, e in direct_services:
            if column or src["name"] in covered:
                continue
            roots = self._reverse_roots(src["id"])
            entries.append({"mode": mode, "api": None, "screens": [], "via": e["props"].get("via") or [],
                            "chain": roots + [src["name"]], "fields": []})
        owners = [t["id"]] + [e["src"] for e in self._in(t["id"], "USES", {"Entity"})]
        rules = list(dict.fromkeys(r for o in owners for r, _ in g.assoc(o, "IMPACTS") if g.nodes[r]["type"] == "BusinessRule"))
        rules_data = [{"id": r, "description": g.nodes[r]["props"].get("description"), "kind": g.nodes[r]["props"].get("kind")}
                      for r in rules if not col or (col in (g.nodes[r]["props"].get("description") or "") or (field or "\0") in (g.nodes[r]["props"].get("fields") or []))]
        downstream = []
        for r in rules:
            for i, e in g.assoc(r, "IMPACTS"):
                if i != t["id"] and g.nodes[i]["type"] == "Table":
                    downstream.append(f"{g.nodes[r]['props'].get('description')} → writes {self._name(i)}")
        fk_up = [self._name(e["dst"]) for e in self._out(t["id"], "DEPENDS_ON", {"Table"})]
        fk_down = [self._name(e["src"]) for e in self._in(t["id"], "DEPENDS_ON", {"Table"})]
        writers = [e for e in entries if "W" in e["mode"]]
        readers = [e for e in entries if e["mode"] == "R"]
        label = f"{t['name']}.{col}" if col else t["name"]
        lines = [f"Data lineage of **{label}**" + (" (column-level, heuristic)" if col else "") + f": {len(writers)} writer(s), {len(readers)} reader(s)."]
        for title, items in (("Writes", writers), ("Reads", readers)):
            for en in items:
                scr = ", ".join(f"{s['screen']}" + (f" ({s['routes'][0]})" if s["routes"] else "") for s in en["screens"]) or "no screen (async / batch)"
                lines.append(f"- **{title}** ← {scr} → {en['api'] or 'internal flow'} → " + " → ".join(en["chain"][1:] if en["api"] else en["chain"]) + (f" [{', '.join(en['via'])}]" if en["via"] else ""))
        if fk_up:
            lines.append(f"- references (FK): {', '.join(fk_up)}")
        if fk_down:
            lines.append(f"- referenced by (FK): {', '.join(fk_down)}")
        for d in downstream:
            lines.append(f"- downstream: {d}")
        if rules_data:
            lines.append(f"- {len(rules_data)} rule(s) constrain this {'column' if col else 'table'}.")
        ids = {t["id"]} | set(rules)
        for en in entries:
            for nm in en["chain"] + [en["api"] or ""] + [s["screen"] for s in en["screens"]]:
                ids |= {n["id"] for n in g.nodes.values() if n["name"] == nm and n["type"] in ("Screen", "API", "Service", "Repository", "Controller")}
        ids |= {e["src"] for e in g.in_edges(t["id"]) if e["rel"] in ("READS", "WRITES") and e["src"] in ids}
        for en in entries:
            for sc in en["screens"]:
                ids |= {r["id"] for n in g.of_type("Screen") if n["name"] == sc["screen"] for r in self._routes(n["id"])}
        return {"query": "find_data_lineage", "target": _ref(t), "title": f"Data lineage of {label}", "summary": "\n".join(lines),
                "data": {"table": _ref(t), "column": col, "writers": writers, "readers": readers, "rules": rules_data,
                         "fk_references": fk_up, "fk_referenced_by": fk_down, "downstream": downstream},
                "graph": self._graph_payload(ids, t["id"])}

    def _chain_to_via(self, aid, via: list[str]) -> list[str]:
        g = self.g
        names = [g.nodes[aid]["name"]]
        for v in via[:1]:
            cls = re.split(r"[.\s]", v)[0]
            dst = next((i for i, n in g.nodes.items() if n["name"] == cls and n["type"] in ("Repository", "Service")), None)
            if dst:
                p = self._shortest(aid, dst, types={"Service", "Repository", "Controller"})
                if p:
                    names = [g.nodes[i]["name"] for i, _ in p]
        return names

    def _reverse_roots(self, nid) -> list[str]:
        g = self.g
        chain, cur, seen = [], nid, {nid}
        while True:
            preds = [(p, e) for p, e in g.flow_pred(cur) if p not in seen and g.nodes[p]["type"] in ("Service", "Topic", "API", "Controller")]
            if not preds:
                break
            p, _ = preds[0]
            chain.append(g.nodes[p]["name"])
            seen.add(p)
            cur = p
        return list(reversed(chain))

    def _lineage_forward(self, n: dict) -> dict:
        g = self.g
        apis = self._exact_apis(n["id"]) if n["type"] in ("Screen", "Component") else [n["id"]]
        touched: dict[str, dict] = {}
        for aid in apis:
            a = g.nodes[aid]
            access = a["props"].get("field_access") or {}
            for e in self._out(aid, None, {"Table"}, derived=True):
                if e["rel"] not in ("READS", "WRITES"):
                    continue
                tn = self._name(e["dst"])
                d = touched.setdefault(tn, {"table": tn, "modes": set(), "apis": set(), "via": set(), "fields": {}})
                d["modes"].add("R" if e["rel"] == "READS" else "W")
                d["apis"].add(a["name"])
                d["via"].update(e["props"].get("via") or [])
                ent = g.nodes[e["dst"]]["props"].get("entity")
                for f, m in access.items():
                    if ent and f.startswith(ent + "."):
                        d["fields"][f.split(".", 1)[1]] = m
        rows = [{"table": d["table"], "mode": "".join(sorted(d["modes"])), "apis": sorted(d["apis"]), "via": sorted(d["via"]),
                 "columns": d["fields"]} for d in touched.values()]
        lines = [f"**{n['name']}** touches {len(rows)} table(s):"]
        for r in rows:
            cols = f" — columns: {', '.join(f'{k}({v})' for k, v in r['columns'].items())}" if r["columns"] else ""
            lines.append(f"- `{r['table']}` [{r['mode']}] via {', '.join(r['apis'])}" + cols)
        ids = {n["id"]} | set(apis)
        for r in rows:
            ids |= {i for i, x in g.nodes.items() if x["type"] == "Table" and x["name"] == r["table"]}
        return {"query": "find_data_lineage", "target": _ref(n), "title": f"Data lineage from {n['name']}", "summary": "\n".join(lines),
                "data": {"source": _ref(n), "tables": rows}, "graph": self._graph_payload(ids, n["id"])}

    # --------------------------------------------------------- explore_ontology
    def ontology_schema(self) -> dict:
        """The ontology instantiated on this repository: counts per type / relation and per (from, relation, to) triple."""
        g = self.g
        nodes = {t: 0 for t in NODE_TYPES}
        for n in g.nodes.values():
            nodes[n["type"]] += 1
        rels = {r: 0 for r in REL_TYPES}
        triples: dict[tuple, list] = {}
        for e in g.edges.values():
            rels[e["rel"]] += 1
            t = triples.setdefault((g.nodes[e["src"]]["type"], e["rel"], g.nodes[e["dst"]]["type"]), [0, 0])
            t[0] += 1
            t[1] += 1 if e["props"].get("derived") else 0
        rows = [{"from": a, "rel": r, "to": b, "count": c, "exact": x} for (a, r, b), (c, x) in triples.items()]
        rows.sort(key=lambda r: (-r["count"], r["from"], r["rel"], r["to"]))
        return {"nodes": nodes, "relations": rels, "triples": rows}

    def _ontology_search(self, source_type, relation, target_type, direction, depth, st, tt, want, links, limit):
        """(number of start nodes, total matches, first `limit` matches). Overridden by the Neo4j engine (one Cypher statement)."""
        g = self.g
        def edge_ok(e):
            return links == "all" or (links == "exact") == bool(e["props"].get("derived"))

        def neighbours(nid):
            if direction in ("out", "both"):
                for e in g.out_edges(nid):
                    if (not relation or e["rel"] == relation) and edge_ok(e):
                        yield e["dst"], e, "out"
            if direction in ("in", "both"):
                for e in g.in_edges(nid):
                    if (not relation or e["rel"] == relation) and edge_ok(e):
                        yield e["src"], e, "in"

        starts = [n for n in g.nodes.values() if (not source_type or n["type"] == source_type) and (not st or st in n["name"].lower())]
        matches, total = [], 0
        for s in sorted(starts, key=lambda n: (n["type"], n["name"])):
            prev, dist, queue = {s["id"]: None}, {s["id"]: 0}, deque([s["id"]])
            while queue:
                cur = queue.popleft()
                if dist[cur] >= depth:
                    continue
                for nxt, e, way in neighbours(cur):
                    if nxt not in prev:
                        prev[nxt], dist[nxt] = (cur, e, way), dist[cur] + 1
                        queue.append(nxt)
            for nid, d in sorted(dist.items(), key=lambda x: (x[1], g.nodes[x[0]]["name"])):
                n = g.nodes[nid]
                if nid == s["id"] or (target_type and n["type"] != target_type) or (tt and tt not in n["name"].lower()) \
                        or (want and nid != want):
                    continue
                total += 1
                if len(matches) >= limit:
                    continue
                steps, cur = [], nid
                while prev[cur] is not None:
                    p, e, way = prev[cur]
                    steps.append({**_ref(g.nodes[cur]), "rel": e["rel"], "way": way, "derived": bool(e["props"].get("derived")),
                                  "via": e["props"].get("via")})
                    cur = p
                steps.append({**_ref(s), "rel": None, "way": None, "derived": False, "via": None})
                matches.append({"source": _ref(s), "target": _ref(n), "hops": d, "path": list(reversed(steps))})

        return len(starts), total, matches

    def explore_ontology(self, source_type=None, relation=None, target_type=None, direction="out", depth=1,
                         source_text=None, target_text=None, target=None, links="all", limit=300) -> dict:
        """Free query on the ontology: `source_type` -[relation]-> `target_type`. `direction` is the one written in the
        ontology (out: A -REL-> B, in: A <-REL- B); up to `depth` hops along that relation (any relation if none given)."""
        g = self.g
        for value, allowed, what in ((source_type, NODE_TYPES, "node type"), (target_type, NODE_TYPES, "node type"),
                                     (relation, REL_TYPES, "relationship"), (direction, ("out", "in", "both"), "direction"),
                                     (links, ("all", "structural", "exact"), "links")):
            if value and value not in allowed:
                raise QueryError(f"Unknown {what} '{value}'", list(allowed))
        try:
            depth = max(1, min(5, int(depth or 1)))
            limit = max(1, min(2000, int(limit)))
        except (TypeError, ValueError):
            raise QueryError("depth and limit must be integers")
        direction, links = direction or "out", links or "all"
        want = self.resolve(target)["id"] if target else None
        st, tt = (source_text or "").strip().lower(), (target_text or "").strip().lower()

        n_starts, total, matches = self._ontology_search(source_type, relation, target_type, direction, depth, st, tt, want, links, limit)

        def arrow(path):
            out = []
            for i, step in enumerate(path):
                if i:
                    out.append(f" ─{step['rel']}→ " if step["way"] == "out" else f" ←{step['rel']}─ ")
                out.append(f"{step['type']}:{step['name']}")
            return "".join(out)

        rel = relation or "any"
        link = {"out": f"─{rel}→", "in": f"←{rel}─", "both": f"─{rel}─"}[direction]
        pattern = f"{source_type or 'any'} {link} {target_type or 'any'}"
        head = (f"**{total}** match(es) for `{pattern}`" + (f", depth {depth}" if depth > 1 else "")
                + (f", {links} links" if links != "all" else "")
                + (f" (showing the first {len(matches)})" if total > len(matches) else "") + "\n")
        lines = [head] + [f"- {arrow(m['path'])}" + (f"  [{', '.join(m['path'][-1]['via'])}]" if m["path"][-1].get("via") else "")
                          for m in matches[:80]]
        if len(matches) > 80:
            lines.append(f"- … {len(matches) - 80} more in the data")
        if not matches:
            lines.append("No link matches this pattern. `data.schema` lists the (from, relation, to) triples that exist in this graph.")
        ids = {s_["id"] for m in matches for s_ in m["path"]}
        return {"query": "explore_ontology", "target": None, "title": "Explore by ontology", "summary": "\n".join(lines),
                "data": {"pattern": pattern, "total": total, "truncated": total > len(matches), "starts": n_starts,
                         "matches": matches, "schema": self.ontology_schema()["triples"]},
                "graph": self._graph_payload(ids)}

    # ------------------------------------------------------------------ dispatch
    def run(self, query: str, **params) -> dict:
        fn = {"explain_screen": lambda: self.explain_screen(params["name"]),
              "show_call_chain": lambda: self.show_call_chain(params["source"], params.get("target") or None),
              "find_business_rules": lambda: self.find_business_rules(params.get("text"), params.get("scope") or None,
                                                                      params.get("kind") or None, float(params.get("min_confidence") or 0)),
              "determine_impact": lambda: self.determine_impact(params["name"], params.get("direction") or "upstream"),
              "show_capability": lambda: self.show_capability(params.get("name") or None),
              "find_data_lineage": lambda: self.find_data_lineage(params["target"], params.get("column") or None),
              "explore_ontology": lambda: self.explore_ontology(
                  params.get("source_type") or None, params.get("relation") or None, params.get("target_type") or None,
                  params.get("direction") or "out", params.get("depth") or 1, params.get("source_text") or None,
                  params.get("target_text") or None, params.get("target") or None, params.get("links") or "all")}.get(query)
        if fn is None:
            raise QueryError(f"Unknown query '{query}'")
        try:
            res = fn()
        except KeyError as ex:
            raise QueryError(f"Missing parameter {ex}")
        res["engine"] = getattr(self, "engine_name", "memory")
        return res
