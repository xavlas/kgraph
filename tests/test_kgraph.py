import io
import json
import os
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kgraph import backend as be            # noqa: E402
from kgraph import frontend as fe           # noqa: E402
from kgraph.docs import generate_documentation  # noqa: E402
from kgraph.graph import Graph              # noqa: E402
from kgraph.lex import Src, flatten, match_close, norm_path, skeleton  # noqa: E402
from kgraph.nlq import NaturalLanguage      # noqa: E402
from kgraph.pipeline import scan_repository  # noqa: E402
from kgraph.queries import QueryEngine, QueryError  # noqa: E402
from kgraph.source import RepoError, load_repository  # noqa: E402
from kgraph.sqlscan import parse_sql        # noqa: E402

SHOP = ROOT / "samples" / "shop"
_cache = {}


def shop_graph() -> Graph:
    if "g" not in _cache:
        _cache["g"] = scan_repository(str(SHOP))
    return _cache["g"]


def names(g, t):
    return {n["name"] for n in g.of_type(t)}


class LexTests(unittest.TestCase):
    def test_skeleton_blanks_strings_and_comments(self):
        t = 'a("x{", \'}\'); // brace }\n/* { */ b();'
        s = skeleton(t)
        self.assertEqual(len(s), len(t))
        self.assertEqual(s.count("{"), 0)
        self.assertEqual(s.count("}"), 0)
        self.assertIn("b()", s)

    def test_match_close_and_flatten(self):
        s = skeleton("class A { void f() { if (x) { y(); } } int z; }")
        o = s.index("{")
        self.assertEqual(s[match_close(s, o)], "}")
        self.assertEqual(match_close(s, o), len(s) - 1)
        flat = flatten(s, o + 1, len(s) - 1)
        self.assertNotIn("if", flat)
        self.assertIn("int z;", flat)

    def test_norm_path(self):
        self.assertEqual(norm_path("http://h:8080/api/orders/{id}/?q=1"), "/api/orders/{}")
        self.assertEqual(norm_path("/orders/:id/lines"), "/orders/{}/lines")
        self.assertEqual(norm_path("orders"), "/orders")


class BackendTests(unittest.TestCase):
    def parse(self, rel):
        return be.parse_jvm_file(rel, (SHOP / rel).read_text())

    def test_java_class_roles_and_endpoints(self):
        [ci] = self.parse("backend/src/main/java/com/acme/shop/order/OrderController.java")
        self.assertEqual(be.classify(ci), "controller")
        eps = {(e["verb"], e["path"]) for e in be.endpoints(ci, {}, {}, "/api")}
        self.assertEqual(eps, {("GET", "/api/orders"), ("GET", "/api/orders/{id}"), ("POST", "/api/orders"),
                               ("POST", "/api/orders/{id}/cancel")})

    def test_kotlin_primary_constructor_injection_and_entity(self):
        [svc] = self.parse("backend/src/main/kotlin/com/acme/shop/inventory/InventoryService.kt")
        self.assertEqual(svc.deps["stockRepository"], "StockRepository")
        self.assertEqual({m.name for m in svc.methods}, {"levels", "reserve"})
        [ent] = self.parse("backend/src/main/kotlin/com/acme/shop/inventory/StockItem.kt")
        info = be.entity_info(ent, {})
        self.assertEqual(info["table"], "stock_items")
        cols = {c["field"]: c["column"] for c in info["columns"]}
        self.assertEqual(cols["availableQty"], "available_qty")

    def test_repository_generic_and_derived_fields(self):
        [repo] = self.parse("backend/src/main/java/com/acme/shop/order/OrderRepository.java")
        self.assertEqual(be.classify(repo), "repository")
        self.assertEqual(repo.bases[0][0], "JpaRepository")
        self.assertEqual(be.derived_query_fields("findByCustomerIdAndStatus"), ["customerId", "status"])
        self.assertEqual(be.classify_method("save"), "W")
        self.assertEqual(be.classify_method("findAll"), "R")

    def test_sql_table_extraction(self):
        r, w = be.sql_tables("INSERT INTO a(x) SELECT x FROM b JOIN c ON b.i=c.i")
        self.assertEqual((r, w), ({"b", "c"}, {"a"}))
        r, w = be.sql_tables("update Order o set o.status = 'X' where o.id = 1")
        self.assertEqual((r, w), (set(), {"Order"}))

    def test_broken_source_does_not_raise(self):
        be.parse_jvm_file("Broken.java", "public class Broken { void f( { if (")
        be.parse_jvm_file("Broken.kt", "class X(val a: ) { fun ( }")


