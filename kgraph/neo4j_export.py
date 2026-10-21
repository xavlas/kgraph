"""Neo4j export: Cypher script (file) and direct loader (Neo4j HTTP transactional API, standard library only).

Model: one label per ontology node type (+ a common :KGNode label), one relationship type per ontology relation.
Every node carries `uid` (unique, repo-scoped), `id`, `name`, `type`, `kg_repo`; nested properties that Neo4j cannot store
(maps, lists of maps) are kept as JSON strings in `<key>_json`. Target: Neo4j 4.4+ / 5.

For the query engine (neo4j_engine.py) the export also stores:
  * `props_json` on every node / relationship : the complete original properties (lossless round trip);
  * `derived` (boolean) on every relationship : exact (method level) link or structural link;
  * a materialised `FLOW` relationship per executable link, oriented in RUNTIME direction (user -> screen -> API -> ... -> table;
    Controller->API EXPOSES is reversed), with `rel` = the original relationship. Impact analysis and call chains become plain
    variable-length patterns: (a)-[:FLOW*]->(b);
  * one `(:KGMeta {kg_repo})` node written LAST: it marks a complete, consistent load.
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from urllib.parse import urlparse

from .graph import Graph
from .ontology import ASSOC_RELS, NODE_TYPES

BATCH = 200
EXPORT_VERSION = 2          # bump when the stored model changes: servers then reload older data automatically


def fingerprint(g: Graph) -> str:
    return f"v{EXPORT_VERSION}:{len(g.nodes)}:{len(g.edges)}:{g.meta.get('scanned_at', '')}"
SAMPLE_QUERIES = """\
// ---- Example queries -------------------------------------------------------------------------------------
// Everything a screen reaches, down to the tables (the call chain of the ontology):
//   MATCH p = (s:Screen {name: 'OrderCreateComponent'})-[*1..6]->(t:Table) RETURN p
// Impact: what depends on a table (walk the arrows backwards):
//   MATCH p = (x)-[:CALLS|USES|READS|WRITES|EXECUTES|DEPENDS_ON*1..6]->(t:Table {name: 'customers'}) RETURN p
// Who writes a table, and through which repository method:
//   MATCH (s)-[w:WRITES]->(t:Table {name: 'orders'}) RETURN s.type, s.name, w.via
// Business rules, most worrying first:
//   MATCH (r:BusinessRule) WHERE r.suspected_defect IS NOT NULL RETURN r.rule_id, r.name, r.priority ORDER BY r.priority
// The ontology as found in the data (which relations exist between which types):
//   MATCH (a)-[r]->(b) RETURN labels(a)[0] AS from, type(r) AS rel, labels(b)[0] AS to, count(*) AS n ORDER BY n DESC
"""


class Neo4jError(Exception):
    pass


class Neo4jClient:
    """Minimal client for the Neo4j HTTP transactional endpoint (standard library only)."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j", timeout: int = 60):
        u = urlparse(uri)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise Neo4jError("the URI must be the Neo4j HTTP endpoint, e.g. http://localhost:7474")
        if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise Neo4jError("refusing to send credentials in clear text to a remote host: use https://")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", database or ""):
            raise Neo4jError("invalid database name")
        self.base = f"{u.scheme}://{u.netloc}"
        self.url = f"{self.base}/db/{database}/tx/commit"
        self.database, self.timeout = database, timeout
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    @classmethod
    def from_env(cls, env=None):
        import os
        env = env if env is not None else os.environ
        if not env.get("NEO4J_PASSWORD"):
            return None
        return cls(env.get("NEO4J_URI", "http://localhost:7474"), env.get("NEO4J_USER", "neo4j"), env["NEO4J_PASSWORD"],
                   env.get("NEO4J_DATABASE", "neo4j"))

    def post(self, stmts: list[tuple[str, dict]]) -> dict:
        body = json.dumps({"statements": [{"statement": s, "parameters": p} for s, p in stmts]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json", "Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as ex:
            raise Neo4jError("authentication failed (check user / password)" if ex.code == 401 else f"Neo4j answered HTTP {ex.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise Neo4jError(f"cannot reach Neo4j at {self.base}: {getattr(ex, 'reason', ex)}")
        if data.get("errors"):
            raise Neo4jError(f"{data['errors'][0].get('code', 'error')}: {data['errors'][0].get('message', '')}")
        return data

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Rows of ONE statement as dicts. Values always travel as parameters, never inside the text."""
        res = self.post([(cypher, params or {})])["results"][0]
        return [dict(zip(res["columns"], d["row"])) for d in res["data"]]


def flatten_props(props: dict) -> dict:
    """Keep what Neo4j can store (primitives, homogeneous primitive lists); JSON-encode the rest."""
    out = {}
    for k, v in (props or {}).items():
        if v is None:
            continue
        key = re.sub(r"\W", "_", str(k))
        if isinstance(v, bool) or isinstance(v, (str, int)):
            out[key] = v
        elif isinstance(v, float):
            if math.isfinite(v):
                out[key] = v
        elif isinstance(v, list):
            if not v:
                continue
            if all(isinstance(x, (str, int, float, bool)) for x in v) and len({type(x) for x in v}) == 1:
                out[key] = v
            else:
                out[key + "_json"] = json.dumps(v, ensure_ascii=False, default=str)
        else:
            out[key + "_json"] = json.dumps(v, ensure_ascii=False, default=str)
    return out


def _label(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _is_schema(stmt: str) -> bool:
    return stmt.lstrip().upper().startswith(("CREATE CONSTRAINT", "CREATE INDEX"))


def statements(g: Graph, batch: int = BATCH) -> list[tuple[str, dict]]:
    """[(cypher, parameters)] : schema, nodes per label, relationships per (from, rel, to), FLOW links, KGMeta (last)."""
    repo = str(g.meta.get("repository") or "repo")
    uid = lambda nid: f"{repo}|{nid}"
    stmts: list[tuple[str, dict]] = []
    for lab in [*NODE_TYPES, "KGNode"]:
        stmts.append((f"CREATE CONSTRAINT kg_{lab.lower()}_uid IF NOT EXISTS FOR (n:{_label(lab)}) REQUIRE n.uid IS UNIQUE", {}))
    stmts.append(("CREATE CONSTRAINT kg_kgmeta_repo IF NOT EXISTS FOR (m:`KGMeta`) REQUIRE m.kg_repo IS UNIQUE", {}))
    for prop in ("id", "kg_repo", "type", "name"):
        stmts.append((f"CREATE INDEX kg_node_{prop} IF NOT EXISTS FOR (n:`KGNode`) ON (n.{prop})", {}))
    by_type: dict[str, list] = defaultdict(list)
    for order, n in enumerate(g.nodes.values()):
        props = {**flatten_props(n["props"]), "props_json": json.dumps(n["props"], ensure_ascii=False, default=str),
                 "uid": uid(n["id"]), "id": n["id"], "name": n["name"], "type": n["type"], "kg_repo": repo, "ord": order}
        by_type[n["type"]].append({"uid": props["uid"], "props": props})
    for t, rows in by_type.items():
        for i in range(0, len(rows), batch):
            stmts.append((f"UNWIND $rows AS row MERGE (n:{_label(t)} {{uid: row.uid}}) SET n += row.props SET n:KGNode",
                          {"rows": rows[i:i + batch]}))
    groups: dict[tuple, list] = defaultdict(list)
    flow: dict[tuple, list] = defaultdict(list)
    for e in g.edges.values():
        a, b = g.nodes[e["src"]], g.nodes[e["dst"]]
        derived = bool(e["props"].get("derived"))
        props = {**flatten_props(e["props"]), "derived": derived, "props_json": json.dumps(e["props"], ensure_ascii=False, default=str)}
        groups[(a["type"], e["rel"], b["type"])].append({"src": uid(e["src"]), "dst": uid(e["dst"]), "props": props})
        if e["rel"] not in ASSOC_RELS:       # runtime-direction copy used for impact / call-chain traversals
            fa, fb = g.flow_dir(e)
            fprops = {"rel": e["rel"], "derived": derived, **({"via": e["props"]["via"]} if e["props"].get("via") else {})}
            flow[(g.nodes[fa]["type"], g.nodes[fb]["type"])].append({"src": uid(fa), "dst": uid(fb), "rel": e["rel"], "props": fprops})
    for (a, rel, b), rows in sorted(groups.items()):
        for i in range(0, len(rows), batch):
            stmts.append((f"UNWIND $rows AS row MATCH (a:{_label(a)} {{uid: row.src}}) MATCH (b:{_label(b)} {{uid: row.dst}}) "
                          f"MERGE (a)-[x:{_label(rel)}]->(b) SET x += row.props", {"rows": rows[i:i + batch]}))
    for (a, b), rows in sorted(flow.items()):
        for i in range(0, len(rows), batch):
            stmts.append((f"UNWIND $rows AS row MATCH (a:{_label(a)} {{uid: row.src}}) MATCH (b:{_label(b)} {{uid: row.dst}}) "
                          f"MERGE (a)-[x:`FLOW` {{rel: row.rel}}]->(b) SET x += row.props", {"rows": rows[i:i + batch]}))
    meta = {"kg_repo": repo, "nodes": len(g.nodes), "edges": len(g.edges), "flow": sum(len(r) for r in flow.values()),
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "fingerprint": fingerprint(g),
            "meta_json": json.dumps(g.meta, ensure_ascii=False, default=str)}
    stmts.append(("UNWIND $rows AS row MERGE (m:`KGMeta` {kg_repo: row.kg_repo}) SET m += row", {"rows": [meta]}))   # LAST: marks a complete load
    return stmts


CLEAR_STATEMENT = "MATCH (n) WHERE (n:KGNode OR n:KGMeta) AND n.kg_repo = $repo DETACH DELETE n"


# ------------------------------------------------------------------ Cypher text
def lit(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(lit(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_label(k)}: {lit(x)}" for k, x in v.items()) + "}"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    s = re.sub(r"[\x00-\x1f\x7f\x85\u2028\u2029]", lambda m: "\\u%04x" % ord(m.group()), s)   # incl. Unicode line separators
    return '"' + s + '"'


def _comment(s) -> str:
    """Text safe to put after `//`: a newline would end the comment and turn the rest into executable Cypher."""
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]", " ", str(s))


def to_cypher(g: Graph, batch: int = BATCH) -> str:
    stats = g.stats()
    head = [f"// kgraph export for Neo4j 4.4+/5 — repository: {_comment(g.meta.get('repository', '?'))}",
            f"// generated {time.strftime('%Y-%m-%dT%H:%M:%S')} · {stats['nodes']} nodes · {stats['edges']} relationships",
            "// Run with:  cypher-shell -u neo4j -f graph.cypher     (or paste it into Neo4j Browser)",
            "// Re-running is safe (MERGE). Everything is tagged :KGNode and kg_repo so it can be removed without touching other data:",
            "//   MATCH (n:KGNode {kg_repo: '%s'}) DETACH DELETE n;" % _comment(g.meta.get("repository", "repo")).replace("'", "\\'"), ""]
    body = [(cy.replace("$rows", lit(p["rows"])) if p else cy) + ";" for cy, p in statements(g, batch)]
    return "\n".join(head + body) + "\n\n" + SAMPLE_QUERIES


# ------------------------------------------------------------------ direct load
_RETRYABLE = ("cannot reach", "DatabaseUnavailable", "TransientError", "HTTP 503", "HTTP 502")


def load(g: Graph, uri: str | None = None, user: str | None = None, password: str | None = None, database: str = "neo4j",
         clear: bool = False, batch: int = BATCH, timeout: int = 60, wait: int = 0, client: "Neo4jClient | None" = None) -> dict:
    """Push the graph through the HTTP transactional endpoint. The password is only used in the Authorization header.
    Pass `client` to reuse an existing connection."""
    client = client or Neo4jClient(uri, user, password, database, timeout)
    database = client.database
    repo = str(g.meta.get("repository") or "repo")

    if wait:                                    # a freshly started server answers HTTP before the database accepts queries
        t0 = time.time()
        while True:
            try:
                client.post([("RETURN 1", {})])
                break
            except Neo4jError as ex:
                if time.time() - t0 > wait or not any(k in str(ex) for k in _RETRYABLE):
                    raise
                time.sleep(2)

    todo = statements(g, batch)
    schema = [t for t in todo if _is_schema(t[0])]
    writes = [t for t in todo if not _is_schema(t[0])]
    # the old data and its KGMeta marker are removed first: a crash half-way must never look like a complete load
    writes.insert(0, (CLEAR_STATEMENT, {"repo": repo}) if clear else ("MATCH (m:`KGMeta` {kg_repo: $repo}) DELETE m", {"repo": repo}))
    # Neo4j forbids schema changes in a transaction that already wrote: every schema statement goes alone, before any write
    for stmt in schema:
        client.post([stmt])
    for i in range(0, len(writes), 10):
        client.post(writes[i:i + 10])
    s = g.stats()
    return {"nodes": s["nodes"], "relationships": s["edges"], "statements": len(schema) + len(writes), "database": database,
            "cleared": clear}
