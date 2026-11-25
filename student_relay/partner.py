"""合作方/门岗终端接口客户端。

门岗回执一律通过项目自带的对端 HTTP 接口（partner_stub）收发：
- POST /operations 首次提交，对端先落日志再应答；
- outcome=commit_disconnect 时连接被对端断开，但操作已在对端落账，
  此时改用 GET /operations/<id> 补取回报；
- 同一 id + 同一正文重试返回原回报（幂等）；复用 id 提交其他内容返回 409。

本客户端只负责协议收发与补传，不做任何业务放行判断。
"""
from __future__ import annotations

from http.client import HTTPConnection, RemoteDisconnected
import json
from urllib.parse import urlparse

from .auth import ApiError


class PartnerClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        parsed = urlparse(base_url)
        if parsed.scheme != "http":
            raise ValueError("对端地址仅支持 http")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.timeout = timeout

    def _connection(self) -> HTTPConnection:
        return HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _request(self, method: str, path: str, body: dict | None = None):
        conn = self._connection()
        try:
            payload = None if body is None else json.dumps(
                body, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            try:
                parsed = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = {"raw": data.decode("utf-8", "replace")}
            return resp.status, parsed
        finally:
            conn.close()

    def submit(self, operation: str, op_id: str, request: dict) -> dict:
        """提交对端操作，自动处理“已接单但断连”的补传。

        返回 {"id", "reply", "delivered_via"}；
        delivered_via 为 post / replay_get / replay_post，
        便于服务端区分这是哪一次实际交接的回报，而不是把旧证据当新放行。
        """
        body = {"id": op_id, "operation": operation, "request": request}
        try:
            status, data = self._request("POST", "/operations", body)
        except (RemoteDisconnected, ConnectionError, OSError) as exc:
            # 对端可能已落账后断连：按 id 查询回报
            return self._recover_by_get(op_id, exc)

        if status == 200:
            return {"id": op_id, "reply": data.get("reply", {}),
                    "delivered_via": "post"}
        if status == 409:
            raise ApiError(409, "operation_id_conflict",
                           "对端拒绝：同一操作 ID 被用于不同正文")
        if status in (502, 503, 504):
            return self._recover_by_get(op_id, f"http_{status}")
        raise ApiError(502, "partner_error",
                       f"对端返回 {status}: {data}")

    def _recover_by_get(self, op_id: str, cause) -> dict:
        try:
            status, data = self._request("GET", f"/operations/{op_id}")
        except (RemoteDisconnected, ConnectionError, OSError) as exc:
            raise ApiError(503, "partner_unavailable",
                           f"对端暂不可达，证据已在本地待补传: {exc}") from exc
        if status == 200:
            return {"id": op_id, "reply": data.get("reply", {}),
                    "delivered_via": "replay_get",
                    "recovery_cause": str(cause)}
        if status == 404:
            # 对端没接到单：允许稍后用同一 id 原样重试
            raise ApiError(503, "operation_not_acked",
                           "对端无该操作记录，需保留证据稍后按同一 ID 重试")
        raise ApiError(502, "partner_error", f"补传查询失败: {status} {data}")

    def query(self, op_id: str) -> dict:
        status, data = self._request("GET", f"/operations/{op_id}")
        if status == 404:
            raise ApiError(404, "unknown_operation", "对端无该操作记录")
        if status != 200:
            raise ApiError(502, "partner_error", f"对端返回 {status}: {data}")
        return {"id": op_id, "reply": data.get("reply", {}),
                "delivered_via": "replay_get"}
