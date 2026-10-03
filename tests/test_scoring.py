import unittest
from dataclasses import replace
from decimal import Decimal

from support import make_data, make_weights
from grid_resilience.contracts import (
    GridAsset,
    PlanningData,
    RepairResources,
    RetrofitCandidate,
    RiskScenario,
    ServicePoint,
    WorkBudget,
)
from grid_resilience.planning import evaluate
from grid_resilience.scoring import RiskWeights, schedule_projects
from grid_resilience.topology import Network


class ScoringTests(unittest.TestCase):
    def test_root_corridor_outranks_leaf(self):
        result = evaluate(make_data(), make_weights())
        order = [s.project_id for s in result.rankings]
        self.assertEqual(order[0], "p-line7")
        line7 = result.rankings[0]
        # 走廊根节点连带全部避险点：加权人口 1000+500+4000+300
        self.assertEqual(line7.exposed_population, 5800)
        self.assertGreater(line7.risk_reduction, 0)

    def test_bypass_time_enters_restoration_benefit(self):
        # 最小场景：孤立线路是避险点唯一供电资产，修复 30 天；
        # 候选新增 5 天替代线路旁路后应避免更多年期望停电人口日
        assets = (GridAsset("line-x", "line", ("shelter-x",), ()),)
        points = (ServicePoint("shelter-x", "x", 1000, 1.0),)
        scenarios = (
            RiskScenario("ice", "冰崩", 0.5, (("line-x", 0.8),)),
        )
        base = PlanningData(
            assets=assets,
            service_points=points,
            scenarios=scenarios,
            repair=RepairResources(repair_days=(("line-x", 30),)),
            candidates=(
                RetrofitCandidate(
                    "p-no-bypass", "line-x", Decimal("100"), 10, (),
                    residual_failure_factor=0.2,
                ),
            ),
            resources=WorkBudget(Decimal("1000"), 2, 120),
        )
        with_bypass = replace(
            base,
            candidates=(
                replace(base.candidates[0], bypass_after_days=5),
            ),
        )
        r_no = evaluate(base).rankings[0]
        r_yes = evaluate(with_bypass).rankings[0]
        self.assertGreater(
            r_yes.restoration_reduction, r_no.restoration_reduction
        )

    def test_weight_change_reorders_candidates(self):
        # 把风险权重压到 0、恢复权重拉高：含 bypass_after_days 的项目受益
        risk_heavy = evaluate(make_data(), RiskWeights(risk=1, restoration=0, dependency=0))
        restore_heavy = evaluate(make_data(), RiskWeights(risk=0, restoration=1, dependency=0))
        self.assertEqual(risk_heavy.rankings[0].project_id, "p-line7")
        # 极端权重下排序可能变化，但两套结果的分数必须不同
        self.assertNotEqual(
            [s.priority_score for s in risk_heavy.rankings],
            [s.priority_score for s in restore_heavy.rankings],
        )

    def test_baseline_harder_with_ice_avalanche_scenario(self):
        only_storm = make_data(
            scenarios=tuple(s for s in make_data().scenarios if s.scenario_id == "ice-storm")
        )
        both = make_data()
        self.assertGreater(
            evaluate(both).baseline_annual_risk,
            evaluate(only_storm).baseline_annual_risk,
        )

    def test_prerequisite_blocks_selection_and_is_explained(self):
        # 预算仅够 p-line7：sub2 落选，依赖 sub2 的 radial3 必须展示前置阻塞
        tight = make_data(resources=WorkBudget(Decimal("180"), 2, 120))
        result = evaluate(tight)
        by_id = {s.project_id: s for s in result.rankings}
        self.assertEqual(result.selected, ["p-line7"])
        self.assertEqual(by_id["p-radial3"].blockers, ("p-sub2",))
        self.assertIn("前置工程", by_id["p-radial3"].not_selected_reason)
        # 前置已入选、但预算不足的项目给出的是资源原因
        self.assertIn("预算", by_id["p-sub1"].not_selected_reason)

    def test_scores_are_reproducible(self):
        r1 = evaluate(make_data(), make_weights())
        r2 = evaluate(make_data(), make_weights())
        self.assertEqual(
            [(s.project_id, s.priority_score, s.risk_reduction) for s in r1.rankings],
            [(s.project_id, s.priority_score, s.risk_reduction) for s in r2.rankings],
        )

    def test_benefit_cost_ratio_and_explanation_fields_present(self):
        s = evaluate(make_data()).rankings[0]
        self.assertGreater(s.benefit_cost_ratio, 0)
        self.assertEqual(
            set(s.contributions),
            {"risk_share", "restoration_share", "dependency_share"},
        )


class SchedulingTests(unittest.TestCase):
    def test_parallel_teams_and_prerequisite_ordering(self):
        data = make_data(resources=WorkBudget(Decimal("1000"), 2, 60))
        result = evaluate(data)
        self.assertEqual(
            set(result.selected), {"p-line7", "p-sub1", "p-sub2", "p-radial3"}
        )
        sched = {
            s.project_id: (s.scheduled_start, s.scheduled_finish, s.team)
            for s in result.rankings
            if s.scheduled_start is not None
        }
        # p-sub1 的前置 p-line7（25天）必须先完工，尽管有空闲施工队
        self.assertGreaterEqual(sched["p-sub1"][0], sched["p-line7"][1])
        # 两个项目并行：p-sub2 与 p-line7 在第 0 天同时开工（不同队）
        self.assertEqual(sched["p-line7"][0], 0)
        self.assertEqual(sched["p-sub2"][0], 0)
        self.assertNotEqual(sched["p-line7"][2], sched["p-sub2"][2])

    def test_resource_conflict_excludes_and_warns(self):
        # 仅 1 支施工队、35 天窗口：p-line7(25)+p-sub1(12) 排在 37 天完工，超窗
        tight = make_data(
            resources=WorkBudget(Decimal("1000"), 1, 35)
        )
        result = evaluate(tight)
        self.assertTrue(result.warnings)
        self.assertIn("资源冲突", result.warnings[0])
        # 超窗项目必须落选且给出原因
        reasons = {s.project_id: s.not_selected_reason for s in result.rankings}
        self.assertIsNotNone(reasons["p-sub1"])
        self.assertIn("工期窗口", reasons["p-sub1"])

    def test_budget_blocks_expensive_project(self):
        tight = make_data(resources=WorkBudget(Decimal("120"), 2, 120))
        result = evaluate(tight)
        self.assertNotIn("p-line7", result.selected)
        by_id = {s.project_id: s for s in result.rankings}
        self.assertIn("预算", by_id["p-line7"].not_selected_reason)

    def test_schedule_projects_unit_delay_shifts_downstream(self):
        data = make_data()
        by_id = {c.project_id: c for c in data.candidates}
        ordered = [by_id[p] for p in ("p-line7", "p-sub2", "p-sub1", "p-radial3")]
        base = schedule_projects(ordered, crew_teams=2)
        # p-line7 从第 25 天延期到第 40 天
        delayed = schedule_projects(
            ordered,
            crew_teams=2,
            delays={"p-line7": 40},
            in_progress={"p-line7": (0, 25, 0)},
        )
        self.assertEqual(delayed["p-line7"][1], 40)
        self.assertGreaterEqual(delayed["p-sub1"][0], 40)
        self.assertGreater(delayed["p-sub1"][0], base["p-sub1"][0])
        # 非依赖链上的 p-sub2 不应受影响
        self.assertEqual(delayed["p-sub2"], base["p-sub2"])


if __name__ == "__main__":
    unittest.main()
