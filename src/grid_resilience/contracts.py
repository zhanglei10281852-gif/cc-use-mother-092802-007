"""资产依赖、风险情景与韧性工程输入结构。

该模块只描述数据，不包含排序逻辑，便于在计划版本中整体冻结。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Optional


@dataclass(frozen=True)
class ServicePoint:
    """避险点 / 供电服务点。"""

    point_id: str
    name: str
    population: int


@dataclass(frozen=True)
class GridAsset:
    """电网资产。

    upstream_asset_ids 表示向本资产供电的上游资产：任一上游失效，
    本资产及其供电点都会被连带切断。
    """

    asset_id: str
    asset_kind: str
    serves: tuple[str, ...] = ()
    upstream_asset_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskScenario:
    """山地冰崩等风险情景。

    asset_fail_prob: 情景下各资产失效概率。
    repair_time_days: 资产失效后通过抢修或替代线路恢复供电所需天数。
    """

    scenario_id: str
    name: str
    asset_fail_prob: Mapping[str, Decimal] = field(default_factory=dict)
    repair_time_days: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskWeights:
    """规划人员可调整的风险维度权重。"""

    population: Decimal = Decimal("1.0")
    restoration: Decimal = Decimal("0.5")
    cascade: Decimal = Decimal("0.2")


@dataclass(frozen=True)
class RepairResources:
    """年度可投入的修复资源。"""

    budget: Decimal
    crew_days: int


@dataclass(frozen=True)
class RetrofitCandidate:
    """候选改造项目。

    residual_fail_fraction: 改造后残余失效概率比例（加固取小值）。
    residual_repair_fraction: 改造后残余抢修时间比例（建设替代线路取小值）。
    duration_days: 工期日历天，缺省按班组工日折算。
    prerequisite_ids: 工程前置项目，必须先排序、先完工。
    """

    project_id: str
    asset_id: str
    cost: Decimal
    crew_days: int
    prerequisite_ids: tuple[str, ...] = ()
    duration_days: Optional[int] = None
    residual_fail_fraction: Decimal = Decimal("0.1")
    residual_repair_fraction: Decimal = Decimal("1.0")
    name: str = ""

    def effective_duration_days(self) -> int:
        return self.crew_days if self.duration_days is None else self.duration_days


@dataclass(frozen=True)
class ProjectProgress:
    """施工中途反馈。

    state: completed / in_progress。
    delay_days: 在途项目距完工还需的额外天数（施工延期），0 表示当前即释放班组。
    """

    project_id: str
    state: str
    delay_days: int = 0
