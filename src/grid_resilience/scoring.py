"""韧性评分：情景风险、替代线路恢复时间与候选组合优先级。

风险约定（便于复核）：
- 同一情景内各资产失效相互独立；某避险点失电当且仅当其任一供电资产
  （直接供电资产及其全部上游）失效。
- 资产年期望受影响人口 = Σ_情景 频率 × Σ_避险点 人口权重 × 失电概率。
- 避险点恢复时间取供电链上最慢资产的恢复工期；资产恢复工期取
  「完全修复」与「替代线路旁路」二者较短者。
- 改造可降低资产失效概率（残存系数）、新增旁路或缩短修复工期。
"""

from dataclasses import dataclass, field
from decimal import Decimal

from .contracts import (
    PlanningData,
    RetrofitCandidate,
    RiskScenario,
    ServicePoint,
)
from .topology import Network


@dataclass(frozen=True)
class RiskWeights:
    """规划人员可调的风险维度权重（审批时随计划冻结）。"""

    risk: float = 1.0
    restoration: float = 1.0
    dependency: float = 0.5

    def as_dict(self) -> dict[str, float]:
        return {"risk": self.risk, "restoration": self.restoration, "dependency": self.dependency}


@dataclass
class CandidateScore:
    project_id: str
    asset_id: str
    name: str
    priority_score: float                      # 0-100 的内在优先级
    benefit_cost_ratio: float                  # 每万元效益指数，用于预算约束下排序
    risk_reduction: float                      # 年期望避免的受影响人口（人·次/年）
    restoration_reduction: float               # 年期望避免的停电人口日（人·日/年）
    exposed_population: float                  # 失效连带影响的加权人口
    contributions: dict[str, float]            # 各维度归一化贡献（可解释）
    blockers: tuple[str, ...] = ()             # 未满足的前置工程
    not_selected_reason: str | None = None
    scheduled_start: int | None = None
    scheduled_finish: int | None = None
    team: int | None = None


@dataclass
class PortfolioResult:
    rankings: list[CandidateScore]
    selected: list[str]
    baseline_annual_risk: float
    baseline_annual_person_days: float
    portfolio_risk: float
    portfolio_person_days: float
    total_cost: Decimal
    weights: dict[str, float]
    warnings: tuple[str, ...] = ()


def _weighted_population(points: dict[str, ServicePoint]) -> float:
    return float(sum(p.population * p.criticality for p in points.values()))


def _hardened_factors(
    selected_projects: frozenset[str], candidates: dict[str, RetrofitCandidate]
) -> dict[str, float]:
    factors: dict[str, float] = {}
    for pid in sorted(selected_projects):
        c = candidates[pid]
        factors[c.asset_id] = min(factors.get(c.asset_id, 1.0), c.residual_failure_factor)
    return factors


def _restoration_days(
    data: PlanningData, asset_id: str, selected: frozenset[str]
) -> int:
    repair_days = dict(data.repair.repair_days).get(asset_id)
    bypass_days = dict(data.repair.bypass_days).get(asset_id)
    for c in data.candidates:
        if c.asset_id != asset_id or c.project_id not in selected:
            continue
        if repair_days is not None and c.repair_days_reduction:
            repair_days = max(0, repair_days - c.repair_days_reduction)
        if c.bypass_after_days is not None:
            bypass_days = (
                c.bypass_after_days
                if bypass_days is None
                else min(bypass_days, c.bypass_after_days)
            )
    candidates_days = [d for d in (repair_days, bypass_days) if d is not None]
    return min(candidates_days) if candidates_days else 0


def _point_loss_prob(
    scenario: RiskScenario, feeding: frozenset[str], factors: dict[str, float]
) -> float:
    survive = 1.0
    # 排序固定迭代顺序，保证浮点结果跨进程可重复
    for asset_id in sorted(feeding):
        p = scenario.failure_prob(asset_id) * factors.get(asset_id, 1.0)
        p = min(1.0, max(0.0, p))
        survive *= 1.0 - p
    return 1.0 - survive


def annual_impacts(
    data: PlanningData,
    network: Network,
    selected: frozenset[str],
) -> tuple[float, float, dict[str, float]]:
    """返回 (年期望受影响人口, 年期望停电人口日, 各避险点失电概率)。"""
    factors = _hardened_factors(selected, {c.project_id: c for c in data.candidates})
    points = {p.point_id: p for p in data.service_points}
    total_risk = 0.0
    total_person_days = 0.0
    loss_probs: dict[str, float] = {}
    for point_id in sorted(points):
        point = points[point_id]
        feeding = network.feeding_set(point_id)
        pop_weight = point.population * point.criticality
        restore_days = max(
            (_restoration_days(data, a, selected) for a in sorted(feeding)),
            default=0,
        )
        point_risk = 0.0
        for scenario in data.scenarios:
            loss = _point_loss_prob(scenario, feeding, factors)
            point_risk += scenario.frequency_per_year * loss
        loss_probs[point_id] = point_risk
        total_risk += pop_weight * point_risk
        total_person_days += pop_weight * point_risk * restore_days
    return total_risk, total_person_days, loss_probs


