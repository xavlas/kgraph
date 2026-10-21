import io
import json
import sys
import unittest
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kgraph.nlq import NaturalLanguage                 # noqa: E402
from kgraph.ontology import NODE_TYPES, REL_TYPES      # noqa: E402
from kgraph.pipeline import scan_repository            # noqa: E402
from kgraph.queries import QueryEngine, QueryError     # noqa: E402

G = scan_repository(str(ROOT / "samples" / "shop"))
Q = QueryEngine(G)


def pairs(r, arrow=False):
    return {(m["source"]["name"], m["target"]["name"]) for m in r["data"]["matches"]}


class ExploreOntologyTests(unittest.TestCase):
    def test_type_relation_type(self):
        r = Q.explore_ontology("Service", "WRITES", "Table")
        self.assertIn(("OrderService", "orders"), pairs(r))
        self.assertIn(("CustomerService", "customers"), pairs(r))
        self.assertEqual(r["data"]["pattern"], "Service ─WRITES→ Table")
        self.assertTrue(all(m["path"][-1]["rel"] == "WRITES" for m in r["data"]["matches"]))

    def test_every_match_is_a_real_edge_of_the_requested_shape(self):
        for src, rel, dst in (("Screen", "CALLS", "API"), ("Controller", "EXPOSES", "API"), ("Repository", "READS", "Table"),
                              ("Entity", "USES", "Table"), ("BusinessRule", "IMPACTS", "Entity")):
            r = Q.explore_ontology(src, rel, dst)
            expected = {(G.nodes[e["src"]]["name"], G.nodes[e["dst"]]["name"]) for e in G.edges.values()
                        if e["rel"] == rel and G.nodes[e["src"]]["type"] == src and G.nodes[e["dst"]]["type"] == dst}
            self.assertEqual(pairs(r), expected, (src, rel, dst))
            self.assertEqual(r["data"]["total"], len(expected))

    def test_direction_in_is_the_mirror_of_out(self):
        out = Q.explore_ontology("Repository", "WRITES", "Table", direction="out")
        back = Q.explore_ontology("Table", "WRITES", "Repository", direction="in")
        self.assertEqual(pairs(out), {(b, a) for a, b in pairs(back)})
        self.assertEqual(Q.explore_ontology("Table", "WRITES", "Repository", direction="out")["data"]["total"], 0)
        both = Q.explore_ontology("Repository", "WRITES", "Table", direction="both")
        self.assertGreaterEqual(both["data"]["total"], out["data"]["total"])

    def test_links_filter_separates_structural_from_exact(self):
        structural = Q.explore_ontology("Service", "WRITES", "Table", links="structural")
        exact = Q.explore_ontology("Service", "WRITES", "Table", links="exact")
        self.assertTrue(all(not s["derived"] for m in structural["data"]["matches"] for s in m["path"][1:]))
        self.assertTrue(all(s["derived"] for m in exact["data"]["matches"] for s in m["path"][1:]))
        self.assertGreater(exact["data"]["total"], 0)

    def test_depth_follows_the_relation(self):
        one = Q.explore_ontology("Screen", "CALLS", "Service", depth=1)
        two = Q.explore_ontology("Screen", "CALLS", "Service", depth=2, source_text="OrderCreate")
        self.assertEqual(one["data"]["total"], 0)                          # Screen -CALLS-> API only
        self.assertEqual(pairs(two), {("OrderCreateComponent", "OrderService")})
        self.assertEqual([s["type"] for s in two["data"]["matches"][0]["path"]], ["Screen", "API", "Service"])
        self.assertEqual(two["data"]["matches"][0]["hops"], 2)

    def test_name_filters_and_exact_target(self):
        self.assertEqual(pairs(Q.explore_ontology("Service", "WRITES", "Table", target_text="order")), {("OrderService", "orders")})
        self.assertEqual(pairs(Q.explore_ontology("Service", "WRITES", None, source_text="CustomerS")), {("CustomerService", "customers")})
        self.assertEqual(pairs(Q.explore_ontology(None, "WRITES", None, target="orders", source_text="Order")),
                         {("OrderService", "orders"), ("OrderRepository", "orders"),
                          ("POST /api/orders", "orders"), ("POST /api/orders/{id}/cancel", "orders")})

    def test_limit_truncates_but_counts_everything(self):
        r = Q.explore_ontology(None, None, None, limit=5)
        self.assertEqual(len(r["data"]["matches"]), 5)
        self.assertTrue(r["data"]["truncated"])
        self.assertGreater(r["data"]["total"], 5)
        self.assertIn("showing the first 5", r["summary"])

    def test_graph_payload_contains_the_paths(self):
        r = Q.explore_ontology("Screen", "CALLS", "Service", depth=2, source_text="OrderCreate")
        ids = set(r["graph"]["nodes"])
        self.assertEqual({G.nodes[i]["name"] for i in ids}, {"OrderCreateComponent", "POST /api/orders", "OrderService"})

    def test_invalid_input_is_rejected_with_the_valid_choices(self):
        for kw, key in (({"source_type": "Banana"}, "Banana"), ({"relation": "LOVES"}, "LOVES"), ({"direction": "up"}, "up"),
                        ({"links": "weird"}, "weird"), ({"depth": "x"}, "depth")):
            with self.assertRaises(QueryError) as cm:
                Q.explore_ontology(**kw)
            self.assertIn(key, str(cm.exception))
        with self.assertRaises(QueryError) as cm:
            Q.explore_ontology(relation="LOVES")
        self.assertEqual(set(cm.exception.suggestions), set(REL_TYPES))

    def test_empty_result_explains_itself(self):
        r = Q.explore_ontology("Table", "CALLS", "Screen")
        self.assertEqual(r["data"]["total"], 0)
        self.assertIn("No link matches", r["summary"])

    def test_depth_is_bounded(self):
        self.assertIn("depth 5", Q.explore_ontology("Screen", None, None, depth=99)["summary"])

    def test_schema_describes_the_instantiated_ontology(self):
        s = Q.ontology_schema()
        self.assertEqual(set(s["nodes"]), set(NODE_TYPES))
        self.assertEqual(set(s["relations"]), set(REL_TYPES))
        self.assertEqual(sum(s["nodes"].values()), len(G.nodes))
        self.assertEqual(sum(s["relations"].values()), len(G.edges))
        self.assertEqual(sum(t["count"] for t in s["triples"]), len(G.edges))
        self.assertIn({"from": "Repository", "rel": "WRITES", "to": "Table"}, [{k: t[k] for k in ("from", "rel", "to")} for t in s["triples"]])

    def test_run_dispatch_and_query_name_registered(self):
        from kgraph.ontology import QUERIES
        self.assertIn("explore_ontology", QUERIES)
        r = Q.run("explore_ontology", source_type="Controller", relation="EXPOSES", target_type="API", direction="out", depth="1")
        self.assertEqual(r["data"]["total"], 7)