class FrontendTests(unittest.TestCase):
    def test_eval_expr(self):
        env = {"apiUrl": "http://x:8080/api", "baseUrl": "/api/orders"}
        self.assertEqual(fe.eval_expr("`${this.baseUrl}/${id}`", env), "/api/orders/{}")
        self.assertEqual(fe.eval_expr("environment.apiUrl + '/orders'", env), "http://x:8080/api/orders")
        self.assertEqual(fe.eval_expr("'/a/' + id", env), "/a/{}")
        self.assertIsNone(fe.eval_expr("", env))

    def test_angular_nested_and_lazy_routes(self):
        text = (SHOP / "frontend-angular/src/app/app.routes.ts").read_text()
        routes = fe.parse_route_objects("app.routes.ts", text, "angular")
        paths = [p for p in fe.full_route_paths(routes)]
        self.assertIn("/orders/new", paths)
        self.assertIn("/orders/:id", paths)
        self.assertTrue(any(r.lazy_children for r in routes))

    def test_react_jsx_routes(self):
        text = '<Routes><Route path="/a" element={<A />}><Route path="b" element={<B/>} /></Route></Routes>'
        routes = fe.parse_jsx_routes("App.tsx", text)
        self.assertEqual(fe.full_route_paths(routes), ["/a", "/a/b"])


