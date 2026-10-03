"""资产依赖和韧性工程输入结构。"""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class GridAsset:
    asset_id: str
    asset_kind: str
    serves: tuple[str, ...]
    upstream_asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class ServicePoint:
    """避险点 / 供电服务对象及服务人口。"""

    point_id: str
    name: str
    population: int
    criticality: float = 1.0


@dataclass(frozen=True)
class RiskScenario:
    """风险情景：年发生频率及各资产在该情景下的失效概率。"""

    scenario_id: str
    name: str
    frequency_per_year: float
    failure_probs: tuple[tuple[str, float], ...] = ()
    default_failure_prob: float = 0.0

    def failure_prob(self, asset_id: str) -> float:
        return dict(self.failure_probs).get(asset_id, self.default_failure_prob)


@dataclass(frozen=True)
class RepairResources:
    """修复资源：完全修复工期与替代线路（旁路）恢复工期（天）。"""

    repair_days: tuple[tuple[str, int], ...]
    bypass_days: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class WorkBudget:
    """年度投资与施工资源约束。"""

    budget: Decimal
    crew_teams: int
    horizon_days: int


@dataclass(frozen=True)
class RetrofitCandidate:
    project_id: str
    asset_id: str
    cost: Decimal
    crew_days: int
    prerequisite_ids: tuple[str, ...]
    name: str = ""
    # 加固后该资产失效概率的残存系数（0.1 表示降至原来的 10%）
    residual_failure_factor: float = 1.0
    # 改造后新增替代线路，旁路恢复所需天数；None 表示不新增
    bypass_after_days: int | None = None
    # 改造使完全修复工期缩短的天数
    repair_days_reduction: int = 0


@dataclass(frozen=True)
class PlanningData:
    """一次排序所依赖的完整输入集合。"""

    assets: tuple[GridAsset, ...]
    service_points: tuple[ServicePoint, ...]
    scenarios: tuple[RiskScenario, ...]
    repair: RepairResources
    candidates: tuple[RetrofitCandidate, ...]
    resources: WorkBudget
