import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from grid_resilience.contracts import GridAsset, RetrofitCandidate


class GridContractTests(unittest.TestCase):
    def test_asset_records_service_and_topology_edges(self):
        asset = GridAsset("sub-1", "substation", ("shelter-2",), ("line-7",))
        self.assertIn("shelter-2", asset.serves)
        self.assertIn("line-7", asset.upstream_asset_ids)

    def test_candidate_carries_resource_cost(self):
        candidate = RetrofitCandidate("p-1", "sub-1", Decimal("12.5"), 8, ())
        self.assertEqual(candidate.crew_days, 8)


if __name__ == "__main__":
    unittest.main()
