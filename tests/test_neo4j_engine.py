"""Neo4j as the query engine.

* ParityTests : integration tests against a REAL Neo4j (skipped unless KGRAPH_NEO4J_TEST=1 and NEO4J_PASSWORD is set; they only
  touch nodes tagged kg_repo='kgparity' and remove them afterwards). Both engines answer the same questions on the same graph.
  Ties between equally short paths may be resolved differently by the two engines, so paths are compared by length / endpoints,
  everything else (sets, counts, scores, rules, lineage) must be identical.
* SafetyTests : always run, with a fake client; they check that user text never reaches the Cypher text.
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kgraph.neo4j_engine import Neo4jEngine, Neo4jGraph, synced            # noqa: E402
from kgraph.neo4j_export import Neo4jClient, fingerprint, load             # noqa: E402
from kgraph.pipeline import scan_repository                                  # noqa: E402
from kgraph.queries import QueryEngine, QueryError                           # noqa: E402

URI = os.environ.get("NEO4J_URI", "http://127.0.0.1:7474")
REPO = "kgparity"


def canon(x):
    """Order-insensitive form (Cypher does not promise an order)."""
    if isinstance(x, dict):
        return {k: canon(v) for k, v in x.items() if k != "engine"}
    if isinstance(x, list):
        return sorted((canon(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return x


def lines(text):
    return sorted(l.strip() for l in text.splitlines() if l.strip())


def reachable() -> bool:
    if os.environ.get("KGRAPH_NEO4J_TEST") != "1" or not os.environ.get("NEO4J_PASSWORD"):
        return False
    try:
        urllib.request.urlopen(URI, timeout=3).close()
        return True
    except (urllib.error.URLError, OSError):
        return False


@unittest.skipUnless(reachable(), "set KGRAPH_NEO4J_TEST=1 and NEO4J_PASSWORD with a running Neo4j to run the parity tests")
class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = scan_repository(str(ROOT / "samples" / "shop"))
        cls.g.meta["repository"] = REPO
        pw = os.environ["NEO4J_PASSWORD"]
        load(cls.g, URI, "neo4j", pw, clear=True, wait=30)
        cls.client = Neo4jClient(URI, "neo4j", pw)
        cls.mem, cls.neo = QueryEngine(cls.g), Neo4jEngine(cls.client, REPO)

    @classmethod
    def tearDownClass(cls):
        cls.client.query("MATCH (n) WHERE (n:KGNode OR n:KGMeta) AND n.kg_repo = $repo DETACH DELETE n", {"repo": REPO})

    def both(self, query, **kw):
        a, b = self.mem.run(query, **kw), self.neo.run(query, **kw)
        self.assertEqual((a["engine"], b["engine"]), ("memory", "neo4j"))
        return a, b

    def test_load_is_complete_and_marked(self):
        meta = synced(self.client, REPO)
        self.assertEqual((meta["nodes"], meta["edges"]), (len(self.g.nodes), len(self.g.edges)))
        n = self.client.query("MATCH (n:KGNode {kg_repo: $repo}) RETURN count(n) AS n", {"repo": REPO})[0]["n"]
        r = self.client.query("MATCH (:KGNode {kg_repo: $repo})-[r]->() WHERE type(r) <> 'FLOW' RETURN count(r) AS n", {"repo": REPO})[0]["n"]
        self.assertEqual((n, r), (len(self.g.nodes), len(self.g.edges)))

    def test_explore_ontology(self):
        for kw in (dict(source_type="Service", relation="WRITES", target_type="Table"),
                   dict(source_type="Repository", relation="WRITES", target_type="Table", links="structural"),
                   dict(source_type="Service", relation="WRITES", target_type="Table", links="exact"),
                   dict(source_type="Screen", relation="CALLS", target_type="Service", depth=2),
                   dict(source_type="Table", relation="READS", target_type="Repository", direction="in"),
                   dict(source_type="Controller", relation="EXPOSES", target_type="API"),
                   dict(source_type="Service", direction="both", depth=2, target_text="order"),
                   dict(relation="IMPACTS"), dict(source_type="BusinessRule", relation="EXECUTES", direction="in", target_type="Table"),
                   dict(source_text="OrderCreate", depth=3, relation="CALLS"), dict(target="orders", relation="WRITES")):
            with self.subTest(kw=kw):
                a, b = self.both("explore_ontology", **kw)
                key = lambda r: sorted((m["source"]["id"], m["target"]["id"], m["hops"]) for m in r["data"]["matches"])
                self.assertEqual(key(a), key(b))
                self.assertEqual((a["data"]["total"], a["data"]["starts"], a["data"]["pattern"]), (b["data"]["total"], b["data"]["starts"], b["data"]["pattern"]))
                # every returned path really is a path of the requested shape
                for m in b["data"]["matches"]:
                    self.assertEqual(len(m["path"]) - 1, m["hops"])
                    self.assertTrue(all(s["rel"] == kw.get("relation", s["rel"]) for s in m["path"][1:]))

    def test_ontology_schema(self):
        a, b = self.mem.ontology_schema(), self.neo.ontology_schema()
        self.assertEqual(canon(a), canon(b))

    def test_determine_impact(self):
        for kw in (dict(name="customers"), dict(name="OrderService"), dict(name="orders", max_depth=2), dict(name="StockLevelsComponent"),
                   dict(name="OrderCreateComponent", direction="downstream"), dict(name="orders", direction="downstream"),
                   dict(name="Customer"), dict(name="POST /api/orders")):
            with self.subTest(kw=kw):
                a, b = self.both("determine_impact", **kw)
                dist = lambda r: {t: sorted((i["name"], i["distance"]) for i in items) for t, items in r["data"]["impacted"].items()}
                self.assertEqual(dist(a), dist(b))
                for k in ("risk", "score", "counts", "affects"):
                    self.assertEqual(canon(a["data"][k]), canon(b["data"][k]), k)
                for t, items in b["data"]["impacted"].items():                      # a path is as long as its distance
                    for i in items:
                        if t != "Controller":
                            self.assertEqual(len(i["path"]) - 1, i["distance"])

    def test_show_call_chain(self):
        for kw in (dict(source="OrderCreateComponent"), dict(source="OrderCreateComponent", target="orders"),
                   dict(source="OrderDetailComponent"), dict(source="CustomersPage", target="customers"), dict(source="POST /api/orders"),
                   dict(source="StockLevelsComponent"), dict(source="OrderService", target="orders"), dict(source="/orders/new")):
            with self.subTest(kw=kw):
                a, b = self.both("show_call_chain", **kw)
                ids = lambda r: sorted(tuple(n["id"] for n in c) for c in r["data"]["chains"])
                self.assertEqual(ids(a), ids(b))
                self.assertEqual(lines(a["summary"]), lines(b["summary"]))

    def test_composed_queries_are_identical(self):
        for q, kw in (("explain_screen", dict(name="OrderCreateComponent")), ("explain_screen", dict(name="/orders/new")),
                      ("explain_screen", dict(name="CustomerFormPage")),
                      ("find_business_rules", dict()), ("find_business_rules", dict(scope="OrderCreateComponent")),
                      ("find_business_rules", dict(scope="customers")), ("find_business_rules", dict(kind="guard")),
                      ("find_business_rules", dict(text="credit limit")), ("find_business_rules", dict(scope="Orders")),
                      ("show_capability", dict()), ("show_capability", dict(name="Orders")), ("show_capability", dict(name="Inventory")),
                      ("find_data_lineage", dict(target="orders")), ("find_data_lineage", dict(target="orders", column="status")),
                      ("find_data_lineage", dict(target="OrderCreateComponent")), ("find_data_lineage", dict(target="customers"))):
            with self.subTest(q=q, kw=kw):
                a, b = self.both(q, **kw)
                self.assertEqual(canon(a["data"]), canon(b["data"]))
                self.assertEqual(lines(a["summary"]), lines(b["summary"]))

    def test_errors_and_suggestions_match(self):
        for q, kw in (("explain_screen", dict(name="NoSuchScreen")), ("find_data_lineage", dict(target="orders", column="nope")),
                      ("explore_ontology", dict(relation="LOVES"))):
            with self.subTest(q=q):
                with self.assertRaises(QueryError) as ea:
                    self.mem.run(q, **kw)
                with self.assertRaises(QueryError) as eb:
                    self.neo.run(q, **kw)
                self.assertEqual(str(ea.exception), str(eb.exception))
                self.assertEqual(sorted(ea.exception.suggestions), sorted(eb.exception.suggestions))

    def test_fuzzy_and_substring_resolution(self):
        for text, types in (("order service", None), ("OrdreService", ["Service"]), ("ORDERS", ["Table"]), ("creat", ["Screen"]), ("api/orders", ["API"])):
            with self.subTest(text=text):
                a, b = self.mem.g.find(text, types), self.neo.g.find(text, types)
                self.assertEqual(sorted(n["id"] for n in a), sorted(n["id"] for n in b))

    def test_hostile_text_is_only_ever_data(self):
        evil = "x' OR 1=1 //\"}) MATCH (n) DETACH DELETE n //"
        before = self.client.query("MATCH (n:KGNode {kg_repo: $repo}) RETURN count(n) AS n", {"repo": REPO})[0]["n"]
        r = self.neo.explore_ontology(source_text=evil, target_text=evil)
        self.assertEqual(r["data"]["total"], 0)
        with self.assertRaises(QueryError):
            self.neo.explain_screen(evil)
        after = self.client.query("MATCH (n:KGNode {kg_repo: $repo}) RETURN count(n) AS n", {"repo": REPO})[0]["n"]
        self.assertEqual(before, after)


class FakeClient:
    """Records every statement; answers with empty result sets."""

    def __init__(self):
        self.calls = []

    def query(self, cypher, params=None):
        self.calls.append((cypher, params or {}))
        return []


class SafetyTests(unittest.TestCase):
    EVIL = "x\"}) MATCH (n) DETACH DELETE n //'; DROP"

    def run_queries(self):
        c = FakeClient()
        e = Neo4jEngine(c, "repo")
        for fn in (lambda: e.explore_ontology(source_text=self.EVIL, target_text=self.EVIL, target=self.EVIL),
                   lambda: e.g.find(self.EVIL), lambda: e.g.node(self.EVIL), lambda: e.g.of_type(self.EVIL),
                   lambda: e._impact_reach(self.EVIL, "upstream", 3), lambda: e._graph_payload([self.EVIL])):
            try:
                fn()
            except QueryError:
                pass
        return c

    def test_user_text_never_appears_in_the_cypher_text(self):
        c = self.run_queries()
        self.assertGreater(len(c.calls), 4)
        for cypher, params in c.calls:
            self.assertNotIn("DETACH", cypher)
            self.assertNotIn("DROP", cypher)
            self.assertNotIn(self.EVIL, cypher)
        values = [str(v).lower() for _, p in c.calls for v in p.values()]
        self.assertTrue(any(self.EVIL.lower() in v for v in values))        # it travelled as a parameter

    def test_only_whitelisted_identifiers_are_interpolated(self):
        e = Neo4jEngine(FakeClient(), "repo")
        for bad in (dict(source_type="Screen) MATCH (n) DETACH DELETE n //"), dict(relation="CALLS`]->() DETACH DELETE n //"),
                    dict(direction="out; DROP"), dict(links="all' OR 1=1")):
            with self.assertRaises(QueryError):
                e.explore_ontology(**bad)
        self.assertEqual(e.client.calls, [])                               # rejected before any query is sent
        with self.assertRaises(QueryError):
            e.explore_ontology(depth="3; DETACH DELETE n")
        with self.assertRaises(QueryError):
            e._impact_reach("x", "upstream", "3; DETACH DELETE n")

    def test_depth_is_an_integer_and_bounded(self):
        c = FakeClient()
        e = Neo4jEngine(c, "repo")
        e.explore_ontology(source_type="Screen", relation="CALLS", depth=99)
        self.assertTrue(any("*1..5]" in cy for cy, _ in c.calls))
        e._impact_reach("Table:t", "upstream", 999)
        self.assertTrue(any("*1..60" in cy for cy, _ in c.calls))

    def test_adjacency_is_memoised(self):
        c = FakeClient()
        g = Neo4jGraph(c, "repo")
        g.flow_succ("a"); g.flow_succ("a"); g.flow_pred("a"); g.out_edges("a"); g.out_edges("a")
        self.assertEqual(len(c.calls), 3)

    def test_an_older_export_format_is_reloaded(self):
        st = ServerEngineTests().make_state(NEO4J_PASSWORD="x")
        g = scan_repository(str(ROOT / "samples" / "shop"))
        old = f"{len(g.nodes)}:{len(g.edges)}:{g.meta.get('scanned_at', '')}"          # pre-v2 fingerprint
        with mock.patch("kgraph.server.neo4j_synced", return_value={"synced_at": "T", "nodes": 1, "fingerprint": old}), \
                mock.patch("kgraph.server.neo4j_load") as loaded:
            st.set_graph(g, sync=True)
        loaded.assert_called_once()

    def test_a_graph_whose_marker_is_missing_is_not_considered_synced(self):
        self.assertIsNone(synced(FakeClient(), "repo"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------- server integration
from kgraph.neo4j_export import Neo4jError                                   # noqa: E402
from unittest import mock                                                    # noqa: E402


class ServerEngineTests(unittest.TestCase):
    """State: sync after a scan, engine swap, per-request fallback to memory. Neo4j is faked here (see the live test below)."""

    def make_state(self, **env):
        import tempfile
        from kgraph.server import State
        keep = {k: os.environ.pop(k, None) for k in ("NEO4J_PASSWORD", "NEO4J_URI", "KGRAPH_ENGINE")}
        os.environ.update(env)
        try:
            return State(Path(tempfile.mkdtemp()))
        finally:
            for k, v in keep.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    G = scan_repository(str(ROOT / "samples" / "shop"))

    def test_without_neo4j_everything_stays_in_memory(self):
        st = self.make_state()
        st.set_graph(self.G, sync=True)
        self.assertEqual(st.engine_info()["active"], "memory")
        self.assertIsNone(st.engine_info()["neo4j"])
        self.assertEqual(st.run_query("show_capability", {"name": "Orders"})["engine"], "memory")

    def test_engine_memory_forces_memory_even_when_neo4j_is_configured(self):
        st = self.make_state(NEO4J_PASSWORD="x", KGRAPH_ENGINE="memory")
        self.assertIsNone(st.neo)

    def test_scan_syncs_then_queries_go_to_neo4j(self):
        st = self.make_state(NEO4J_PASSWORD="x")
        with mock.patch("kgraph.server.neo4j_synced", side_effect=[None, {"synced_at": "T", "nodes": 72, "fingerprint": "f"}]), \
                mock.patch("kgraph.server.neo4j_load") as loaded:
            st.set_graph(self.G, sync=True)
        loaded.assert_called_once()
        self.assertTrue(loaded.call_args.kwargs["clear"])                  # replaces the previous load of this repository
        info = st.engine_info()
        self.assertEqual((info["active"], info["neo4j"]["state"], info["neo4j"]["synced_at"]), ("neo4j", "ready", "T"))
        self.assertIsInstance(st.engine, Neo4jEngine)
        self.assertIs(st.nlq.engine, st.engine)

    def test_unchanged_graph_is_not_pushed_again(self):
        st = self.make_state(NEO4J_PASSWORD="x")
        fp = fingerprint(self.G)
        with mock.patch("kgraph.server.neo4j_synced", return_value={"synced_at": "T", "nodes": 1, "fingerprint": fp}), \
                mock.patch("kgraph.server.neo4j_load") as loaded:
            st.set_graph(self.G, sync=True)
        loaded.assert_not_called()
        self.assertEqual(st.engine_info()["active"], "neo4j")

    def test_unreachable_neo4j_keeps_the_app_working_in_memory(self):
        st = self.make_state(NEO4J_PASSWORD="x")
        with mock.patch("kgraph.server.neo4j_synced", side_effect=Neo4jError("cannot reach Neo4j at http://localhost:7474: refused")):
            st.set_graph(self.G, sync=True)
        info = st.engine_info()
        self.assertEqual(info["active"], "memory")
        self.assertEqual(info["neo4j"]["state"], "error")
        self.assertIn("cannot reach", info["neo4j"]["error"])
        self.assertEqual(st.run_query("show_capability", {"name": "Orders"})["engine"], "memory")

    def test_connection_lost_mid_request_falls_back_and_says_so(self):
        st = self.make_state(NEO4J_PASSWORD="x")
        with mock.patch("kgraph.server.neo4j_synced", return_value={"fingerprint": "different", "synced_at": "T", "nodes": 1}), \
                mock.patch("kgraph.server.neo4j_load"):
            st.set_graph(self.G, sync=True)
        with mock.patch.object(Neo4jEngine, "run", side_effect=Neo4jError("cannot reach Neo4j")):
            res = st.run_query("show_capability", {"name": "Orders"})
            asked = st.ask("show capability Orders")
        self.assertEqual(res["engine"], "memory")
        self.assertIn("answered in memory", res["engine_note"])
        self.assertIn("elapsed_ms", res)
        self.assertEqual(asked["engine"], "memory")
        self.assertEqual(st.engine_info()["neo4j"]["state"], "error")

    def test_a_newer_graph_is_not_overwritten_by_a_slow_sync(self):
        st = self.make_state(NEO4J_PASSWORD="x")
        other = scan_repository(str(ROOT / "samples" / "shop"))

        def slow_synced(client, repo):
            st.set_graph(other)                                            # a newer scan lands while this sync is running
            return {"synced_at": "T", "nodes": 1, "fingerprint": fingerprint(self.G)}
        with mock.patch("kgraph.server.neo4j_synced", side_effect=slow_synced), mock.patch("kgraph.server.neo4j_load"):
            st._sync(self.G)
        self.assertIs(st.graph, other)
        self.assertEqual(st.engine_info()["active"], "memory")             # the stale sync did not install its engine


@unittest.skipUnless(reachable(), "set KGRAPH_NEO4J_TEST=1 and NEO4J_PASSWORD with a running Neo4j")
class LiveServerTests(unittest.TestCase):
    def test_full_cycle_on_a_real_neo4j(self):
        import tempfile
        from kgraph.server import State
        g = scan_repository(str(ROOT / "samples" / "shop"))
        g.meta["repository"] = REPO
        old = {k: os.environ.get(k) for k in ("NEO4J_URI", "KGRAPH_ENGINE")}
        os.environ["NEO4J_URI"] = URI
        os.environ.pop("KGRAPH_ENGINE", None)
        try:
            st = State(Path(tempfile.mkdtemp()))
            st.set_graph(g, sync=True)
            self.assertEqual(st.engine_info()["active"], "neo4j", st.engine_info())
            res = st.run_query("determine_impact", {"name": "customers"})
            self.assertEqual((res["engine"], res["data"]["counts"]["Screen"]), ("neo4j", 5))
            self.assertEqual(st.ask("which services write to orders?")["engine"], "neo4j")
            self.assertEqual(st.run_query("explore_ontology", {"source_type": "Service", "relation": "WRITES", "target_type": "Table"})["engine"], "neo4j")
            before = st.neo_state["synced_at"]
            st._sync(g)                                                   # unchanged: no reload, same marker
            self.assertEqual(st.neo_state["synced_at"], before)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            Neo4jClient.from_env().query("MATCH (n) WHERE (n:KGNode OR n:KGMeta) AND n.kg_repo = $repo DETACH DELETE n", {"repo": REPO})
