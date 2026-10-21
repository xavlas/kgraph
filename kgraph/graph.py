"""In-memory typed property graph with flow-aware traversal helpers."""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter

from .ontology import NODE_TYPES, REL_TYPES, ASSOC_RELS


def split_words(name: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", s.lower()) if w]


class Graph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.meta: dict = {}
        self._out = self._in = None

    # -- mutation -----------------------------------------------------------
    def add_node(self, type: str, name: str, key: str | None = None, **props) -> str:
        assert type in NODE_TYPES, type
        nid = f"{type}:{key or name}"
        node = self.nodes.get(nid)
        if node is None:
            node = self.nodes[nid] = {"id": nid, "type": type, "name": name, "props": {}}
            self._out = self._in = None
        node["props"].update({k: v for k, v in props.items() if v is not None})
        return nid

    def add_edge(self, src: str, dst: str, rel: str, **props) -> None:
        assert rel in REL_TYPES, rel
        if src not in self.nodes or dst not in self.nodes or src == dst:
            return
        k = (src, dst, rel)
        e = self.edges.get(k)
        new_derived = bool(props.pop("derived", None))
        if e is None:
            e = self.edges[k] = {"src": src, "dst": dst, "rel": rel, "props": {}}
            self._out = self._in = None
            if new_derived:
                e["props"]["derived"] = True
        elif e["props"].get("derived") and not new_derived:
            del e["props"]["derived"]       # a structural contribution makes the edge structural
        for pk, pv in props.items():
            if pv is None:
                continue
            if isinstance(pv, list):
                cur = e["props"].setdefault(pk, [])
                cur.extend(x for x in pv if x not in cur)
            else:
                e["props"][pk] = pv

    # -- adjacency ----------------------------------------------------------
    def _index(self):
        if self._out is None:
            self._out, self._in = {}, {}
            for e in self.edges.values():
                self._out.setdefault(e["src"], []).append(e)
                self._in.setdefault(e["dst"], []).append(e)

    def out_edges(self, nid):
        self._index()
        return self._out.get(nid, [])

    def in_edges(self, nid):
        self._index()
        return self._in.get(nid, [])

    def flow_dir(self, e) -> tuple[str, str]:
        """Edge oriented in runtime direction (None-safe for associative rels)."""
        if e["rel"] == "EXPOSES" and self.nodes[e["src"]]["type"] == "Controller":
            return e["dst"], e["src"]
        return e["src"], e["dst"]

    def flow_succ(self, nid, derived=False):
        """[(next_id, edge)] following runtime direction."""
        self._index()
        res = []
        for e in self._out.get(nid, []) + self._in.get(nid, []):
            if e["rel"] in ASSOC_RELS:
                continue
            if e["props"].get("derived") and not derived:
                continue
            a, b = self.flow_dir(e)
            if a == nid:
                res.append((b, e))
        return res

    def flow_pred(self, nid, derived=False):
        self._index()
        res = []
        for e in self._out.get(nid, []) + self._in.get(nid, []):
            if e["rel"] in ASSOC_RELS:
                continue
            if e["props"].get("derived") and not derived:
                continue
            a, b = self.flow_dir(e)
            if b == nid:
                res.append((a, e))
        return res

    def assoc(self, nid, rel):
        """Nodes linked by an associative relation, in either direction."""
        self._index()
        res = [(e["dst"], e) for e in self._out.get(nid, []) if e["rel"] == rel]
        res += [(e["src"], e) for e in self._in.get(nid, []) if e["rel"] == rel]
        return res

    # -- lookup -------------------------------------------------------------
    def of_type(self, *types):
        return [n for n in self.nodes.values() if n["type"] in types]

    def find(self, text: str, types=None, limit=10) -> list[dict]:
        t = text.strip().lower()
        pool = [n for n in self.nodes.values() if not types or n["type"] in types]
        exact = [n for n in pool if t in (n["name"].lower(), n["id"].lower())]
        if exact:
            return exact[:limit]
        sub = [n for n in pool if t in n["name"].lower() or t in n["id"].lower()]
        sub.sort(key=lambda n: (len(n["name"]), n["name"]))
        if sub:
            return sub[:limit]
        names = {n["name"].lower(): n for n in pool}
        close = difflib.get_close_matches(t, list(names), n=limit, cutoff=0.6)
        return [names[c] for c in close]

    def stats(self) -> dict:
        return {
            "nodes": len(self.nodes), "edges": len(self.edges),
            "by_type": dict(Counter(n["type"] for n in self.nodes.values())),
            "by_rel": dict(Counter(e["rel"] for e in self.edges.values())),
        }

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        return {"meta": self.meta, "stats": self.stats(),
                "nodes": list(self.nodes.values()), "edges": list(self.edges.values())}

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    @classmethod
    def from_dict(cls, d: dict) -> "Graph":
        g = cls()
        g.meta = d.get("meta", {})
        for n in d["nodes"]:
            g.nodes[n["id"]] = n
        for e in d["edges"]:
            g.edges[(e["src"], e["dst"], e["rel"])] = e
        return g

    def subgraph(self, node_ids) -> dict:
        ids = set(node_ids)
        return {"nodes": [self.nodes[i] for i in ids if i in self.nodes],
                "edges": [e for e in self.edges.values() if e["src"] in ids and e["dst"] in ids]}
