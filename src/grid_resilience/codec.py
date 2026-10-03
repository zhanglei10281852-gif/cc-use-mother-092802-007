"""HTTP 接口的 JSON 编解码。"""

from .planning import DelayRevision, PlanVersion
from .scoring import RiskWeights
from .serialization import data_from_dict, result_to_dict


def parse_data(raw: dict) -> dict:
    if "data" in raw:
        raw = raw["data"]
    return {"data": data_from_dict(raw)}


def parse_weights(raw: dict | None) -> RiskWeights | None:
    if raw is None:
        return None
    raw = raw.get("weights", raw)
    return RiskWeights(
        risk=float(raw.get("risk", 1.0)),
        restoration=float(raw.get("restoration", 1.0)),
        dependency=float(raw.get("dependency", 0.5)),
    )


def revision_to_dict(rev: DelayRevision) -> dict:
    return {
        "revision_no": rev.revision_no,
        "delayed_project": rev.delayed_project,
        "new_finish_day": rev.new_finish_day,
        "affected_downstream": list(rev.affected_downstream),
        "warnings": list(rev.warnings),
        "recorded_at": rev.recorded_at,
        "schedule": {
            pid: {"start_day": s, "finish_day": f, "team": t}
            for pid, (s, f, t) in rev.schedule.items()
        },
    }


def version_to_dict(v: PlanVersion, include_snapshot: bool = False) -> dict:
    out = {
        "plan_id": v.plan_id,
        "version": v.version,
        "status": v.status,
        "parent_version": v.parent_version,
        "created_at": v.created_at,
        "approved_at": v.approved_at,
        "frozen": v.status != "draft",
        "content_hash": v.content_hash,
        "weights": v.weights.as_dict(),
        "ranking": result_to_dict(v.result),
        "revisions": [revision_to_dict(r) for r in v.revisions],
    }
    if include_snapshot:
        from .serialization import canonical_json
        import json

        out["snapshot"] = json.loads(canonical_json(v.data, v.weights))
    return out
