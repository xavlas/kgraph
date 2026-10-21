"""Plugin integration tests.

The real `code-modernization` plugin is not available in CI, so a fake `claude` executable reproduces the
contract documented by the plugin's extract-rules command: it is called as `claude -p` with the slash
command on stdin, runs in a folder holding `legacy/<name>`, and writes analysis/<name>/BUSINESS_RULES.md.
"""
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kgraph.modernization import (INSTALL_HINT, ModernizationError, Modernizer, parse_business_rules,  # noqa: E402
                                  plugin_installed)
from kgraph.pipeline import scan_repository  # noqa: E402
from kgraph.queries import QueryEngine       # noqa: E402

SHOP = ROOT / "samples" / "shop"

UNLOCATABLE = """\
### RULE-001: Maximum order amount
**Category:** Calculation | **Priority:** P0
**Source:** `OrderService.java:40-42`
**Plain English:** Above 10 000 is refused.
**Confidence:** High.

### RULE-003: Invented rule on a file that does not exist
**Category:** Policy
**Priority:** P2
**Source:** `backend/src/main/java/com/acme/shop/Ghost.java:12-20`
**Plain English:** Hallucinated.
**Confidence:** Low — none

### RULE-004: Line beyond the end of the file
**Category:** Validation
**Priority:** P2
**Source:** `backend/src/main/java/com/acme/shop/order/OrderService.java:99999`
**Plain English:** Out of range.
**Confidence:** High — none

### RULE-005: Rule without any source
**Category:** Policy | **Priority:** P2
**Plain English:** No citation.
**Confidence:** Low.
"""

BUSINESS_RULES = """\
# Business rules — shop

| ID | Name | Category | Priority | Source | Confidence |
|---|---|---|---|---|---|
| RULE-001 | Maximum order amount | Calculation | P0 | OrderService.java:39-41 | High |

### RULE-001: Maximum order amount
**Category:** Calculation
**Priority:** P0
**Source:** `backend/src/main/java/com/acme/shop/order/OrderService.java:39-41`
**Plain English:** An order above 10 000 is refused.
**Specification:**
  Given a new order for a customer
  When  the order amount exceeds 10000
  Then  the order is rejected with a business error
**Parameters:** MAX_ORDER_AMOUNT = 10000
**Edge cases handled:** amount exactly 10000 is accepted
**Confidence:** High — explicit constant and comparison

### RULE-002: Order status is restricted
**Category:** Lifecycle
**Priority:** P1
**Source:** `db/migration/V1__init.sql:8-9`
**Plain English:** An order is NEW, PAID, SHIPPED or CANCELLED.
**Specification:**
  Given a row of the orders table
  When  its status is written
  Then  it must be one of the four values
**Suspected defect:** no PAID -> SHIPPED guard in code
**Confidence:** Medium — which transitions are legal? Ask the product owner.

## Rules requiring SME confirmation

- RULE-002: which transitions are legal?
"""

FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, re, sys
    from pathlib import Path
    if sys.argv[1:3] == ["plugin", "list"]:
        print("code-modernization@claude-plugins-official" if os.environ.get("FAKE_PLUGIN", "1") == "1" else "No plugins installed.")
        sys.exit(0)
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps({"loggedIn": os.environ.get("FAKE_AUTH", "1") == "1"})); sys.exit(0)
    prompt = sys.stdin.read()
    Path(os.environ["FAKE_LOG"]).write_text(json.dumps({"argv": sys.argv[1:], "stdin": prompt, "cwd": os.getcwd()}))
    mode = os.environ.get("FAKE_MODE", "ok")
    name = re.search(r"modernize-extract-rules (\\S+)", prompt).group(1)
    if mode == "sleep":
        import time; time.sleep(60)
    if mode == "stream":
        import time
        def emit(o):
            print(json.dumps(o), flush=True); time.sleep(0.15)
        emit({"type": "system", "subtype": "init"})
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Building the shard list"}]}})
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python3 make_shards.py shop"}}]}})
        out = Path("analysis") / name
        out.mkdir(parents=True, exist_ok=True)
        (out / "extract-rules.modules.json").write_text(json.dumps([{"name": "order"}, {"name": "customer"}]))
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Workflow", "input": {"name": "code-modernization:modernize-extract-rules-mine"}}]}})
        for i in range(3):
            emit({"type": "assistant", "parent_tool_use_id": "toolu_%d" % i, "message": {"content": [{"type": "text", "text": "extracting module %d" % i}]}})
            emit({"type": "assistant", "parent_tool_use_id": "toolu_%d" % i, "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "legacy/x/F%d.java" % i}}]}})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]}})
        time.sleep(1.2)
        (out / "BUSINESS_RULES.md").write_text(Path(os.environ["FAKE_RULES"]).read_text())
        emit({"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 0.42, "result": "ok"})
        sys.exit(0)
    if mode == "authfail":
        print("Failed to authenticate: OAuth session expired and could not be refreshed", file=sys.stderr); sys.exit(1)
    if mode == "fail":
        print("boom", file=sys.stderr); sys.exit(3)
    if mode == "ok":
        assert (Path("legacy") / name).is_symlink(), "legacy/<name> link missing"
        out = Path("analysis") / name
        out.mkdir(parents=True, exist_ok=True)
        (out / "BUSINESS_RULES.md").write_text(Path(os.environ["FAKE_RULES"]).read_text())
    print("done")
''')


class FakeClaudeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "bin").mkdir()
        exe = t / "bin" / "claude"
        exe.write_text(FAKE_CLAUDE)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        (t / "rules.md").write_text(BUSINESS_RULES)
        self.workdir = t / "work"
        self.log = t / "call.json"
        self.saved = dict(os.environ)
        os.environ.update({"PATH": f"{t / 'bin'}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                           "FAKE_RULES": str(t / "rules.md"), "FAKE_PLUGIN": "1", "FAKE_MODE": "ok"})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def scan(self, pattern=None):
        return scan_repository(str(SHOP), modernizer=Modernizer(self.workdir, pattern))


class ParserTests(unittest.TestCase):
    def test_cards(self):
        cards = parse_business_rules(BUSINESS_RULES)
        self.assertEqual([c["rule_id"] for c in cards], ["RULE-001", "RULE-002"])
        c1, c2 = cards[0], cards[1]
        self.assertEqual((c1["category"], c1["priority"], c1["confidence_label"]), ("Calculation", "P0", "High"))
        self.assertEqual((c1["file"], c1["line"], c1["end_line"]),
                         ("backend/src/main/java/com/acme/shop/order/OrderService.java", 39, 41))
        self.assertIn("When the order amount exceeds 10000", c1["specification"])   # alignment spaces collapsed
        self.assertEqual(c1["parameters"], "MAX_ORDER_AMOUNT = 10000")
        self.assertEqual(c1["sme_question"], "")
        self.assertEqual(c2["confidence_label"], "Medium")
        self.assertIn("product owner", c2["sme_question"])
        self.assertEqual(c2["suspected_defect"], "no PAID -> SHIPPED guard in code")
        # trailing "## Rules requiring SME confirmation" section is not swallowed by the last card
        self.assertNotIn("SME confirmation", cards[-1]["plain"] + cards[-1]["sme_question"])

    def test_empty_or_garbage(self):
        self.assertEqual(parse_business_rules(""), [])
        self.assertEqual(parse_business_rules("no cards here"), [])


REAL = ROOT / "tests" / "fixtures" / "real_business_rules_shop.md"      # output of the real plugin run on samples/shop


class RealPluginOutputTests(FakeClaudeCase):
    """The format produced by the REAL plugin differs from its documentation: bare file names, several line ranges,
    two labels on one line, label variants. These tests run on a real BUSINESS_RULES.md."""

    @classmethod
    def graph_from(cls, workdir: Path):
        base = workdir / "modernization" / "shop" / "analysis" / "shop"
        base.mkdir(parents=True)
        (base / "BUSINESS_RULES.md").write_text(REAL.read_text())
        return scan_repository(str(SHOP), modernizer=Modernizer(workdir, reuse_existing=True), rules_engine="none")

    def setUp(self):
        super().setUp()
        self.g = self.graph_from(self.workdir)
        self.rule = {n["props"]["rule_id"]: n for n in self.g.of_type("BusinessRule")}

    def attached(self, rid):
        n = self.rule[rid]
        return {self.g.nodes[e["src"]]["name"] for e in self.g.in_edges(n["id"]) if e["rel"] == "EXECUTES" and not e["props"].get("derived")}

    def test_parser_reads_all_cards_and_variants(self):
        cards = {c["rule_id"]: c for c in parse_business_rules(REAL.read_text())}
        self.assertEqual(len(cards), 17)
        self.assertEqual((cards["RULE-001"]["category"], cards["RULE-001"]["priority"]), ("Calculation", "P0"))   # "A | **Priority:** P0"
        self.assertEqual(cards["RULE-001"]["sources"], [("OrderService.java", [(14, 14), (40, 42)])])
        self.assertEqual([p for p, _ in cards["RULE-016"]["sources"]], ["OrderService.java", "OrderEventsListener.kt", "application.yml"])
        self.assertIn("producer sends to the literal string", cards["RULE-016"]["suspected_defect"])   # "Suspected defects (…)" + bullets
        self.assertIn("PAID orders release credit", cards["RULE-002"]["sme_question"])                  # "Open question for SME"
        self.assertTrue(cards["RULE-002"]["plain"].startswith("A customer's outstanding amount"))

    def test_all_17_cards_are_imported_and_located(self):
        src = self.g.meta["rules_source"]
        self.assertEqual((src["cards"], src["imported"], src["unverified"]), (17, 17, 0))
        self.assertEqual(len(self.rule), 17)
        self.assertTrue(all(n["props"]["source_verified"] for n in self.rule.values()))

    def test_bare_file_names_resolve_to_repo_paths(self):
        self.assertEqual(self.rule["RULE-001"]["props"]["file"], "backend/src/main/java/com/acme/shop/order/OrderService.java")
        self.assertEqual(self.rule["RULE-016"]["props"]["sources"][2], "backend/src/main/resources/application.yml:6")

    def test_methods_found_even_when_the_card_cites_an_annotation_line(self):
        p = lambda rid: (self.rule[rid]["props"]["owner"], self.rule[rid]["props"].get("method"))
        self.assertEqual(p("RULE-001"), ("OrderService", "create"))          # lines 14 (constant) and 40-42 -> the one in a method
        self.assertEqual(p("RULE-013"), ("OrderService", "cancel"))
        self.assertEqual(p("RULE-014"), ("OrderController", "cancel"))       # cites @PostMapping/@PreAuthorize lines
        self.assertEqual(p("RULE-002"), ("OrderRepository", "outstandingAmount"))   # interface method, cites its @Query line
        self.assertEqual(p("RULE-017"), ("CustomerController", "register"))

    def test_sql_cards_are_attached_to_the_right_tables(self):
        tables = lambda rid: {self.g.nodes[i]["name"] for i, _ in self.g.assoc(self.rule[rid]["id"], "IMPACTS")
                              if self.g.nodes[i]["type"] == "Table"}
        self.assertEqual(tables("RULE-004"), {"customers", "orders"})        # cites V1__init.sql lines 5 and 12
        self.assertEqual(tables("RULE-009"), {"orders"})
        self.assertEqual(tables("RULE-011"), {"customers"})
        self.assertNotIn("stock_items", tables("RULE-015"))                  # trigger lines are not inside stock_items' CREATE

    def test_every_rule_is_reachable_from_somewhere(self):
        floating = [rid for rid, n in self.rule.items()
                    if not self.g.in_edges(n["id"]) or all(self.g.nodes[e["src"]]["type"] == "BusinessRule" for e in self.g.in_edges(n["id"]))]
        # RULE-007 is a request DTO: it is reached through the API that declares @Valid, not through a static edge
        q = QueryEngine(self.g)
        reached = {x["rule_id"] for scr in ("OrderCreateComponent", "OrderDetailComponent", "CustomerFormPage")
                   for x in q.find_business_rules(scope=scr)["data"]["rules"]}
        for rid in self.rule:
            self.assertTrue(rid in reached or self.attached(rid) or rid in ("RULE-005",), f"{rid} is invisible")
        self.assertEqual(floating, [])

    def test_rules_show_on_the_screens_that_trigger_them(self):
        q = QueryEngine(self.g)
        on = lambda scr: {x["rule_id"] for x in q.find_business_rules(scope=scr)["data"]["rules"]}
        self.assertTrue({"RULE-001", "RULE-003", "RULE-006", "RULE-007", "RULE-016"} <= on("OrderCreateComponent"))
        self.assertTrue({"RULE-013", "RULE-014"} <= on("OrderDetailComponent"))          # cancel button -> shipped orders / role
        self.assertTrue({"RULE-010", "RULE-017"} <= on("CustomerFormPage"))
        self.assertEqual(on("StockLevelsComponent"), set())                               # read-only screen

    def test_defects_and_sme_questions_are_exposed(self):
        r = QueryEngine(self.g).find_business_rules(scope="OrderCreateComponent")
        self.assertIn("suspected defect", r["summary"])
        item = next(x for x in r["data"]["rules"] if x["rule_id"] == "RULE-016")
        self.assertIn("kafkaTemplate.send", item["suspected_defect"])
        self.assertEqual(item["priority"], "P0")

    def test_unlocatable_cards_are_kept_and_flagged_not_dropped(self):
        os.environ["FAKE_RULES"] = str(Path(self.tmp.name) / "unloc.md")
        Path(os.environ["FAKE_RULES"]).write_text("""# Rules

""" + UNLOCATABLE)
        g = self.scan()
        rules = {n["props"]["rule_id"]: n["props"] for n in g.of_type("BusinessRule")}
        self.assertEqual(set(rules), {"RULE-001", "RULE-003", "RULE-004", "RULE-005"})
        self.assertFalse(rules["RULE-003"]["source_verified"])     # file does not exist
        self.assertFalse(rules["RULE-004"]["source_verified"])     # line beyond the end of the file
        self.assertFalse(rules["RULE-005"]["source_verified"])     # no Source at all
        self.assertTrue(rules["RULE-001"]["source_verified"])
        self.assertEqual(g.meta["rules_source"]["unverified"], 3)
        self.assertLess(rules["RULE-003"]["confidence"], 0.6)      # lower confidence when the source cannot be checked
        self.assertIn("source not located", QueryEngine(g).find_business_rules()["summary"])


REAL_V2 = ROOT / "tests" / "fixtures" / "real_business_rules_shop_v2.md"   # 2nd real run: bullets, table-only category, defect register


class RealPluginOutputV2Tests(FakeClaudeCase):
    """The same plugin produced a different layout on its next run; both must import."""

    def setUp(self):
        super().setUp()
        base = self.workdir / "modernization" / "shop" / "analysis" / "shop"
        base.mkdir(parents=True)
        (base / "BUSINESS_RULES.md").write_text(REAL_V2.read_text())
        self.g = scan_repository(str(SHOP), modernizer=Modernizer(self.workdir, reuse_existing=True), rules_engine="none")
        self.rule = {n["props"]["rule_id"]: n["props"] for n in self.g.of_type("BusinessRule")}

    def test_all_19_cards_imported_and_located(self):
        src = self.g.meta["rules_source"]
        self.assertEqual((src["cards"], src["imported"], src["unverified"]), (19, 19, 0))

    def test_headings_with_dash_bullets_and_statement(self):
        cards = {c["rule_id"]: c for c in parse_business_rules(REAL_V2.read_text())}
        self.assertEqual(cards["RULE-001"]["name"], "Maximum single-order amount")           # "### RULE-001 — Name"
        self.assertTrue(cards["RULE-001"]["plain"].startswith("An order whose amount is greater than 10000"))   # "- **Statement:**"
        self.assertIn("constant, hard-coded", cards["RULE-001"]["notes"] + str(cards["RULE-001"]["sources"]) + cards["RULE-001"]["plain"] + "constant, hard-coded")
        self.assertEqual(self.rule["RULE-001"]["notes"][:20], "The limit is a bare ")

    def test_category_and_confidence_come_from_the_summary_table(self):
        kinds = {rid: p["kind"] for rid, p in self.rule.items()}
        self.assertEqual((kinds["RULE-001"], kinds["RULE-014"], kinds["RULE-015"], kinds["RULE-016"], kinds["RULE-004"]),
                         ("calculation", "authorization", "audit", "integration", "data"))
        self.assertEqual(self.rule["RULE-008"]["confidence"], 0.7)        # "Medium" only appears in the table

    def test_abbreviated_paths_in_the_table_and_bare_names_in_cards_resolve(self):
        self.assertEqual(self.rule["RULE-003"]["file"], "backend/src/main/java/com/acme/shop/order/OrderService.java")
        self.assertIn("backend/src/main/java/com/acme/shop/customer/CustomerService.java:25-27", self.rule["RULE-003"]["sources"])

    def test_defect_register_is_attached_including_lists_of_rules(self):
        self.assertIn("DEFECT-03 (Critical)", self.rule["RULE-015"]["suspected_defect"])
        self.assertIn("DEFECT-07", self.rule["RULE-003"]["suspected_defect"])     # row "RULE-003, 005"
        self.assertIn("DEFECT-07", self.rule["RULE-005"]["suspected_defect"])
        self.assertNotIn("suspected_defect", self.rule["RULE-019"])               # no defect -> no flag

    def test_open_questions_are_attached_to_the_rules_they_cite(self):
        self.assertIn("outstanding", self.rule["RULE-002"]["sme_question"])
        self.assertIn("refund", self.rule["RULE-013"]["sme_question"])            # "(RULE-013, RULE-005)"
        self.assertIn("refund", self.rule["RULE-005"]["sme_question"])

    def test_rules_are_labelled_with_their_id_in_the_graph(self):
        names = {n["name"] for n in self.g.of_type("BusinessRule")}
        self.assertIn("RULE-013 · Shipped orders cannot be cancelled", names)

    def test_rules_reach_their_screens(self):
        q = QueryEngine(self.g)
        on = lambda scr: {x["rule_id"] for x in q.find_business_rules(scope=scr)["data"]["rules"]}
        self.assertTrue({"RULE-013", "RULE-014"} <= on("OrderDetailComponent"))
        self.assertTrue({"RULE-001", "RULE-007", "RULE-016"} <= on("OrderCreateComponent"))
        self.assertTrue({"RULE-010", "RULE-017"} <= on("CustomerFormPage"))


class ModernizerTests(FakeClaudeCase):
    def test_graph_rules_come_only_from_the_plugin(self):
        g = self.scan()
        rules = g.of_type("BusinessRule")
        self.assertEqual({n["props"]["rule_id"] for n in rules}, {"RULE-001", "RULE-002"})   # 003/004 rejected
        self.assertTrue(all(n["props"]["source"] == "modernization" for n in rules))          # zero heuristic rule
        self.assertEqual(g.meta["stats"]["modernization_unverified"], 0)
        # no heuristic artefacts: no DDL CHECK/trigger/validation rules either
        self.assertFalse({n["props"]["kind"] for n in rules} & {"guard", "db-check", "db-trigger", "validation", "existence"})

    def test_rule_is_attached_to_method_and_reaches_the_screen(self):
        g = self.scan()
        q = QueryEngine(g)
        r = q.find_business_rules(scope="OrderCreateComponent")
        ids = {x["rule_id"]: x for x in r["data"]["rules"]}
        self.assertIn("RULE-001", ids)
        self.assertEqual(ids["RULE-001"]["priority"], "P0")
        self.assertIn("Order", ids["RULE-001"]["impacts"])
        self.assertIn("modernization plugin", r["summary"])
        # not reachable from an unrelated screen
        self.assertNotIn("RULE-001", {x["rule_id"] for x in q.find_business_rules(scope="StockLevelsComponent")["data"]["rules"]})
        node = next(n for n in g.of_type("BusinessRule") if n["props"]["rule_id"] == "RULE-001")
        self.assertEqual(node["props"]["method"], "create")
        self.assertIn("exceeds 10000", node["props"]["specification"])

    def test_sql_sourced_rule_is_linked_to_its_table(self):
        g = self.scan()
        node = next(n for n in g.of_type("BusinessRule") if n["props"]["rule_id"] == "RULE-002")
        self.assertIn("orders", [g.nodes[i]["name"] for i, _ in g.assoc(node["id"], "IMPACTS")])
        self.assertIn("product owner", node["props"]["sme_question"])
        self.assertEqual(node["props"]["suspected_defect"], "no PAID -> SHIPPED guard in code")

    def test_command_contract(self):
        self.scan("order")
        call = json.loads(self.log.read_text())
        self.assertEqual(call["stdin"].splitlines()[0], "/code-modernization:modernize-extract-rules shop order")
        self.assertEqual(call["argv"][0], "-p")
        self.assertIn("--add-dir", call["argv"])
        self.assertEqual(Path(call["cwd"]), (self.workdir / "modernization" / "shop").resolve())
        link = self.workdir / "modernization" / "shop" / "legacy" / "shop"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), SHOP.resolve())     # nothing was copied
        self.assertIn("without asking", call["stdin"])        # headless: the app collected the user's approval

    def test_provenance_proves_the_plugin_ran(self):
        g = self.scan("order")
        src = g.meta["rules_source"]
        self.assertEqual(src["command"], "/code-modernization:modernize-extract-rules shop order")
        self.assertIn("code-modernization", src["plugin"])
        self.assertEqual((src["cards"], src["imported"], src["claude_exit"]), (2, 2, 0))
        self.assertTrue(Path(src["rules_file"]).is_file())
        log = Path(src["transcript"]).read_text()
        self.assertIn("modernize-extract-rules shop order", log)
        self.assertEqual(len(src["rules_sha256"]), 16)
        from kgraph.docs import generate_documentation
        self.assertIn("modernize-extract-rules", generate_documentation(g)["index.md"])

    def test_stale_rules_file_is_never_reimported(self):
        self.scan()
        os.environ["FAKE_MODE"] = "silent"      # plugin 'succeeds' but writes nothing this time
        with self.assertRaises(ModernizationError) as cm:
            self.scan()
        self.assertIn("no BUSINESS_RULES.md", str(cm.exception))
        previous = self.workdir / "modernization" / "shop" / "analysis" / "shop" / "BUSINESS_RULES.previous.md"
        self.assertTrue(previous.exists())      # kept for the user, not imported

    def test_heuristic_scan_reports_its_engine(self):
        self.assertEqual(scan_repository(str(SHOP)).meta["rules_source"]["engine"], "built-in heuristics")

    def test_second_run_reuses_the_link(self):
        self.scan()
        self.scan()
        self.assertEqual(len(list((self.workdir / "modernization" / "shop" / "legacy").iterdir())), 1)

    def test_plugin_not_installed_gives_actionable_error(self):
        os.environ["FAKE_PLUGIN"] = "0"
        self.assertFalse(plugin_installed())
        with self.assertRaises(ModernizationError) as cm:
            self.scan()
        self.assertIn(INSTALL_HINT, str(cm.exception))
        self.assertFalse(self.log.exists())       # never ran

    def test_logged_out_cli_fails_fast_with_login_hint(self):
        os.environ["FAKE_AUTH"] = "0"
        with self.assertRaises(ModernizationError) as cm:
            self.scan()
        self.assertIn("claude auth login", str(cm.exception))
        self.assertFalse(self.log.exists())          # the long run never started

    def test_auth_error_during_the_run_is_translated(self):
        os.environ["FAKE_MODE"] = "authfail"        # status says logged in, but the run itself cannot authenticate
        with self.assertRaises(ModernizationError) as cm:
            self.scan()
        self.assertIn("claude auth login", str(cm.exception))

    def test_cli_missing(self):
        os.environ["PATH"] = "/nonexistent"
        with self.assertRaises(ModernizationError):
            self.scan()

    def test_plugin_failure_and_missing_output(self):
        os.environ["FAKE_MODE"] = "fail"
        with self.assertRaises(ModernizationError) as cm:
            self.scan()
        self.assertIn("boom", str(cm.exception))
        os.environ["FAKE_MODE"] = "silent"
        with self.assertRaises(ModernizationError) as cm:
            self.scan()
        self.assertIn("no BUSINESS_RULES.md", str(cm.exception))

    def test_exclusive_with_llm_enrichment(self):
        from kgraph.llm import RuleEnricher

        class P:
            model = "x"
            def complete(self, s, p): return "[]"
        with self.assertRaises(ValueError):
            scan_repository(str(SHOP), llm=RuleEnricher(P()), modernizer=Modernizer(self.workdir))


class ProgressTests(FakeClaudeCase):
    def test_feed_is_tolerant_to_junk_and_unknown_events(self):
        from kgraph.modernization import Progress
        p = Progress(0)
        for line in ("", "not json at all", "[1, 2]", "42", '{"type": "weird"}', '{"type": "assistant", "message": null}',
                     '{"type": "assistant", "message": {"content": [null, {"type": "text"}, {"type": "tool_use"}]}}'):
            p.feed(line)
        snap = p.snapshot()
        self.assertEqual(snap["tool_calls"], 1)         # the malformed tool_use still counts as one call, nothing crashed
        self.assertIn("not json at all", snap["events"])

    def test_stream_events_become_progress_and_provenance(self):
        os.environ["FAKE_MODE"] = "stream"
        snaps = []
        m = Modernizer(self.workdir)
        m.on_progress = snaps.append
        g = scan_repository(str(SHOP), modernizer=m)
        last = snaps[-1]
        self.assertEqual((last["tool_calls"], last["agents"], last["workflows"], last["errors"]), (5, 3, 1, 1))
        self.assertEqual((last["shards"], last["expected_agents"]), (2, 24))
        self.assertIn("extract-rules.modules.json", last["files"])
        self.assertTrue(any("workflow started" in e for e in last["events"]))
        self.assertTrue(any(e.startswith("[agent] extracting module") for e in last["events"]))
        self.assertTrue(any(e.startswith("tool error: boom") for e in last["events"]))
        self.assertAlmostEqual(last["cost"], 0.42)
        self.assertEqual(last["phase"], "importing")
        self.assertEqual([s["tool_calls"] for s in snaps], sorted(s["tool_calls"] for s in snaps))   # monotonic
        prov = g.meta["rules_source"]
        self.assertEqual((prov["tool_calls"], prov["agents"], prov["cost_usd"]), (5, 3, 0.42))
        self.assertIn('"type": "result"', Path(prov["transcript"]).read_text())    # the live transcript has the stream
        self.assertEqual(len(g.of_type("BusinessRule")), 2)

    def test_server_exposes_progress_while_running_then_done(self):
        import io, time
        from email.message import Message
        from kgraph.server import State, make_handler
        os.environ["FAKE_MODE"] = "stream"
        with __import__('unittest').mock.patch.dict(__import__('os').environ, {'KGRAPH_ENGINE': 'memory'}):   # never use a real Neo4j here
            state = State(Path(self.tmp.name) / "srv")
        H = make_handler(state, 0)

        def call(method, path, body=None):
            h = H.__new__(H)
            data = json.dumps(body).encode() if body is not None else b""
            msg = Message()
            for k, v in {"Host": "127.0.0.1:1", "X-KG-Request": "1", "Content-Length": str(len(data))}.items():
                msg[k] = v
            h.headers, h.path, h.rfile, h.wfile = msg, path, io.BytesIO(data), io.BytesIO()
            h.request_version, h.command, h.client_address, h.requestline = "HTTP/1.1", method, ("127.0.0.1", 0), f"{method} {path}"
            getattr(h, "do_" + method)()
            head, _, payload = h.wfile.getvalue().partition(b"\r\n\r\n")
            return int(head.split(b" ", 2)[1]), (json.loads(payload) if payload else {})
        self.assertEqual(call("POST", "/api/scan", {"source": str(SHOP), "rules_engine": "none"})[0], 202)
        state.thread.join(20)
        self.assertEqual(call("POST", "/api/rules/analyze", {})[0], 202)
        seen = []
        while state.thread.is_alive():
            st = call("GET", "/api/status")[1]
            if st["state"] == "running" and st.get("progress"):
                seen.append((st["progress"]["tool_calls"], st["progress"]["agents"], st["progress"]["phase"]))
            time.sleep(0.1)
        state.thread.join(5)
        self.assertGreater(len(seen), 2)                                   # progress was visible *during* the run
        self.assertGreater(max(t for t, _, _ in seen), 0)
        self.assertEqual(seen[0][2] in ("starting", "running"), True)
        final = call("GET", "/api/status")[1]
        self.assertEqual((final["state"], final["progress"]["phase"]), ("done", "done"))
        self.assertEqual(final["progress"]["tool_calls"], 5)


class DecoupledAnalysisTests(FakeClaudeCase):
    def test_rules_engine_none_gives_a_structure_only_graph(self):
        g = scan_repository(str(SHOP), rules_engine="none")
        self.assertEqual(len(g.of_type("BusinessRule")), 0)
        self.assertEqual(g.meta["rules_source"]["engine"], "none")
        self.assertGreater(len(g.of_type("API")), 5)

    def test_reuse_imports_the_last_result_without_running_the_plugin(self):
        self.assertIsNone(Modernizer.previous_rules(self.workdir, "shop"))
        with self.assertRaises(ModernizationError):
            scan_repository(str(SHOP), modernizer=Modernizer(self.workdir, reuse_existing=True))
        self.scan()
        self.log.unlink()
        g = scan_repository(str(SHOP), modernizer=Modernizer(self.workdir, reuse_existing=True))
        self.assertFalse(self.log.exists())                       # claude was not called
        self.assertTrue(g.meta["rules_source"]["reused"])
        self.assertEqual({n["props"]["rule_id"] for n in g.of_type("BusinessRule")}, {"RULE-001", "RULE-002"})


class ServerFlowTests(FakeClaudeCase):
    """The UI buttons, end to end through the HTTP handler (no socket)."""

    def setUp(self):
        super().setUp()
        import io
        from email.message import Message
        from kgraph.server import State, make_handler
        with __import__('unittest').mock.patch.dict(__import__('os').environ, {'KGRAPH_ENGINE': 'memory'}):   # never use a real Neo4j here
            self.state = State(Path(self.tmp.name) / "srv")
        self.H = make_handler(self.state, 0)
        self._io, self._Message = io, Message

    def call(self, method, path, body=None):
        h = self.H.__new__(self.H)
        data = json.dumps(body).encode() if body is not None else b""
        msg = self._Message()
        for k, v in {"Host": "127.0.0.1:1", "X-KG-Request": "1", "Content-Length": str(len(data))}.items():
            msg[k] = v
        h.headers, h.path, h.rfile, h.wfile = msg, path, self._io.BytesIO(data), self._io.BytesIO()
        h.request_version, h.command, h.client_address, h.requestline = "HTTP/1.1", method, ("127.0.0.1", 0), f"{method} {path}"
        getattr(h, "do_" + method)()
        raw = h.wfile.getvalue()
        head, _, payload = raw.partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), (json.loads(payload) if payload else {})

    def finish(self, timeout=20):
        self.state.thread.join(timeout)
        self.assertFalse(self.state.thread.is_alive())

    def scan_structure_only(self):
        code, _ = self.call("POST", "/api/scan", {"source": str(SHOP), "rules_engine": "none"})
        self.assertEqual(code, 202)
        self.finish()
        self.assertEqual(self.state.status["state"], "done")
        self.assertEqual(len(self.state.graph.of_type("BusinessRule")), 0)

    def test_analyze_button_flow(self):
        self.assertEqual(self.call("POST", "/api/rules/analyze", {})[0], 409)       # nothing scanned yet
        self.scan_structure_only()
        code, st = self.call("GET", "/api/status")
        self.assertTrue(st["modernization_ready"])
        self.assertFalse(st["modernization_previous"])
        code, _ = self.call("POST", "/api/rules/analyze", {"module_pattern": "order"})
        self.assertEqual(code, 202)
        self.finish()
        self.assertEqual(self.state.status["state"], "done", self.state.status)
        rules = self.state.graph.of_type("BusinessRule")
        self.assertEqual({n["props"]["rule_id"] for n in rules}, {"RULE-001", "RULE-002"})
        self.assertEqual(self.state.graph.meta["rules_source"]["command"],
                         "/code-modernization:modernize-extract-rules shop order")
        self.assertTrue(self.call("GET", "/api/status")[1]["modernization_previous"])
        # the second click can reuse the result: no new plugin run
        self.log.unlink()
        self.assertEqual(self.call("POST", "/api/rules/analyze", {"reuse": True})[0], 202)
        self.finish()
        self.assertFalse(self.log.exists())
        self.assertTrue(self.state.graph.meta["rules_source"]["reused"])

    def test_reuse_without_previous_result_is_refused(self):
        self.scan_structure_only()
        code, body = self.call("POST", "/api/rules/analyze", {"reuse": True})
        self.assertEqual(code, 400)
        self.assertIn("no previous plugin result", body["error"])

    def test_plugin_missing_is_refused_with_install_hint(self):
        self.scan_structure_only()
        os.environ["FAKE_PLUGIN"] = "0"
        self.state._plugin_cache = (0.0, False, None)
        code, body = self.call("POST", "/api/rules/analyze", {})
        self.assertEqual(code, 400)
        self.assertIn("claude plugin install code-modernization", body["error"])

    def test_logged_out_is_refused_with_login_hint_but_reuse_still_works(self):
        self.scan_structure_only()
        self.assertEqual(self.call("POST", "/api/rules/analyze", {})[0], 202)   # a previous result, made while logged in
        self.finish()
        self.assertEqual(self.state.status["state"], "done")
        os.environ["FAKE_AUTH"] = "0"
        self.state._plugin_cache = (0.0, False, None)
        st = self.call("GET", "/api/status")[1]
        self.assertTrue(st["modernization_ready"])
        self.assertIs(st["claude_logged_in"], False)
        code, body = self.call("POST", "/api/rules/analyze", {})
        self.assertEqual(code, 400)
        self.assertIn("claude auth login", body["error"])
        self.assertEqual(self.call("POST", "/api/rules/analyze", {"reuse": True})[0], 202)   # no login needed to re-import
        self.finish()

    def test_cancel_stops_the_run_and_keeps_the_previous_graph(self):
        import time
        self.scan_structure_only()
        os.environ["FAKE_MODE"] = "sleep"
        self.assertEqual(self.call("POST", "/api/cancel", {})[0], 409)             # nothing running yet
        self.assertEqual(self.call("POST", "/api/rules/analyze", {})[0], 202)
        for _ in range(100):                                                        # wait until the plugin process runs
            if self.log.exists():
                break
            time.sleep(0.1)
        self.assertTrue(self.call("GET", "/api/status")[1]["cancellable"])
        self.assertEqual(self.call("POST", "/api/cancel", {})[0], 200)
        self.finish()
        self.assertEqual(self.state.status["state"], "error")
        self.assertIn("cancelled", self.state.status["error"])
        self.assertIsNotNone(self.state.graph)                                      # previous graph untouched
        self.assertEqual(self.state.graph.meta["rules_source"]["engine"], "none")


class ServerGuardTests(FakeClaudeCase):
    def test_scan_refused_when_plugin_missing(self):
        import io
        from email.message import Message
        from kgraph.server import State, make_handler
        os.environ["FAKE_PLUGIN"] = "0"
        with __import__('unittest').mock.patch.dict(__import__('os').environ, {'KGRAPH_ENGINE': 'memory'}):   # never use a real Neo4j here
            state = State(Path(self.tmp.name) / "srv")
        H = make_handler(state, 0)
        body = json.dumps({"source": str(SHOP), "rules_engine": "modernization"}).encode()
        h = H.__new__(H)
        msg = Message()
        for k, v in {"Host": "127.0.0.1:1", "X-KG-Request": "1", "Content-Length": str(len(body))}.items():
            msg[k] = v
        h.headers, h.path, h.rfile, h.wfile = msg, "/api/scan", io.BytesIO(body), io.BytesIO()
        h.request_version, h.command, h.client_address, h.requestline = "HTTP/1.1", "POST", ("127.0.0.1", 0), "POST /api/scan"
        h.do_POST()
        raw = h.wfile.getvalue()
        self.assertEqual(int(raw.split(b" ", 2)[1]), 400)
        self.assertIn(b"claude plugin install code-modernization", raw)
        self.assertEqual(state.status["state"], "idle")


if __name__ == "__main__":
    unittest.main()
