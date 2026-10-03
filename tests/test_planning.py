import copy
import unittest
from dataclasses import replace
from decimal import Decimal

from support import make_assets, make_data, make_weights
from grid_resilience.contracts import GridAsset, WorkBudget
from grid_resilience.errors import InvalidTransition, UnknownReference
from grid_resilience.planning import APPROVED, DRAFT, WITHDRAWN, PlanningStore
from grid_resilience.scoring import RiskWeights


def approved_store():
    store = PlanningStore()
    # 预算充足：四个项目全部入选，延期时上游/下游依赖链完整可验证
    data = make_data(resources=WorkBudget(Decimal("1000"), 2, 60))
    store.create_plan("annual-2026", data, make_weights())
    store.approve("annual-2026")
    return store, data


class VersionFreezeTests(unittest.TestCase):
    def test_approved_version_is_frozen_against_data_and_weights(self):
        store, _ = approved_store()
        v1 = store.get_version("annual-2026", 1)
        self.assertEqual(v1.status, APPROVED)
        self.assertTrue(v1.content_hash)

        with self.assertRaises(InvalidTransition):
            store.update_draft_data("annual-2026", make_data())
        with self.assertRaises(InvalidTransition):
            store.adjust_weights("annual-2026", RiskWeights(9, 9, 9))

    def test_new_asset_relations_do_not_rewrite_approved_plan(self):
        store, data = approved_store()
        v1 = store.get_version("annual-2026", 1)
        old_ranking = [(s.project_id, s.priority_score) for s in v1.result.rankings]
        old_hash = v1.content_hash
        old_schedule = dict(v1.schedule)

        # 灾后补勘：新增独立走廊并把 radial-3 改接出去
        changed_assets = tuple(
            GridAsset("radial-3", "feeder", ("shelter-4",), ("line-new",))
            if a.asset_id == "radial-3"
            else a
            for a in data.assets
        ) + (GridAsset("line-new", "line", (), ()),)
        new_data = replace(data, assets=changed_assets)

        v2 = store.new_version("annual-2026", new_data)
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.parent_version, 1)
        self.assertEqual(v2.status, DRAFT)

        # 旧版本快照、指纹、排序、排程原封不动
        still_v1 = store.get_version("annual-2026", 1)
        self.assertEqual(still_v1.status, APPROVED)
        self.assertEqual(still_v1.content_hash, old_hash)
        self.assertEqual(
            [(s.project_id, s.priority_score) for s in still_v1.result.rankings],
            old_ranking,
        )
        self.assertEqual(still_v1.schedule, old_schedule)
        self.assertNotEqual(v2.content_hash, old_hash)

    def test_cannot_open_version_while_draft_open(self):
        store = PlanningStore()
        store.create_plan("p", make_data())
        with self.assertRaises(InvalidTransition):
            store.new_version("p", make_data())

    def test_weight_adjustment_only_on_draft(self):
        store = PlanningStore()
        store.create_plan("p", make_data(), make_weights())
        moved = store.adjust_weights("p", RiskWeights(0, 5, 0))
        self.assertEqual(moved.weights.restoration, 5)
        self.assertEqual(moved.version, 1)  # 草稿就地更新，不产生版本号


class ApprovalRollbackTests(unittest.TestCase):
    def test_rollback_withdraws_and_recreates_editable_draft(self):
        store, data = approved_store()
        v1 = store.get_version("annual-2026", 1)
        self.assertEqual(v1.status, APPROVED)

        draft = store.rollback_approval("annual-2026", reason="情景参数订正")
        self.assertEqual(v1.status, WITHDRAWN)
        self.assertEqual(draft.version, 2)
        self.assertEqual(draft.status, DRAFT)
        self.assertEqual(draft.parent_version, 1)
        # 回退开出的草稿继承冻结快照与权重，指纹一致便于审计
        self.assertEqual(draft.content_hash, v1.content_hash)

        # 新草稿可以改，撤回的旧版仍不可改
        moved = store.adjust_weights("annual-2026", RiskWeights(2, 1, 1))
        self.assertEqual(moved.version, 2)
        self.assertEqual(store.get_version("annual-2026", 1).status, WITHDRAWN)

    def test_cannot_rollback_non_approved(self):
        store = PlanningStore()
        store.create_plan("p", make_data())
        with self.assertRaises(InvalidTransition):
            store.rollback_approval("p")
        store.approve("p")
        store.rollback_approval("p")
        with self.assertRaises(InvalidTransition):
            store.rollback_approval("p")  # 最新版已是草稿

    def test_reapprove_after_rollback_freezes_new_weights(self):
        store, _ = approved_store()
        store.rollback_approval("annual-2026")
        store.adjust_weights("annual-2026", RiskWeights(3, 1, 1))
        v2 = store.approve("annual-2026")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.status, APPROVED)
        self.assertEqual(v2.weights.risk, 3)


