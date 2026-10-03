"""确定性序列化：计划冻结快照与接口 JSON 均由此产生。"""

import hashlib
import json
from decimal import Decimal

from .contracts import (
    PlanningData,
    RepairResources,
    RetrofitCandidate,
    RiskScenario,
    ServicePoint,
    WorkBudget,
    GridAsset,
)
from .scoring import PortfolioResult, RiskWeights

_SCHEMA_VERSION = "1.0"


def _data_to_dict(data: PlanningData, weights: RiskWeights) -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "assets": [
            {
                "asset_id": a.asset_id,
                "asset_kind": a.asset_kind,
                "serves": list(a.serves),
                "upstream_asset_ids": list(a.upstream_asset_ids),
            }
            for a in data.assets
        ],
        "service_points": [
            {
                "point_id": p.point_id,
                "name": p.name,
                "population": p.population,
                "criticality": p.criticality,
            }
            for p in data.service_points
        ],
        "scenarios": [
            {
                "scenario_id": s.scenario_id,
                "name": s.name,
                "frequency_per_year": s.frequency_per_year,
                "failure_probs": [[a, p] for a, p in s.failure_probs],
                "default_failure_prob": s.default_failure_prob,
            }
            for s in data.scenarios
        ],
        "repair": {
            "repair_days": [[a, d] for a, d in data.repair.repair_days],
            "bypass_days": [[a, d] for a, d in data.repair.bypass_days],
        },
        "candidates": [
            {
                "project_id": c.project_id,
                "asset_id": c.asset_id,
                "cost": str(c.cost),
                "crew_days": c.crew_days,
                "prerequisite_ids": list(c.prerequisite_ids),
                "name": c.name,
                "residual_failure_factor": c.residual_failure_factor,
                "bypass_after_days": c.bypass_after_days,
                "repair_days_reduction": c.repair_days_reduction,
            }
            for c in data.candidates
        ],
        "resources": {
            "budget": str(data.resources.budget),
            "crew_teams": data.resources.crew_teams,
            "horizon_days": data.resources.horizon_days,
        },
        "weights": weights.as_dict(),
    }


def canonical_json(data: PlanningData, weights: RiskWeights) -> str:
    """排序键固定、分隔符固定的 JSON，用于内容指纹。"""
    return json.dumps(
        _data_to_dict(data, weights),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def content_hash(data: PlanningData, weights: RiskWeights) -> str:
    return hashlib.sha256(canonical_json(data, weights).encode("utf-8")).hexdigest()


def data_from_dict(raw: dict) -> PlanningData:
    """从冻结快照重建 PlanningData。"""
    assets = tuple(
        GridAsset(
            a["asset_id"],
            a["asset_kind"],
            tuple(a["serves"]),
            tuple(a["upstream_asset_ids"]),
        )
        for a in raw["assets"]
    )
    points = tuple(
        ServicePoint(
            p["point_id"], p["name"], p["population"], p.get("criticality", 1.0)
        )
        for p in raw["service_points"]
    )
    scenarios = tuple(
        RiskScenario(
            s["scenario_id"],
            s["name"],
            s["frequency_per_year"],
            tuple((a, p) for a, p in s["failure_probs"]),
            s.get("default_failure_prob", 0.0),
        )
        for s in raw["scenarios"]
    )
    repair = RepairResources(
        repair_days=tuple((a, d) for a, d in raw["repair"]["repair_days"]),
        bypass_days=tuple((a, d) for a, d in raw["repair"].get("bypass_days", [])),
    )
    candidates = tuple(
        RetrofitCandidate(
            project_id=c["project_id"],
            asset_id=c["asset_id"],
            cost=Decimal(c["cost"]),
            crew_days=c["crew_days"],
            prerequisite_ids=tuple(c["prerequisite_ids"]),
            name=c.get("name", ""),
            residual_failure_factor=c.get("residual_failure_factor", 1.0),
            bypass_after_days=c.get("bypass_after_days"),
            repair_days_reduction=c.get("repair_days_reduction", 0),
        )
        for c in raw["candidates"]
    )
    resources = WorkBudget(
        budget=Decimal(raw["resources"]["budget"]),
        crew_teams=raw["resources"]["crew_teams"],
        horizon_days=raw["resources"]["horizon_days"],
    )
    return PlanningData(
        assets=assets,
        service_points=points,
        scenarios=scenarios,
        repair=repair,
        candidates=candidates,
        resources=resources,
    )


def weights_from_dict(raw: dict) -> RiskWeights:
    return RiskWeights(
        risk=raw.get("risk", 1.0),
        restoration=raw.get("restoration", 1.0),
        dependency=raw.get("dependency", 0.5),
    )


def result_to_dict(result: PortfolioResult) -> dict:
    return {
        "baseline": {
            "annual_expected_affected_people": result.baseline_annual_risk,
            "annual_expected_person_days": result.baseline_annual_person_days,
        },
        "portfolio": {
            "selected": list(result.selected),
            "annual_expected_affected_people": result.portfolio_risk,
            "annual_expected_person_days": result.portfolio_person_days,
            "total_cost": str(result.total_cost),
        },
        "weights": result.weights,
        "warnings": list(result.warnings),
        "rankings": [
            {
                "project_id": s.project_id,
                "asset_id": s.asset_id,
                "name": s.name,
                "priority_score": s.priority_score,
                "benefit_cost_ratio": s.benefit_cost_ratio,
                "risk_reduction_per_year": s.risk_reduction,
                "restoration_reduction_person_days": s.restoration_reduction,
                "exposed_population": s.exposed_population,
                "contributions": s.contributions,
                "blockers": list(s.blockers),
                "selected": s.project_id in result.selected,
                "not_selected_reason": s.not_selected_reason,
                "schedule": (
                    None
                    if s.scheduled_start is None
                    else {
                        "start_day": s.scheduled_start,
                        "finish_day": s.scheduled_finish,
                        "team": s.team,
                    }
                ),
            }
            for s in result.rankings
        ],
    }
