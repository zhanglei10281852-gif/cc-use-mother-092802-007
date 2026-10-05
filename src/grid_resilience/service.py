"""可重复调用的规划服务接口。

ResilienceService 是纯内存、确定性的应用服务；HTTP 层仅做 JSON 编解码，
相同输入永远返回相同 content_hash 与排序，便于复现与审计。

路由：
  GET  /health
  GET  /versions                         版本列表
  POST /versions                         提交草案（请求体=输入快照）
  GET  /versions/{id}                    版本详情：排序依据、计划、阻塞、哈希
  POST /versions/{id}/approve            审批（冻结数据与权重）
  POST /versions/rollback                审批回退
  POST /replan                           带施工进度/新数据生成子版本
  POST /compare                          只评分不建版本，用于比较情景与权重
"""

import dataclasses
import json
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlsplit

from .contracts import (
    ProjectProgress,
    RepairResources,
    RetrofitCandidate,
    RiskScenario,
    RiskWeights,
    ServicePoint,
    GridAsset,
)
from .versioning import (
    FrozenInputs,
    PlanRegistry,
    PlanVersion,
    content_hash,
    evaluate,
)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def inputs_from_dict(data: dict) -> FrozenInputs:
    """把 JSON 请求体解析为冻结输入（Decimal 不经过 float，避免精度漂移）。"""
    return FrozenInputs(
        assets=tuple(
            GridAsset(
                asset_id=a["asset_id"],
                asset_kind=a["asset_kind"],
                serves=tuple(a.get("serves", ())),
                upstream_asset_ids=tuple(a.get("upstream_asset_ids", ())),
            )
            for a in data.get("assets", [])
        ),
        service_points=tuple(
            ServicePoint(
                point_id=p["point_id"],
                name=p.get("name", p["point_id"]),
                population=int(p["population"]),
            )
            for p in data.get("service_points", [])
        ),
        scenarios=tuple(
            RiskScenario(
                scenario_id=s["scenario_id"],
                name=s.get("name", s["scenario_id"]),
                asset_fail_prob={k: _decimal(v) for k, v in s.get("asset_fail_prob", {}).items()},
                repair_time_days={k: _decimal(v) for k, v in s.get("repair_time_days", {}).items()},
            )
            for s in data.get("scenarios", [])
        ),
        candidates=tuple(
            RetrofitCandidate(
                project_id=c["project_id"],
                asset_id=c["asset_id"],
                cost=_decimal(c["cost"]),
                crew_days=int(c["crew_days"]),
                prerequisite_ids=tuple(c.get("prerequisite_ids", ())),
                duration_days=c.get("duration_days"),
                residual_fail_fraction=_decimal(c.get("residual_fail_fraction", "0.1")),
                residual_repair_fraction=_decimal(c.get("residual_repair_fraction", "1.0")),
                name=c.get("name", ""),
            )
            for c in data.get("candidates", [])
        ),
        weights=RiskWeights(
            population=_decimal((data.get("weights") or {}).get("population", "1.0")),
            restoration=_decimal((data.get("weights") or {}).get("restoration", "0.5")),
            cascade=_decimal((data.get("weights") or {}).get("cascade", "0.2")),
        ),
        resources=RepairResources(
            budget=_decimal(data["resources"]["budget"]),
            crew_days=int(data["resources"]["crew_days"]),
        ),
        progress=tuple(
            ProjectProgress(
                project_id=p["project_id"],
                state=p["state"],
                delay_days=int(p.get("delay_days", 0)),
            )
            for p in data.get("progress", [])
        ),
        note=data.get("note", ""),
    )


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, frozenset):
        return sorted(_jsonable(v) for v in obj)
    if isinstance(obj, Decimal):
        return obj.to_eng_string()
    return obj


def version_view(version: PlanVersion) -> dict:
    return {
        "version_id": version.version_id,
        "parent_version_id": version.parent_version_id,
        "state": version.state,
        "created_at": version.created_at,
        "content_hash": version.content_hash,
        "note": version.inputs.note,
        "weights": _jsonable(version.inputs.weights),
        "scores": [
            {
                "project_id": s.project_id,
                "asset_id": s.asset_id,
                "rank": s.rank,
                "score": _jsonable(s.score),
                "components": _jsonable(s.components),
                "raw_benefits": _jsonable(s.benefits_raw),
                "normalizers": _jsonable(s.normalizers),
                "explanation": s.explain(),
                "scenarios": {
                    sid: _jsonable(metric)
                    for sid, metric in s.scenario_metrics.items()
                },
            }
            for s in version.scores
        ],
        "plan": {
            "items": [_jsonable(i) for i in version.plan.items],
            "blocked": [_jsonable(b) for b in version.plan.blocked],
            "budget_used": _jsonable(version.plan.budget_used),
            "crew_days_used": version.plan.crew_days_used,
            "horizon_days": version.plan.horizon_days,
            "replan_reason": version.plan.replan_reason,
        },
    }


class ResilienceService:
    """应用服务：所有操作对同一 PlanRegistry 进行。"""

    def __init__(self) -> None:
        self.registry = PlanRegistry()

    def compare(self, data: dict) -> dict:
        inputs = inputs_from_dict(data)
        scores, plan = evaluate(inputs)
        return {
            "content_hash": content_hash(inputs),
            "weights": _jsonable(inputs.weights),
            "scores": [
                {
                    "project_id": s.project_id,
                    "rank": s.rank,
                    "score": _jsonable(s.score),
                    "components": _jsonable(s.components),
                    "explanation": s.explain(),
                    "scenarios": {sid: _jsonable(m) for sid, m in s.scenario_metrics.items()},
                }
                for s in scores
            ],
            "blocked": [_jsonable(b) for b in plan.blocked],
        }

    def submit(self, data: dict, parent_version_id: Optional[str] = None) -> PlanVersion:
        return self.registry.submit(inputs_from_dict(data), parent_version_id)

    def replan(self, data: dict) -> PlanVersion:
        parent_version_id = data.get("parent_version_id")
        if parent_version_id is None:
            current = self.registry.current_approved
            parent_version_id = current.version_id if current else None
        return self.submit(data, parent_version_id)

    def approve(self, version_id: str) -> PlanVersion:
        return self.registry.approve(version_id)

    def rollback(self) -> PlanVersion:
        return self.registry.rollback()


def create_server(service: Optional[ResilienceService] = None, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    service = service or ResilienceService()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默，测试输出保持干净
            pass

        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._send(200, {"status": "ok"})
            elif path == "/versions":
                current = service.registry.current_approved
                self._send(200, {
                    "versions": [
                        {"version_id": v.version_id, "state": v.state,
                         "content_hash": v.content_hash, "parent_version_id": v.parent_version_id}
                        for v in service.registry.list_versions()
                    ],
                    "current_approved": current.version_id if current else None,
                })
            elif path.startswith("/versions/"):
                self._send(200, version_view(service.registry.get(path.split("/")[2])))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path.rstrip("/") or "/"
            try:
                if path == "/compare":
                    self._send(200, service.compare(self._body()))
                elif path == "/versions":
                    version = service.submit(self._body())
                    self._send(201, version_view(version))
                elif path == "/replan":
                    version = service.replan(self._body())
                    self._send(201, version_view(version))
                elif path.startswith("/versions/") and path.endswith("/approve"):
                    version_id = path.split("/")[2]
                    self._send(200, version_view(service.approve(version_id)))
                elif path == "/versions/rollback":
                    self._send(200, version_view(service.rollback()))
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)
