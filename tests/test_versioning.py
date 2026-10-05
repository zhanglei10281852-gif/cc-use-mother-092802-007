import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import fixtures
from grid_resilience.contracts import (
    GridAsset,
    ProjectProgress,
    RepairResources,
    RiskWeights,
)
from grid_resilience.versioning import (
    APPROVED,
    DRAFT,
    ROLLED_BACK,
    SUPERSEDED,
    PlanRegistry,
    content_hash,
)


class VersioningTests(unittest.TestCase):
    def test_approval_freezes_data_and_weights(self):
        registry = PlanRegistry()
        inputs = fixtures.frozen_inputs(note="年度计划v1")
        v1 = registry.submit(inputs)
        approved = registry.approve(v1.version_id)
        self.assertEqual(approved.state, APPROVED)
        # 版本对象不可变。
        with self.assertRaises(Exception):
            approved.state = DRAFT  # type: ignore[misc]
        # 同一输入重新提交得到同一哈希、同一版本。
        self.assertIs(registry.submit(fixtures.frozen_inputs(note="年度计划v1")), approved)

    def test_new_asset_relation_cannot_rewrite_approved_plan(self):
        registry = PlanRegistry()
        v1 = registry.approve(registry.submit(fixtures.frozen_inputs()).version_id)
        frozen_scores = list(v1.scores)

        # 规划库新增资产关系（拓扑变化）只产生新版本草案。
        changed_assets = list(fixtures.ASSETS) + [
            GridAsset("fdr-4", "feeder", ("shelter-e",), ("sub-1",))
        ]
        v2_inputs = fixtures.frozen_inputs(
            assets=changed_assets, note="新增馈线关系"
        )
        v2 = registry.submit(v2_inputs)
        self.assertNotEqual(v2.version_id, v1.version_id)
        self.assertNotEqual(v2.content_hash, v1.content_hash)
        self.assertEqual(v2.state, DRAFT)
        # 旧版本原样保留：分数、哈希、状态均未倒改。
        self.assertEqual(registry.get(v1.version_id).state, APPROVED)
        self.assertEqual(
            [s.project_id for s in registry.get(v1.version_id).scores],
            [s.project_id for s in frozen_scores],
        )
        self.assertEqual(registry.current_approved.version_id, v1.version_id)

    def test_weight_adjustment_creates_separate_draft(self):
        registry = PlanRegistry()
        v1 = registry.approve(registry.submit(fixtures.frozen_inputs()).version_id)
        tweaked = fixtures.frozen_inputs(
            weights=RiskWeights(Decimal("1"), Decimal("2"), Decimal("1")),
            note="提高抢通权重",
        )
        v2 = registry.submit(tweaked)
        self.assertNotEqual(v2.content_hash, v1.content_hash)
        self.assertEqual(registry.current_approved.version_id, v1.version_id)

    def test_approval_chain_supersedes_previous(self):
        registry = PlanRegistry()
        v1 = registry.approve(registry.submit(fixtures.frozen_inputs(note="1")).version_id)
        v2_inputs = fixtures.frozen_inputs(
            progress=[ProjectProgress("p-sub1", "in_progress", delay_days=12)],
            note="施工延期重排",
        )
        v2 = registry.approve(registry.submit(v2_inputs).version_id)
        self.assertEqual(registry.get(v1.version_id).state, SUPERSEDED)
        self.assertEqual(registry.current_approved.version_id, v2.version_id)
        self.assertEqual(v2.parent_version_id, v1.version_id)

    def test_rollback_restores_previous_approved_version(self):
        registry = PlanRegistry()
        v1 = registry.approve(registry.submit(fixtures.frozen_inputs(note="1")).version_id)
        v2 = registry.approve(
            registry.submit(fixtures.frozen_inputs(note="2")).version_id
        )
        restored = registry.rollback()
        self.assertEqual(restored.version_id, v1.version_id)
        self.assertEqual(restored.state, APPROVED)
        self.assertEqual(registry.get(v2.version_id).state, ROLLED_BACK)
        self.assertEqual(registry.current_approved.version_id, v1.version_id)
        # 回退后旧计划的冻结数据与排序仍可读取。
        self.assertEqual(restored.content_hash, content_hash(fixtures.frozen_inputs(note="1")))

    def test_rollback_without_history_rejected(self):
        with self.assertRaises(ValueError):
            PlanRegistry().rollback()

    def test_cannot_approve_rolled_back_version_again(self):
        registry = PlanRegistry()
        registry.approve(registry.submit(fixtures.frozen_inputs(note="1")).version_id)
        v2 = registry.approve(registry.submit(fixtures.frozen_inputs(note="2")).version_id)
        registry.rollback()
        with self.assertRaises(ValueError):
            registry.approve(v2.version_id)

    def test_replan_midstream_preserves_approved_and_links_parent(self):
        registry = PlanRegistry()
        v1 = registry.approve(registry.submit(fixtures.frozen_inputs()).version_id)
        delayed_inputs = fixtures.frozen_inputs(
            resources=RepairResources(Decimal("1000"), 1000),
            progress=[ProjectProgress("p-sub1", "in_progress", delay_days=20)],
            note="中途重排",
        )
        redraft = registry.submit(delayed_inputs, parent_version_id=v1.version_id)
        self.assertEqual(redraft.state, DRAFT)
        self.assertEqual(redraft.parent_version_id, v1.version_id)
        self.assertIsNotNone(redraft.plan.replan_reason)
        impacted = {i.project_id: i for i in redraft.plan.items}["p-fdr3"]
        self.assertEqual(impacted.delay_impact_days, 20)


if __name__ == "__main__":
    unittest.main()
