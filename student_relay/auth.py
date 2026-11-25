"""身份、岗位与最小可见范围。

两类调用方：
- 值班台/随车人员（operator）：desk 只能看本站所需学生资料，crew 只能看本车班次；
- 家长侧（collector）：凭预约令牌只能查看与自己接领请求有关的状态。
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets

from .storage import Store


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


@dataclass(frozen=True)
class Principal:
    kind: str                 # 'operator' | 'collector'
    identifier: str
    role: str | None = None   # desk / crew
    scope: str | None = None  # 站点 id 或车辆 id
    request_id: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.kind == "operator"


def issue_collector_token(store: Store, collector_id: str, request_id: str | None,
                          now: str) -> str:
    token = "tok_" + secrets.token_urlsafe(24)
    with store.tx() as db:
        db.execute(
            "INSERT INTO collector_tokens(token, collector_id, request_id, issued_at)"
            " VALUES (?,?,?,?)",
            (token, collector_id, request_id, now),
        )
    return token


def authenticate(store: Store, header: str | None) -> Principal:
    if not header or not header.startswith("Bearer "):
        raise ApiError(401, "unauthorized", "缺少 Bearer 令牌")
    token = header[len("Bearer "):].strip()
    db = store.conn
    row = db.execute(
        "SELECT collector_id, request_id FROM collector_tokens WHERE token=?",
        (token,),
    ).fetchone()
    if row is not None:
        return Principal(
            kind="collector",
            identifier=row["collector_id"],
            request_id=row["request_id"],
        )
    row = db.execute("SELECT id, role, scope FROM operators WHERE id=?", (token,)).fetchone()
    if row is not None:
        return Principal(
            kind="operator",
            identifier=row["id"],
            role=row["role"],
            scope=row["scope"],
        )
    raise ApiError(401, "unauthorized", "令牌无效")


def require_operator(principal: Principal) -> None:
    if not principal.is_operator:
        raise ApiError(403, "forbidden", "该操作仅值班台/随车人员可执行")


def require_desk(principal: Principal) -> None:
    require_operator(principal)
    if principal.role != "desk":
        raise ApiError(403, "forbidden", "该操作仅值班台可执行")


def ensure_site_access(principal: Principal, site_id: str) -> None:
    """跨校值班员只能操作本站；随车人员不能代值班台放行。"""
    require_desk(principal)
    if principal.scope != site_id:
        raise ApiError(403, "out_of_scope", "该学生不在本岗位照管范围")


def ensure_crew_access(principal: Principal, trip_id: str) -> None:
    require_operator(principal)
    if principal.role != "crew" or principal.scope != trip_id:
        raise ApiError(403, "out_of_scope", "该班次不属于本随车岗位")
