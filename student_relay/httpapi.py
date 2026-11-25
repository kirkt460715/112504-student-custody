"""HTTP JSON 接口（仅标准库）。

所有业务操作都经这些接口完成；写操作在 SQLite 写事务内串行执行，
服务重启后从同一数据库继续处理在途学生与未决证据。
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any

from . import auth, domain
from .auth import ApiError, Principal, authenticate
from .partner import PartnerClient
from .storage import Store


class Service:
    def __init__(self, store: Store, partner: PartnerClient | None):
        self.store = store
        self.partner = partner


# --------------------------------------------------------------- 请求处理辅助
def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ApiError(400, "invalid_request", "Content-Length 非法")
    if not 0 < length <= 1 << 20:
        raise ApiError(400, "invalid_request", "请求体为空或过大")
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiError(400, "invalid_json", f"JSON 解析失败: {exc}")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_request", "请求体必须是 JSON 对象")
    return data


def _require(data: dict, key: str, typ: type | tuple = (str,)) -> Any:
    if key not in data:
        raise ApiError(400, "missing_field", f"缺少字段: {key}")
    value = data[key]
    if not isinstance(value, typ):
        expect = typ.__name__ if isinstance(typ, type) else \
            "/".join(t.__name__ for t in typ)
        raise ApiError(400, "invalid_field", f"字段 {key} 应为 {expect}")
    return value


def _optional(data: dict, key: str, default=None, typ: type | tuple = (str,)):
    if key not in data or data[key] is None:
        return default
    if not isinstance(data[key], typ):
        raise ApiError(400, "invalid_field", f"字段 {key} 类型不符")
    return data[key]


def _scope_desk(principal: Principal, data: dict) -> str:
    site = _require(data, "site")
    auth.ensure_site_access(principal, site)
    return site


def _ensure_site_student(store: Store, principal: Principal,
                         student_id: str) -> None:
    """值班台只能处理本站照管或经停本站班次涉及的学生。"""
    auth.require_desk(principal)
    if not domain.student_visible_to_site(
            store.conn, student_id, principal.scope):
        raise ApiError(403, "out_of_scope", "该学生不在本岗位所需范围")


# ------------------------------------------------------------------- 路由表
def handle(service: Service, method: str, path: str, principal: Principal,
           data: dict, query: dict) -> dict:
    store = service.store

    # ---- 班次与转运 -------------------------------------------------------
    if method == "POST" and path == "/trips":
        auth.require_operator(principal)
        return domain.create_trip(
            store, principal.identifier,
            _require(data, "trip_id"), _require(data, "from_site"),
            _require(data, "to_site"),
            _require(data, "student_ids", list),
            _optional(data, "as_of"),
        )
    if method == "GET" and path.startswith("/trips/"):
        auth.require_operator(principal)
        trip = domain.get_trip(store, path.split("/", 2)[2])
        if principal.role == "crew" and principal.scope != trip["id"]:
            raise ApiError(403, "out_of_scope", "该班次不属于本随车岗位")
        if principal.role == "desk" and principal.scope not in (
                trip["from_site"], trip["to_site"]):
            raise ApiError(403, "out_of_scope", "该班次不经过本站")
        return trip
    if method == "POST" and path == "/trips/boarding":
        trip_id = _require(data, "trip_id")
        auth.ensure_crew_access(principal, trip_id)
        return domain.mark_boarding(
            store, principal.identifier, trip_id,
            _require(data, "boarded", list), _optional(data, "as_of"),
            _optional(data, "idempotency_key"),
            _optional(data, "handover_ids", {}, dict),
        )
    if method == "POST" and path == "/trips/arrival-count":
        trip_id = _require(data, "trip_id")
        trip = domain.get_trip(store, trip_id)
        auth.ensure_site_access(principal, trip["to_site"])
        return domain.mark_arrival_count(
            store, principal.identifier, trip_id,
            _require(data, "counted", list), _optional(data, "as_of"),
        )
    if method == "POST" and path == "/trips/arrival-sign":
        trip_id = _require(data, "trip_id")
        trip = domain.get_trip(store, trip_id)
        auth.ensure_site_access(principal, trip["to_site"])
        return domain.sign_trip_arrival(
            store, principal.identifier, trip_id, _optional(data, "as_of"))
    if method == "POST" and path == "/trips/roster-correction":
        auth.require_desk(principal)
        trip_id = _require(data, "trip_id")
        trip = domain.get_trip(store, trip_id)
        if principal.scope not in (trip["from_site"], trip["to_site"]):
            raise ApiError(403, "out_of_scope", "该班次不经过本站")
        return domain.correct_roster(
            store, principal.identifier, trip_id, _require(data, "student_id"),
            _require(data, "field"), int(_require(data, "value", int)),
            _optional(data, "as_of"), _optional(data, "reason", ""))
    if method == "GET" and path == "/crew/trip":
        auth.ensure_crew_access(principal, principal.scope)
        return domain.crew_view(store, principal.scope)

    # ---- 接领资格 ---------------------------------------------------------
    if method == "POST" and path == "/delegations":
        auth.require_desk(principal)
        _ensure_site_student(store, principal, _require(data, "student_id"))
        return domain.create_delegation(
            store, principal.identifier,
            _require(data, "delegation_id"), _require(data, "student_id"),
            _require(data, "guardian_id"), _require(data, "delegate_id"),
            _require(data, "expires_at"), _optional(data, "as_of"))
    if method == "POST" and path == "/delegations/revoke":
        auth.require_desk(principal)
        dele = store.conn.execute(
            "SELECT student_id FROM delegations WHERE id=?",
            (_require(data, "delegation_id"),)).fetchone()
        if dele is not None:
            _ensure_site_student(store, principal, dele["student_id"])
        return domain.revoke_delegation(
            store, principal.identifier, _require(data, "delegation_id"),
            _optional(data, "as_of"))
    if method == "POST" and path == "/authorization/check":
        auth.require_operator(principal)
        return domain.evaluate_authorization(
            store.conn, _require(data, "student_id"),
            _require(data, "collector_id"),
            domain.effective_time(_optional(data, "as_of")),
            _optional(data, "delegation_id"))

    # ---- 接领请求与现场交接 -----------------------------------------------
    if method == "POST" and path == "/pickup-requests":
        auth.require_operator(principal)
        student_id = _require(data, "student_id")
        if principal.role == "desk":
            _ensure_site_student(store, principal, student_id)
        note = _optional(data, "note", "")
        return domain.register_pickup_request(
            store, principal.identifier, student_id,
            _require(data, "collector_id"), _optional(data, "as_of"),
            _optional(data, "request_id"), note)
    if method == "POST" and path == "/pickup-requests/queue":
        auth.require_desk(principal)
        request_id = _require(data, "request_id")
        req_row = store.conn.execute(
            "SELECT student_id FROM pickup_requests WHERE id=?",
            (request_id,)).fetchone()
        if req_row is not None:
            _ensure_site_student(store, principal, req_row["student_id"])
        return domain.queue_pickup(
            store, principal.identifier, request_id,
            _optional(data, "as_of"), _optional(data, "handover_id"))
    if method == "POST" and path == "/pickup-requests/confirm":
        site = _scope_desk(principal, data)
        return domain.confirm_pickup(
            store, principal.identifier, _require(data, "request_id"), site,
            _optional(data, "as_of"), _optional(data, "idempotency_key"))
    if method == "POST" and path == "/pickup-requests/reject":
        auth.require_desk(principal)
        request_id = _require(data, "request_id")
        req_row = store.conn.execute(
            "SELECT student_id FROM pickup_requests WHERE id=?",
            (request_id,)).fetchone()
        if req_row is not None:
            _ensure_site_student(store, principal, req_row["student_id"])
        return domain.reject_pickup(
            store, principal.identifier, request_id,
            _optional(data, "reason", ""), _optional(data, "as_of"))
    if method == "POST" and path == "/pickup-requests/relay":
        # 转达去向：值班台可登记，不构成放行
        auth.require_desk(principal)
        request_id = _require(data, "request_id")
        req_row = store.conn.execute(
            "SELECT student_id FROM pickup_requests WHERE id=?",
            (request_id,)).fetchone()
        if req_row is not None:
            _ensure_site_student(store, principal, req_row["student_id"])
        return domain.relay_pickup_message(
            store, principal.identifier, request_id,
            _require(data, "message"), _optional(data, "as_of"))

    # ---- 争议与查找后的照管确认 -------------------------------------------
    if method == "GET" and path == "/disputes":
        auth.require_operator(principal)
        rows = store.conn.execute(
            "SELECT * FROM disputes WHERE state='open' ORDER BY created_at, id"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return {"disputes": out}
    if method == "POST" and path == "/custody/resolve":
        auth.require_operator(principal)
        student_id = _require(data, "student_id")
        # 只能调整与本岗位有关的学生
        if principal.role == "desk":
            snap = domain.student_snapshot(store.conn, student_id)
            sites = {t["from_site"] for t in snap["trips"]} | \
                    {t["to_site"] for t in snap["trips"]}
            custody = snap["custody"]
            if custody and custody["party_type"] == "site":
                sites.add(custody["party_id"])
            if principal.scope not in sites:
                raise ApiError(403, "out_of_scope", "该学生不在本岗位范围")
        elif principal.role == "crew":
            if _optional(data, "trip_id") and data["trip_id"] != principal.scope:
                raise ApiError(403, "out_of_scope", "该班次不属于本随车岗位")
        return domain.resolve_custody(
            store, principal.identifier, student_id,
            _require(data, "party_type"), _require(data, "party_id"),
            _optional(data, "dispute_id"), _require(data, "resolution"),
            _optional(data, "as_of"))

    # ---- 门岗证据：对端接口 + 本地待核对 ----------------------------------
    if method == "POST" and path == "/gate-receipts":
        auth.require_operator(principal)
        return _submit_gate_receipt(service, principal, data)
    if method == "POST" and path == "/gate-evidence":
        auth.require_operator(principal)
        return domain.register_gate_evidence(
            store, principal.identifier, _require(data, "op_id"),
            _require(data, "terminal"), _require(data, "student_id"),
            _require(data, "handover_id"), _optional(data, "collector_id"),
            _optional(data, "receipt_id"), _optional(data, "observed_at"),
            _optional(data, "as_of"))
    if method == "GET" and path.startswith("/gate-evidence/"):
        auth.require_operator(principal)
        op_id = path.split("/", 2)[2]
        row = store.conn.execute(
            "SELECT * FROM gate_evidence WHERE op_id=?", (op_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_evidence", f"未知门岗证据: {op_id}")
        result = dict(row)
        result["detail"] = json.loads(result["detail"])
        return result
    if method == "POST" and path == "/gate-evidence/review":
        auth.require_desk(principal)
        return domain.review_gate_evidence(
            store, principal.identifier, _require(data, "op_id"),
            _require(data, "decision"), _optional(data, "note", ""),
            _optional(data, "as_of"))

    # ---- 查询视图 ---------------------------------------------------------
    if method == "GET" and path == "/desk/queue":
        auth.ensure_site_access(principal, principal.scope)
        return domain.desk_queue(store, principal.scope)
    if method == "GET" and path == "/desk/shift-change":
        auth.ensure_site_access(principal, principal.scope)
        return domain.shift_change_report(store, principal.scope)
    if method == "GET" and path.startswith("/students/"):
        auth.require_operator(principal)
        student_id = path.split("/", 2)[2]
        snap = domain.student_snapshot(store.conn, student_id)
        _ensure_student_visible(principal, snap)
        return snap

    # ---- 家长侧 -----------------------------------------------------------
    if method == "GET" and path == "/parent/requests":
        if principal.is_operator:
            raise ApiError(403, "forbidden", "家长凭证才能访问该接口")
        return domain.parent_status(
            store, principal.identifier,
            query.get("request_id", [None])[0])

    raise ApiError(404, "not_found", f"无此接口: {method} {path}")


def _ensure_student_visible(principal: Principal, snap: dict) -> None:
    if principal.role == "crew":
        if not any(t["id"] == principal.scope for t in snap["trips"]):
            raise ApiError(403, "out_of_scope", "该学生不在本车班次")
        return
    if principal.role == "desk":
        custody = snap["custody"]
        if custody and custody["party_type"] == "site" \
                and custody["party_id"] == principal.scope:
            return
        if any(principal.scope in (t["from_site"], t["to_site"])
               for t in snap["trips"]):
            return
        raise ApiError(403, "out_of_scope", "该学生不在本岗位所需范围")


def _submit_gate_receipt(service: Service, principal: Principal, data: dict) -> dict:
    """经合作方接口提交门岗实物回执，并把回报登记为门岗证据。

    断连时 PartnerClient 自动按操作 ID 补取；终端重传同一 op_id 时
    本地证据原样返回，保证能辨清是哪一次实际交接。
    """
    if service.partner is None:
        raise ApiError(503, "partner_not_configured", "未配置门岗对端地址")
    op_id = _require(data, "op_id")
    request = {
        "terminal": _require(data, "terminal"),
        "student": _require(data, "student_id"),
        "handover": _require(data, "handover_id"),
        "collector": _require(data, "collector_id"),
    }
    store = service.store
    with store.tx() as db:
        existing = domain.get_gate_evidence(db, op_id)
        if existing is not None:
            result = dict(existing)
            result["detail"] = json.loads(result["detail"])
            result["redelivery"] = True
            return result
        # 对端调用在事务外网络语义上仍是同一次业务操作；对端先落日志，
        # 即使断连也能用同一 op_id 补回，不会产生第二次实际交接
        exchange = service.partner.submit("gate-receipt", op_id, request)
        reply = exchange["reply"]
        evidence = domain._register_gate_evidence_db(
            db, principal.identifier, op_id, request["terminal"],
            request["student"], request["handover"], request["collector"],
            reply.get("receipt"), reply.get("observed_at"),
            _optional(data, "as_of"),
        )
    evidence["partner_delivery"] = exchange["delivered_via"]
    if "recovery_cause" in exchange:
        evidence["recovery_cause"] = exchange["recovery_cause"]
    return evidence


# ---------------------------------------------------------------- HTTP 外壳
class Handler(BaseHTTPRequestHandler):
    server_version = "StudentRelay/1.0"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        try:
            principal = authenticate(self.server.service.store,
                                     self.headers.get("Authorization"))
            data = _read_json(self) if method == "POST" else {}
            result = handle(self.server.service, method, parts.path,
                            principal, data, query)
            self._send(200, {"ok": True, "data": result})
        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.code,
                                    "message": exc.message})
        except Exception as exc:  # 防御性兜底，避免连接半挂
            self._send(500, {"ok": False, "error": "internal_error",
                             "message": str(exc)})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, *args) -> None:
        pass


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: Service):
        self.service = service
        super().__init__(address, Handler)