class SqlTests(unittest.TestCase):
    def test_postgres_trigger_and_checks(self):
        p = parse_sql("x.sql", (SHOP / "db/migration/V1__init.sql").read_text())
        self.assertEqual(p["dialect"], "postgresql")
        self.assertEqual(set(p["tables"]), {"customers", "orders", "order_lines", "stock_items"})
        self.assertIn(("customer_id", "customers", "id"), p["tables"]["orders"]["fks"])
        self.assertIn(("order_id", "orders", "id"), p["tables"]["order_lines"]["fks"])
        kinds = {r["kind"] for r in p["rules"]}
        self.assertEqual(kinds, {"db-check", "db-unique", "db-trigger"})
        trig = next(r for r in p["rules"] if r["kind"] == "db-trigger")
        self.assertEqual(trig["also_writes"], ["order_audit"])

    def test_oracle_and_mysql_dialects(self):
        ora = parse_sql("o.sql", "CREATE TABLE emp (id NUMBER(10) PRIMARY KEY, name VARCHAR2(50) NOT NULL, "
                                 "mgr NUMBER(10) REFERENCES emp(id));")
        self.assertEqual(ora["dialect"], "oracle")
        self.assertEqual(ora["tables"]["emp"]["fks"], [("mgr", "emp", "id")])
        my = parse_sql("m.sql", "CREATE TABLE `t` (`id` INT AUTO_INCREMENT, PRIMARY KEY (`id`)) ENGINE=InnoDB;")
        self.assertEqual(my["dialect"], "mysql")
        self.assertTrue(my["tables"]["t"]["columns"][0]["pk"])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.g = shop_graph()

    def test_node_inventory(self):
        g = self.g
        self.assertEqual(names(g, "Controller"), {"OrderController", "CustomerController", "InventoryController"})
        self.assertEqual(names(g, "Table"), {"customers", "orders", "order_lines", "stock_items", "order_audit"})
        self.assertEqual(names(g, "Capability"), {"Orders", "Customers", "Inventory"})
        self.assertIn("/inventory/levels", names(g, "Route"))     # lazy-loaded Angular child routes
        self.assertIn("/customers/new", names(g, "Route"))        # React Router
        self.assertEqual(names(g, "Topic"), {"orders.created"})   # resolved from ${app.topics...} placeholder
        self.assertFalse([n for n in g.of_type("API") if n["props"].get("unresolved")])

    def test_context_path_and_front_back_linking(self):
        g = self.g
        screen = next(n for n in g.of_type("Screen") if n["name"] == "OrderCreateComponent")
        apis = {g.nodes[e["dst"]]["name"] for e in g.out_edges(screen["id"]) if e["rel"] == "CALLS" and e["props"].get("derived")}
        self.assertEqual(apis, {"POST /api/orders"})

    def test_listener_and_async_edge(self):
        g = self.g
        tid = next(n["id"] for n in g.of_type("Topic"))
        self.assertEqual({g.nodes[d]["name"] for d, _ in g.flow_succ(tid)}, {"OrderEventsListener"})

    def test_rule_kinds_detected(self):
        kinds = {n["props"]["kind"] for n in self.g.of_type("BusinessRule")}
        self.assertTrue({"guard", "existence", "validation", "authorization", "declared", "lifecycle",
                         "db-check", "db-unique", "db-trigger"} <= kinds)

    def test_exact_rule_scoping(self):
        # cancelling must not trigger entity validation (bulk JPQL update) but must fire the DB trigger
        g = self.g
        cancel = next(n for n in g.of_type("API") if n["name"].endswith("/cancel"))
        rules = {g.nodes[e["dst"]]["props"]["description"] for e in g.out_edges(cancel["id"]) if e["rel"] == "EXECUTES"}
        self.assertIn("A shipped order cannot be cancelled", rules)
        self.assertTrue(any(r.startswith("Trigger trg_order_status") for r in rules))
        self.assertFalse(any("PrePersist" in r for r in rules))

    def test_empty_and_unreadable_repository(self):
        with tempfile.TemporaryDirectory() as d:
            g = scan_repository(d)
            self.assertEqual(len(g.nodes), 0)
        with self.assertRaises(RepoError):
            scan_repository("/definitely/not/here")

    def test_serialisation_roundtrip(self):
        g2 = Graph.from_dict(json.loads(self.g.to_json()))
        self.assertEqual(g2.stats(), self.g.stats())


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.q = QueryEngine(shop_graph())

    def test_explain_screen_by_route_and_name(self):
        r = self.q.explain_screen("/orders/new")
        self.assertEqual(r["target"]["name"], "OrderCreateComponent")
        api = r["data"]["apis"][0]
        self.assertEqual(api["api"]["name"], "POST /api/orders")
        self.assertEqual(api["controller"], "OrderController")
        self.assertIn("orders", {t["table"] for t in api["tables"] if t["mode"] == "W"})
        self.assertEqual(api["topics"], ["orders.created"])

    def test_call_chain_front_to_database(self):
        r = self.q.show_call_chain("OrderCreateComponent", "orders")
        chains = r["data"]["chains"]
        self.assertTrue(chains)
        types = [n["type"] for n in chains[0]]
        self.assertEqual(types[0], "Screen")
        self.assertEqual(types[-1], "Table")
        self.assertEqual(types[:3], ["Screen", "Component", "API"])

    def test_business_rules_filters(self):
        all_rules = self.q.find_business_rules()
        self.assertGreater(len(all_rules["data"]["rules"]), 20)
        guards = self.q.find_business_rules(kind="guard")["data"]["rules"]
        self.assertTrue(guards and all(r["kind"] == "guard" for r in guards))
        credit = self.q.find_business_rules(text="credit limit")["data"]["rules"]
        # the service guard and the DB CHECK on customers.credit_limit both match
        self.assertEqual({r["kind"] for r in credit}, {"guard", "db-check"})
        guard = next(r for r in credit if r["kind"] == "guard")
        self.assertEqual(guard["impacts"], ["Customer"])

    def test_impact_reaches_screens_from_table(self):
        r = self.q.determine_impact("customers")
        impacted = r["data"]["impacted"]
        self.assertIn("OrderCreateComponent", {i["name"] for i in impacted["Screen"]})
        self.assertIn("CustomersPage", {i["name"] for i in impacted["Screen"]})
        self.assertEqual(r["data"]["risk"], "high")
        # a leaf screen impacts nothing but its route
        self.assertEqual(self.q.determine_impact("StockLevelsComponent")["data"]["counts"].get("Screen", 0), 0)

    def test_impact_downstream(self):
        r = self.q.determine_impact("OrderCreateComponent", direction="downstream")
        self.assertIn("orders", {i["name"] for i in r["data"]["impacted"]["Table"]})

    def test_capability_and_sharing(self):
        r = self.q.show_capability("Orders")
        members = {k: {n["name"] for n in v} for k, v in r["data"]["members"].items()}
        self.assertEqual(members["Screen"], {"OrderListComponent", "OrderCreateComponent", "OrderDetailComponent"})
        self.assertNotIn("InventoryService", members["Service"])
        self.assertIn("Customers", {s["capability"] for s in r["data"]["shared_with"]})
        self.assertEqual(len(self.q.show_capability()["data"]["capabilities"]), 3)

    def test_lineage_table_column_and_forward(self):
        r = self.q.find_data_lineage("orders")
        writers = {w["api"] for w in r["data"]["writers"]}
        self.assertEqual(writers, {"POST /api/orders", "POST /api/orders/{id}/cancel"})
        self.assertIn("order_audit", " ".join(r["data"]["downstream"]))
        col = self.q.find_data_lineage("orders.status")
        self.assertEqual(col["data"]["column"], "status")
        w = {(x["api"], tuple(x["via"])) for x in col["data"]["writers"]}
        self.assertIn(("POST /api/orders/{id}/cancel", ("OrderRepository.markCancelled",)), w)
        fwd = self.q.find_data_lineage("OrderCreateComponent")
        self.assertIn("orders", {t["table"] for t in fwd["data"]["tables"]})

    def test_errors_carry_suggestions(self):
        with self.assertRaises(QueryError) as cm:
            self.q.explain_screen("NoSuchScreenAnywhere")
        self.assertTrue(cm.exception.suggestions)
        with self.assertRaises(QueryError):
            self.q.find_data_lineage("orders.nope")
        with self.assertRaises(QueryError):
            self.q.run("drop_database")


