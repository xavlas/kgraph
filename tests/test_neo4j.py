import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kgraph.graph import Graph                       # noqa: E402
from kgraph.neo4j_export import (Neo4jError, flatten_props, lit, load, statements, to_cypher)  # noqa: E402
from kgraph.pipeline import scan_repository          # noqa: E402

G = scan_repository(str(ROOT / "samples" / "shop"))


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


class ExportTests(unittest.TestCase):
    def test_flatten_keeps_primitives_and_json_encodes_the_rest(self):
        out = flatten_props({"a": 1, "b": "x", "c": True, "d": None, "e": [1, 2], "f": ["a", "b"], "g": [1, "a"],
                             "h": {"k": 1}, "i": [{"x": 1}], "j": [], "k": float("nan"), "bad key": 2})
        self.assertEqual((out["a"], out["b"], out["c"], out["e"], out["f"]), (1, "x", True, [1, 2], ["a", "b"]))
        self.assertNotIn("d", out); self.assertNotIn("j", out); self.assertNotIn("k", out)
        self.assertEqual(json.loads(out["g_json"]), [1, "a"])               # mixed list: Neo4j arrays must be homogeneous
        self.assertEqual(json.loads(out["h_json"]), {"k": 1})
        self.assertEqual(out["bad_key"], 2)

    def test_literals_are_escaped(self):
        self.assertEqual(lit('a"b\\c\nd'), '"a\\"b\\\\c\\nd"')
        self.assertEqual(lit(None), "null"); self.assertEqual(lit(True), "true"); self.assertEqual(lit([1, "x"]), '[1, "x"]')
        self.assertEqual(lit({"k": 1}), "{`k`: 1}")
        self.assertIn("\\u0001", lit("\x01"))
        self.assertEqual(lit("'; MATCH (n) DETACH DELETE n //"), '"\'; MATCH (n) DETACH DELETE n //"')   # data can never become a statement

    def test_every_node_and_relationship_is_exported_once(self):
        stm = statements(G)
        n_rows = sum(len(p["rows"]) for cy, p in stm if "MERGE (n:" in cy)
        r_rows = sum(len(p["rows"]) for cy, p in stm if "MERGE (a)-[" in cy and "`FLOW`" not in cy)
        self.assertEqual((n_rows, r_rows), (len(G.nodes), len(G.edges)))
        labels = {re.search(r"MERGE \(n:`(\w+)`", cy).group(1) for cy, p in stm if "MERGE (n:" in cy}
        self.assertEqual(labels, {n["type"] for n in G.nodes.values()})
        rels = {re.search(r"\[x:`(\w+)`\]", cy).group(1) for cy, p in stm if "MERGE (a)-[" in cy and "`FLOW`" not in cy}
        self.assertEqual(rels, {e["rel"] for e in G.edges.values()})

    def test_flow_links_follow_the_runtime_direction(self):
        flow = [(r, cy) for cy, p in statements(G) if "`FLOW`" in cy for r in p["rows"]]
        self.assertEqual(len(flow), sum(1 for e in G.edges.values() if e["rel"] not in ("IMPACTS", "IMPLEMENTS")))
        by = {(r["src"], r["dst"], r["rel"]) for r, _ in flow}
        # Controller -EXPOSES-> API is reversed: the request enters through the API
        api = next(n for n in G.nodes.values() if n["name"] == "POST /api/orders")
        ctrl = next(n for n in G.nodes.values() if n["name"] == "OrderController")
        self.assertIn((f"shop|{api['id']}", f"shop|{ctrl['id']}", "EXPOSES"), by)
        self.assertNotIn((f"shop|{ctrl['id']}", f"shop|{api['id']}", "EXPOSES"), by)
        # Route -EXPOSES-> Screen keeps its declared direction
        route = next(n for n in G.nodes.values() if n["type"] == "Route")
        self.assertTrue(any(a == f"shop|{route['id']}" and r == "EXPOSES" for a, _, r in by))
        # associative relations are not part of FLOW
        self.assertFalse(any(r in ("IMPACTS", "IMPLEMENTS") for _, _, r in by))

    def test_lossless_properties_and_explicit_derived_flag(self):
        nodes = [r for cy, p in statements(G) if "MERGE (n:" in cy for r in p["rows"]]
        api = next(r for r in nodes if r["props"]["name"] == "POST /api/orders")
        self.assertEqual(json.loads(api["props"]["props_json"])["field_access"], G.nodes[api["props"]["id"]]["props"]["field_access"])
        rels = [r for cy, p in statements(G) if "MERGE (a)-[x:" in cy and "`FLOW`" not in cy for r in p["rows"]]
        self.assertTrue(all(isinstance(r["props"]["derived"], bool) for r in rels))
        self.assertTrue(any(r["props"]["derived"] for r in rels) and any(not r["props"]["derived"] for r in rels))

    def test_kgmeta_marks_a_complete_load_and_is_written_last(self):
        stm = statements(G)
        self.assertIn("`KGMeta`", stm[-1][0])
        row = stm[-1][1]["rows"][0]
        self.assertEqual((row["kg_repo"], row["nodes"], row["edges"]), ("shop", len(G.nodes), len(G.edges)))
        self.assertTrue(all("`KGMeta`" not in cy for cy, _ in stm[:-1] if not cy.startswith("CREATE")))

    def test_batching(self):
        small = statements(G, batch=10)
        self.assertTrue(all(len(p["rows"]) <= 10 for cy, p in small if p))
        self.assertGreater(len(small), len(statements(G, batch=500)))

    def test_properties_survive(self):
        rows = [r for cy, p in statements(G) if "MERGE (n:`API`" in cy for r in p["rows"]]
        api = next(r for r in rows if r["props"]["name"] == "POST /api/orders")
        self.assertEqual(api["props"]["kg_repo"], "shop")
        self.assertEqual(api["uid"], "shop|API:POST /api/orders")
        self.assertIn("field_access_json", api["props"])                    # nested map kept as JSON
        derived = [r for cy, p in statements(G) if "MERGE (a)-[x:`CALLS`" in cy and "`Screen`" in cy for r in p["rows"]
                   if r["props"].get("derived")]
        self.assertTrue(derived)                                            # exact (derived) links keep their flag

    def test_cypher_script_is_complete_and_inert(self):
        text = to_cypher(G)
        self.assertIn("CREATE CONSTRAINT kg_screen_uid IF NOT EXISTS", text)
        self.assertIn("MERGE (a)-[x:`WRITES`]->(b)", text)
        self.assertNotIn("$rows", text)                                    # parameters are inlined in the file version
        self.assertIn("DETACH DELETE n;", text)                            # documented clean-up, as a comment only
        for line in text.splitlines():
            if "DETACH DELETE" in line:
                self.assertTrue(line.startswith("//"))
        code = [l for l in text.splitlines() if l and not l.startswith("//")]
        self.assertTrue(all(l.endswith(";") for l in code))                # one statement per line, all terminated

    @staticmethod
    def outside_strings(line):
        """The part of a Cypher line that is NOT inside a double-quoted literal (escapes honoured)."""
        out, in_str, i = [], False, 0
        while i < len(line):
            ch = line[i]
            if in_str and ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                out.append(ch)
            i += 1
        return "".join(out)

    def test_hostile_data_stays_inside_string_literals(self):
        g = Graph()
        g.meta = {"repository": "x'y"}
        a = g.add_node("Table", 'evil"}) DETACH DELETE n //', note="line1\nline2", sql="'; MATCH (n) DETACH DELETE n; //")
        b = g.add_node("Table", "ok")
        g.add_edge(a, b, "DEPENDS_ON")
        for line in to_cypher(g).splitlines():
            if not line or line.startswith("//"):
                continue
            outside = self.outside_strings(line)
            self.assertNotIn("DETACH", outside, line[:120])            # nothing destructive outside a literal
            self.assertEqual(outside.count(";"), 1, line[:120])         # exactly one terminator, at the end
            self.assertTrue(outside.rstrip().endswith(";"))

    def test_a_repository_name_with_newlines_cannot_escape_the_header_comments(self):
        g = Graph()
        g.meta = {"repository": "a\nDETACH DELETE (n);\r\u2028MATCH (m) DETACH DELETE m;"}
        g.add_node("Table", "t")
        for line in to_cypher(g).splitlines():
            if line and not line.startswith("//"):
                self.assertNotIn("DETACH", self.outside_strings(line), line[:120])


