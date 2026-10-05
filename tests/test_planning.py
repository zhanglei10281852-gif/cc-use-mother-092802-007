import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import fixtures
from grid_resilience.contracts import ProjectProgress, RepairResources
from grid_resilience.versioning import evaluate


def _plan(inputs):
    return evaluate(inputs)


class PlanningTests(unittest.TestCase):
    def test_ranked_plan_respects_budget_and_crew_caps(self):
        inputs = fixtures.frozen_inputs()
        scores, plan = _plan(inputs)
        ids = [i.project_id for i in plan.items]
        # 最高分序列 p-sub1(80/10) 后，预算 50、工日 15 容不下 p-line7(60/8)，
        # 只能选更便宜的项目。
        self.assertEqual(ids[0], "p-sub1")
        self.assertNotIn("p-line7", ids)
        self.assertLessEqual(plan.budget_used, Decimal("130"))
        self.assertLessEqual(plan.crew_days_used, 25)
        blocked = {b.project_id: b for b in plan.blocked}
        self.assertIn("p-line7", blocked)
        self.assertEqual(blocked["p-line7"].reason, "resource")

    def test_prerequisite_blocks_until_predecessor_selected(self):
        inputs = fixtures.frozen_inputs(
            resources=RepairResources(Decimal("1000"), 1000)
        )
        scores, plan = _plan(inputs)
        ids = [i.project_id for i in plan.items]
        self.assertIn("p-sub1", ids)
        self.assertIn("p-fdr3", ids)
        self.assertLess(ids.index("p-sub1"), ids.index("p-fdr3"))
        # 时间轴上后置项目不得早于前置项目完工。
        item = {i.project_id: i for i in plan.items}
        self.assertGreaterEqual(item["p-fdr3"].start_day, item["p-sub1"].finish_day)

    def test_missing_prerequisite_is_reported_as_blocker(self):
        # 资源只够后置项目、不够前置项目时，阻塞原因必须指向前置项目。
        inputs = fixtures.frozen_inputs(
            resources=RepairResources(Decimal("25"), 4)
        )
        _, plan = _plan(inputs)
        blocked = {b.project_id: b for b in plan.blocked}
        self.assertIn("p-fdr3", blocked)
        self.assertEqual(blocked["p-fdr3"].reason, "prerequisite")
        self.assertIn("p-sub1", blocked["p-fdr3"].blocking_project_ids)

    def test_serial_schedule_is_gap_free_and_cumulative(self):
        inputs = fixtures.frozen_inputs(
            resources=RepairResources(Decimal("1000"), 1000)
        )
        _, plan = _plan(inputs)
        scheduled = [i for i in plan.items if i.state == "scheduled"]
        for earlier, later in zip(scheduled, scheduled[1:]):
            self.assertEqual(later.start_day, earlier.finish_day)
        self.assertEqual(plan.horizon_days, scheduled[-1].finish_day)

    def test_delayed_inflight_project_shifts_downstream_only(self):
        # p-sub1 在途且延期 12 天：只有其下游 p-fdr3 被推迟并标注影响。
        inputs = fixtures.frozen_inputs(
            resources=RepairResources(Decimal("1000"), 1000),
            progress=[ProjectProgress("p-sub1", "in_progress", delay_days=12)],
        )
        _, plan = _plan(inputs)
        item = {i.project_id: i for i in plan.items}
        self.assertEqual(item["p-sub1"].state, "in_progress")
        self.assertEqual(item["p-sub1"].finish_day, 12)
        self.assertGreaterEqual(item["p-fdr3"].start_day, 12)
        self.assertEqual(item["p-fdr3"].delay_impact_days, 12)
        self.assertEqual(item["p-line7"].delay_impact_days, 0)
        self.assertIsNotNone(plan.replan_reason)
        self.assertIn("p-sub1", plan.replan_reason)

    def test_completed_project_consumes_resources_and_locks_slot(self):
        inputs = fixtures.frozen_inputs(
            resources=RepairResources(Decimal("100"), 20),
            progress=[ProjectProgress("p-sub1", "completed")],
        )
        _, plan = _plan(inputs)
        states = {i.project_id: i.state for i in plan.items}
        self.assertEqual(states["p-sub1"], "completed")
        # 只剩 20 预算 / 10 工日：p-line7 必然无法排入。
        self.assertNotIn("p-line7", states)
        self.assertLessEqual(plan.budget_used, Decimal("100"))

    def test_progress_overrun_beyond_resources_rejected(self):
        inputs = fixtures.frozen_inputs(
            resources=RepairResources(Decimal("10"), 1),
            progress=[ProjectProgress("p-sub1", "completed")],
        )
        with self.assertRaises(ValueError):
            _plan(inputs)


if __name__ == "__main__":
    unittest.main()