def score_portfolio(
    data: PlanningData,
    network: Network,
    weights: RiskWeights,
    selected: frozenset[str],
) -> tuple[float, float]:
    risk, person_days, _ = annual_impacts(data, network, selected)
    return risk, person_days


def rank_candidates(
    data: PlanningData,
    network: Network,
    weights: RiskWeights | None = None,
) -> PortfolioResult:
    """计算全部候选的可解释优先级，并在预算/工期/前置约束下贪心组合。"""
    weights = weights or RiskWeights()
    candidates = {c.project_id: c for c in data.candidates}
    points = {p.point_id: p for p in data.service_points}
    total_pop = _weighted_population(points) or 1.0
    w_sum = weights.risk + weights.restoration + weights.dependency or 1.0

    baseline_risk, baseline_days, _ = annual_impacts(data, network, frozenset())

    # 每个候选单独加入时的边际效益，作为内在优先级
    marginal: dict[str, tuple[float, float]] = {}
    for c in data.candidates:
        risk, days, _ = annual_impacts(data, network, frozenset({c.project_id}))
        marginal[c.project_id] = (baseline_risk - risk, baseline_days - days)

    scores: dict[str, CandidateScore] = {}
    for c in data.candidates:
        exposed = sum(
            points[pid].population * points[pid].criticality
            for pid in network.affected_points(c.asset_id)
            if pid in points
        )
        d_risk, d_restore = marginal[c.project_id]
        contrib = {
            "risk_share": d_risk / baseline_risk if baseline_risk else 0.0,
            "restoration_share": d_restore / baseline_days if baseline_days else 0.0,
            "dependency_share": exposed / total_pop,
        }
        benefit = (
            weights.risk * contrib["risk_share"]
            + weights.restoration * contrib["restoration_share"]
            + weights.dependency * contrib["dependency_share"]
        ) / w_sum
        priority = round(100.0 * benefit, 2)
        cost_value = float(c.cost) if c.cost > 0 else 0.01
        scores[c.project_id] = CandidateScore(
            project_id=c.project_id,
            asset_id=c.asset_id,
            name=c.name or c.project_id,
            priority_score=priority,
            benefit_cost_ratio=round(priority / cost_value, 4),
            risk_reduction=round(d_risk, 3),
            restoration_reduction=round(d_restore, 3),
            exposed_population=round(exposed, 3),
            contributions={k: round(v, 6) for k, v in contrib.items()},
        )

    selected, schedule, reasons, warnings = _greedy_select(
        data, candidates, scores, network, weights, baseline_risk, baseline_days
    )
    for pid, score in scores.items():
        blockers = tuple(
            pre for pre in candidates[pid].prerequisite_ids if pre not in selected
        )
        score.blockers = blockers
        if pid in schedule:
            start, finish, team = schedule[pid]
            score.scheduled_start, score.scheduled_finish, score.team = start, finish, team
        if pid not in selected:
            score.not_selected_reason = reasons.get(pid, "效益费用比低于入选项目")

    portfolio_risk, portfolio_days, _ = annual_impacts(
        data, network, frozenset(selected)
    )
    total_cost = sum(
        (candidates[pid].cost for pid in selected), Decimal("0")
    )
    rankings = sorted(
        scores.values(),
        key=lambda s: (
            s.project_id not in selected,
            s.scheduled_start if s.scheduled_start is not None else 0,
            -s.priority_score,
            s.project_id,
        ),
    )
    return PortfolioResult(
        rankings=rankings,
        selected=list(selected),
        baseline_annual_risk=round(baseline_risk, 3),
        baseline_annual_person_days=round(baseline_days, 3),
        portfolio_risk=round(portfolio_risk, 3),
        portfolio_person_days=round(portfolio_days, 3),
        total_cost=total_cost,
        weights=weights.as_dict(),
        warnings=tuple(warnings),
    )


