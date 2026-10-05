import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import fixtures
from grid_resilience.contracts import GridAsset
from grid_resilience.network import Network, TopologyError

POP = {p.point_id: p.population for p in fixtures.SERVICE_POINTS}


class NetworkTopologyTests(unittest.TestCase):
    def setUp(self):
        self.network = Network.build(fixtures.ASSETS)

    def test_substation_failure_cuts_all_downstream_shelters(self):
        # 变电站失效连带切断自己和两条馈线的避险点。
        down = self.network.downstream("sub-1")
        self.assertEqual(down, {"sub-1", "fdr-2", "fdr-3"})
        points, population = self.network.served_points(POP, down)
        self.assertEqual(points, {"shelter-a", "shelter-b", "shelter-c"})
        self.assertEqual(population, 1000)

    def test_line_failure_reaches_substation_and_feeders(self):
        down = self.network.downstream("line-7")
        self.assertEqual(down, {"line-7", "sub-1", "fdr-2", "fdr-3"})

    def test_independent_substation_not_affected(self):
        self.assertNotIn("sub-9", self.network.downstream("sub-1"))

    def test_upstream_closure_models_series_failure(self):
        self.assertEqual(
            self.network.upstream_closure("fdr-2"),
            {"fdr-2", "sub-1", "line-7"},
        )
        prob = self.network.failure_probability("fdr-2", fixtures.ICEFALL.asset_fail_prob)
        self.assertAlmostEqual(float(prob), 1 - 0.88 * 0.85 * 0.9)

    def test_dangling_upstream_reference_rejected(self):
        bad = list(fixtures.ASSETS) + [GridAsset("x", "switch", (), ("ghost",))]
        with self.assertRaises(TopologyError):
            Network.build(bad)

    def test_dependency_cycle_rejected(self):
        assets = [
            GridAsset("a", "line", (), ("b",)),
            GridAsset("b", "line", (), ("a",)),
        ]
        with self.assertRaises(TopologyError):
            Network.build(assets)

    def test_topology_change_expands_cascade_footprint(self):
        # 新增一条由 sub-1 供电的馈线：连带切断范围扩大。
        assets = list(fixtures.ASSETS) + [
            GridAsset("fdr-4", "feeder", ("shelter-e",), ("sub-1",))
        ]
        network = Network.build(assets)
        points, population = network.served_points(
            {**POP, "shelter-e": 150}, network.downstream("sub-1")
        )
        self.assertIn("shelter-e", points)
        self.assertEqual(population, 1150)


if __name__ == "__main__":
    unittest.main()
