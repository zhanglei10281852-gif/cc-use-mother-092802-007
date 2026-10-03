"""计划版本管理：草稿、审批冻结、审批回退与施工延期重评。

版本规则：
- 计划（plan_id）下的每个版本一经审批即不可变：数据快照、权重、入选组合、
  内容指纹一并冻结。事后新增的资产关系只进入新版本，无法倒改旧版本。
- 审批回退把已审批版本标记为 withdrawn，并以其快照为父本开出新草稿版本。
- 施工延期不改变已冻结的入选组合与权重，而是在该审批版本下生成 revision：
  重新排程并标记超出工期窗口的受影响下游项目。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .contracts import PlanningData
from .errors import InvalidTransition, NotFoundError, UnknownReference
from .scoring import (
    PortfolioResult,
    RiskWeights,
    rank_candidates,
    schedule_projects,
)
from .serialization import content_hash
from .topology import Network
from .validation import validate

DRAFT = "draft"
APPROVED = "approved"
WITHDRAWN = "withdrawn"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DelayRevision:
    revision_no: int
    delayed_project: str
    new_finish_day: int
    affected_downstream: tuple[str, ...]
    schedule: dict[str, tuple[int, int, int]]
    warnings: tuple[str, ...]
    recorded_at: str


@dataclass
class PlanVersion:
    plan_id: str
    version: int
    status: str
    parent_version: int | None
    data: PlanningData
    weights: RiskWeights
    content_hash: str
    result: PortfolioResult
    created_at: str
    approved_at: str | None = None
    revisions: list[DelayRevision] = field(default_factory=list)

    @property
    def schedule(self) -> dict[str, tuple[int, int, int]]:
        if self.revisions:
            return self.revisions[-1].schedule
        return {
            s.project_id: (s.scheduled_start, s.scheduled_finish, s.team)
            for s in self.result.rankings
            if s.project_id in self.result.selected
            and s.scheduled_start is not None
        }


def evaluate(data: PlanningData, weights: RiskWeights | None = None) -> PortfolioResult:
    """无副作用试算：用于规划人员调整权重或比较情景。"""
    weights = weights or RiskWeights()
    validate(data)
    return rank_candidates(data, Network(data.assets), weights)


class PlanningStore:
    def __init__(self):
        self._plans: dict[str, list[PlanVersion]] = {}

    # ---------- 版本存取 ----------

    def list_plans(self) -> list[str]:
        return sorted(self._plans)

    def versions(self, plan_id: str) -> list[PlanVersion]:
        self._require_plan(plan_id)
        return list(self._plans[plan_id])

    def get_version(self, plan_id: str, version: int) -> PlanVersion:
        self._require_plan(plan_id)
        for v in self._plans[plan_id]:
            if v.version == version:
                return v
        raise NotFoundError(f"计划 {plan_id} 不存在版本 {version}")

    def latest(self, plan_id: str) -> PlanVersion:
        return self.versions(plan_id)[-1]

    def _require_plan(self, plan_id: str):
        if plan_id not in self._plans:
            raise NotFoundError(f"计划不存在: {plan_id}")

    # ---------- 草稿 / 数据与权重 ----------

    def create_plan(
        self,
        plan_id: str,
        data: PlanningData,
        weights: RiskWeights | None = None,
    ) -> PlanVersion:
        if plan_id in self._plans:
            raise InvalidTransition(f"计划 {plan_id} 已存在，请改用 new_version")
        weights = weights or RiskWeights()
        record = self._build_version(plan_id, 1, None, data, weights, DRAFT)
        self._plans[plan_id] = [record]
        return record

    def new_version(
        self,
        plan_id: str,
        data: PlanningData,
        weights: RiskWeights | None = None,
    ) -> PlanVersion:
        """以最新版本为父本开出下一版草稿（如新增资产关系后）。"""
        latest = self.latest(plan_id)
        if latest.status == DRAFT:
            raise InvalidTransition(
                f"计划 {plan_id} 已有未审批草稿 v{latest.version}，"
                "请先更新草稿或完成审批"
            )
        weights = weights if weights is not None else latest.weights
        record = self._build_version(
            plan_id, latest.version + 1, latest.version, data, weights, DRAFT
        )
        self._plans[plan_id].append(record)
        return record

    def update_draft_data(self, plan_id: str, data: PlanningData) -> PlanVersion:
        """替换草稿数据（草稿阶段允许；已审批版本拒绝）。"""
        record = self._require_draft(plan_id)
        new = self._build_version(
            plan_id, record.version, record.parent_version, data, record.weights, DRAFT
        )
        self._replace_latest(plan_id, new)
        return new

    def adjust_weights(self, plan_id: str, weights: RiskWeights) -> PlanVersion:
        record = self._require_draft(plan_id)
        new = self._build_version(
            plan_id, record.version, record.parent_version, record.data, weights, DRAFT
        )
        self._replace_latest(plan_id, new)
        return new

    def compare_weights(
        self, plan_id: str, weights_options: dict[str, RiskWeights]
    ) -> dict[str, PortfolioResult]:
        """同一草稿数据下比较多套权重，不落库。"""
        record = self.latest(plan_id)
        return {
            label: rank_candidates(record.data, Network(record.data.assets), w)
            for label, w in weights_options.items()
        }

    @staticmethod
    def compare_scenarios(
        data: PlanningData,
        weights: RiskWeights | None,
        scenario_groups: dict[str, tuple[str, ...]],
    ) -> dict[str, PortfolioResult]:
        """比较不同情景组合（如冰崩 / 滑坡 / 复合灾害），不落库。"""
        weights = weights or RiskWeights()
        network = Network(data.assets)
        out: dict[str, PortfolioResult] = {}
        for label, scenario_ids in scenario_groups.items():
            wanted = set(scenario_ids)
            sub = PlanningData(
                assets=data.assets,
                service_points=data.service_points,
                scenarios=tuple(
                    s for s in data.scenarios if s.scenario_id in wanted
                ),
                repair=data.repair,
                candidates=data.candidates,
                resources=data.resources,
            )
            out[label] = rank_candidates(sub, network, weights)
        return out

    # ---------- 审批与回退 ----------

    def approve(self, plan_id: str) -> PlanVersion:
        record = self._require_draft(plan_id)
        record.status = APPROVED
        record.approved_at = _now()
        return record

    def rollback_approval(self, plan_id: str, reason: str = "") -> PlanVersion:
        """撤回审批：冻结旧版本（withdrawn），以其快照开出新草稿版本。

        新草稿沿用旧快照与权重，内容指纹相同，便于审计关联。
        """
        latest = self.latest(plan_id)
        if latest.status != APPROVED:
            raise InvalidTransition(
                f"计划 {plan_id} 最新版本 v{latest.version} 不是已审批状态，无法回退"
            )
        latest.status = WITHDRAWN
        draft = self._build_version(
            plan_id,
            latest.version + 1,
            latest.version,
            latest.data,
            latest.weights,
            DRAFT,
        )
        self._plans[plan_id].append(draft)
        return draft

    # ---------- 施工延期与下游重评 ----------

    def report_delay(
        self, plan_id: str, project_id: str, new_finish_day: int
    ) -> DelayRevision:
        record = self.latest(plan_id)
        if record.status != APPROVED:
            raise InvalidTransition(
                f"只能对已审批版本登记施工延期，当前状态 {record.status}"
            )
        candidates = {c.project_id: c for c in record.data.candidates}
        if project_id not in record.result.selected:
            raise UnknownReference(
                f"项目 {project_id} 不在已审批组合中，不能登记延期"
            )
        old_schedule = record.schedule
        if project_id not in old_schedule:
            raise UnknownReference(f"项目 {project_id} 尚未排程")
        start, old_finish, _ = old_schedule[project_id]
        if new_finish_day < start:
            raise InvalidTransition(
                f"延期完工日 {new_finish_day} 早于开工日 {start}"
            )
        if new_finish_day <= old_finish:
            raise InvalidTransition(
                f"新完工日 {new_finish_day} 未晚于原完工日 {old_finish}，不构成延期"
            )

        # 历次延期的在执行状态累积沿用，保留原施工队
        delays: dict[str, int] = {}
        in_progress: dict[str, tuple[int, int, int]] = {}
        for rev in record.revisions:
            delays[rev.delayed_project] = rev.new_finish_day
        delays[project_id] = new_finish_day
        for pid, finish_day in delays.items():
            s, f, team = old_schedule.get(pid, (0, finish_day, 0))
            in_progress[pid] = (s, f, team)

        selected_projects = [candidates[pid] for pid in record.result.selected]
        new_schedule = schedule_projects(
            selected_projects,
            record.data.resources.crew_teams,
            delays=delays,
            in_progress=in_progress,
        )

        downstream = self._downstream_projects(candidates, project_id)
        affected = tuple(
            pid
            for pid in downstream
            if pid in new_schedule
            and new_schedule[pid][:2] != old_schedule.get(pid, (None, None))[:2]
        )
        horizon = record.data.resources.horizon_days
        warnings = tuple(
            f"项目 {pid} 因上游延期改期，完工于第 {new_schedule[pid][1]} 天，"
            f"超出工期窗口 {horizon} 天"
            for pid in (*affected, project_id)
            if pid in new_schedule and new_schedule[pid][1] > horizon
        )
        revision = DelayRevision(
            revision_no=len(record.revisions) + 1,
            delayed_project=project_id,
            new_finish_day=new_finish_day,
            affected_downstream=affected,
            schedule=new_schedule,
            warnings=warnings,
            recorded_at=_now(),
        )
        record.revisions.append(revision)
        self._overlay_revised_schedule(record, new_schedule, warnings, project_id)
        return revision

    @staticmethod
    def _downstream_projects(
        candidates: dict, project_id: str
    ) -> set[str]:
        dependents: dict[str, set[str]] = {}
        for pid, c in candidates.items():
            for pre in c.prerequisite_ids:
                dependents.setdefault(pre, set()).add(pid)
        seen: set[str] = set()
        stack = [project_id]
        while stack:
            cur = stack.pop()
            for child in dependents.get(cur, ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    def _overlay_revised_schedule(
        self,
        record: PlanVersion,
        schedule: dict[str, tuple[int, int, int]],
        warnings: tuple[str, ...],
        delayed_project: str,
    ) -> None:
        """冻结数据与权重重算（结果确定性一致），再覆盖延期后的排程。"""
        replayed = rank_candidates(record.data, Network(record.data.assets), record.weights)
        for score in replayed.rankings:
            if score.project_id in schedule:
                start, finish, team = schedule[score.project_id]
                score.scheduled_start, score.scheduled_finish, score.team = (
                    start,
                    finish,
                    team,
                )
        replayed.warnings = (*replayed.warnings, *warnings)
        record.result = replayed

    # ---------- 内部 ----------

    def _build_version(
        self,
        plan_id: str,
        version: int,
        parent_version: int | None,
        data: PlanningData,
        weights: RiskWeights,
        status: str,
    ) -> PlanVersion:
        validate(data)
        result = rank_candidates(data, Network(data.assets), weights)
        return PlanVersion(
            plan_id=plan_id,
            version=version,
            status=status,
            parent_version=parent_version,
            data=data,
            weights=weights,
            content_hash=content_hash(data, weights),
            result=result,
            created_at=_now(),
        )

    def _require_draft(self, plan_id: str) -> PlanVersion:
        record = self.latest(plan_id)
        if record.status != DRAFT:
            raise InvalidTransition(
                f"计划 {plan_id} 最新版本 v{record.version} 已审批冻结，"
                "请回退审批或创建新版本"
            )
        return record

    def _replace_latest(self, plan_id: str, record: PlanVersion) -> None:
        versions = self._plans[plan_id]
        versions[-1] = record
