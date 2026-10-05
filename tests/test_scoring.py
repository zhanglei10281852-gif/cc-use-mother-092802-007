import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import fixtures
from grid_resilience.contracts import RiskScenario, RiskWeights
from grid_resilience.network import Network
from grid_resilience.scoring import rank_projects


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.network = Network.build(fixtures.ASSETS)

    def scores(self, scenarios=None, weights=None):
        return rank_projects(
            self.network,
            fixtures.SERVICE_POINTS,
            scenarios or [fixtures.ICEFALL],
            fixtures.candidates(),
            weights or fixtures.DEFAULT_WEIGHTS,
        )

    def test_substation_outranks_feeder_due_to_cascade_population(self):
        ranked = self.scores()
        order = [s.project_id for s in ranked]
        self.assertEqual(order[0], "p-sub1")
        self.assertLess(ranked[0].score, Decimal("1.700001"))
        self.assertGreater(ranked[0].score, Decimal("1"))
        # 馈线分数应低于它供电的变电站加固。
        ranks = {s.project_id: s.rank for s in ranked}
        self.assertLess(ranks["p-sub1"], ranks["p-fdr2"])
        self.assertLess(ranks["p-sub1"], ranks["p-fdr3"])

    def test_explain_lists_three_weighted_components(self):
        top = self.scores()[0]
        lines = top.explain()
        self.assertTrue(any("人口覆盖" in line for line in lines))
        self.assertTrue(any("抢通时间" in line for line in lines))
        self.assertTrue(any("连锁切断" in line for line in lines))
        # 分数 = 三个维度贡献之和。
        self.assertEqual(top.score, sum(top.components.values(), Decimal("0")))

    def test_alternative_line_cuts_restoration_time(self):
        # p-line7 建设替代线路（修复时间降到 20%）：抢通维度收益必须为正。
        ranked = {s.project_id: s for s in self.scores()}
        metric = ranked["p-line7"].scenario_metrics["icefall"]
        self.assertLess(metric.restore_days_after, metric.restore_days_before)
        self.assertGreater(ranked["p-line7"].components["restoration"], 0)

    def test_weights_change_ordering_repeatably(self):
        pop_first = self.scores(weights=RiskWeights(Decimal("1"), Decimal("0"), Decimal("0")))
        cascade_heavy = self.scores(weights=RiskWeights(Decimal("0"), Decimal("0"), Decimal("1")))
        # 极端加权下结果仍可重复，且纯人口视角与纯连锁视角可能给出不同冠军。
        self.assertEqual(
            [s.project_id for s in pop_first],
            [s.project_id for s in self.scores(
                weights=RiskWeights(Decimal("1"), Decimal("0"), Decimal("0")))],
        )
        self.assertEqual(cascade_heavy[0].project_id, "p-line7")  # line-7 连锁资产最多

    def test_scenario_comparison_aggregates_risk(self):
        # 温和情景为严重冰崩概率的等比缩小（修复时间不变）：
        # 同权重下排序保持稳定，仅风险指标随情景变化。
        mild = RiskScenario(
            "mild-winter", "暖冬轻冰",
            asset_fail_prob={
                k: v * Decimal("0.05")
                for k, v in fixtures.ICEFALL.asset_fail_prob.items()
            },
            repair_time_days=fixtures.ICEFALL.repair_time_days,
        )
        mild_scores = self.scores(scenarios=[mild])
        severe_scores = self.scores(scenarios=[fixtures.ICEFALL])
        # 情景只影响归一化前数值；同样权重下最高分项目保持一致，
        # 但暴露指标随情景变化。
        self.assertEqual(mild_scores[0].project_id, severe_scores[0].project_id)
        top_mild = mild_scores[0].scenario_metrics["mild-winter"]
        top_severe = severe_scores[0].scenario_metrics["icefall"]
        self.assertLess(top_mild.fail_prob_before, top_severe.fail_prob_before)

    def test_zero_probability_scenario_gives_zero_scores(self):
        calm = RiskScenario("calm", "无冰情", {}, {})
        ranked = self.scores(scenarios=[calm])
        for score in ranked:
            self.assertEqual(score.score, 0)


if __name__ == "__main__":
    unittest.main()
