"""输入数据完整性校验。"""

from decimal import Decimal

from .contracts import PlanningData
from .errors import CyclicDependency, InvalidPlanningData, UnknownReference
from .topology import Network, detect_prerequisite_cycle


def validate(data: PlanningData) -> None:
    asset_ids: set[str] = set()
    for asset in data.assets:
        if not asset.asset_id:
            raise InvalidPlanningData("资产 id 不能为空")
        if asset.asset_id in asset_ids:
            raise InvalidPlanningData(f"资产 id 重复: {asset.asset_id}")
        asset_ids.add(asset.asset_id)

    point_ids: set[str] = set()
    for point in data.service_points:
        if point.point_id in point_ids:
            raise InvalidPlanningData(f"服务点 id 重复: {point.point_id}")
        if point.population < 0:
            raise InvalidPlanningData(f"服务点 {point.point_id} 人口不能为负")
        if not 0 <= point.criticality:
            raise InvalidPlanningData(f"服务点 {point.point_id} 重要度不能为负")
        point_ids.add(point.point_id)

    for asset in data.assets:
        for pid in asset.serves:
            if pid not in point_ids:
                raise UnknownReference(
                    f"资产 {asset.asset_id} 服务了不存在的避险点 {pid}"
                )

    # 拓扑构造本身会校验上游引用并抛出环异常
    Network(data.assets)

    scenario_ids: set[str] = set()
    for scenario in data.scenarios:
        if scenario.scenario_id in scenario_ids:
            raise InvalidPlanningData(f"情景 id 重复: {scenario.scenario_id}")
        scenario_ids.add(scenario.scenario_id)
        if scenario.frequency_per_year < 0:
            raise InvalidPlanningData(f"情景 {scenario.scenario_id} 年频率不能为负")
        if not 0.0 <= scenario.default_failure_prob <= 1.0:
            raise InvalidPlanningData(
                f"情景 {scenario.scenario_id} 默认失效概率超出 [0,1]"
            )
        for aid, prob in scenario.failure_probs:
            if aid not in asset_ids:
                raise UnknownReference(
                    f"情景 {scenario.scenario_id} 引用了不存在的资产 {aid}"
                )
            if not 0.0 <= prob <= 1.0:
                raise InvalidPlanningData(
                    f"情景 {scenario.scenario_id} 中资产 {aid} 失效概率超出 [0,1]"
                )

    for attr in ("repair_days", "bypass_days"):
        for aid, days in getattr(data.repair, attr):
            if aid not in asset_ids:
                raise UnknownReference(f"修复资源引用了不存在的资产 {aid}")
            if days < 0:
                raise InvalidPlanningData(f"资产 {aid} 的修复工期不能为负")

    project_ids: set[str] = set()
    prereqs: dict[str, set[str]] = {}
    for candidate in data.candidates:
        if candidate.project_id in project_ids:
            raise InvalidPlanningData(f"候选项目 id 重复: {candidate.project_id}")
        project_ids.add(candidate.project_id)
        if candidate.asset_id not in asset_ids:
            raise UnknownReference(
                f"项目 {candidate.project_id} 针对不存在的资产 {candidate.asset_id}"
            )
        if candidate.cost < Decimal("0"):
            raise InvalidPlanningData(f"项目 {candidate.project_id} 造价不能为负")
        if candidate.crew_days < 0:
            raise InvalidPlanningData(f"项目 {candidate.project_id} 工期不能为负")
        if not 0.0 <= candidate.residual_failure_factor <= 1.0:
            raise InvalidPlanningData(
                f"项目 {candidate.project_id} 残存失效系数应在 [0,1]"
            )
        prereqs[candidate.project_id] = set(candidate.prerequisite_ids)

    for pid, pre_set in prereqs.items():
        for pre in pre_set:
            if pre not in project_ids:
                raise UnknownReference(
                    f"项目 {pid} 的前置工程 {pre} 不存在"
                )
            if pre == pid:
                raise CyclicDependency(f"项目 {pid} 不能以前置自身")
    detect_prerequisite_cycle(project_ids, prereqs)

    if data.resources.crew_teams <= 0:
        raise InvalidPlanningData("施工队数量必须为正")
    if data.resources.horizon_days <= 0:
        raise InvalidPlanningData("工期窗口必须为正")
    if data.resources.budget < Decimal("0"):
        raise InvalidPlanningData("年度预算不能为负")
