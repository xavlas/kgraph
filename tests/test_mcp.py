import json
import tempfile
import unittest
from pathlib import Path

from kgraph.mcp import GraphProvider, McpServer
from kgraph.pipeline import scan_repository

SHOP = Path(__file__).resolve().parent.parent / "samples" / "shop"


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        g = scan_repository(str(SHOP), cls.tmp.name + "/ws", None)
        cls.path = Path(cls.tmp.name) / "g.json"
        cls.path.write_text(g.to_json(), encoding="utf-8")
        cls.srv = McpServer(GraphProvider(str(cls.path)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rpc(self, method, params=None, mid=1):
        return self.srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_initialize_and_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["capabilities"])
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertTrue({"determine_impact", "ask", "explore_ontology", "graph_overview"} <= names)
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def call(self, name, args=None):
        return self.rpc("tools/call", {"name": name, "arguments": args or {}})

    def test_tools(self):
        r = self.call("graph_overview")["result"]
        self.assertFalse(r["isError"])
        self.assertGreater(r["structuredContent"]["stats"]["nodes"], 10)
        r = self.call("determine_impact", {"name": "customers"})["result"]
        self.assertFalse(r["isError"])
        self.assertNotIn("graph", r["structuredContent"])
        self.assertTrue(r["content"][0]["text"])
        r = self.call("ask", {"question": "What is the impact if I change the table customers?"})["result"]
        self.assertFalse(r["isError"])
        r = self.call("explore_ontology", {"source_type": "Service", "relation": "WRITES", "target_type": "Table"})["result"]
        self.assertFalse(r["isError"])
        hits = self.call("search_nodes", {"text": "customer", "types": ["Table"]})["result"]["structuredContent"]["nodes"]
        self.assertTrue(hits and all(h["type"] == "Table" for h in hits))

    def test_errors(self):
        r = self.call("determine_impact", {"name": "zzzz-nothing"})["result"]
        self.assertTrue(r["isError"])
        self.assertTrue(self.call("determine_impact")["result"]["isError"])           # missing argument
        self.assertEqual(self.call("nope")["error"]["code"], -32602)
        self.assertEqual(self.rpc("bogus")["error"]["code"], -32601)

    def test_stdio_framing(self):
        import io
        out = io.StringIO()
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "not json", ""]
        self.srv.serve(io.StringIO("\n".join(lines) + "\n"), out)
        got = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(got[0]["result"], {})
        self.assertEqual(got[1]["error"]["code"], -32700)

    def test_missing_graph(self):
        r = McpServer(GraphProvider(self.tmp.name + "/none.json")).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "graph_overview"}})["result"]
        self.assertTrue(r["isError"])


if __name__ == "__main__":
    unittest.main()
