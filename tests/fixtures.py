"""山地冰崩情景测试夹具。

拓扑（供电方向自左向右）：

    line-7 ──> sub-1 ──> fdr-2 ──> shelter-b(300)
                  │                  shelter-a(500，变电站直供)
                  └────> fdr-3 ──> shelter-c(200)
    sub-9（独立） ────────────────> shelter-d(400)
"""

from decimal import Decimal

from grid_resilience.contracts import (
    GridAsset,
    RepairResources,
    RetrofitCandidate,
    RiskScenario,
    RiskWeights,
    ServicePoint,
)
from grid_resilience.versioning import FrozenInputs

ASSETS = [
    GridAsset("line-7", "line", (), ()),
    GridAsset("sub-1", "substation", ("shelter-a",), ("line-7",)),
    GridAsset("fdr-2", "feeder", ("shelter-b",), ("sub-1",)),
    GridAsset("fdr-3", "feeder", ("shelter-c",), ("sub-1",)),
    GridAsset("sub-9", "substation", ("shelter-d",), ()),
]

SERVICE_POINTS = [
    ServicePoint("shelter-a", "甲避险点", 500),
    ServicePoint("shelter-b", "乙避险点", 300),
    ServicePoint("shelter-c", "丙避险点", 200),
    ServicePoint("shelter-d", "丁避险点", 400),
]

ICEFALL = RiskScenario(
    "icefall",
    "春季山地冰崩",
    asset_fail_prob={
        "line-7": Decimal("0.12"),
        "sub-1": Decimal("0.15"),
        "fdr-2": Decimal("0.1"),
        "fdr-3": Decimal("0.1"),
        "sub-9": Decimal("0.15"),
    },
    repair_time_days={
        "line-7": Decimal("10"),
        "sub-1": Decimal("6"),
        "fdr-2": Decimal("3"),
        "fdr-3": Decimal("3"),
        "sub-9": Decimal("4"),
    },
)

DEFAULT_WEIGHTS = RiskWeights(
    population=Decimal("1.0"),
    restoration=Decimal("0.5"),
    cascade=Decimal("0.2"),
)

RESOURCES = RepairResources(budget=Decimal("130"), crew_days=25)


def candidates():
    return [
        RetrofitCandidate(
            "p-sub1", "sub-1", Decimal("80"), 10,
            residual_fail_fraction=Decimal("0.05"),
            residual_repair_fraction=Decimal("0.5"),
            name="变电站加固",
        ),
        RetrofitCandidate(
            "p-line7", "line-7", Decimal("60"), 8,
            residual_fail_fraction=Decimal("0.2"),
            residual_repair_fraction=Decimal("0.2"),
            name="替代线路",
        ),
        RetrofitCandidate(
            "p-fdr2", "fdr-2", Decimal("30"), 4,
            residual_fail_fraction=Decimal("0.1"),
        ),
        RetrofitCandidate(
            "p-fdr3", "fdr-3", Decimal("20"), 3,
            prerequisite_ids=("p-sub1",),
            residual_fail_fraction=Decimal("0.1"),
        ),
        RetrofitCandidate(
            "p-sub9", "sub-9", Decimal("40"), 5,
            residual_fail_fraction=Decimal("0.1"),
        ),
    ]


def frozen_inputs(weights=None, resources=None, progress=(), assets=None,
                  scenarios=None, note=""):
    return FrozenInputs(
        assets=tuple(assets if assets is not None else ASSETS),
        service_points=tuple(SERVICE_POINTS),
        scenarios=tuple(scenarios if scenarios is not None else [ICEFALL]),
        candidates=tuple(candidates()),
        weights=weights or DEFAULT_WEIGHTS,
        resources=resources or RESOURCES,
        progress=tuple(progress),
        note=note,
    )
