"""Neo4j as the query engine.

The graph lives in Neo4j (see neo4j_export.py for the model: one label per ontology type, `FLOW` links in runtime direction,
`props_json`, `derived`, a `KGMeta` marker). No in-memory graph is used to answer a query.

How each query runs
  * pushed down as ONE Cypher statement:  explore_ontology, determine_impact (the traversal), show_call_chain (the path
    enumeration), ontology_schema, and the edge list of every result graph;
  * the other queries (explain_screen, find_business_rules, show_capability, find_data_lineage) reuse the exact same composition
    code as the in-memory engine, on top of `Neo4jGraph`: a read-only graph API whose every adjacency lookup is a Cypher query
    (memoised per engine). Same answers by construction; each lookup costs one round trip.

Values never travel inside the Cypher text: they are parameters. The only interpolated pieces are identifiers taken from fixed
whitelists (node types, relationship types) and integers.
"""
from __future__ import annotations

import difflib
import json

from .neo4j_export import Neo4jClient, Neo4jError
from .ontology import NODE_TYPES, REL_TYPES
from .queries import SKIP_IN_CHAINS, QueryEngine, QueryError

__all__ = ["Neo4jEngine", "Neo4jGraph", "Neo4jError", "synced"]

NODE_COLS = "{n}.id AS id, {n}.type AS type, {n}.name AS name, {n}.props_json AS pj, {n}.ord AS ord"


def _node(row: dict, p: str = "") -> dict:
    pj = row.get(p + "pj")
    return {"id": row[p + "id"], "type": row[p + "type"], "name": row[p + "name"], "props": json.loads(pj) if pj else {},
            "ord": row.get(p + "ord") if row.get(p + "ord") is not None else 10 ** 9}


def synced(client: Neo4jClient, repo: str) -> dict | None:
    """The KGMeta marker of a complete load of `repo`, or None."""
    rows = client.query("MATCH (m:`KGMeta` {kg_repo: $repo}) RETURN m.nodes AS nodes, m.edges AS edges, m.flow AS flow, "
                        "m.synced_at AS synced_at, m.fingerprint AS fingerprint", {"repo": repo})
    return rows[0] if rows else None


class _Nodes:
    """dict-like access to nodes: `g.nodes[id]`, `id in g.nodes`, `.items()`, `.values()`."""

    def __init__(self, g: "Neo4jGraph"):
        self._g = g

    def __getitem__(self, nid):
        n = self._g.node(nid)
        if n is None:
            raise KeyError(nid)
        return n

    def __contains__(self, nid):
        return self._g.node(nid) is not None

    def get(self, nid, default=None):
        return self._g.node(nid) or default

    def values(self):
        return self._g.all_nodes()

    def items(self):
        return [(n["id"], n) for n in self._g.all_nodes()]

    def __len__(self):
        return len(self._g.all_nodes())


