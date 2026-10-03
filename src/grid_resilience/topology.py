"""供电网络拓扑：上游依赖、级联断电范围与环检测。"""

from collections.abc import Iterable

from .contracts import GridAsset
from .errors import CyclicDependency, UnknownReference


class Network:
    """资产供电网。

    资产 ``a`` 的 ``upstream_asset_ids`` 指向为其送电的上游资产；
    任一上游（或上游的上游）失效都会连带切断 ``a`` 及其避险点。
    """

    def __init__(self, assets: Iterable[GridAsset]):
        self.assets: dict[str, GridAsset] = {}
        self.upstream: dict[str, frozenset[str]] = {}
        self.children: dict[str, set[str]] = {}

        for asset in assets:
            if asset.asset_id in self.assets:
                raise UnknownReference(f"资产 id 重复: {asset.asset_id}")
            self.assets[asset.asset_id] = asset

        for asset in self.assets.values():
            for up in asset.upstream_asset_ids:
                if up not in self.assets:
                    raise UnknownReference(
                        f"资产 {asset.asset_id} 引用了不存在的上游资产 {up}"
                    )
            self.upstream[asset.asset_id] = frozenset(asset.upstream_asset_ids)
            self.children.setdefault(asset.asset_id, set())
        for asset in self.assets.values():
            for up in asset.upstream_asset_ids:
                self.children[up].add(asset.asset_id)

        self._ancestors: dict[str, frozenset[str]] = {}
        for asset_id in self.assets:
            self._ancestors_for(asset_id, ())
        self._downstream: dict[str, frozenset[str]] = {
            asset_id: frozenset(
                a for a in self.assets if asset_id in self._ancestors[a] or a == asset_id
            )
            for asset_id in self.assets
        }

    def _ancestors_for(self, asset_id: str, stack: tuple[str, ...]) -> frozenset[str]:
        if asset_id in self._ancestors:
            return self._ancestors[asset_id]
        if asset_id in stack:
            raise CyclicDependency(
                f"供电上游关系存在环: {' -> '.join((*stack, asset_id))}"
            )
        result: set[str] = set()
        for up in self.upstream.get(asset_id, frozenset()):
            result.add(up)
            result |= self._ancestors_for(up, (*stack, asset_id))
        self._ancestors[asset_id] = frozenset(result)
        return self._ancestors[asset_id]

    def ancestors(self, asset_id: str) -> frozenset[str]:
        """所有（传递的）上游资产。"""
        return self._ancestors[asset_id]

    def downstream(self, asset_id: str) -> frozenset[str]:
        """该资产失效后连带失电的资产（含自身）。"""
        return self._downstream[asset_id]

    def feeding_set(self, point_id: str) -> frozenset[str]:
        """切断该服务点所需的最小资产集合：直接供电资产及其全部上游。"""
        served_by = [
            aid
            for aid, asset in self.assets.items()
            if point_id in asset.serves
        ]
        result: set[str] = set(served_by)
        for aid in served_by:
            result |= self.ancestors(aid)
        return frozenset(result)

    def affected_points(self, asset_id: str) -> tuple[str, ...]:
        """资产失效后被连带切断的全部服务点。"""
        points: set[str] = set()
        for aid in self.downstream(asset_id):
            points.update(self.assets[aid].serves)
        return tuple(sorted(points))


def detect_prerequisite_cycle(project_ids: Iterable[str], prereqs: dict[str, set[str]]):
    """工程前置关系环检测。"""
    color: dict[str, int] = {pid: 0 for pid in project_ids}

    def visit(pid: str, stack: tuple[str, ...]):
        if color.get(pid, 2) == 2:
            return
        if color[pid] == 1:
            raise CyclicDependency(
                f"工程前置关系存在环: {' -> '.join((*stack, pid))}"
            )
        color[pid] = 1
        for pre in prereqs.get(pid, ()):  # 缺失引用在数据校验阶段处理
            visit(pre, (*stack, pid))
        color[pid] = 2

    for pid in project_ids:
        visit(pid, ())
