import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import fixtures
from grid_resilience.service import ResilienceService, create_server


def _payload(**overrides):
    data = {
        "assets": [
            {
                "asset_id": a.asset_id,
                "asset_kind": a.asset_kind,
                "serves": list(a.serves),
                "upstream_asset_ids": list(a.upstream_asset_ids),
            }
            for a in fixtures.ASSETS
        ],
        "service_points": [
            {"point_id": p.point_id, "name": p.name, "population": p.population}
            for p in fixtures.SERVICE_POINTS
        ],
        "scenarios": [
            {
                "scenario_id": fixtures.ICEFALL.scenario_id,
                "name": fixtures.ICEFALL.name,
                "asset_fail_prob": {k: str(v) for k, v in fixtures.ICEFALL.asset_fail_prob.items()},
                "repair_time_days": {k: str(v) for k, v in fixtures.ICEFALL.repair_time_days.items()},
            }
        ],
        "candidates": [
            {
                "project_id": c.project_id,
                "asset_id": c.asset_id,
                "cost": str(c.cost),
                "crew_days": c.crew_days,
                "prerequisite_ids": list(c.prerequisite_ids),
                "residual_fail_fraction": str(c.residual_fail_fraction),
                "residual_repair_fraction": str(c.residual_repair_fraction),
            }
            for c in fixtures.candidates()
        ],
        "weights": {"population": "1.0", "restoration": "0.5", "cascade": "0.2"},
        "resources": {"budget": "130", "crew_days": 25},
    }
    data.update(overrides)
    return data


class HttpServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server(ResilienceService(), port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def _request(self, method, path, payload=None):
        raw = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=raw,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_submit_approve_freeze_and_get_version(self):
        status, created = self._request("POST", "/versions", _payload(note="v1"))
        self.assertEqual(status, 201)
        vid = created["version_id"]
        self.assertEqual(created["state"], "draft")
        self.assertEqual(created["scores"][0]["project_id"], "p-sub1")
        self.assertTrue(created["scores"][0]["explanation"])
        self.assertTrue(any(b["project_id"] == "p-line7" for b in created["plan"]["blocked"]))

        status, approved = self._request("POST", f"/versions/{vid}/approve")
        self.assertEqual(status, 200)
        self.assertEqual(approved["state"], "approved")

        status, fetched = self._request("GET", f"/versions/{vid}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["content_hash"], approved["content_hash"])

        # 重复提交同样输入：返回同一版本，保证可重复。
        _, again = self._request("POST", "/versions", _payload(note="v1"))
        self.assertEqual(again["version_id"], vid)

    def test_compare_changes_weights_without_creating_version(self):
        before = self._request("POST", "/compare", _payload())[1]
        tweaked = self._request(
            "POST", "/compare", _payload(weights={"population": "0", "restoration": "0", "cascade": "1"})
        )[1]
        self.assertEqual(before["scores"][0]["project_id"], "p-sub1")
        self.assertEqual(tweaked["scores"][0]["project_id"], "p-line7")
        self.assertNotEqual(before["content_hash"], tweaked["content_hash"])
        # /compare 不产生版本。
        self.assertEqual(self._request("GET", "/versions")[1]["versions"], [])

    def test_replan_after_delay_and_rollback_over_http(self):
        v1 = self._request("POST", "/versions", _payload(note="1"))[1]
        self._request("POST", f"/versions/{v1['version_id']}/approve")

        delayed = _payload(note="延期重排", resources={"budget": "1000", "crew_days": 1000}, progress=[
            {"project_id": "p-sub1", "state": "in_progress", "delay_days": 15}
        ])
        status, v2 = self._request("POST", "/replan", delayed)
        self.assertEqual(status, 201)
        self.assertEqual(v2["parent_version_id"], v1["version_id"])
        self.assertIn("p-sub1", v2["plan"]["replan_reason"])
        fdr3 = next(i for i in v2["plan"]["items"] if i["project_id"] == "p-fdr3")
        self.assertEqual(fdr3["delay_impact_days"], 15)
        self.assertGreaterEqual(fdr3["start_day"], 15)

        self._request("POST", f"/versions/{v2['version_id']}/approve")
        status, restored = self._request("POST", "/versions/rollback")
        self.assertEqual(status, 200)
        self.assertEqual(restored["version_id"], v1["version_id"])
        self.assertEqual(restored["state"], "approved")

    def test_new_topology_does_not_mutate_approved_version(self):
        v1 = self._request("POST", "/versions", _payload(note="1"))[1]
        self._request("POST", f"/versions/{v1['version_id']}/approve")

        extended = _payload(note="新增资产")
        extended["assets"].append(
            {"asset_id": "fdr-4", "asset_kind": "feeder",
             "serves": ["shelter-e"], "upstream_asset_ids": ["sub-1"]}
        )
        extended["service_points"].append(
            {"point_id": "shelter-e", "name": "戊避险点", "population": 150}
        )
        v2 = self._request("POST", "/versions", extended)[1]
        self.assertNotEqual(v2["version_id"], v1["version_id"])

        untouched = self._request("GET", f"/versions/{v1['version_id']}")[1]
        self.assertEqual(untouched["state"], "approved")
        self.assertEqual(len(untouched["scores"]), len(v1["scores"]))

    def test_bad_topology_returns_400(self):
        bad = _payload()
        bad["assets"][0]["upstream_asset_ids"] = ["ghost"]
        status, body = self._request("POST", "/compare", bad)
        self.assertEqual(status, 400)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