class OntologyNaturalLanguageTests(unittest.TestCase):
    NL = NaturalLanguage(G)

    def ask(self, q):
        return self.NL.ask(q)

    def test_questions_in_english_and_french(self):
        for q in ("which services write to orders?", "quels services écrivent dans orders ?"):
            r = self.ask(q)
            self.assertEqual(r["intent"], "explore_ontology", q)
            self.assertEqual(pairs(r), {("OrderService", "orders")}, q)
        r = self.ask("which screens call APIs")
        self.assertEqual((r["intent"], r["data"]["pattern"]), ("explore_ontology", "Screen ─CALLS→ API"))
        r = self.ask("quelles API appellent des services")
        self.assertEqual(r["data"]["pattern"], "API ─CALLS→ Service")
        r = self.ask("which repositories read orders")
        self.assertEqual(pairs(r), {("OrderRepository", "orders")})

    def test_explicit_mention_of_the_ontology(self):
        r = self.ask("explore the ontology: controllers expose apis")
        self.assertEqual(r["data"]["pattern"], "Controller ─EXPOSES→ API")

    def test_existing_intents_are_not_hijacked(self):
        for q, intent in (("which screens write to orders.status?", "find_data_lineage"),
                          ("what is the impact if I change the table customers?", "determine_impact"),
                          ("show the call chain from /orders/new to the database", "show_call_chain"),
                          ("explain the screen OrderCreateComponent", "explain_screen"),
                          ("show capability Orders", "show_capability")):
            self.assertEqual(self.ask(q)["intent"], intent, q)

    def test_unparseable_ontology_request_gets_help(self):
        r = self.ask("explore the ontology please")
        self.assertTrue(r["error"])
        self.assertIn("which services write to orders", r["summary"])


class OntologyServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from kgraph.server import State, make_handler
        with __import__('unittest').mock.patch.dict(__import__('os').environ, {'KGRAPH_ENGINE': 'memory'}):   # never use a real Neo4j here
            cls.state = State(Path("/tmp/kgraph-ontology-test"))
        cls.state.set_graph(G)
        cls.H = make_handler(cls.state, 0)

    def call(self, method, path, body=None):
        h = self.H.__new__(self.H)
        data = json.dumps(body).encode() if body is not None else b""
        msg = Message()
        for k, v in {"Host": "127.0.0.1:1", "X-KG-Request": "1", "Content-Length": str(len(data))}.items():
            msg[k] = v
        h.headers, h.path, h.rfile, h.wfile = msg, path, io.BytesIO(data), io.BytesIO()
        h.request_version, h.command, h.client_address, h.requestline = "HTTP/1.1", method, ("127.0.0.1", 0), f"{method} {path}"
        getattr(h, "do_" + method)()
        head, _, payload = h.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), json.loads(payload)

    def test_ontology_endpoint(self):
        code, body = self.call("GET", "/api/ontology")
        self.assertEqual(code, 200)
        self.assertEqual(set(body["nodes"]), set(NODE_TYPES))
        self.assertTrue(body["triples"])

    def test_query_endpoint(self):
        code, body = self.call("POST", "/api/query", {"query": "explore_ontology",
                                                        "params": {"source_type": "Service", "relation": "WRITES", "target_type": "Table"}})
        self.assertEqual(code, 200)
        self.assertGreater(body["data"]["total"], 0)
        code, body = self.call("POST", "/api/query", {"query": "explore_ontology", "params": {"relation": "LOVES"}})
        self.assertEqual(code, 404)
        self.assertIn("suggestions", body)


if __name__ == "__main__":
    unittest.main()