class NlqTests(unittest.TestCase):
    CASES = [
        ("explain the screen OrderCreateComponent", "explain_screen", "OrderCreateComponent"),
        ("what is the impact if I change the table customers?", "determine_impact", "customers"),
        ("which screens write to orders.status?", "find_data_lineage", "orders"),
        ("show the call chain from /orders/new to the database", "show_call_chain", "OrderCreateComponent"),
        ("what business rules apply to POST /api/orders?", "find_business_rules", "POST /api/orders"),
        ("show capability Orders", "show_capability", "Orders"),
        ("quelles règles métier s'appliquent à l'écran de création de commande ?", "find_business_rules", "OrderCreateComponent"),
        ("quel est l'impact si je modifie la table customers ?", "determine_impact", "customers"),
        ("qui écrit dans la colonne status de la table orders", "find_data_lineage", "orders"),
    ]

    def test_intents_and_targets(self):
        nl = NaturalLanguage(shop_graph())
        for q, intent, target in self.CASES:
            with self.subTest(q=q):
                r = nl.ask(q)
                self.assertEqual(r["intent"], intent)
                self.assertEqual(r["target"]["name"], target)

    def test_unknown_question_gets_help_not_crash(self):
        r = NaturalLanguage(shop_graph()).ask("bonjour")
        self.assertTrue(r["error"])
        self.assertIn("Examples", r["summary"])