class Neo4jGraph:
    """Read-only graph API (the subset QueryEngine needs) answered by Cypher."""

    def __init__(self, client: Neo4jClient, repo: str):
        self.client, self.repo = client, repo
        self._nodes: dict[str, dict] = {}
        self._all = False
        self._adj: dict[tuple, list] = {}
        self.nodes = _Nodes(self)
        self.queries = 0

    # -- plumbing ----------------------------------------------------------
    def q(self, cypher: str, **params) -> list[dict]:
        self.queries += 1
        return self.client.query(cypher, {"repo": self.repo, **params})

    def uid(self, nid: str) -> str:
        return f"{self.repo}|{nid}"

    def _remember(self, n: dict) -> dict:
        return self._nodes.setdefault(n["id"], n)

    # -- nodes -------------------------------------------------------------
    def node(self, nid: str) -> dict | None:
        if nid in self._nodes:
            return self._nodes[nid]
        rows = self.q(f"MATCH (n:KGNode {{uid: $uid}}) RETURN {NODE_COLS.format(n='n')}", uid=self.uid(nid))
        return self._remember(_node(rows[0])) if rows else None

    def prefetch(self, ids) -> None:
        missing = [i for i in set(ids) if i not in self._nodes]
        if missing:
            for r in self.q(f"MATCH (n:KGNode {{kg_repo: $repo}}) WHERE n.id IN $ids RETURN {NODE_COLS.format(n='n')}", ids=missing):
                self._remember(_node(r))

    def all_nodes(self) -> list[dict]:
        if not self._all:
            for r in self.q(f"MATCH (n:KGNode {{kg_repo: $repo}}) RETURN {NODE_COLS.format(n='n')} ORDER BY n.ord, n.id"):
                self._remember(_node(r))
            self._all = True
        return sorted(self._nodes.values(), key=lambda n: (n["ord"], n["id"]))

    def of_type(self, *types) -> list[dict]:
        rows = self.q(f"MATCH (n:KGNode {{kg_repo: $repo}}) WHERE n.type IN $types RETURN {NODE_COLS.format(n='n')} ORDER BY n.ord, n.id",
                      types=list(types))
        return [self._remember(_node(r)) for r in rows]

    def find(self, text: str, types=None, limit: int = 10) -> list[dict]:
        t = text.strip().lower()
        flt = "($types IS NULL OR n.type IN $types)"
        for where in ("toLower(n.name) = $t OR toLower(n.id) = $t", "toLower(n.name) CONTAINS $t OR toLower(n.id) CONTAINS $t"):
            rows = self.q(f"MATCH (n:KGNode {{kg_repo: $repo}}) WHERE {flt} AND ({where}) RETURN {NODE_COLS.format(n='n')} ORDER BY n.ord, n.id",
                          t=t, types=list(types) if types else None)
            hits = [self._remember(_node(r)) for r in rows]
            if hits:
                return hits[:limit] if where.startswith("toLower(n.name) =") else \
                    sorted(hits, key=lambda n: (len(n["name"]), n["name"]))[:limit]
        rows = self.q(f"MATCH (n:KGNode {{kg_repo: $repo}}) WHERE {flt} RETURN {NODE_COLS.format(n='n')} ORDER BY n.ord, n.id", types=list(types) if types else None)
        pool = [self._remember(_node(r)) for r in rows]
        names = {n["name"].lower(): n for n in pool}
        return [names[c] for c in difflib.get_close_matches(t, list(names), n=limit, cutoff=0.6)]

    # -- adjacency ---------------------------------------------------------
    def _edges(self, nid: str, outgoing: bool) -> list[dict]:
        key = ("out" if outgoing else "in", nid)
        if key not in self._adj:
            pat = "(a:KGNode {uid: $uid})-[r]->(b:KGNode)" if outgoing else "(a:KGNode {uid: $uid})<-[r]-(b:KGNode)"
            rows = self.q(f"MATCH {pat} WHERE type(r) <> 'FLOW' RETURN {NODE_COLS.format(n='b')}, "
                          "type(r) AS rel, r.props_json AS rp ORDER BY b.name, type(r)", uid=self.uid(nid))
            out = []
            for r in rows:
                other = self._remember(_node(r))
                props = json.loads(r["rp"]) if r["rp"] else {}
                out.append({"src": nid, "dst": other["id"], "rel": r["rel"], "props": props} if outgoing
                           else {"src": other["id"], "dst": nid, "rel": r["rel"], "props": props})
            self._adj[key] = out
        return self._adj[key]

    def out_edges(self, nid):
        return self._edges(nid, True)

    def in_edges(self, nid):
        return self._edges(nid, False)

    def _flow(self, nid: str, forward: bool, derived: bool) -> list[tuple]:
        key = ("fs" if forward else "fp", nid)
        if key not in self._adj:
            pat = "(a:KGNode {uid: $uid})-[f:FLOW]->(b:KGNode)" if forward else "(a:KGNode {uid: $uid})<-[f:FLOW]-(b:KGNode)"
            rows = self.q(f"MATCH {pat} RETURN {NODE_COLS.format(n='b')}, f.rel AS rel, coalesce(f.derived, false) AS derived, "
                          "f.via AS via ORDER BY b.name, f.rel", uid=self.uid(nid))
            res = []
            for r in rows:
                other = self._remember(_node(r))
                props = {"via": r["via"]} if r["via"] else {}
                if r["derived"]:
                    props["derived"] = True
                e = {"src": nid, "dst": other["id"], "rel": r["rel"], "props": props} if forward \
                    else {"src": other["id"], "dst": nid, "rel": r["rel"], "props": props}
                res.append((other["id"], e))
            self._adj[key] = res
        return [(o, e) for o, e in self._adj[key] if derived or not e["props"].get("derived")]

    def flow_succ(self, nid, derived=False):
        return self._flow(nid, True, derived)

    def flow_pred(self, nid, derived=False):
        return self._flow(nid, False, derived)

    def assoc(self, nid, rel):
        return [(e["dst"], e) for e in self.out_edges(nid) if e["rel"] == rel] + \
               [(e["src"], e) for e in self.in_edges(nid) if e["rel"] == rel]


