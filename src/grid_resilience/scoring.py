"""候选改造项目的可解释韧性评分。

三个风险维度（均在情景上求和）：

1. 人口风险：失电概率 × 连带失电服务人口；
2. 抢通风险：失电概率 × 人口 × 预期抢通天数（替代线路修复时间纳入该维度）；
3. 连锁风险：失电概率 × 连带失电的下游资产数。

各维度“改造前 -> 改造后”的收益按候选集合内最大值归一化到 [0, 1]，
再按规划人员给定的权重加权，因此每个分数都能展开成维度贡献说明。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from .contracts import (
    RetrofitCandidate,
    RiskScenario,
    RiskWeights,
    ServicePoint,
)
from .network import Network

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class ScenarioMetric:
    scenario_id: str
    fail_prob_before: Decimal
    fail_prob_after: Decimal
    restore_days_before: Decimal
    restore_days_after: Decimal
    exposed_population: int
    cascade_assets: int


@dataclass(frozen=True)
class ProjectScore:
    project_id: str
    asset_id: str
    score: Decimal
    rank: int
    components: Mapping[str, Decimal]
    benefits_raw: Mapping[str, Decimal]
    normalizers: Mapping[str, Decimal]
    scenario_metrics: Mapping[str, ScenarioMetric] = field(default_factory=dict)

    def explain(self) -> list[str]:
        """返回逐条中文排序依据。"""
        lines = [f"项目 {self.project_id}（资产 {self.asset_id}）总分 {self.score}："]
        labels = {
            "population": "人口覆盖",
            "restoration": "抢通时间",
            "cascade": "连锁切断",
        }
        for key, value in self.components.items():
            lines.append(
                f"  - {labels[key]}贡献 {value}（原始收益 "
                f"{self.benefits_raw[key]}，归一化基准 {self.normalizers[key]}）"
            )
        return lines


def _expected_restore_days(
    network: Network,
    root_asset_id: str,
    scenario: RiskScenario,
    repair_scale: Optional[Mapping[str, Decimal]] = None,
) -> Decimal:
    """根资产供电点的预期抢通天数。

    上游每个资产独立失效都会触发对应抢修/替代线路修复，
    期望天数取 Σ p_u × 修复天数_u。
    """
    repair_scale = repair_scale or {}
    total = _ZERO
    for asset_id in network.upstream_closure(root_asset_id):
        prob = Decimal(scenario.asset_fail_prob.get(asset_id, 0))
        days = Decimal(scenario.repair_time_days.get(asset_id, 0))
        scale = Decimal(repair_scale.get(asset_id, _ONE))
        total += prob * days * scale
    return total


def _scenario_metric(
    network: Network,
    population: Mapping[str, int],
    scenario: RiskScenario,
    candidate: RetrofitCandidate,
) -> ScenarioMetric:
    root = candidate.asset_id
    downstream = network.downstream(root)
    _, exposed_pop = network.served_points(population, downstream)
    cascade = len(downstream) - 1

    fail_before = network.failure_probability(root, scenario.asset_fail_prob)

    scaled_prob: dict[str, Decimal] = {
        aid: Decimal(p) for aid, p in scenario.asset_fail_prob.items()
    }
    scaled_prob[root] = scaled_prob.get(root, _ZERO) * candidate.residual_fail_fraction
    fail_after = network.failure_probability(root, scaled_prob)

    restore_before = _expected_restore_days(network, root, scenario)
    restore_after = _expected_restore_days(
        network,
        root,
        scenario,
        repair_scale={root: candidate.residual_repair_fraction},
    )
    return ScenarioMetric(
        scenario_id=scenario.scenario_id,
        fail_prob_before=fail_before,
        fail_prob_after=fail_after,
        restore_days_before=restore_before,
        restore_days_after=restore_after,
        exposed_population=exposed_pop,
        cascade_assets=cascade,
    )


def _validate_candidates(
    network: Network, candidates: Sequence[RetrofitCandidate]
) -> None:
    ids = {c.project_id for c in candidates}
    if len(ids) != len(candidates):
        raise ValueError("候选项目编号重复")
    for candidate in candidates:
        if candidate.asset_id not in network.assets:
            raise ValueError(
                f"项目 {candidate.project_id} 引用了未知资产 {candidate.asset_id}"
            )
        for prereq in candidate.prerequisite_ids:
            if prereq not in ids:
                raise ValueError(
                    f"项目 {candidate.project_id} 的前置项目 {prereq} 不存在"
                )


def rank_projects(
    network: Network,
    service_points: Sequence[ServicePoint],
    scenarios: Sequence[RiskScenario],
    candidates: Sequence[RetrofitCandidate],
    weights: RiskWeights,
) -> list[ProjectScore]:
    """对候选项目在给定情景集合下评分排序（分数相同时按项目编号确定顺序）。"""
    _validate_candidates(network, candidates)
    population = {p.point_id: p.population for p in service_points}

    metrics: dict[str, dict[str, ScenarioMetric]] = {}
    raw: dict[str, dict[str, Decimal]] = {}
    for candidate in candidates:
        per_scenario = {
            s.scenario_id: _scenario_metric(network, population, s, candidate)
            for s in scenarios
        }
        metrics[candidate.project_id] = per_scenario
        raw[candidate.project_id] = {
            "population": sum(
                (m.fail_prob_before - m.fail_prob_after) * m.exposed_population
                for m in per_scenario.values()
            ),
            "restoration": sum(
                (m.fail_prob_before - m.fail_prob_after)
                * m.exposed_population
                * (m.restore_days_before - m.restore_days_after)
                for m in per_scenario.values()
            ),
            "cascade": sum(
                (m.fail_prob_before - m.fail_prob_after) * m.cascade_assets
                for m in per_scenario.values()
            ),
        }
        # 抢通天数即使失电概率不变也可能下降，单独计入一次人口加权收益。
        raw[candidate.project_id]["restoration"] += sum(
            m.fail_prob_after
            * m.exposed_population
            * (m.restore_days_before - m.restore_days_after)
            for m in per_scenario.values()
        )

    normalizers = {
        key: max(
            (raw[c.project_id][key] for c in candidates),
            default=_ZERO,
        )
        for key in ("population", "restoration", "cascade")
    }

    weight_map = {
        "population": Decimal(weights.population),
        "restoration": Decimal(weights.restoration),
        "cascade": Decimal(weights.cascade),
    }
    scored: list[ProjectScore] = []
    for candidate in candidates:
        components = {}
        for key, weight in weight_map.items():
            norm = normalizers[key]
            normalized = raw[candidate.project_id][key] / norm if norm > 0 else _ZERO
            components[key] = weight * normalized
        total = sum(components.values(), _ZERO)
        scored.append(
            ProjectScore(
                project_id=candidate.project_id,
                asset_id=candidate.asset_id,
                score=total,
                rank=0,
                components=components,
                benefits_raw=raw[candidate.project_id],
                normalizers=normalizers,
                scenario_metrics=metrics[candidate.project_id],
            )
        )

    scored.sort(key=lambda s: (-s.score, s.project_id))
    return [
        ProjectScore(
            project_id=s.project_id,
            asset_id=s.asset_id,
            score=s.score,
            rank=i + 1,
            components=s.components,
            benefits_raw=s.benefits_raw,
            normalizers=s.normalizers,
            scenario_metrics=s.scenario_metrics,
        )
        for i, s in enumerate(scored)
    ]
