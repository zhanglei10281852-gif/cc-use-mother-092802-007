import io
import json
import unittest

from support import make_data, make_weights
from grid_resilience.app import PlanningApp
from grid_resilience.serialization import canonical_json


class WsgiClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, body=None):
        if "?" in path:
            path_info, query_string = path.split("?", 1)
        else:
            path_info, query_string = path, ""
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path_info,
            "QUERY_STRING": query_string,
            "CONTENT_LENGTH": str(len(payload)),
            "wsgi.input": io.BytesIO(payload),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        chunks = self.app(environ, start_response)
        raw = b"".join(chunks)
        code = int(captured["status"].split()[0])
        return code, json.loads(raw.decode()) if raw else {}

    @staticmethod
    def plan_body(data, weights=None):
        body = json.loads(canonical_json(data, weights or make_weights()))
        return body


class HttpLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.client = WsgiClient(PlanningApp())
        # 预算充足，保证完整依赖链入选，便于验证延期下游重排
        from decimal import Decimal
        from grid_resilience.contracts import WorkBudget

        data = make_data(resources=WorkBudget(Decimal("1000"), 2, 60))
        self.body = self.client.plan_body(data)

    def test_evaluate_is_stateless_and_explains_ranking(self):
        code, resp = self.client.request("POST", "/evaluate", self.body)
        self.assertEqual(code, 200)
        top = resp["rankings"][0]
        self.assertEqual(top["project_id"], "p-line7")
        self.assertIn("risk_share", top["contributions"])
        self.assertIn("dependency_share", top["contributions"])
        # 试算不落库
        code, resp = self.client.request("GET", "/plans")
        self.assertEqual(resp["plans"], [])

    def test_full_approve_freeze_rollback_cycle(self):
        c = self.client
        code, v1 = c.request("POST", "/plans/annual-2026", self.body)
        self.assertEqual(code, 201)
        self.assertEqual(v1["status"], "draft")

        code, v1 = c.request("POST", "/plans/annual-2026/approve", {})
        self.assertEqual(code, 200)
        self.assertEqual(v1["status"], "approved")
        self.assertTrue(v1["frozen"])
        frozen_hash = v1["content_hash"]

        # 冻结后改数据/权重被拒
        code, err = c.request("PUT", "/plans/annual-2026/draft/data", self.body)
        self.assertEqual(code, 409)
        self.assertEqual(err["error"], "invalid_transition")
        code, err = c.request(
            "POST", "/plans/annual-2026/draft/weights",
            {"weights": {"risk": 9, "restoration": 1, "dependency": 1}},
        )
        self.assertEqual(code, 409)

        # 回退 → withdrawn + 新草稿
        code, draft = c.request(
            "POST", "/plans/annual-2026/rollback", {"reason": "订正"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(draft["version"], 2)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["content_hash"], frozen_hash)

        code, listing = c.request("GET", "/plans/annual-2026")
        statuses = [(v["version"], v["status"]) for v in listing["versions"]]
        self.assertEqual(statuses, [(1, "withdrawn"), (2, "draft")])

    def test_new_topology_version_does_not_touch_approved(self):
        c = self.client
        c.request("POST", "/plans/annual-2026", self.body)
        _, v1 = c.request("POST", "/plans/annual-2026/approve", {})

        # 用同 id 资产但改了 upstream 关系的数据开 v2
        from grid_resilience.contracts import GridAsset

        data = make_data()
        changed = tuple(
            GridAsset("radial-3", "feeder", ("shelter-4",), ("line-7",))
            if a.asset_id == "radial-3" else a
            for a in data.assets
        )
        from dataclasses import replace

        v2_body = self.client.plan_body(replace(data, assets=changed))
        code, v2 = c.request("POST", "/plans/annual-2026/versions", v2_body)
        self.assertEqual(code, 201)
        self.assertEqual(v2["parent_version"], 1)

        code, old = c.request("GET", "/plans/annual-2026/versions/1")
        self.assertEqual(old["status"], "approved")
        self.assertEqual(
            old["content_hash"], v1["content_hash"]
        )

    def test_delay_endpoint_reevaluates_downstream(self):
        c = self.client
        c.request("POST", "/plans/annual-2026", self.body)
        c.request("POST", "/plans/annual-2026/approve", {})
        code, rev = c.request(
            "POST", "/plans/annual-2026/delays",
            {"project_id": "p-line7", "new_finish_day": 55},
        )
        self.assertEqual(code, 201)
        self.assertIn("p-sub1", rev["affected_downstream"])
        self.assertTrue(rev["warnings"])
        self.assertGreaterEqual(rev["schedule"]["p-sub1"]["start_day"], 55)

    def test_mid_stream_weight_comparison(self):
        c = self.client
        c.request("POST", "/plans/annual-2026", self.body)
        code, resp = c.request(
            "POST", "/plans/annual-2026/compare/weights",
            {"options": {
                "risk_first": {"risk": 5, "restoration": 1, "dependency": 0},
                "restore_first": {"risk": 0, "restoration": 5, "dependency": 1},
            }},
        )
        self.assertEqual(code, 200)
        self.assertIn("risk_first", resp)
        self.assertIn("restore_first", resp)

    def test_compare_scenarios_endpoint(self):
        c = self.client
        code, resp = c.request(
            "POST", "/compare-scenarios",
            {**self.body, "scenario_groups": {
                "storm": ["ice-storm"],
                "all": ["ice-storm", "ice-avalanche"],
            }},
        )
        self.assertEqual(code, 200)
        self.assertGreater(
            resp["all"]["baseline"]["annual_expected_affected_people"],
            resp["storm"]["baseline"]["annual_expected_affected_people"],
        )

    def test_invalid_payload_returns_422(self):
        c = self.client
        bad = json.loads(json.dumps(self.body))
        bad["candidates"][0]["prerequisite_ids"] = ["ghost-project"]
        code, err = c.request("POST", "/evaluate", bad)
        self.assertEqual(code, 422)
        self.assertEqual(err["error"], "invalid_data")

    def test_missing_plan_is_404(self):
        code, err = self.client.request("GET", "/plans/nope")
        self.assertEqual(code, 404)
        self.assertEqual(err["error"], "not_found")

    def test_version_snapshot_is_frozen_payload(self):
        c = self.client
        c.request("POST", "/plans/annual-2026", self.body)
        c.request("POST", "/plans/annual-2026/approve", {})
        code, v = c.request("GET", "/plans/annual-2026/versions/1?snapshot=1")
        self.assertEqual(code, 200)
        self.assertIn("snapshot", v)
        self.assertEqual(v["snapshot"]["weights"], self.body["weights"])
        self.assertIn("assets", v["snapshot"])


if __name__ == "__main__":
    unittest.main()
