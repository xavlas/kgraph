"""Generalisation tests: code styles that the bundled sample does not use."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgraph.pipeline import scan_repository  # noqa: E402
from kgraph.queries import QueryEngine       # noqa: E402

FILES = {
    "api/src/main/java/demo/ClaimController.java": '''
package demo;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(path = "/v1/claims")
public class ClaimController {
    @Autowired
    private ClaimService claimService;

    @RequestMapping(value = "/{id}", method = RequestMethod.GET)
    public Object get(@PathVariable Long id) { return claimService.find(id); }

    @PutMapping(value = {"/{id}/approve", "/{id}/validate"})
    public void approve(@PathVariable Long id) { claimService.approve(id); }
}
''',
    "api/src/main/java/demo/ClaimService.java": '''
package demo;
public interface ClaimService { Object find(Long id); void approve(Long id); }
''',
    "api/src/main/java/demo/ClaimServiceImpl.java": '''
package demo;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class ClaimServiceImpl implements ClaimService {
    private final ClaimDao claimDao;
    private final JdbcTemplate jdbcTemplate;

    public Object find(Long id) { return claimDao.load(id); }

    public void approve(Long id) {
        Object c = find(id);
        double amount = 0;
        if (amount > 5000) {
            throw new IllegalStateException("Manual review required above 5000");
        }
        jdbcTemplate.update("UPDATE claims SET status = 'APPROVED' WHERE id = ?", id);
    }
}
''',
    "api/src/main/java/demo/ClaimDao.java": '''
package demo;
import org.springframework.stereotype.Repository;

@Repository
public class ClaimDao {
    private JdbcTemplate jdbc;
    public Object load(Long id) {
        return jdbc.queryForObject("SELECT * FROM claims " +
                                   "WHERE id = ?", Object.class, id);
    }
}
''',
    "api/src/main/kotlin/demo/PolicyController.kt": '''
package demo
import org.springframework.web.bind.annotation.*

@RestController
@RequestMapping("/v1/policies")
class PolicyController(private val svc: PolicyService) {
    @GetMapping("/{id}") fun get(@PathVariable id: Long) = svc.find(id)
}
''',
    "api/src/main/kotlin/demo/PolicyService.kt": '''
package demo
import org.springframework.stereotype.Service

@Service
class PolicyService {
    fun find(id: Long): String = if (id > 1000) throw IllegalArgumentException("Legacy policy ids are not supported") else "p"
}
''',
    "db/schema.sql": '''
CREATE TABLE claims (
  id     NUMBER(19) PRIMARY KEY,
  status VARCHAR2(20),
  amount NUMBER(12,2) CHECK (amount >= 0)
);
''',
    "web/src/api.js": '''
import axios from 'axios';
const client = axios.create({ baseURL: '/v1' });
export const getClaim = id => client.get(`/claims/${id}`);
export function approveClaim(id) { return client.put(`/claims/${id}/approve`); }
''',
    "web/src/ClaimPage.jsx": '''
import { getClaim, approveClaim } from './api';
export default function ClaimPage() {
  const load = () => getClaim(1);
  return <button onClick={() => approveClaim(1)}>Approve</button>;
}
''',
    "web/src/router.jsx": '''
import { createBrowserRouter } from 'react-router-dom';
import ClaimPage from './ClaimPage';
export const router = createBrowserRouter([
  { path: '/', element: <Layout />, children: [ { path: 'claims/:id', element: <ClaimPage /> } ] },
]);
function Layout() { return <div/>; }
''',
}


class VariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for rel, text in FILES.items():
            p = Path(cls.tmp.name, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        cls.g = scan_repository(cls.tmp.name)
        cls.q = QueryEngine(cls.g)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def names(self, t):
        return {n["name"] for n in self.g.of_type(t)}

    def test_endpoints_with_requestmapping_style_and_multiple_paths(self):
        apis = self.names("API")
        for a in ("GET /v1/claims/{id}", "PUT /v1/claims/{id}/approve", "PUT /v1/claims/{id}/validate", "GET /v1/policies/{id}"):
            self.assertIn(a, apis)

    def test_interface_resolves_to_implementation(self):
        self.assertIn("ClaimServiceImpl", self.names("Service"))
        api = next(n for n in self.g.of_type("API") if n["name"] == "PUT /v1/claims/{id}/approve")
        callees = {self.g.nodes[e["dst"]]["name"] for e in self.g.out_edges(api["id"]) if e["rel"] == "CALLS"}
        self.assertEqual(callees, {"ClaimServiceImpl"})

    def test_jdbc_sql_strings_give_table_access_and_dao_is_repository(self):
        self.assertIn("ClaimDao", self.names("Repository"))
        lin = self.q.find_data_lineage("claims")
        modes = {(w["api"], w["mode"]) for w in lin["data"]["writers"] + lin["data"]["readers"]}
        self.assertIn(("PUT /v1/claims/{id}/approve", "W"), modes)
        self.assertIn(("GET /v1/claims/{id}", "R"), modes)

    def test_oracle_table_and_check_constraint(self):
        t = next(n for n in self.g.of_type("Table") if n["name"] == "claims")
        self.assertEqual(t["props"]["dialect"], "oracle")
        self.assertTrue(any(r["props"]["kind"] == "db-check" for r in self.g.of_type("BusinessRule")))

    def test_guard_rules_in_java_and_kotlin_expression_body(self):
        descs = {n["props"]["description"] for n in self.g.of_type("BusinessRule")}
        self.assertIn("Legacy policy ids are not supported", descs)

    def test_axios_instance_resolves_against_base_path_by_suffix(self):
        self.assertFalse([n for n in self.g.of_type("API") if n["props"].get("unresolved")],
                         [n["name"] for n in self.g.of_type("API") if n["props"].get("unresolved")])
        r = self.q.explain_screen("ClaimPage")
        self.assertEqual({a["api"]["name"] for a in r["data"]["apis"]},
                         {"GET /v1/claims/{id}", "PUT /v1/claims/{id}/approve"})

    def test_object_style_router_with_layout_prefix(self):
        self.assertIn("/claims/:id", self.names("Route"))
        r = self.q.explain_screen("/claims/:id")
        self.assertEqual(r["target"]["name"], "ClaimPage")


if __name__ == "__main__":
    unittest.main()