class Neo4jEngine(QueryEngine):
    engine_name = "neo4j"

    def __init__(self, client: Neo4jClient, repo: str):
        self.client, self.repo = client, repo
        self.g = Neo4jGraph(client, repo)

    def q(self, cypher: str, **params) -> list[dict]:
        return self.g.q(cypher, **params)

    # -- result graph: the edges among the nodes of a result ------------------
    def _graph_payload(self, ids, focus=None, paths=None):
        ids = sorted(set(ids))
        edges = []
        if ids:
            rows = self.q("MATCH (a:KGNode {kg_repo: $repo})-[r]->(b:KGNode {kg_repo: $repo}) "
                          "WHERE a.id IN $ids AND b.id IN $ids AND type(r) <> 'FLOW' RETURN a.id AS s, b.id AS d, type(r) AS rel", ids=ids)
            edges = [[r["s"], r["d"], r["rel"]] for r in rows]
        return {"nodes": ids, "edges": edges, "focus": focus}

    # -- the ontology as found in the data ----------------------------------------
    def ontology_schema(self) -> dict:
        nodes = {t: 0 for t in NODE_TYPES}
        for r in self.q("MATCH (n:KGNode {kg_repo: $repo}) RETURN n.type AS t, count(*) AS c"):
            nodes[r["t"]] = r["c"]
        rels = {r: 0 for r in REL_TYPES}
        rows = self.q("MATCH (a:KGNode {kg_repo: $repo})-[r]->(b:KGNode {kg_repo: $repo}) WHERE type(r) <> 'FLOW' "
                      "RETURN a.type AS f, type(r) AS rel, b.type AS t, count(*) AS c, "
                      "sum(CASE WHEN coalesce(r.derived, false) THEN 1 ELSE 0 END) AS x")
        triples = []
        for r in rows:
            rels[r["rel"]] = rels.get(r["rel"], 0) + r["c"]
            triples.append({"from": r["f"], "rel": r["rel"], "to": r["t"], "count": r["c"], "exact": r["x"]})
        triples.sort(key=lambda r: (-r["count"], r["from"], r["rel"], r["to"]))
        return {"nodes": nodes, "relations": rels, "triples": triples}

    # -- explore_ontology: one Cypher statement -------------------------------------
    def _ontology_search(self, source_type, relation, target_type, direction, depth, st, tt, want, links, limit):
        rel = f":`{relation}`" if relation in REL_TYPES else ""          # whitelisted identifier
        d = int(depth)
        arrow = {"out": f"-[{rel}*1..{d}]->", "in": f"<-[{rel}*1..{d}]-", "both": f"-[{rel}*1..{d}]-"}[direction]
        counted = self.q("MATCH (s:KGNode {kg_repo: $repo}) WHERE ($st IS NULL OR s.type = $st) AND ($stxt IS NULL OR toLower(s.name) CONTAINS $stxt) "
                         "RETURN count(s) AS n", st=source_type, stxt=st or None)
        n_starts = counted[0]["n"] if counted else 0
        rows = self.q(f"""
            MATCH (s:KGNode {{kg_repo: $repo}})
            WHERE ($st IS NULL OR s.type = $st) AND ($stxt IS NULL OR toLower(s.name) CONTAINS $stxt)
            MATCH p = (s){arrow}(t:KGNode {{kg_repo: $repo}})
            WHERE t <> s AND ($tt IS NULL OR t.type = $tt) AND ($ttxt IS NULL OR toLower(t.name) CONTAINS $ttxt)
              AND ($want IS NULL OR t.id = $want)
              AND ALL(r IN relationships(p) WHERE type(r) <> 'FLOW' AND ($links = 'all' OR coalesce(r.derived, false) = ($links = 'exact')))
            WITH s, t, p ORDER BY length(p)
            WITH s, t, collect(p)[0] AS p
            RETURN s.id AS sid, t.id AS tid, length(p) AS hops, [n IN nodes(p) | n.id] AS ids,
                   [r IN relationships(p) | {{rel: type(r), derived: coalesce(r.derived, false), via: r.via, frm: startNode(r).id}}] AS rels
            ORDER BY s.type, s.name, hops, t.name""", st=source_type, stxt=st or None, tt=target_type, ttxt=tt or None, want=want, links=links)
        self.g.prefetch({i for r in rows[:limit] for i in r["ids"]})
        matches = []
        for r in rows[:limit]:
            steps = [{**self._ref(self.g.nodes[r["ids"][0]]), "rel": None, "way": None, "derived": False, "via": None}]
            for i, rr in enumerate(r["rels"]):
                nid = r["ids"][i + 1]
                steps.append({**self._ref(self.g.nodes[nid]), "rel": rr["rel"], "way": "out" if rr["frm"] == r["ids"][i] else "in",
                              "derived": bool(rr["derived"]), "via": rr["via"]})
            matches.append({"source": self._ref(self.g.nodes[r["sid"]]), "target": self._ref(self.g.nodes[r["tid"]]), "hops": r["hops"], "path": steps})
        return n_starts, len(rows), matches

    @staticmethod
    def _ref(n: dict) -> dict:
        return {"id": n["id"], "type": n["type"], "name": n["name"]}

    # -- determine_impact: one shortest-path traversal ------------------------------
    def _impact_reach(self, tid, direction, max_depth):
        try:
            depth = 30 if max_depth is None else max(1, min(60, int(max_depth)))
        except (TypeError, ValueError):
            raise QueryError("max_depth must be an integer")
        # upstream: who reaches the target through FLOW links, or through IMPACTS (rule -> entity / table);
        # downstream: what the target reaches through FLOW links
        pat = (f"shortestPath((y)-[:FLOW|IMPACTS*1..{depth}]->(x))" if direction == "upstream"
               else f"shortestPath((x)-[:FLOW*1..{depth}]->(y))")
        # downstream follows structural links only (the in-memory engine does the same)
        where = "" if direction == "upstream" else "WHERE ALL(r IN relationships(p) WHERE NOT coalesce(r.derived, false))"
        rows = self.q(f"""
            MATCH (x:KGNode {{uid: $uid}})
            MATCH (y:KGNode {{kg_repo: $repo}}) WHERE y <> x
            MATCH p = {pat}
            {where}
            RETURN y.id AS id, length(p) AS d, [n IN nodes(p) | n.type + ':' + n.name] AS names""", uid=self.g.uid(tid))
        dist = {tid: 0}
        paths = {}
        for r in rows:
            dist[r["id"]] = r["d"]
            paths[r["id"]] = r["names"]
        self.g.prefetch(dist)
        return dist, lambda nid: paths[nid]

    # -- show_call_chain: one path enumeration per pass ------------------------------
    def _chain_paths(self, s, tgt, allowed_apis, max_chains, max_depth):
        skip = sorted(set(SKIP_IN_CHAINS) | {"Route"})
        edges = int(max_depth) - 1
        apis = sorted(allowed_apis) if allowed_apis is not None else None
        common = """
            ALL(r IN relationships(p) WHERE $derived OR NOT coalesce(r.derived, false))
            AND ALL(n IN nodes(p) WHERE size([m IN nodes(p) WHERE m = n]) = 1)"""
        for derived in (False, True):
            if tgt:
                rows = self.q(f"""
                    MATCH p = (s:KGNode {{uid: $suid}})-[:FLOW*1..{edges}]->(t:KGNode {{uid: $tuid}})
                    WHERE {common}
                      AND ALL(n IN nodes(p)[1..-1] WHERE NOT n.type IN $skip AND (n.type <> 'API' OR $apis IS NULL OR n.id IN $apis))
                      AND ALL(n IN nodes(p)[0..-1] WHERE n.id <> $tid)
                    RETURN [n IN nodes(p) | n.id] AS ids,
                           [r IN relationships(p) | {{rel: r.rel, derived: coalesce(r.derived, false), via: r.via}}] AS rels
                    LIMIT $limit""", suid=self.g.uid(s["id"]), tuid=self.g.uid(tgt["id"]), tid=tgt["id"], skip=skip, apis=apis,
                                derived=derived, limit=int(max_chains))
            else:
                rows = self.q(f"""
                    MATCH p = (s:KGNode {{uid: $suid}})-[:FLOW*1..{edges}]->(t:KGNode)
                    WHERE {common}
                      AND ALL(n IN nodes(p)[1..] WHERE NOT n.type IN $skip AND (n.type <> 'API' OR $apis IS NULL OR n.id IN $apis))
                      AND ALL(n IN nodes(p)[1..-1] WHERE NOT n.type IN ['Table', 'BusinessRule'])   // a chain stops at the first table
                      AND (t.type IN ['Table', 'BusinessRule'] OR NOT EXISTS {{
                            MATCH (t)-[r2:FLOW]->(m:KGNode)
                            WHERE ($derived OR NOT coalesce(r2.derived, false)) AND NOT m.type IN $skip
                              AND (m.type <> 'API' OR $apis IS NULL OR m.id IN $apis)
                              AND NOT m.id IN [n IN nodes(p) | n.id] }})
                    RETURN [n IN nodes(p) | n.id] AS ids,
                           [r IN relationships(p) | {{rel: r.rel, derived: coalesce(r.derived, false), via: r.via}}] AS rels
                    LIMIT $limit""", suid=self.g.uid(s["id"]), skip=skip, apis=apis, derived=derived, limit=int(max_chains))
            if rows:
                self.g.prefetch({i for r in rows for i in r["ids"]})
                chains = []
                for r in rows:
                    path = [(r["ids"][0], None)]
                    for i, rr in enumerate(r["rels"]):
                        props = {"via": rr["via"]} if rr["via"] else {}
                        if rr["derived"]:
                            props["derived"] = True
                        path.append((r["ids"][i + 1], {"src": r["ids"][i], "dst": r["ids"][i + 1], "rel": rr["rel"], "props": props}))
                    chains.append(path)
                return sorted(chains, key=lambda c: [i for i, _ in c])
        return []
