"""业务领域规则。

核心不变量：
1. 每个学生只有一个当前照管方（custody 单行），照管转移只能由已核实的
   现场交接驱动；两个岗位同时确认同一学生时，数据库写事务串行化，
   先提交者生效，后到者读到新照管方后只能得到冲突结果，不会产生第二种生效交接。
2. handovers 只追加：proposed/confirmed/rejected/superseded/blocked，
   名单更正不得删除或改写已核实交接。
3. 点名差异逐人保留；少一个孩子不能整车签收，系统也不会自动把失配学生
   退回出发点——必须经过查找后的新确认才能变更照管方。
4. 接领资格 = 登记监护人，或“有时限且指定该孩子”的代领授权；
   撤回/过期后，即使请求已排队，确认时也要按交接时刻重新核验。
5. 门岗证据是独立实物记录，与业务记录冲突时留在 pending/conflict 待核对，
   既不重新签一次，也不把旧证据当新放行。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from .auth import ApiError
from .storage import Store

VALID_PARTIES = ("site", "crew", "person")


# --------------------------------------------------------------------------- 工具
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ApiError(400, "invalid_timestamp", f"时间缺少时区信息: {value}")
    return parsed


def effective_time(as_of: str | None) -> str:
    if as_of is None:
        return now_iso()
    _parse_ts(as_of)
    return as_of


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _audit(db, actor: str, action: str, payload: dict, ts: str) -> None:
    db.execute(
        "INSERT INTO audit_log(ts, actor, action, payload) VALUES (?,?,?,?)",
        (ts, actor, action, _json(payload)),
    )


def _row(db, sql: str, params=()):
    return db.execute(sql, params).fetchone()


def _all(db, sql: str, params=()):
    return db.execute(sql, params).fetchall()


def require_student(db, student_id: str):
    row = _row(db, "SELECT id, home_site FROM students WHERE id=?", (student_id,))
    if row is None:
        raise ApiError(404, "unknown_student", f"未知学生: {student_id}")
    return row


def get_custody(db, student_id: str) -> dict | None:
    row = _row(
        db,
        "SELECT student_id, party_type, party_id, since, updated_by"
        " FROM custody WHERE student_id=?",
        (student_id,),
    )
    return dict(row) if row else None


def _open_dispute(db, student_id: str) -> dict | None:
    row = _row(
        db,
        "SELECT id, kind, state, detail FROM disputes"
        " WHERE student_id=? AND state='open' ORDER BY created_at DESC, id DESC LIMIT 1",
        (student_id,),
    )
    return dict(row) if row else None


# ------------------------------------------------------------------- 接领资格核验
def evaluate_authorization(db, student_id: str, collector_id: str, at: str,
                           delegation_id: str | None = None) -> dict:
    """按 at 时刻核验接领资格，返回依据快照。

    撤回授权后尚未完成的交接必须重新核验：调用方在确认时传入排队时记录的
    delegation_id，本函数会检查该授权此刻是否仍有效；已撤回/过期即拒绝。
    """
    moment = _parse_ts(at)
    guardian = _row(
        db, "SELECT guardian_id FROM guardians WHERE student_id=?", (student_id,)
    )
    if guardian and guardian["guardian_id"] == collector_id:
        return {
            "authorized": True,
            "basis": "registered_guardian",
            "collector": collector_id,
            "at": at,
        }

    if delegation_id is not None:
        dele = _row(
            db, "SELECT * FROM delegations WHERE id=? AND student_id=?",
            (delegation_id, student_id),
        )
        if dele is None:
            return {"authorized": False, "reason": "delegation_not_found",
                    "delegation_id": delegation_id, "at": at}
        if dele["delegate_id"] != collector_id:
            return {"authorized": False, "reason": "delegate_mismatch",
                    "delegation_id": delegation_id, "at": at}
        if dele["revoked_at"] is not None and _parse_ts(dele["revoked_at"]) <= moment:
            return {"authorized": False, "reason": "delegation_revoked",
                    "delegation_id": delegation_id,
                    "revoked_at": dele["revoked_at"], "at": at}
        if _parse_ts(dele["expires_at"]) <= moment:
            return {"authorized": False, "reason": "delegation_expired",
                    "delegation_id": delegation_id,
                    "expires_at": dele["expires_at"], "at": at}
        return {
            "authorized": True,
            "basis": "delegation",
            "delegation_id": delegation_id,
            "guardian": dele["guardian_id"],
            "collector": collector_id,
            "expires_at": dele["expires_at"],
            "at": at,
        }

    rows = _all(
        db,
        "SELECT * FROM delegations WHERE student_id=? AND delegate_id=?"
        " ORDER BY created_at",
        (student_id, collector_id),
    )
    for dele in rows:
        if dele["revoked_at"] is not None and _parse_ts(dele["revoked_at"]) <= moment:
            continue
        if _parse_ts(dele["expires_at"]) <= moment:
            continue
        return {
            "authorized": True,
            "basis": "delegation",
            "delegation_id": dele["id"],
            "guardian": dele["guardian_id"],
            "collector": collector_id,
            "expires_at": dele["expires_at"],
            "at": at,
        }
    if not guardian:
        return {"authorized": False, "reason": "no_registered_guardian", "at": at}
    return {"authorized": False, "reason": "not_authorized",
            "collector": collector_id, "at": at}


# --------------------------------------------------------------------- 照管转移
def _transfer_custody(db, student_id: str, party_type: str, party_id: str,
                      actor: str, ts: str) -> None:
    if party_type not in VALID_PARTIES:
        raise ApiError(500, "internal_error", "未知照管方类型")
    db.execute(
        "INSERT INTO custody(student_id, party_type, party_id, since, updated_by)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(student_id) DO UPDATE SET"
        " party_type=excluded.party_type, party_id=excluded.party_id,"
        " since=excluded.since, updated_by=excluded.updated_by",
        (student_id, party_type, party_id, ts, actor),
    )


def _idempotent(db, key: str | None):
    if not key:
        return None
    return _row(db, "SELECT * FROM handovers WHERE idempotency_key=?", (key,))


def _handover_payload(row) -> dict:
    data = dict(row)
    if data.get("auth_basis"):
        data["auth_basis"] = json.loads(data["auth_basis"])
    if data.get("detail"):
        data["detail"] = json.loads(data["detail"])
    return data


# ----------------------------------------------------------------------- 班次
def create_trip(store: Store, actor: str, trip_id: str, from_site: str,
                to_site: str, student_ids: list[str], as_of: str | None) -> dict:
    ts = effective_time(as_of)
    with store.tx() as db:
        if _row(db, "SELECT id FROM trips WHERE id=?", (trip_id,)):
            raise ApiError(409, "trip_exists", f"班次已存在: {trip_id}")
        for site in (from_site, to_site):
            if not _row(db, "SELECT id FROM sites WHERE id=?", (site,)):
                raise ApiError(404, "unknown_site", f"未知站点: {site}")
        if from_site == to_site:
            raise ApiError(400, "invalid_trip", "出发与到达站点不能相同")
        for student_id in student_ids:
            require_student(db, student_id)
        db.execute(
            "INSERT INTO trips(id, from_site, to_site, status, created_at)"
            " VALUES (?,?,?,'planned',?)",
            (trip_id, from_site, to_site, ts),
        )
        for student_id in student_ids:
            db.execute(
                "INSERT INTO trip_roster(trip_id, student_id, booked, boarded, counted)"
                " VALUES (?,?,1,0,0)",
                (trip_id, student_id),
            )
        _audit(db, actor, "trip_created",
               {"trip": trip_id, "from": from_site, "to": to_site,
                "booked": student_ids}, ts)
        return _trip_dict(db, trip_id)


def _trip_dict(db, trip_id: str) -> dict:
    trip = _row(db, "SELECT * FROM trips WHERE id=?", (trip_id,))
    if trip is None:
        raise ApiError(404, "unknown_trip", f"未知班次: {trip_id}")
    roster = _all(
        db,
        "SELECT student_id, booked, boarded, counted, note FROM trip_roster"
        " WHERE trip_id=? ORDER BY rowid",
        (trip_id,),
    )
    result = dict(trip)
    result["roster"] = [dict(r) for r in roster]
    result["booked"] = [r["student_id"] for r in roster if r["booked"]]
    result["boarded"] = [r["student_id"] for r in roster if r["boarded"]]
    result["counted_on_arrival"] = [r["student_id"] for r in roster if r["counted"]]
    return result


def get_trip(store: Store, trip_id: str) -> dict:
    return _trip_dict(store.conn, trip_id)


def mark_boarding(store: Store, actor: str, trip_id: str, boarded_ids: list[str],
                  as_of: str | None, idempotency_key: str | None = None,
                  handover_ids: dict | None = None) -> dict:
    """发车点到：以实际点到的人为准。同一班可分车，未上车的留校照管。"""
    ts = effective_time(as_of)
    handover_ids = handover_ids or {}
    with store.tx() as db:
        trip = _row(db, "SELECT * FROM trips WHERE id=?", (trip_id,))
        if trip is None:
            raise ApiError(404, "unknown_trip", f"未知班次: {trip_id}")
        if trip["status"] != "planned":
            raise ApiError(409, "trip_not_planned", "班次已发车，不能重复点到")
        rostered = {
            r["student_id"]: r
            for r in _all(db, "SELECT student_id FROM trip_roster WHERE trip_id=?",
                          (trip_id,))
        }
        unknown = [s for s in boarded_ids if s not in rostered]
        if unknown:
            raise ApiError(400, "student_not_booked",
                           f"以下学生不在本班预安排名单: {unknown}")
        if len(set(boarded_ids)) != len(boarded_ids):
            raise ApiError(400, "duplicate_student", "上车名单存在重复学生")

        for student_id in boarded_ids:
            custody = get_custody(db, student_id)
            if custody is None or custody["party_type"] != "site" \
                    or custody["party_id"] != trip["from_site"]:
                raise ApiError(
                    409, "not_in_departure_custody",
                    f"学生 {student_id} 当前不由出发校区照管，不能记为上车",
                )
            hid = handover_ids.get(student_id) or new_id("handover")
            if _row(db, "SELECT id FROM handovers WHERE id=?", (hid,)):
                raise ApiError(409, "handover_id_exists",
                               f"交接编号已存在: {hid}")
            db.execute(
                "INSERT INTO handovers(id, student_id, kind, from_party_type,"
                " from_party_id, to_party_type, to_party_id, trip_id, status,"
                " created_at, confirmed_at, operator_id)"
                " VALUES (?,?, 'boarding', 'site', ?, 'crew', ?, ?, 'confirmed', ?, ?, ?)",
                (hid, student_id, trip["from_site"], trip_id, trip_id, ts, ts, actor),
            )
            _transfer_custody(db, student_id, "crew", trip_id, actor, ts)
            db.execute(
                "UPDATE trip_roster SET boarded=1, note=? WHERE trip_id=? AND student_id=?",
                (_json({"boarding_handover": hid, "at": ts}), trip_id, student_id),
            )
        db.execute(
            "UPDATE trips SET status='departed', departed_at=? WHERE id=?",
            (ts, trip_id),
        )
        _audit(db, actor, "boarding_confirmed",
               {"trip": trip_id, "boarded": boarded_ids,
                "not_boarded": [s for s in rostered if s not in set(boarded_ids)]},
               ts)
        return _trip_dict(db, trip_id)


def mark_arrival_count(store: Store, actor: str, trip_id: str,
                       counted_ids: list[str], as_of: str | None) -> dict:
    """到场清点：只签收实际清点到的人，逐人保留差异。

    少一个孩子不会把整车签成已到，也不会自动把失配学生退回出发点；
    未清点到的学生照管方保持随车人员，并开启逐人争议。
    """
    ts = effective_time(as_of)
    with store.tx() as db:
        trip = _row(db, "SELECT * FROM trips WHERE id=?", (trip_id,))
        if trip is None:
            raise ApiError(404, "unknown_trip", f"未知班次: {trip_id}")
        if trip["status"] != "departed":
            raise ApiError(409, "trip_not_in_transit", "班次不在转运中，无法到场清点")
        roster = {
            r["student_id"]: r
            for r in _all(db, "SELECT * FROM trip_roster WHERE trip_id=?", (trip_id,))
        }
        if len(set(counted_ids)) != len(counted_ids):
            raise ApiError(400, "duplicate_student", "清点名单存在重复学生")
        unknown = [s for s in counted_ids if s not in roster]
        if unknown:
            raise ApiError(400, "student_not_booked",
                           f"以下学生不在本班名单: {unknown}")
        not_boarded = [s for s in counted_ids if not roster[s]["boarded"]]
        if not_boarded:
            raise ApiError(409, "counted_but_not_boarded",
                           f"以下学生没有上车记录，不能在到场清单: {not_boarded}")

        discrepancies = []
        counted_set = set(counted_ids)
        for student_id, row in roster.items():
            if not row["boarded"]:
                continue  # 没上车的学生由出发校区照管，不属于本车到场清点
            seen = student_id in counted_set
            if seen:
                custody = get_custody(db, student_id)
                if custody is not None and custody["party_type"] == "site" \
                        and custody["party_id"] == trip["to_site"] and row["counted"]:
                    # 上一轮清点已完成到场交接：重复清点不重复签，事实保持不变
                    continue
                review = _row(
                    db,
                    "SELECT id FROM handovers WHERE student_id=? AND kind='custody_review'"
                    " AND status='confirmed' ORDER BY confirmed_at DESC LIMIT 1",
                    (student_id,),
                )
                if review is not None and custody is not None \
                        and custody["party_type"] == "site" \
                        and custody["party_id"] == trip["to_site"]:
                    # 查找后已确认由到达校区照管：只补清点记录，不再制造重复交接
                    db.execute(
                        "UPDATE trip_roster SET counted=1, note=? WHERE trip_id=?"
                        " AND student_id=?",
                        (_json({"confirmed_via_review": review["id"], "at": ts}),
                         trip_id, student_id),
                    )
                    continue
                if custody is None or custody["party_type"] != "crew" \
                        or custody["party_id"] != trip_id:
                    raise ApiError(
                        409, "custody_changed_in_transit",
                        f"学生 {student_id} 照管方已变更且无查找确认，需先核对再清点",
                    )
                hid = new_id("handover")
                db.execute(
                    "INSERT INTO handovers(id, student_id, kind, from_party_type,"
                    " from_party_id, to_party_type, to_party_id, trip_id, status,"
                    " created_at, confirmed_at, operator_id)"
                    " VALUES (?, ?, 'arrival', 'crew', ?, 'site', ?, ?, 'confirmed', ?, ?, ?)",
                    (hid, student_id, trip_id, trip["to_site"], trip_id, ts, ts, actor),
                )
                _transfer_custody(db, student_id, "site", trip["to_site"], actor, ts)
                db.execute(
                    "UPDATE trip_roster SET counted=1, note=? WHERE trip_id=?"
                    " AND student_id=?",
                    (_json({"arrival_handover": hid, "at": ts}), trip_id, student_id),
                )
            else:
                db.execute(
                    "UPDATE trip_roster SET counted=0 WHERE trip_id=? AND student_id=?",
                    (trip_id, student_id),
                )
                # 照管方保持 crew（绝不自动退回出发点）；已开争议则不重复开单
                if _open_dispute(db, student_id) is None:
                    did = new_id("dispute")
                    db.execute(
                        "INSERT INTO disputes(id, student_id, kind, state, detail,"
                        " created_at) VALUES (?, ?, 'count_mismatch', 'open', ?, ?)",
                        (did, student_id,
                         _json({"trip": trip_id, "expected": "boarded",
                                "observed": "not_seen", "at": ts}), ts),
                    )
                    discrepancies.append({"student": student_id,
                                          "status": "not_seen", "dispute": did})
                else:
                    discrepancies.append({"student": student_id,
                                          "status": "not_seen",
                                          "dispute": "already_open"})
        _audit(db, actor, "arrival_counted",
               {"trip": trip_id, "counted": counted_ids,
                "discrepancies": discrepancies}, ts)
        result = _trip_dict(db, trip_id)
        result["discrepancies"] = discrepancies
        return result


def sign_trip_arrival(store: Store, actor: str, trip_id: str,
                      as_of: str | None) -> dict:
    """整车签收：只有所有实际上车学生都已到场清点且无未决差异才允许。"""
    ts = effective_time(as_of)
    with store.tx() as db:
        trip = _row(db, "SELECT * FROM trips WHERE id=?", (trip_id,))
        if trip is None:
            raise ApiError(404, "unknown_trip", f"未知班次: {trip_id}")
        if trip["status"] == "arrived":
            return _trip_dict(db, trip_id)
        if trip["status"] != "departed":
            raise ApiError(409, "trip_not_departed", "班次尚未发车")
        roster = _all(db, "SELECT * FROM trip_roster WHERE trip_id=?", (trip_id,))
        missing = [
            r["student_id"] for r in roster
            if r["boarded"] and not r["counted"]
        ]
        if missing:
            raise ApiError(
            409, "arrival_blocked_by_discrepancy",
            f"仍有上车学生未清点到，不能整车签收: {missing}",
        )
        open_disputes = _all(
            db,
            "SELECT student_id FROM disputes WHERE state='open' AND student_id IN"
            " (SELECT student_id FROM trip_roster WHERE trip_id=? AND boarded=1)",
            (trip_id,),
        )
        if open_disputes:
            raise ApiError(
                409, "arrival_blocked_by_dispute",
                f"仍有未决逐人差异: {[r['student_id'] for r in open_disputes]}",
            )
        db.execute(
            "UPDATE trips SET status='arrived', arrived_at=? WHERE id=?",
            (ts, trip_id),
        )
        _audit(db, actor, "trip_arrived", {"trip": trip_id}, ts)
        return _trip_dict(db, trip_id)


def correct_roster(store: Store, actor: str, trip_id: str, student_id: str,
                   field: str, value: int, as_of: str | None,
                   reason: str = "") -> dict:
    """名单更正：booked 可改；已核实的上车/到场交接事实不得被抹掉。"""
    if field not in ("booked", "boarded", "counted"):
        raise ApiError(400, "invalid_field", "可更正字段仅 booked/boarded/counted")
    if value not in (0, 1):
        raise ApiError(400, "invalid_value", "名单字段只能为 0/1")
    ts = effective_time(as_of)
    with store.tx() as db:
        row = _row(
            db, "SELECT * FROM trip_roster WHERE trip_id=? AND student_id=?",
            (trip_id, student_id),
        )
        if row is None:
            raise ApiError(404, "roster_entry_not_found", "该学生不在班次名单")
        if field in ("boarded", "counted") and row[field] == 1 and value == 0:
            fact = _row(
                db,
                "SELECT id, kind, status FROM handovers"
                " WHERE student_id=? AND trip_id=? AND kind=? AND status='confirmed'"
                " LIMIT 1",
                (student_id, trip_id, "boarding" if field == "boarded" else "arrival"),
            )
            if fact is not None:
                raise ApiError(
                    409, "verified_handover_protected",
                    f"已核实的{('上车' if field == 'boarded' else '到场')}交接 "
                    f"{fact['id']} 不能被名单更正抹除；如确有错误请走争议核对",
                )
        db.execute(
            f"UPDATE trip_roster SET {field}=? WHERE trip_id=? AND student_id=?",
            (value, trip_id, student_id),
        )
        _audit(db, actor, "roster_corrected",
               {"trip": trip_id, "student": student_id, "field": field,
                "value": value, "reason": reason}, ts)
        return _trip_dict(db, trip_id)


# --------------------------------------------------------------- 接领请求与交接
def register_pickup_request(store: Store, actor: str, student_id: str,
                            collector_id: str, as_of: str | None,
                            request_id: str | None = None,
                            note: str = "") -> dict:
    """登记到校接领请求/转达去向。请求本身不证明实际接领，也不能放行。"""
    ts = effective_time(as_of)
    with store.tx() as db:
        require_student(db, student_id)
        request_id = request_id or new_id("request")
        if _row(db, "SELECT id FROM pickup_requests WHERE id=?", (request_id,)):
            raise ApiError(409, "request_exists", f"请求已存在: {request_id}")
        db.execute(
            "INSERT INTO pickup_requests(id, student_id, collector_id, state,"
            " registered_at, note) VALUES (?,?,?, 'registered', ?, ?)",
            (request_id, student_id, collector_id, ts, note),
        )
        _audit(db, actor, "pickup_request_registered",
               {"request": request_id, "student": student_id,
                "collector": collector_id, "note": note}, ts)
        return dict(_row(db, "SELECT * FROM pickup_requests WHERE id=?",
                         (request_id,)))


def relay_pickup_message(store: Store, actor: str, request_id: str,
                         message: str, as_of: str | None) -> dict:
    """监护人到校但孩子不在本站（如转运途中）：登记转达去向。

    只记录信息，不产生放行、不改变照管方；现场照管方才能做实际交接。
    """
    ts = effective_time(as_of)
    with store.tx() as db:
        req = _row(db, "SELECT * FROM pickup_requests WHERE id=?", (request_id,))
        if req is None:
            raise ApiError(404, "unknown_request", f"未知接领请求: {request_id}")
        note = {"relay": message, "relayed_by": actor, "relayed_at": ts}
        db.execute(
            "UPDATE pickup_requests SET note=? WHERE id=?",
            (_json(note), request_id),
        )
        _audit(db, actor, "pickup_message_relayed",
               {"request": request_id, "message": message}, ts)
        return dict(_row(db, "SELECT * FROM pickup_requests WHERE id=?",
                         (request_id,)))


def queue_pickup(store: Store, actor: str, request_id: str,
                 as_of: str | None, handover_id: str | None = None) -> dict:
    """值班台核验资格后把请求排入交接队列；记录授权快照供确认时重验。"""
    ts = effective_time(as_of)
    with store.tx() as db:
        req = _row(db, "SELECT * FROM pickup_requests WHERE id=?", (request_id,))
        if req is None:
            raise ApiError(404, "unknown_request", f"未知接领请求: {request_id}")
        if req["state"] in ("fulfilled", "cancelled"):
            raise ApiError(409, "request_closed", f"请求已结束: {req['state']}")
        auth = evaluate_authorization(db, req["student_id"], req["collector_id"], ts)
        if not auth["authorized"]:
            db.execute(
                "UPDATE pickup_requests SET state='denied' WHERE id=?", (request_id,)
            )
            _audit(db, actor, "pickup_queued_denied",
                   {"request": request_id, "auth": auth}, ts)
            raise ApiError(403, "not_authorized",
                           f"接领资格核验未通过: {auth.get('reason')}")
        hid = handover_id or new_id("handover")
        if _row(db, "SELECT id FROM handovers WHERE id=?", (hid,)):
            raise ApiError(409, "handover_id_exists",
                           f"交接编号已存在: {hid}")
        db.execute(
            "INSERT INTO handovers(id, student_id, kind, to_party_type, to_party_id,"
            " collector_id, request_id, status, created_at, operator_id, auth_basis)"
            " VALUES (?, ?, 'pickup', 'person', ?, ?, ?, 'proposed', ?, ?, ?)",
            (hid, req["student_id"], req["collector_id"], req["collector_id"],
             request_id, ts, actor, _json(auth)),
        )
        db.execute(
            "UPDATE pickup_requests SET state='queued', note=? WHERE id=?",
            (_json({"handover": hid, "queued_at": ts}), request_id),
        )
        _audit(db, actor, "pickup_queued",
               {"request": request_id, "handover": hid, "auth": auth}, ts)
        return _handover_payload(_row(db, "SELECT * FROM handovers WHERE id=?",
                                      (hid,)))


def confirm_pickup(store: Store, actor: str, request_id: str, site_id: str,
                   as_of: str | None, idempotency_key: str | None = None) -> dict:
    """现场确认接领：必须由当前实际照管站点执行，并在交接时刻重新核验资格。

    撤回授权后排在队列里的交接在此被拦下；两岗并发确认时只有先提交者生效。
    """
    ts = effective_time(as_of)
    with store.tx() as db:
        dup = _idempotent(db, idempotency_key)
        if dup is not None:
            return _handover_payload(dup)
        req = _row(db, "SELECT * FROM pickup_requests WHERE id=?", (request_id,))
        if req is None:
            raise ApiError(404, "unknown_request", f"未知接领请求: {request_id}")
        hand = _row(
            db, "SELECT * FROM handovers WHERE request_id=? AND kind='pickup'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (request_id,),
        )
        if hand is None or hand["status"] != "proposed":
            if hand is not None and hand["status"] == "confirmed":
                raise ApiError(409, "handover_already_confirmed",
                               f"该接领已由交接 {hand['id']} 完成，不能重复生效")
            raise ApiError(409, "request_not_queued", "请求未处于可确认的排队状态")
        custody = get_custody(db, req["student_id"])
        if custody is None or custody["party_type"] != "site" \
                or custody["party_id"] != site_id:
            # 非照管方尝试只留审计，不改变交接状态——正确照管站随后仍可确认。
            # 学校可以登记请求或转达去向，但不能代现场照管方放行。
            _audit(db, actor, "pickup_release_refused",
                   {"request": request_id, "handover": hand["id"],
                    "site": site_id, "custody": custody}, ts)
            raise ApiError(
                409, "not_current_custodian",
                "本站点不是该学生当前照管方，不能代现场照管方放行",
            )
        # 重新核验：授权可能在排队后被撤回或过期
        snapshot = json.loads(hand["auth_basis"])
        delegation_id = snapshot.get("delegation_id")
        auth = evaluate_authorization(
            db, req["student_id"], req["collector_id"], ts, delegation_id
        )
        if not auth["authorized"]:
            db.execute(
                "UPDATE handovers SET status='blocked', auth_basis=?, detail=? WHERE id=?",
                (_json(auth),
                 _json({"reason": auth.get("reason"), "rechecked_at": ts}),
                 hand["id"]),
            )
            db.execute(
                "UPDATE pickup_requests SET state='denied' WHERE id=?", (request_id,)
            )
            did = new_id("dispute")
            db.execute(
                "INSERT INTO disputes(id, student_id, kind, state, detail, created_at)"
                " VALUES (?, ?, 'auth_revoked', 'open', ?, ?)",
                (did, req["student_id"],
                 _json({"request": request_id, "handover": hand["id"],
                        "auth": auth, "at": ts}), ts),
            )
            _audit(db, actor, "pickup_recheck_failed",
                   {"request": request_id, "handover": hand["id"], "auth": auth}, ts)
            raise ApiError(403, auth.get("reason", "not_authorized"),
                           "授权已变更，排队交接必须重新核验")
        db.execute(
            "UPDATE handovers SET status='confirmed', confirmed_at=?,"
            " from_party_type='site', from_party_id=?, operator_id=?,"
            " idempotency_key=?, auth_basis=? WHERE id=?",
            (ts, site_id, actor, idempotency_key, _json(auth), hand["id"]),
        )
        _transfer_custody(db, req["student_id"], "person", req["collector_id"],
                          actor, ts)
        db.execute(
            "UPDATE pickup_requests SET state='fulfilled' WHERE id=?", (request_id,)
        )
        _audit(db, actor, "pickup_confirmed",
               {"request": request_id, "handover": hand["id"],
                "student": req["student_id"], "collector": req["collector_id"],
                "auth": auth}, ts)
        return _handover_payload(_row(db, "SELECT * FROM handovers WHERE id=?",
                                      (hand["id"],)))


def reject_pickup(store: Store, actor: str, request_id: str, reason: str,
                  as_of: str | None) -> dict:
    ts = effective_time(as_of)
    with store.tx() as db:
        req = _row(db, "SELECT * FROM pickup_requests WHERE id=?", (request_id,))
        if req is None:
            raise ApiError(404, "unknown_request", f"未知接领请求: {request_id}")
        hand = _row(
            db, "SELECT * FROM handovers WHERE request_id=? AND kind='pickup'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (request_id,),
        )
        if hand is not None and hand["status"] == "proposed":
            db.execute(
                "UPDATE handovers SET status='rejected', detail=? WHERE id=?",
                (_json({"reason": reason, "at": ts}), hand["id"]),
            )
        db.execute(
            "UPDATE pickup_requests SET state='cancelled' WHERE id=?", (request_id,)
        )
        _audit(db, actor, "pickup_rejected",
               {"request": request_id, "reason": reason}, ts)
        return dict(_row(db, "SELECT * FROM pickup_requests WHERE id=?",
                         (request_id,)))


# ------------------------------------------------- 查找后的照管确认（争议解决）
def resolve_custody(store: Store, actor: str, student_id: str,
                    party_type: str, party_id: str, dispute_id: str | None,
                    resolution: str, as_of: str | None) -> dict:
    """配合查找有了新确认后，才能变更学生照管方，并闭合相关争议。"""
    if party_type not in VALID_PARTIES:
        raise ApiError(400, "invalid_party", "照管方类型应为 site/crew/person")
    ts = effective_time(as_of)
    with store.tx() as db:
        require_student(db, student_id)
        before = get_custody(db, student_id)
        hid = new_id("handover")
        db.execute(
            "INSERT INTO handovers(id, student_id, kind, to_party_type, to_party_id,"
            " status, created_at, confirmed_at, operator_id, detail)"
            " VALUES (?, ?, 'custody_review', ?, ?, 'confirmed', ?, ?, ?, ?)",
            (hid, student_id, party_type, party_id, ts, ts, actor,
             _json({"previous": before, "resolution": resolution,
                    "dispute": dispute_id})),
        )
        _transfer_custody(db, student_id, party_type, party_id, actor, ts)
        if dispute_id is not None:
            dispute = _row(db, "SELECT * FROM disputes WHERE id=?", (dispute_id,))
            if dispute is None:
                raise ApiError(404, "unknown_dispute", f"未知争议: {dispute_id}")
            if dispute["student_id"] != student_id:
                raise ApiError(400, "dispute_student_mismatch",
                               "争议与学生不匹配")
        targets = [dispute_id] if dispute_id else None
        if targets:
            db.execute(
                "UPDATE disputes SET state='resolved', resolved_at=?, resolution=?"
                " WHERE id=? AND state='open'",
                (ts, _json({"handover": hid, "note": resolution}), dispute_id),
            )
        else:
            db.execute(
                "UPDATE disputes SET state='resolved', resolved_at=?, resolution=?"
                " WHERE student_id=? AND state='open'",
                (ts, _json({"handover": hid, "note": resolution}), student_id),
            )
        _audit(db, actor, "custody_resolved",
               {"student": student_id, "handover": hid, "before": before,
                "after": {"party_type": party_type, "party_id": party_id},
                "dispute": dispute_id, "resolution": resolution}, ts)
        return {"handover": _handover_payload(
            _row(db, "SELECT * FROM handovers WHERE id=?", (hid,))),
            "custody": get_custody(db, student_id)}


# ------------------------------------------------------------- 门岗证据核对
def register_gate_evidence(store_or_db, actor: str, op_id: str, terminal: str,
                           student_id: str, handover_id: str,
                           collector_id: str | None, receipt_id: str | None,
                           observed_at: str | None, as_of: str | None) -> dict:
    """收入门岗终端的实物交接记录。

    第一个参数可传 Store（自行开事务），也可传已处于写事务中的连接，
    供门岗回执补传流程在同一事务内登记证据。

    证据独立于业务规则：匹配则标记 matched；找不到对应交接、或与已核实记录
    冲突（例如接领人不符、交接已拒绝/被取代），一律留 conflict 待核对，
    不触发任何放行，也不覆盖旧证据。
    """
    if isinstance(store_or_db, Store):
        with store_or_db.tx() as db:
            return _register_gate_evidence_db(
                db, actor, op_id, terminal, student_id, handover_id,
                collector_id, receipt_id, observed_at, as_of)
    return _register_gate_evidence_db(
        store_or_db, actor, op_id, terminal, student_id, handover_id,
        collector_id, receipt_id, observed_at, as_of)


def get_gate_evidence(db, op_id: str) -> dict | None:
    row = _row(db, "SELECT * FROM gate_evidence WHERE op_id=?", (op_id,))
    return dict(row) if row else None


def _register_gate_evidence_db(db, actor: str, op_id: str, terminal: str,
                               student_id: str, handover_id: str,
                               collector_id: str | None, receipt_id: str | None,
                               observed_at: str | None, as_of: str | None) -> dict:
    ts = effective_time(as_of)
    existing = _row(db, "SELECT * FROM gate_evidence WHERE op_id=?", (op_id,))
    if existing is not None:
        # 终端补传：同一证据 ID 原样返回，绝不重复记账
        return dict(existing)
    require_student(db, student_id)
    hand = _row(db, "SELECT * FROM handovers WHERE id=?", (handover_id,))
    state = "pending"
    detail: dict = {"at": ts}
    if hand is None:
        state = "conflict"
        detail["reason"] = "handover_not_found"
    elif hand["student_id"] != student_id:
        state = "conflict"
        detail["reason"] = "student_mismatch"
    elif hand["kind"] != "pickup":
        state = "conflict"
        detail["reason"] = "not_a_pickup_handover"
    elif hand["status"] == "confirmed":
        if collector_id and hand["collector_id"] != collector_id:
            state = "conflict"
            detail["reason"] = "collector_mismatch"
            detail["recorded_collector"] = hand["collector_id"]
        else:
            state = "matched"
    elif hand["status"] in ("rejected", "superseded", "blocked"):
        state = "conflict"
        detail["reason"] = f"handover_{hand['status']}"
    else:
        detail["reason"] = "handover_not_confirmed_yet"
    db.execute(
        "INSERT INTO gate_evidence(op_id, terminal, student_id, handover_id,"
        " collector_id, receipt_id, observed_at, state, detail, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, terminal, student_id, handover_id, collector_id, receipt_id,
         observed_at, state, _json(detail), ts, ts),
    )
    if state == "conflict":
        did = new_id("dispute")
        db.execute(
            "INSERT INTO disputes(id, student_id, kind, state, detail, created_at)"
            " VALUES (?, ?, 'gate_conflict', 'open', ?, ?)",
            (did, student_id,
             _json({"op_id": op_id, "handover": handover_id,
                    "terminal": terminal, "detail": detail}), ts),
        )
    _audit(db, actor, "gate_evidence_registered",
           {"op_id": op_id, "state": state, "student": student_id,
            "handover": handover_id}, ts)
    return dict(_row(db, "SELECT * FROM gate_evidence WHERE op_id=?", (op_id,)))


def review_gate_evidence(store: Store, actor: str, op_id: str, decision: str,
                         note: str, as_of: str | None) -> dict:
    """人工核对门岗证据：只改证据状态与争议，不会补签交接或改变照管方。"""
    if decision not in ("matched", "rejected"):
        raise ApiError(400, "invalid_decision", "核对结论应为 matched/rejected")
    ts = effective_time(as_of)
    with store.tx() as db:
        ev = _row(db, "SELECT * FROM gate_evidence WHERE op_id=?", (op_id,))
        if ev is None:
            raise ApiError(404, "unknown_evidence", f"未知门岗证据: {op_id}")
        db.execute(
            "UPDATE gate_evidence SET state=?, detail=?, updated_at=? WHERE op_id=?",
            (decision,
             _json({**json.loads(ev["detail"] or "{}"), "review": note,
                    "reviewed_at": ts}),
             ts, op_id),
        )
        db.execute(
            "UPDATE disputes SET state='resolved', resolved_at=?, resolution=?"
            " WHERE student_id=? AND kind='gate_conflict' AND state='open'"
            " AND detail LIKE ?",
            (ts, _json({"op_id": op_id, "decision": decision, "note": note}),
             ev["student_id"], f'%"op_id": "{op_id}"%'),
        )
        _audit(db, actor, "gate_evidence_reviewed",
               {"op_id": op_id, "decision": decision, "note": note}, ts)
        return dict(_row(db, "SELECT * FROM gate_evidence WHERE op_id=?", (op_id,)))


# ------------------------------------------------------------- 代领授权管理
def create_delegation(store: Store, actor: str, delegation_id: str, student_id: str,
                      guardian_id: str, delegate_id: str, expires_at: str,
                      as_of: str | None) -> dict:
    ts = effective_time(as_of)
    _parse_ts(expires_at)
    with store.tx() as db:
        require_student(db, student_id)
        if _row(db, "SELECT id FROM delegations WHERE id=?", (delegation_id,)):
            raise ApiError(409, "delegation_exists", f"授权已存在: {delegation_id}")
        guardian = _row(
            db, "SELECT guardian_id FROM guardians WHERE student_id=?", (student_id,)
        )
        if guardian is None or guardian["guardian_id"] != guardian_id:
            raise ApiError(403, "not_registered_guardian",
                           "只有登记监护人可以为该学生设立代领授权")
        if _parse_ts(expires_at) <= _parse_ts(ts):
            raise ApiError(400, "invalid_expiry", "授权时限必须晚于当前时刻")
        db.execute(
            "INSERT INTO delegations(id, student_id, guardian_id, delegate_id,"
            " expires_at, created_at) VALUES (?,?,?,?,?,?)",
            (delegation_id, student_id, guardian_id, delegate_id, expires_at, ts),
        )
        _audit(db, actor, "delegation_created",
               {"delegation": delegation_id, "student": student_id,
                "delegate": delegate_id, "expires_at": expires_at}, ts)
        return dict(_row(db, "SELECT * FROM delegations WHERE id=?",
                         (delegation_id,)))


def revoke_delegation(store: Store, actor: str, delegation_id: str,
                      as_of: str | None) -> dict:
    ts = effective_time(as_of)
    with store.tx() as db:
        dele = _row(db, "SELECT * FROM delegations WHERE id=?", (delegation_id,))
        if dele is None:
            raise ApiError(404, "unknown_delegation", f"未知授权: {delegation_id}")
        if dele["revoked_at"] is not None:
            return dict(dele)
        db.execute(
            "UPDATE delegations SET revoked_at=? WHERE id=?", (ts, delegation_id)
        )
        # 撤回后尚未完成的排队交接标记为需重新核验（不删除，保留事实链）
        pending = _all(
            db,
            "SELECT h.id FROM handovers h JOIN pickup_requests p"
            " ON h.request_id=p.id WHERE h.student_id=? AND h.kind='pickup'"
            " AND h.status='proposed' AND h.auth_basis LIKE ?",
            (dele["student_id"], f'%"delegation_id": "{delegation_id}"%'),
        )
        for row in pending:
            db.execute(
                "UPDATE handovers SET detail=? WHERE id=?",
                (_json({"reverify_required": True, "reason": "delegation_revoked",
                        "at": ts}), row["id"]),
            )
        _audit(db, actor, "delegation_revoked",
               {"delegation": delegation_id,
                "pending_handovers": [r["id"] for r in pending]}, ts)
        return dict(_row(db, "SELECT * FROM delegations WHERE id=?",
                         (delegation_id,)))


# ------------------------------------------------------------------ 查询视图
def student_visible_to_site(db, student_id: str, site_id: str) -> bool:
    """值班台最小可见：当前在本站照管，或经停本站的班次涉及该学生。"""
    custody = get_custody(db, student_id)
    if custody is not None and custody["party_type"] == "site" \
            and custody["party_id"] == site_id:
        return True
    row = _row(
        db,
        "SELECT 1 FROM trips t JOIN trip_roster r ON t.id=r.trip_id"
        " WHERE r.student_id=? AND (t.from_site=? OR t.to_site=?) LIMIT 1",
        (student_id, site_id, site_id),
    )
    return row is not None


def student_snapshot(db, student_id: str) -> dict:
    require_student(db, student_id)
    custody = get_custody(db, student_id)
    dispute = _open_dispute(db, student_id)
    handovers = _all(
        db, "SELECT * FROM handovers WHERE student_id=? ORDER BY created_at, id",
        (student_id,),
    )
    requests = _all(
        db, "SELECT * FROM pickup_requests WHERE student_id=?"
        " ORDER BY registered_at, id",
        (student_id,),
    )
    evidence = _all(
        db, "SELECT op_id, terminal, handover_id, receipt_id, observed_at, state"
        " FROM gate_evidence WHERE student_id=? ORDER BY created_at, op_id",
        (student_id,),
    )
    trips = _all(
        db, "SELECT t.* FROM trips t JOIN trip_roster r ON t.id=r.trip_id"
        " WHERE r.student_id=? ORDER BY t.created_at",
        (student_id,),
    )
    return {
        "student": student_id,
        "custody": custody,
        "open_dispute": dispute,
        "handovers": [_handover_payload(r) for r in handovers],
        "pickup_requests": [dict(r) for r in requests],
        "gate_evidence": [dict(r) for r in evidence],
        "trips": [dict(r) for r in trips],
    }


def desk_queue(store: Store, site_id: str) -> dict:
    """值班台四类工作台：待交接 / 转运中 / 已签收 / 有争议。"""
    db = store.conn
    pending, in_transit, signed, disputed = [], [], [], []
    rows = _all(
        db,
        "SELECT s.id AS student_id FROM students s ORDER BY s.id",
    )
    for row in rows:
        sid = row["student_id"]
        snap = student_snapshot(db, sid)
        custody = snap["custody"]
        # 最小可见：本站当前照管、本站相关交接/请求、或经本站班次的学生
        relevant = custody is not None and custody["party_type"] == "site" \
            and custody["party_id"] == site_id
        trip_here = any(
            t["from_site"] == site_id or t["to_site"] == site_id
            for t in snap["trips"]
        )
        request_here = any(r["state"] in ("registered", "queued")
                           for r in snap["pickup_requests"])
        if not (relevant or trip_here or request_here):
            continue
        item = {"student": sid, "custody": custody,
                "open_dispute": snap["open_dispute"]}
        if snap["open_dispute"] is not None:
            disputed.append(item)
        if custody is None:
            continue
        if custody["party_type"] == "site" and custody["party_id"] == site_id:
            if request_here:
                pending.append(item)
            else:
                signed.append(item)
        elif custody["party_type"] == "crew":
            in_transit.append(item)
        elif custody["party_type"] == "person":
            signed.append(item)
    return {"site": site_id, "pending_handover": pending,
            "in_transit": in_transit, "signed": signed, "disputed": disputed}


def crew_view(store: Store, trip_id: str) -> dict:
    db = store.conn
    trip = _row(db, "SELECT * FROM trips WHERE id=?", (trip_id,))
    if trip is None:
        raise ApiError(404, "unknown_trip", f"未知班次: {trip_id}")
    roster = []
    for r in _all(db, "SELECT * FROM trip_roster WHERE trip_id=? ORDER BY rowid",
                  (trip_id,)):
        entry = dict(r)
        entry["custody"] = get_custody(db, r["student_id"])
        entry["open_dispute"] = _open_dispute(db, r["student_id"])
        roster.append(entry)
    return {"trip": dict(trip), "roster": roster}


def shift_change_report(store: Store, site_id: str) -> dict:
    """交班：谁、凭什么记录、在什么时间把孩子接走/转入本站。"""
    db = store.conn
    handovers = _all(
        db,
        "SELECT * FROM handovers WHERE"
        " (to_party_type='site' AND to_party_id=?)"
        " OR (from_party_type='site' AND from_party_id=?)"
        " ORDER BY COALESCE(confirmed_at, created_at), id",
        (site_id, site_id),
    )
    log = _all(
        db,
        "SELECT ts, actor, action, payload FROM audit_log ORDER BY id",
    )
    return {
        "site": site_id,
        "handovers": [_handover_payload(r) for r in handovers],
        "audit": [dict(r, payload=json.loads(r["payload"])) for r in log],
    }


def parent_status(store: Store, collector_id: str, request_id: str | None) -> dict:
    """家长侧：只能查看与自己接领请求有关的状态，看不到他人资料。"""
    db = store.conn
    sql = ("SELECT p.* FROM pickup_requests p"
           " JOIN collector_tokens t ON t.request_id=p.id"
           " WHERE t.collector_id=?")
    params: list = [collector_id]
    if request_id is not None:
        sql += " AND p.id=?"
        params.append(request_id)
    reqs = _all(db, sql + " ORDER BY p.registered_at, p.id", params)
    if request_id is not None and not reqs:
        # 也允许按本人登记的请求直接查（令牌未绑定具体请求时）
        reqs = _all(
            db, "SELECT * FROM pickup_requests WHERE collector_id=? AND id=?"
            " ORDER BY registered_at",
            (collector_id, request_id),
        )
    result = []
    for req in reqs:
        if req["collector_id"] != collector_id:
            continue
        hand = _row(
            db, "SELECT * FROM handovers WHERE request_id=? AND kind='pickup'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (req["id"],),
        )
        evidence = _all(
            db, "SELECT op_id, receipt_id, observed_at, state FROM gate_evidence"
            " WHERE handover_id=? ORDER BY created_at",
            (hand["id"],) if hand else (None,),
        )
        result.append({
            "request": dict(req),
            "relay": json.loads(req["note"]) if req["note"] and req["note"].startswith(
                '{"relay"') else None,
            "handover": _handover_payload(hand) if hand else None,
            "gate_evidence": [dict(r) for r in evidence],
        })
    return {"collector": collector_id, "requests": result}
