"""可重复的 HTTP/WSGI 服务接口（仅依赖标准库）。

路由：
  POST /evaluate                              试算排序（不落库）
  GET  /plans                                 计划列表
  POST /plans/{plan_id}                       建立计划草稿 v1
  GET  /plans/{plan_id}                       版本列表
  GET  /plans/{plan_id}/versions/{version}    版本详情（?snapshot=1 附带冻结快照）
  PUT  /plans/{plan_id}/draft/data            更新草稿数据
  POST /plans/{plan_id}/draft/weights         调整草稿权重
  POST /plans/{plan_id}/compare/weights       多套权重比较
  POST /compare-scenarios                     情景组合比较（不落库）
  POST /plans/{plan_id}/approve               审批冻结当前草稿
  POST /plans/{plan_id}/rollback              审批回退（撤回旧版并开出新草稿）
  POST /plans/{plan_id}/delays                登记施工延期并重评下游
"""

import json
from urllib.parse import parse_qs

from .codec import (
    parse_data,
    parse_weights,
    revision_to_dict,
    version_to_dict,
)
from .errors import (
    InvalidPlanningData,
    InvalidTransition,
    NotFoundError,
    ResilienceError,
)
from .planning import PlanningStore
from .serialization import result_to_dict


class PlanningApp:
    def __init__(self, store: PlanningStore | None = None):
        self.store = store or PlanningStore()

    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/").rstrip("/") or "/"
        try:
            status, body = self._route(method, path, environ)
        except NotFoundError as exc:
            status, body = "404 Not Found", {"error": "not_found", "message": str(exc)}
        except InvalidTransition as exc:
            status, body = "409 Conflict", {"error": "invalid_transition", "message": str(exc)}
        except InvalidPlanningData as exc:
            status, body = "422 Unprocessable Entity", {"error": "invalid_data", "message": str(exc)}
        except ResilienceError as exc:
            status, body = "400 Bad Request", {"error": "resilience_error", "message": str(exc)}
        payload = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        start_response(
            status,
            [("Content-Type", "application/json; charset=utf-8"),
             ("Content-Length", str(len(payload)))],
        )
        return [payload]

    def _route(self, method: str, path: str, environ) -> tuple[str, dict]:
        parts = [p for p in path.split("/") if p]
        body = self._read_json(environ)

        if method == "POST" and path == "/evaluate":
            parsed = parse_data(body)
            from .planning import evaluate

            return "200 OK", result_to_dict(
                evaluate(parsed["data"], parse_weights(body))
            )

        if method == "POST" and path == "/compare-scenarios":
            parsed = parse_data(body)
            groups = {
                label: tuple(ids) for label, ids in body.get("scenario_groups", {}).items()
            }
            results = PlanningStore.compare_scenarios(
                parsed["data"], parse_weights(body), groups
            )
            return "200 OK", {label: result_to_dict(r) for label, r in results.items()}

        if path == "/plans":
            if method == "GET":
                return "200 OK", {"plans": self.store.list_plans()}
            raise ResilienceError(f"不支持的操作 {method} /plans")

        if len(parts) >= 2 and parts[0] == "plans":
            plan_id = parts[1]
            action = parts[2] if len(parts) > 2 else None

            if action is None:
                if method == "POST":
                    parsed = parse_data(body)
                    record = self.store.create_plan(
                        plan_id, parsed["data"], parse_weights(body)
                    )
                    return "201 Created", version_to_dict(record)
                if method == "GET":
                    return "200 OK", {
                        "plan_id": plan_id,
                        "versions": [
                            version_to_dict(v) for v in self.store.versions(plan_id)
                        ],
                    }

            if action == "versions":
                if len(parts) == 4 and method == "GET":
                    query = parse_qs(environ.get("QUERY_STRING", ""))
                    record = self.store.get_version(plan_id, int(parts[3]))
                    return "200 OK", version_to_dict(
                        record,
                        include_snapshot=query.get("snapshot", ["0"])[0] == "1",
                    )
                if len(parts) == 3 and method == "POST":
                    # 审批冻结后，用含新增资产关系的数据开出下一版草稿
                    parsed = parse_data(body)
                    record = self.store.new_version(
                        plan_id, parsed["data"], parse_weights(body)
                    )
                    return "201 Created", version_to_dict(record)

            if action == "draft":
                if len(parts) == 4 and parts[3] == "data" and method == "PUT":
                    parsed = parse_data(body)
                    record = self.store.update_draft_data(plan_id, parsed["data"])
                    return "200 OK", version_to_dict(record)
                if len(parts) == 4 and parts[3] == "weights" and method == "POST":
                    weights = parse_weights(body)
                    record = self.store.adjust_weights(plan_id, weights)
                    return "200 OK", version_to_dict(record)

            if action == "compare" and len(parts) == 4 and parts[3] == "weights" and method == "POST":
                options = {
                    label: w for label, w in (
                        (label, parse_weights({"weights": w}))
                        for label, w in body.get("options", {}).items()
                    )
                }
                results = self.store.compare_weights(plan_id, options)
                return "200 OK", {
                    label: result_to_dict(r) for label, r in results.items()
                }

            if action == "approve" and method == "POST" and len(parts) == 3:
                return "200 OK", version_to_dict(self.store.approve(plan_id))

            if action == "rollback" and method == "POST" and len(parts) == 3:
                record = self.store.rollback_approval(plan_id, body.get("reason", ""))
                return "200 OK", version_to_dict(record)

            if action == "delays" and method == "POST" and len(parts) == 3:
                revision = self.store.report_delay(
                    plan_id, body["project_id"], int(body["new_finish_day"])
                )
                return "201 Created", revision_to_dict(revision)

        return "404 Not Found", {"error": "not_found", "message": f"{method} {path}"}

    @staticmethod
    def _read_json(environ) -> dict:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not length:
            return {}
        raw = environ["wsgi.input"].read(length)
        if not raw.strip():
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ResilienceError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ResilienceError("请求体必须是 JSON 对象")
        return value


def make_server(host: str = "127.0.0.1", port: int = 8000):
    from wsgiref.simple_server import make_server as _make

    return _make(host, port, PlanningApp())
