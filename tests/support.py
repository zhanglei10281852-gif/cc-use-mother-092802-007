"""共享测试数据工厂。"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from grid_resilience.contracts import (
    GridAsset,
    PlanningData,
    RepairResources,
    RetrofitCandidate,
    RiskScenario,
    ServicePoint,
    WorkBudget,
)
from grid_resilience.scoring import RiskWeights


def make_assets():
    """line-7 -> line-9 -> sub-1 (shelter-1/2)
                          -> sub-2 (shelter-3) -> radial-3 (shelter-4)"""
    return (
        GridAsset("line-7", "line", (), ()),
        GridAsset("line-9", "line", (), ("line-7",)),
        GridAsset("sub-1", "substation", ("shelter-1", "shelter-2"), ("line-9",)),
        GridAsset("sub-2", "substation", ("shelter-3",), ("line-9",)),
        GridAsset("radial-3", "feeder", ("shelter-4",), ("sub-2",)),
    )


def make_points():
    return (
        ServicePoint("shelter-1", "避险点1", 1000, 1.0),
        ServicePoint("shelter-2", "避险点2", 500, 1.0),
        ServicePoint("shelter-3", "避险点3", 2000, 2.0),
        ServicePoint("shelter-4", "避险点4", 300, 1.0),
    )


def make_scenarios():
    return (
        RiskScenario(
            "ice-avalanche",
            "山地冰崩",
            0.2,
            (
                ("line-7", 0.6),
                ("line-9", 0.4),
                ("sub-1", 0.2),
                ("sub-2", 0.2),
                ("radial-3", 0.1),
            ),
        ),
        RiskScenario(
            "ice-storm",
            "常规覆冰",
            0.8,
            (
                ("line-7", 0.15),
                ("line-9", 0.2),
                ("sub-1", 0.1),
                ("sub-2", 0.1),
                ("radial-3", 0.2),
            ),
        ),
    )


def make_repair():
    return RepairResources(
        repair_days=(
            ("line-7", 30),
            ("line-9", 20),
            ("sub-1", 14),
            ("sub-2", 14),
            ("radial-3", 7),
        ),
        bypass_days=(("line-7", 12), ("line-9", 8)),
    )


def make_candidates():
    return (
        RetrofitCandidate(
            "p-line7", "line-7", Decimal("180"), 25, (),
            name="7号线抗冰加固", residual_failure_factor=0.2,
            bypass_after_days=5, repair_days_reduction=10,
        ),
        RetrofitCandidate(
            "p-sub1", "sub-1", Decimal("90"), 12, ("p-line7",),
            name="1号变旁路", residual_failure_factor=0.3, bypass_after_days=3,
        ),
        RetrofitCandidate(
            "p-sub2", "sub-2", Decimal("110"), 18, (),
            name="2号变加固", residual_failure_factor=0.3,
        ),
        RetrofitCandidate(
            "p-radial3", "radial-3", Decimal("30"), 6, ("p-sub2",),
            name="辐射线改造", residual_failure_factor=0.2,
        ),
    )


def make_resources(budget="300", crew_teams=2, horizon_days=60):
    return WorkBudget(Decimal(budget), crew_teams, horizon_days)


def make_data(**overrides):
    kwargs = dict(
        assets=make_assets(),
        service_points=make_points(),
        scenarios=make_scenarios(),
        repair=make_repair(),
        candidates=make_candidates(),
        resources=make_resources(),
    )
    kwargs.update(overrides)
    return PlanningData(**kwargs)


def make_weights():
    return RiskWeights(risk=1.0, restoration=1.0, dependency=0.5)