def _greedy_select(
    data: PlanningData,
    candidates: dict[str, RetrofitCandidate],
    scores: dict[str, CandidateScore],
    network: Network,
    weights: RiskWeights,
    baseline_risk: float,
    baseline_days: float,
):
    """按边际效益费用比贪心；每轮重新计算边际效益（上游已加固会稀释下游效益）。"""
    selected: list[str] = []
    chosen = frozenset()
    spent = Decimal("0")
    reasons: dict[str, str] = {}
    warnings: list[str] = []
    remaining = set(candidates)

    cur_risk, cur_days = baseline_risk, baseline_days

    while remaining:
        feasible = []
        for pid in remaining:
            c = candidates[pid]
            unmet = [p for p in c.prerequisite_ids if p not in chosen]
            if unmet:
                # 每轮覆盖：前置可能在本轮之前刚入选
                reasons[pid] = f"等待前置工程: {', '.join(sorted(unmet))}"
                continue
            if spent + c.cost > data.resources.budget:
                reasons[pid] = f"超出年度预算（剩余 {data.resources.budget - spent}）"
                continue
            risk, days, _ = annual_impacts(data, network, chosen | {pid})
            marginal_benefit = (
                weights.risk * (cur_risk - risk) / (baseline_risk or 1.0)
                + weights.restoration * (cur_days - days) / (baseline_days or 1.0)
            )
            bcr = marginal_benefit / (float(c.cost) or 0.01)
            feasible.append((bcr, pid, risk, days))

        if not feasible:
            for pid in remaining:
                reasons.setdefault(pid, "受前置条件或资源约束阻塞")
            break

        feasible.sort(key=lambda x: (-x[0], x[1]))
        bcr, pid, new_risk, new_days = feasible[0]
        if bcr <= 0:
            for _, p, _, _ in feasible:
                reasons.setdefault(p, "边际风险改善为零")
            break

        tentative = selected + [pid]
        schedule = schedule_projects(
            [candidates[p] for p in tentative], data.resources.crew_teams
        )
        finish = schedule[pid][1]
        if finish > data.resources.horizon_days:
            reasons[pid] = (
                f"资源冲突：加入后排程完工于第 {finish} 天，"
                f"超过工期窗口 {data.resources.horizon_days} 天"
            )
            warnings.append(reasons[pid] + f"（项目 {pid}）")
            remaining.discard(pid)
            continue

        selected.append(pid)
        chosen = frozenset(selected)
        spent += candidates[pid].cost
        cur_risk, cur_days = new_risk, new_days
        remaining.discard(pid)

    schedule = schedule_projects(
        [candidates[p] for p in selected], data.resources.crew_teams
    )
    return selected, schedule, reasons, warnings


def schedule_projects(
    projects: list[RetrofitCandidate],
    crew_teams: int,
    delays: dict[str, int] | None = None,
    in_progress: dict[str, tuple[int, int, int]] | None = None,
) -> dict[str, tuple[int, int, int]]:
    """列表调度：多支施工队并行，遵守前置工程完工时间。

    ``delays[p]``：已开工项目宣布延期后的实际完工日。
    ``in_progress[p] = (start, finish, team)``：已在执行的项目，占用其施工队到完工日。
    返回 {project_id: (start, finish, team)}，完工日含工期。
    """
    delays = delays or {}
    in_progress = dict(in_progress or {})
    by_id = {p.project_id: p for p in projects}
    team_free = [0] * max(1, crew_teams)
    result: dict[str, tuple[int, int, int]] = {}

    for pid, (start, finish, team) in in_progress.items():
        if pid in by_id:
            actual_finish = delays.get(pid, finish)
            result[pid] = (start, actual_finish, team)
            team_free[team] = max(team_free[team], actual_finish)

    # 输入顺序即选择优先级（贪心入选顺序）
    order = {p.project_id: i for i, p in enumerate(projects)}
    remaining = {p.project_id: p for p in projects if p.project_id not in result}

    # 列表调度：每轮在「前置已排定」的就绪项目里，挑最早能开工的，
    # 分配给最早空闲的施工队——未就绪（前置未完工）的项目不会霸占空闲队
    while remaining:
        choices = []
        for pid, project in remaining.items():
            prereq_finish = [
                result[pre][1] for pre in project.prerequisite_ids if pre in result
            ]
            if len(prereq_finish) != len(project.prerequisite_ids):
                continue
            earliest = max(prereq_finish, default=0)
            team = min(range(len(team_free)), key=lambda t: team_free[t])
            start = max(team_free[team], earliest)
            choices.append((start, order[pid], project, team, earliest))
        if not choices:
            # 前置缺失（理论上由数据校验拦截）；剩余项目无法排程
            break
        choices.sort(key=lambda x: (x[0], x[1]))
        start, _, project, team, _ = choices[0]
        finish = start + project.crew_days
        result[project.project_id] = (start, finish, team)
        team_free[team] = finish
        del remaining[project.project_id]
    return result