class DocsTests(unittest.TestCase):
    def test_generated_files(self):
        files = generate_documentation(shop_graph())
        for f in ("index.md", "capabilities.md", "apis.md", "business-rules.md", "data-model.md", "topics.md",
                  "screens/ordercreatecomponent.md"):
            self.assertIn(f, files)
        self.assertIn("```mermaid", files["screens/ordercreatecomponent.md"])
        self.assertIn("erDiagram", files["data-model.md"])
        self.assertIn("validate", files["index.md"].lower())


class SourceSafetyTests(unittest.TestCase):
    def test_rejects_option_injection_and_ext_transport(self):
        for bad in ("-oProxyCommand=touch /tmp/pwn", "ext::sh -c touch% /tmp/pwn", "--upload-pack=evil"):
            with self.assertRaises(RepoError):
                load_repository(bad, tempfile.mkdtemp())

    def test_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            Path(outside, "Secret.java").write_text("public class Secret {}")
            os.symlink(Path(outside, "Secret.java"), Path(d, "Link.java"))
            Path(d, "Ok.java").write_text("public class Ok {}")
            repo = load_repository(d)
            self.assertEqual(repo.files(".java"), ["Ok.java"])


class ServerTests(unittest.TestCase):
    """Exercise the HTTP handler in-process (no socket needed)."""

    @classmethod
    def setUpClass(cls):
        from kgraph.server import State, make_handler
        with __import__('unittest').mock.patch.dict(__import__('os').environ, {'KGRAPH_ENGINE': 'memory'}):   # never use a real Neo4j here
            cls.state = State(Path(tempfile.mkdtemp()))
        cls.state.set_graph(shop_graph())
        cls.Handler = make_handler(cls.state, 0)

    def call(self, method, path, body=None, headers=None):
        h = self.Handler.__new__(self.Handler)
        msg = Message()
        for k, v in {"Host": "127.0.0.1:8765", **(headers or {})}.items():
            msg[k] = v
        data = json.dumps(body).encode() if body is not None else b""
        if data:
            msg["Content-Length"] = str(len(data))
        h.headers, h.path, h.rfile, h.wfile = msg, path, io.BytesIO(data), io.BytesIO()
        h.request_version, h.command, h.client_address, h.requestline = "HTTP/1.1", method, ("127.0.0.1", 0), f"{method} {path}"
        getattr(h, "do_" + method)()
        raw = h.wfile.getvalue()
        head, _, payload = raw.partition(b"\r\n\r\n")
        code = int(head.split(b" ", 2)[1])
        return code, payload

    def test_api_happy_path(self):
        code, p = self.call("GET", "/api/meta")
        self.assertEqual(code, 200)
        self.assertIn("Screen", json.loads(p)["node_types"])
        code, p = self.call("POST", "/api/query", {"query": "show_capability", "params": {"name": "Orders"}}, {"X-KG-Request": "1"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(p)["target"]["name"], "Orders")
        code, p = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Knowledge Graph", p)

    def test_post_requires_custom_header_and_local_host(self):
        self.assertEqual(self.call("POST", "/api/ask", {"question": "x"})[0], 403)
        self.assertEqual(self.call("GET", "/api/graph", headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self.call("GET", "/api/graph", headers={"Origin": "https://evil.example.com"})[0], 403)

    def test_errors_and_path_traversal(self):
        code, p = self.call("POST", "/api/query", {"query": "explain_screen", "params": {"name": "zzz"}}, {"X-KG-Request": "1"})
        self.assertEqual(code, 404)
        self.assertIn("suggestions", json.loads(p))
        self.assertEqual(self.call("GET", "/static/../server.py")[0], 404)
        self.assertEqual(self.call("GET", "/static/%2e%2e/server.py")[0], 404)
        self.assertEqual(self.call("POST", "/api/scan", {}, {"X-KG-Request": "1"})[0], 400)


if __name__ == "__main__":
    unittest.main()