class DelayReevaluationTests(unittest.TestCase):
    def test_delay_reschedules_downstream_only(self):
        store, _ = approved_store()
        # 已审批组合：p-line7(0-25)、p-sub1；p-sub1 前置 p-line7
        rev = store.report_delay("annual-2026", "p-line7", 40)
        self.assertEqual(rev.delayed_project, "p-line7")
        self.assertIn("p-sub1", rev.affected_downstream)
        sched = rev.schedule
        self.assertEqual(sched["p-line7"][1], 40)
        self.assertGreaterEqual(sched["p-sub1"][0], 40)

    def test_delay_beyond_horizon_warns_affected_projects(self):
        store, _ = approved_store()
        # 窗口 60 天：line7 延到 55，sub1 最早 55 开工、67 完工 → 超窗告警
        rev = store.report_delay("annual-2026", "p-line7", 55)
        self.assertTrue(rev.warnings)
        joined = " | ".join(rev.warnings)
        self.assertIn("p-sub1", joined)
        self.assertIn("超出工期窗口", joined)
        # 延期事实在版本上可追溯
        v1 = store.get_version("annual-2026", 1)
        self.assertEqual(len(v1.revisions), 1)
        self.assertEqual(v1.schedule["p-sub1"][0], 55)

    def test_delay_requires_approved_version(self):
        store = PlanningStore()
        store.create_plan("p", make_data())
        with self.assertRaises(InvalidTransition):
            store.report_delay("p", "p-line7", 40)

    def test_delay_rejects_backward_and_non_extension_dates(self):
        store, _ = approved_store()
        with self.assertRaises(InvalidTransition):
            store.report_delay("annual-2026", "p-line7", 10)  # 早于开工
        with self.assertRaises(InvalidTransition):
            store.report_delay("annual-2026", "p-line7", 25)  # 未晚于原完工
        with self.assertRaises(UnknownReference):
            store.report_delay("annual-2026", "p-ghost", 30)  # 不在审批组合

    def test_successive_delays_accumulate(self):
        store, _ = approved_store()
        store.report_delay("annual-2026", "p-line7", 35)
        rev2 = store.report_delay("annual-2026", "p-line7", 45)
        self.assertEqual(rev2.revision_no, 2)
        self.assertEqual(rev2.schedule["p-line7"][1], 45)
        self.assertGreaterEqual(rev2.schedule["p-sub1"][0], 45)

    def test_frozen_selection_survives_delay(self):
        store, _ = approved_store()
        store.report_delay("annual-2026", "p-line7", 55)
        v1 = store.get_version("annual-2026", 1)
        # 延期只重排，不改变冻结组合、权重与数据指纹
        self.assertEqual(
            set(v1.result.selected),
            {"p-line7", "p-sub1", "p-sub2", "p-radial3"},
        )
        self.assertEqual(v1.weights, make_weights())


class ComparisonTests(unittest.TestCase):
    def test_compare_weights_does_not_persist(self):
        store, _ = approved_store()
        store.rollback_approval("annual-2026")
        before = copy.deepcopy(
            [(s.project_id, s.priority_score)
             for s in store.latest("annual-2026").result.rankings]
        )
        cmp = store.compare_weights(
            "annual-2026",
            {"risk_first": RiskWeights(5, 1, 0), "restore_first": RiskWeights(0, 5, 0)},
        )
        self.assertEqual(set(cmp), {"risk_first", "restore_first"})
        after = [(s.project_id, s.priority_score)
                 for s in store.latest("annual-2026").result.rankings]
        self.assertEqual(before, after)

    def test_compare_scenarios(self):
        data = make_data()
        cmp = PlanningStore.compare_scenarios(
            data,
            make_weights(),
            {
                "storm_only": ("ice-storm",),
                "avalanche_included": ("ice-storm", "ice-avalanche"),
            },
        )
        self.assertGreater(
            cmp["avalanche_included"].baseline_annual_risk,
            cmp["storm_only"].baseline_annual_risk,
        )


if __name__ == "__main__":
    unittest.main()
