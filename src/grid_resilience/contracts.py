"""资产依赖和韧性工程输入结构。"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GridAsset:
    asset_id: str
    asset_kind: str
    serves: tuple[str, ...]
    upstream_asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetrofitCandidate:
    project_id: str
    asset_id: str
    cost: Decimal
    crew_days: int
    prerequisite_ids: tuple[str, ...]
