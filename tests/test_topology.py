import unittest

from support import make_assets
from grid_resilience.contracts import GridAsset
from grid_resilience.errors import CyclicDependency, UnknownReference
from grid_resilience.topology import Network, detect_prerequisite_cycle


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.network = Network(make_assets())

    def test_root_failure_cascades_to_every_shelter(self):
        # line-7 是走廊根节点，失效连带切断全部 4 个避险点
        affected = self.network.affected_points("line-7")
        self.assertEqual(
            set(affected),
            {"shelter-1", "shelter-2", "shelter-3", "shelter-4"},
        )

    def test_branch_substation_failure_is_scoped(self):
        # sub-1 失效只影响它直供的两个避险点，不影响 sub-2 支路
        self.assertEqual(
            set(self.network.affected_points("sub-1")),
            {"shelter-1", "shelter-2"},
        )

    def test_feeder_failure_only_its_own_point(self):
        self.assertEqual(self.network.affected_points("radial-3"), ("shelter-4",))

    def test_feeding_set_walks_all_ancestors(self):
        # shelter-4 由 radial-3 <- sub-2 <- line-9 <- line-7 整条链供电
        self.assertEqual(
            self.network.feeding_set("shelter-4"),
            frozenset({"radial-3", "sub-2", "line-9", "line-7"}),
        )

    def test_unknown_upstream_rejected(self):
        with self.assertRaises(UnknownReference):
            Network((GridAsset("a", "x", (), ("ghost",)),))

    def test_duplicate_asset_rejected(self):
        with self.assertRaises(UnknownReference):
            Network((
                GridAsset("a", "x", (), ()),
                GridAsset("a", "x", (), ()),
            ))

    def test_supply_cycle_detected(self):
        cyclic = (
            GridAsset("a", "x", (), ("c",)),
            GridAsset("b", "x", (), ("a",)),
            GridAsset("c", "x", (), ("b",)),
        )
        with self.assertRaises(CyclicDependency):
            Network(cyclic)

    def test_topology_change_changes_cascade_scope(self):
        # 灾后规划：把 radial-3 从 sub-2 支路改接到一条独立走廊 line-new，
        # line-7 失效不再连带切断 shelter-4，新走廊风险独立成链
        changed = tuple(
            GridAsset("radial-3", "feeder", ("shelter-4",), ("line-new",))
            if a.asset_id == "radial-3"
            else a
            for a in make_assets()
        ) + (GridAsset("line-new", "line", (), ()),)
        network = Network(changed)
        self.assertNotIn("shelter-4", network.affected_points("line-7"))
        self.assertEqual(
            network.affected_points("line-new"), ("shelter-4",)
        )

    def test_prerequisite_cycle_detected(self):
        with self.assertRaises(CyclicDependency):
            detect_prerequisite_cycle(
                ["p1", "p2"], {"p1": {"p2"}, "p2": {"p1"}}
            )


if __name__ == "__main__":
    unittest.main()
