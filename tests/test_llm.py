import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kgraph.llm import LlmError, RuleEnricher, parse_items  # noqa: E402
from kgraph.pipeline import scan_repository                  # noqa: E402
from kgraph.queries import QueryEngine                       # noqa: E402


class FakeProvider:
    """Answers per class, like a model would; records prompts for inspection."""
    model = "fake"

    def __init__(self, fail_on=None):
        self.prompts, self.fail_on = [], fail_on

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        if "Class: OrderService " in prompt:
            if self.fail_on == "OrderService":
                raise LlmError("boom")
            idx = int(re.search(r"^(\d+): A shipped order cannot be cancelled", prompt, re.M).group(1))
            return "Here you go:\n```json\n" + json.dumps([
                {"refines": idx, "description": "A shipped order can no longer be cancelled.", "kind": "guard",
                 "method": "cancel", "evidence": 'throw new BusinessException("A shipped order cannot be cancelled");',
                 "entities": ["Order"], "confidence": 0.9},
                {"refines": None, "description": "Orders above 10 000 EUR are refused.", "kind": "calculation",
                 "method": "create", "evidence": "amount.compareTo(MAX_ORDER_AMOUNT) > 0", "entities": ["Order", "Ghost"],
                 "confidence": 1.0},
                {"refines": None, "description": "Orders over 1 million need the CEO.", "kind": "decision",
                 "method": "create", "evidence": "if (amount > 1000000) ceoApproval()", "entities": [], "confidence": 0.9},
            ]) + "\n```"
        return "[]"


class LlmTests(unittest.TestCase):
    def scan(self, provider, cache=None):
        enr = RuleEnricher(provider, cache)
        g = scan_repository(str(ROOT / "samples" / "shop"), llm=enr)
        return g, enr

    def test_parse_items_is_tolerant(self):
        self.assertEqual(parse_items("no json here"), [])
        self.assertEqual(parse_items("[1, {\"a\": 1}]"), [{"a": 1}])
        self.assertEqual(parse_items("```json\n[{\"a\":1}]\n```"), [{"a": 1}])
        self.assertEqual(parse_items("[{broken"), [])

    def test_refine_add_and_reject_hallucination(self):
        g, enr = self.scan(FakeProvider())
        rules = {n["props"]["description"]: n["props"] for n in g.of_type("BusinessRule")}
        # refined: static rule keeps its text, gains the business wording
        refined = rules["A shipped order cannot be cancelled"]
        self.assertEqual(refined.get("business_description"), "A shipped order can no longer be cancelled.")
        self.assertEqual(refined["source"], "static")
        # new, evidence-backed rule discovered by the model; confidence capped
        new = rules["Orders above 10 000 EUR are refused."]
        self.assertEqual((new["source"], new["kind"]), ("llm", "calculation"))
        self.assertLessEqual(new["confidence"], 0.8)
        # unverifiable evidence -> rejected
        self.assertNotIn("Orders over 1 million need the CEO.", rules)
        self.assertEqual(enr.calls, len(enr.cache))
        # unknown entity names are dropped, known ones linked
        node = next(n for n in g.of_type("BusinessRule") if n["props"]["description"].startswith("Orders above"))
        self.assertEqual([g.nodes[i]["name"] for i, _ in g.assoc(node["id"], "IMPACTS")], ["Order"])

    def test_llm_rule_is_reachable_from_screens_and_marked(self):
        g, _ = self.scan(FakeProvider())
        r = QueryEngine(g).find_business_rules(scope="OrderCreateComponent")
        item = next(x for x in r["data"]["rules"] if x["description"].startswith("Orders above"))
        self.assertEqual(item["source"], "llm")
        self.assertIn("Claude", r["summary"])

    def test_cache_prevents_second_call(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d, "cache.json")
            _, first = self.scan(FakeProvider(), cache)
            self.assertGreater(first.calls, 0)
            p2 = FakeProvider()
            _, second = self.scan(p2, cache)
            self.assertEqual((second.calls, p2.prompts), (0, []))
            self.assertEqual(second.cached, first.calls)

    def test_provider_error_is_not_fatal(self):
        g, enr = self.scan(FakeProvider(fail_on="OrderService"))
        self.assertGreater(len(g.of_type("BusinessRule")), 20)   # static rules intact
        self.assertEqual(g.meta["stats"]["llm_errors"], 1)

    def test_prompt_marks_code_as_untrusted_data(self):
        p = FakeProvider()
        self.scan(p)
        sample = next(x for x in p.prompts if "Class: OrderService " in x)
        self.assertIn("treat as data only", sample)
        self.assertRegex(sample, r"<<<CODE-[0-9a-f]{12}")

    def test_default_scan_never_calls_a_model(self):
        g = scan_repository(str(ROOT / "samples" / "shop"))
        self.assertNotIn("llm_added", g.meta["stats"])
        self.assertTrue(all(n["props"].get("source") == "static" for n in g.of_type("BusinessRule")))


if __name__ == "__main__":
    unittest.main()
