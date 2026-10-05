"""计划版本管理：审批即冻结数据与权重。

每个版本持有自己的输入快照（资产关系、情景、人口、资源、权重、进度），
并对快照计算内容哈希。因此：

* 已审批版本永不改变：新增资产关系、调整权重、施工延期都生成新版本；
* 同一份输入与权重永远得到同一哈希与同一排序（可重复）；
* 审批回退只是在版本历史上重新指向上一个已审批版本，旧数据原样可用。
"""

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .contracts import (
    ProjectProgress,
    RepairResources,
    RetrofitCandidate,
    RiskScenario,
    RiskWeights,
    ServicePoint,
    GridAsset,
)
from .network import Network
from .planning import Plan, build_plan
from .scoring import ProjectScore, rank_projects

DRAFT = "draft"
APPROVED = "approved"
SUPERSEDED = "superseded"
ROLLED_BACK = "rolled_back"


@dataclasses.dataclass(frozen=True)
class FrozenInputs:
    assets: tuple[GridAsset, ...]
    service_points: tuple[ServicePoint, ...]
    scenarios: tuple[RiskScenario, ...]
    candidates: tuple[RetrofitCandidate, ...]
    weights: RiskWeights
    resources: RepairResources
    progress: tuple[ProjectProgress, ...] = ()
    note: str = ""


@dataclasses.dataclass(frozen=True)
class PlanVersion:
    version_id: str
    parent_version_id: Optional[str]
    state: str
    content_hash: str
    inputs: FrozenInputs
    scores: tuple[ProjectScore, ...]
    plan: Plan
    created_at: str


class VersionError(ValueError):
    pass


def _default(obj):
    if isinstance(obj, Decimal):
        return obj.to_eng_string()
    raise TypeError(f"不可序列化的对象: {type(obj)!r}")


def content_hash(inputs: FrozenInputs) -> str:
    payload = json.dumps(
        dataclasses.asdict(inputs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate(inputs: FrozenInputs) -> tuple[tuple[ProjectScore, ...], Plan]:
    network = Network.build(list(inputs.assets))
    scores = tuple(
        rank_projects(
            network,
            list(inputs.service_points),
            list(inputs.scenarios),
            list(inputs.candidates),
            inputs.weights,
        )
    )
    plan = build_plan(
        scores,
        list(inputs.candidates),
        inputs.resources,
        list(inputs.progress),
    )
    return scores, plan


class PlanRegistry:
    """内存版本库；版本对象本身不可变。"""

    def __init__(self) -> None:
        self._versions: dict[str, PlanVersion] = {}
        self._approval_chain: list[str] = []

    @property
    def current_approved(self) -> Optional[PlanVersion]:
        if not self._approval_chain:
            return None
        return self._versions[self._approval_chain[-1]]

    def get(self, version_id: str) -> PlanVersion:
        try:
            return self._versions[version_id]
        except KeyError:
            raise VersionError(f"未知计划版本: {version_id}") from None

    def list_versions(self) -> tuple[PlanVersion, ...]:
        return tuple(self._versions.values())

    def submit(
        self,
        inputs: FrozenInputs,
        parent_version_id: Optional[str] = None,
    ) -> PlanVersion:
        if parent_version_id is None and self._approval_chain:
            parent_version_id = self._approval_chain[-1]
        if parent_version_id is not None and parent_version_id not in self._versions:
            raise VersionError(f"父版本不存在: {parent_version_id}")
        digest = content_hash(inputs)
        if digest in {v.content_hash for v in self._versions.values()}:
            existing = next(v for v in self._versions.values() if v.content_hash == digest)
            return existing
        scores, plan = evaluate(inputs)
        version = PlanVersion(
            version_id=f"v-{digest[:10]}",
            parent_version_id=parent_version_id,
            state=DRAFT,
            content_hash=digest,
            inputs=inputs,
            scores=scores,
            plan=plan,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._versions[version.version_id] = version
        return version

    def approve(self, version_id: str) -> PlanVersion:
        version = self.get(version_id)
        if version.state == APPROVED:
            return version
        if version.state != DRAFT:
            raise VersionError(
                f"版本 {version_id} 状态为 {version.state}，不能审批（已冻结版本不可改）"
            )
        previous = None
        if self._approval_chain:
            current_id = self._approval_chain[-1]
            previous = dataclasses.replace(self._versions[current_id], state=SUPERSEDED)
            self._versions[current_id] = previous
        approved = dataclasses.replace(version, state=APPROVED)
        self._versions[version_id] = approved
        self._approval_chain.append(version_id)
        return approved

    def rollback(self) -> PlanVersion:
        """撤销最近一次审批，重新启用上一个已审批版本。"""
        if len(self._approval_chain) < 1:
            raise VersionError("没有可回退的审批")
        latest_id = self._approval_chain.pop()
        rolled_back = dataclasses.replace(self._versions[latest_id], state=ROLLED_BACK)
        self._versions[latest_id] = rolled_back
        if self._approval_chain:
            previous_id = self._approval_chain[-1]
            restored = dataclasses.replace(self._versions[previous_id], state=APPROVED)
            self._versions[previous_id] = restored
            return restored
        return rolled_back

    def approval_history(self) -> tuple[PlanVersion, ...]:
        return tuple(self._versions[v] for v in self._approval_chain)