class LoaderTests(unittest.TestCase):
    def run_load(self, **kw):
        calls = []

        def fake(req, timeout=None):
            calls.append((req.full_url, req.headers, json.loads(req.data)))
            return FakeResponse(json.dumps({"results": [], "errors": []}).encode())
        with mock.patch("urllib.request.urlopen", fake):
            r = load(G, **{"uri": "http://localhost:7474", "user": "neo4j", "password": "s3cret", **kw})
        return r, calls

    def test_posts_every_statement_in_small_transactions(self):
        r, calls = self.run_load()
        total = sum(len(c[2]["statements"]) for c in calls)
        self.assertEqual(total, r["statements"])
        self.assertTrue(all(len(c[2]["statements"]) <= 10 for c in calls))
        self.assertEqual(calls[0][0], "http://localhost:7474/db/neo4j/tx/commit")
        self.assertEqual((r["nodes"], r["relationships"]), (len(G.nodes), len(G.edges)))

    def test_basic_auth_header_and_password_never_in_body(self):
        _, calls = self.run_load(password="s3cret")
        self.assertTrue(calls[0][1]["Authorization"].startswith("Basic "))
        self.assertTrue(all("s3cret" not in json.dumps(c[2]) for c in calls))

    def test_clear_only_targets_this_repository(self):
        _, calls = self.run_load(clear=True)
        stmts = [s for c in calls for s in c[2]["statements"]]
        clear = [s for s in stmts if "DETACH DELETE" in s["statement"]]
        self.assertEqual(len(clear), 1)
        self.assertTrue(stmts.index(clear[0]) <= stmts.index(next(s for s in stmts if "MERGE (n:" in s["statement"])))   # first write
        self.assertEqual(clear[0]["statement"], "MATCH (n) WHERE (n:KGNode OR n:KGMeta) AND n.kg_repo = $repo DETACH DELETE n")
        self.assertEqual(clear[0]["parameters"], {"repo": "shop"})

    def test_schema_changes_never_share_a_transaction_with_writes(self):
        """Neo4j: 'Tried to execute Schema modification after executing Write query' (seen on a real server)."""
        for clear in (False, True):
            _, calls = self.run_load(clear=clear)
            kinds = []
            for c in calls:
                batch = [s["statement"] for s in c[2]["statements"]]
                is_schema = [x.startswith(("CREATE CONSTRAINT", "CREATE INDEX")) for x in batch]
                self.assertTrue(all(is_schema) or not any(is_schema), f"mixed request: {batch[:2]}")
                if any(is_schema):
                    self.assertEqual(len(batch), 1)                         # one constraint per transaction
                kinds.append("schema" if any(is_schema) else "write")
            first_write = kinds.index("write")
            self.assertNotIn("schema", kinds[first_write:])                 # every constraint comes before the first write

    def test_wait_retries_until_the_database_is_available(self):
        import urllib.error
        seen = {"n": 0}

        def flaky(req, timeout=None):
            body = json.loads(req.data)
            if body["statements"][0]["statement"] == "RETURN 1" and seen["n"] < 2:
                seen["n"] += 1
                return FakeResponse(json.dumps({"errors": [{"code": "Neo.TransientError.General.DatabaseUnavailable", "message": "starting"}]}).encode())
            return FakeResponse(json.dumps({"results": [], "errors": []}).encode())
        with mock.patch("urllib.request.urlopen", flaky), mock.patch("time.sleep") as slept:
            r = load(G, "http://localhost:7474", "neo4j", "x", wait=30)
        self.assertEqual(seen["n"], 2)
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(r["nodes"], len(G.nodes))

    def test_wait_does_not_retry_authentication_errors(self):
        import urllib.error

        def unauthorized(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", unauthorized), mock.patch("time.sleep") as slept:
            with self.assertRaisesRegex(Neo4jError, "authentication failed"):
                load(G, "http://localhost:7474", "neo4j", "bad", wait=30)
        slept.assert_not_called()

    def test_refuses_unsafe_targets(self):
        for kw in ({"uri": "ftp://localhost"}, {"uri": "bolt://localhost:7687"}, {"uri": "http://db.example.com:7474"},
                   {"database": "neo4j/../x"}):
            with self.assertRaises(Neo4jError, msg=str(kw)):
                self.run_load(**kw)
        self.run_load(uri="https://db.example.com")                       # https to a remote host is fine

    def test_cypher_errors_and_auth_errors_are_reported(self):
        def bad(req, timeout=None):
            return FakeResponse(json.dumps({"errors": [{"code": "Neo.ClientError.Statement.SyntaxError", "message": "boom"}]}).encode())
        with mock.patch("urllib.request.urlopen", bad):
            with self.assertRaisesRegex(Neo4jError, "SyntaxError: boom"):
                load(G, "http://localhost:7474", "neo4j", "x")
        import urllib.error

        def unauthorized(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", unauthorized):
            with self.assertRaisesRegex(Neo4jError, "authentication failed"):
                load(G, "http://localhost:7474", "neo4j", "wrong")
        def down(req, timeout=None):
            raise urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", down):
            with self.assertRaisesRegex(Neo4jError, "cannot reach Neo4j"):
                load(G, "http://localhost:7474", "neo4j", "x")


if __name__ == "__main__":
    unittest.main()
