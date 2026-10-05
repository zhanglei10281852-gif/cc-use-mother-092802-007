"""电网拓扑：连带供电范围与失效传播。

上游方向定义为供电方向（asset.upstream_asset_ids 直接或间接为其供电）。
任一上游资产失效，都会切断该资产及其全部下游资产的供电点。
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from .contracts import GridAsset


class TopologyError(ValueError):
    """资产关系非法（悬空引用或依赖成环）。"""


@dataclass(frozen=True)
class Network:
    assets: Mapping[str, GridAsset]

    @classmethod
    def build(cls, assets: Sequence[GridAsset]) -> "Network":
        by_id: dict[str, GridAsset] = {}
        for asset in assets:
            if asset.asset_id in by_id:
                raise TopologyError(f"资产重复定义: {asset.asset_id}")
            by_id[asset.asset_id] = asset
        for asset in assets:
            for up in asset.upstream_asset_ids:
                if up not in by_id:
                    raise TopologyError(
                        f"资产 {asset.asset_id} 引用了不存在的上游资产 {up}"
                    )
        network = cls(assets=by_id)
        network._reject_cycles()
        return network

    def _reject_cycles(self) -> None:
        # 在“供电方向”（上游 -> 下游）图上做拓扑排序检测环。
        indegree: dict[str, int] = {a: 0 for a in self.assets}
        children: dict[str, list[str]] = defaultdict(list)
        for asset in self.assets.values():
            for up in asset.upstream_asset_ids:
                children[up].append(asset.asset_id)
                indegree[asset.asset_id] += 1
        queue = deque(a for a, d in indegree.items() if d == 0)
        seen = 0
        while queue:
            node = queue.popleft()
            seen += 1
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if seen != len(self.assets):
            raise TopologyError("资产上游关系存在环，无法定义供电方向")

    def downstream(self, asset_id: str) -> frozenset[str]:
        """返回资产失效后会失电的全部资产（含自身）。"""
        if asset_id not in self.assets:
            raise TopologyError(f"未知资产: {asset_id}")
        result: set[str] = {asset_id}
        stack = [asset_id]
        while stack:
            current = stack.pop()
            for other in self.assets.values():
                if other.asset_id in result:
                    continue
                if any(up in result for up in other.upstream_asset_ids):
                    result.add(other.asset_id)
                    stack.append(other.asset_id)
        return frozenset(result)

    def upstream_closure(self, asset_id: str) -> frozenset[str]:
        """返回向该资产供电的全部上游资产（含自身）。"""
        result = {asset_id}
        stack = [asset_id]
        while stack:
            current = stack.pop()
            for up in self.assets[current].upstream_asset_ids:
                if up not in result:
                    result.add(up)
                    stack.append(up)
        return frozenset(result)

    def served_points(
        self, population: Mapping[str, int], asset_ids: frozenset[str]
    ) -> tuple[frozenset[str], int]:
        """资产集合覆盖的服务点及服务总人口。"""
        points: set[str] = set()
        for asset_id in asset_ids:
            points.update(self.assets[asset_id].serves)
        total = sum(int(population.get(p, 0)) for p in points)
        return frozenset(points), total

    def failure_probability(
        self,
        root_asset_id: str,
        asset_fail_prob: Mapping[str, Decimal],
    ) -> Decimal:
        """根资产失效时，其下游失电事件的发生概率（含上游传导）。

        上游任一资产失效都会导致根资产失电，取串联近似：
        P(失电) = 1 - ∏(1 - p)。
        """
        affected = self.upstream_closure(root_asset_id)
        survive = Decimal("1")
        for asset_id in affected:
            p = Decimal(asset_fail_prob.get(asset_id, 0))
            survive *= Decimal("1") - p
        return Decimal("1") - survive
