"""资源受限下的年度计划编排与施工中途重排。

单施工班组串行施工；项目必须等前置项目完工后才能开工。
已完工/在途项目占用已批预算与工日；在途项目延期会推迟其全部
下游项目的开工时间，触发受影响下游项目的重新评估。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from .contracts import ProjectProgress, RepairResources, RetrofitCandidate
from .scoring import ProjectScore

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BlockedCandidate:
    project_id: str
    score_rank: int
    reason: str  # prerequisite | resource | prerequisite_and_resource
    blocking_project_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanItem:
    project_id: str
    score_rank: int
    score: Decimal
    order_index: int
    cumulative_budget: Decimal
    cumulative_crew_days: int
    start_day: int
    finish_day: int
    state: str  # completed | in_progress | scheduled
    delay_impact_days: int = 0  # 因在途项目延期而额外等待的天数


@dataclass(frozen=True)
class Plan:
    items: tuple[PlanItem, ...]
    blocked: tuple[BlockedCandidate, ...]
    budget_used: Decimal
    crew_days_used: int
    horizon_days: int
    replan_reason: Optional[str] = None

    def find(self, project_id: str) -> Optional[PlanItem]:
        for item in self.items:
            if item.project_id == project_id:
                return item
        return None


@dataclass(frozen=True)
class _Committed:
    project_id: str
    finish_day: int
    cost: Decimal
    crew_days: int
    state: str


def _downstream_projects(candidates: Sequence[RetrofitCandidate]) -> Mapping[str, set[str]]:
    """前置项目 -> 直接或间接依赖它的项目。"""
    direct: dict[str, list[str]] = {c.project_id: [] for c in candidates}
    for c in candidates:
        for prereq in c.prerequisite_ids:
            if prereq in direct:
                direct[prereq].append(c.project_id)
    closure: dict[str, set[str]] = {}
    for pid in direct:
        seen: set[str] = set()
        stack = list(direct[pid])
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(direct.get(cur, ()))
        closure[pid] = seen
    return closure


def build_plan(
    scores: Sequence[ProjectScore],
    candidates: Sequence[RetrofitCandidate],
    resources: RepairResources,
    progress: Sequence[ProjectProgress] = (),
) -> Plan:
    by_id = {c.project_id: c for c in candidates}
    score_by_id = {s.project_id: s for s in scores}
    rank_by_id = {s.project_id: s.rank for s in scores}
    downstream = _downstream_projects(candidates)

    progress_by_id = {p.project_id: p for p in progress}
    unknown = set(progress_by_id) - set(by_id)
    if unknown:
        raise ValueError(f"施工进度引用了未知项目: {sorted(unknown)}")

    # 已锁定（完工/在途）的项目：占用资源、满足前置条件。
    committed: dict[str, _Committed] = {}
    budget_used = _ZERO
    crew_used = 0
    crew_free_day = 0
    for pid, p in progress_by_id.items():
        candidate = by_id[pid]
        if p.state == "completed":
            committed[pid] = _Committed(pid, 0, candidate.cost, candidate.crew_days, "completed")
        elif p.state == "in_progress":
            finish = max(0, p.delay_days)
            committed[pid] = _Committed(pid, finish, candidate.cost, candidate.crew_days, "in_progress")
            crew_free_day = max(crew_free_day, finish)
        else:
            raise ValueError(f"项目 {pid} 的施工状态非法: {p.state}")
        budget_used += Decimal(candidate.cost)
        crew_used += candidate.crew_days

    if budget_used > resources.budget or crew_used > resources.crew_days:
        raise ValueError("已完工/在途项目超出年度资源，无法编排计划")

    # 延期的在途项目：受影响的下游项目。
    delayed = {
        pid: p.delay_days
        for pid, p in progress_by_id.items()
        if p.state == "in_progress" and p.delay_days > 0
    }
    delay_impact: dict[str, int] = {}
    for pid, days in delayed.items():
        for down in downstream.get(pid, ()):  # type: ignore[arg-type]
            delay_impact[down] = max(delay_impact.get(down, 0), days)

    remaining_budget = resources.budget - budget_used
    remaining_crew = resources.crew_days - crew_used

    ranked_ids = [s.project_id for s in scores if s.project_id not in committed]
    selected: list[str] = []
    selected_set: set[str] = set()

    def prereqs_ok(pid: str) -> bool:
        return all(
            pre in committed or pre in selected_set
            for pre in by_id[pid].prerequisite_ids
        )

    def fits(pid: str, spent_budget: Decimal, spent_crew: int) -> bool:
        c = by_id[pid]
        return spent_budget + c.cost <= remaining_budget and spent_crew + c.crew_days <= remaining_crew

    # 按分数顺序反复选取“前置已满足且资源可容纳”的最高分项目；
    # 前置可能排名更低，需要多轮扫描让其先入列。
    pending = set(ranked_ids)
    spent_budget = _ZERO
    spent_crew = 0
    while pending:
        choice: Optional[str] = None
        for pid in ranked_ids:
            if pid not in pending:
                continue
            if prereqs_ok(pid) and fits(pid, spent_budget, spent_crew):
                choice = pid
                break
        if choice is None:
            break
        c = by_id[choice]
        spent_budget += Decimal(c.cost)
        spent_crew += c.crew_days
        selected.append(choice)
        selected_set.add(choice)
        pending.remove(choice)

    # 未入选项目给出阻塞原因。
    blocked: list[BlockedCandidate] = []
    for pid in ranked_ids:
        if pid in selected_set:
            continue
        missing_pre = tuple(
            pre for pre in by_id[pid].prerequisite_ids
            if pre not in committed and pre not in selected_set
        )
        no_resource = not fits(pid, spent_budget, spent_crew)
        if missing_pre and no_resource:
            reason = "prerequisite_and_resource"
        elif missing_pre:
            reason = "prerequisite"
        else:
            reason = "resource"
        blocked.append(
            BlockedCandidate(
                project_id=pid,
                score_rank=rank_by_id.get(pid, 0),
                reason=reason,
                blocking_project_ids=missing_pre,
            )
        )
    blocked.sort(key=lambda b: b.score_rank)

    # 时间轴：串行施工，开工不得早于前置完工。
    finish_day_of: dict[str, int] = {pid: c.finish_day for pid, c in committed.items()}
    items: list[PlanItem] = []

    def committed_item(pid: str, c: _Committed, order_index: int) -> PlanItem:
        score = score_by_id[pid].score if pid in score_by_id else _ZERO
        return PlanItem(
            project_id=pid,
            score_rank=rank_by_id.get(pid, 0),
            score=score,
            order_index=order_index,
            cumulative_budget=_ZERO,
            cumulative_crew_days=0,
            start_day=0,
            finish_day=c.finish_day,
            state=c.state,
        )

    # 已锁定项排在最前（完工早于在途），不参与累计资源展示（资源已实际占用）。
    locked = sorted(committed.values(), key=lambda c: (c.state != "completed", c.finish_day))
    items.extend(
        committed_item(c.project_id, c, i + 1) for i, c in enumerate(locked)
    )
    locked_count = len(items)

    cursor = crew_free_day
    cum_budget = budget_used
    cum_crew = crew_used
    horizon = crew_free_day
    for index, pid in enumerate(selected, start=locked_count + 1):
        candidate = by_id[pid]
        prereq_finish = max(
            (finish_day_of[pre] for pre in candidate.prerequisite_ids if pre in finish_day_of),
            default=0,
        )
        start = max(cursor, prereq_finish)
        finish = start + candidate.effective_duration_days()
        cum_budget += Decimal(candidate.cost)
        cum_crew += candidate.crew_days
        items.append(
            PlanItem(
                project_id=pid,
                score_rank=rank_by_id[pid],
                score=score_by_id[pid].score,
                order_index=index,
                cumulative_budget=cum_budget,
                cumulative_crew_days=cum_crew,
                start_day=start,
                finish_day=finish,
                state="scheduled",
                delay_impact_days=delay_impact.get(pid, 0),
            )
        )
        finish_day_of[pid] = finish
        cursor = finish
        horizon = max(horizon, finish)

    reason = None
    if delayed:
        who = ", ".join(sorted(delayed))
        reason = f"在途项目延期触发重排: {who}"

    return Plan(
        items=tuple(items),
        blocked=tuple(blocked),
        budget_used=cum_budget,
        crew_days_used=cum_crew,
        horizon_days=horizon,
        replan_reason=reason,
    )
