"""从演练台账（fixtures/registers.json）幂等装载业务数据。

重复执行不会重复插入；服务重启也可直接运行，已核实的交接事实保持不变。
同时发放演示用令牌：operator id 本身即可作为 Bearer 令牌，
家长侧令牌通过返回值/标准输出给出。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import domain
from .storage import Store
import secrets


def seed(store: Store, fixtures_path: str | Path) -> dict:
    data = json.loads(Path(fixtures_path).read_text(encoding="utf-8"))
    issued: dict[str, str] = {}
    with store.tx() as db:
        for site in data.get("sites", []):
            db.execute(
                "INSERT INTO sites(id, name) VALUES (?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (site, site),
            )
        trip_ids = {t["id"] for t in data.get("trips", [])}
        if data.get("trip"):
            trip_ids.add(data["trip"]["id"])
        site_ids = set(data.get("sites", []))
        for operator_id, scope in data.get("operators", {}).items():
            if scope in trip_ids:
                role = "crew"
            elif scope in site_ids:
                role = "desk"
            else:
                role = "crew"  # 未知 scope 默认按随车岗位，权限最小化
            db.execute(
                "INSERT INTO operators(id, role, scope) VALUES (?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (operator_id, role, scope),
            )
        for student_id in data.get("students", []):
            db.execute(
                "INSERT INTO students(id, home_site) VALUES (?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (student_id, data["sites"][0]),
            )
        for student_id, guardian_id in data.get("guardians", {}).items():
            db.execute(
                "INSERT INTO guardians(student_id, guardian_id) VALUES (?,?) "
                "ON CONFLICT(student_id, guardian_id) DO NOTHING",
                (student_id, guardian_id),
            )

        trip = data.get("trip")
        if trip and not db.execute(
                "SELECT 1 FROM trips WHERE id=?", (trip["id"],)).fetchone():
            times = data.get("trip_times", {})
            db.execute(
                "INSERT INTO trips(id, from_site, to_site, status, created_at,"
                " departed_at, arrived_at) VALUES (?,?,?,?,?,?,?)",
                (trip["id"], trip["from"], trip["to"], "planned",
                 times.get("departure", domain.now_iso()), None, None),
            )
            for student_id in trip["booked"]:
                db.execute(
                    "INSERT INTO trip_roster(trip_id, student_id, booked,"
                    " boarded, counted) VALUES (?,?,1,0,0)",
                    (trip["id"], student_id),
                )

        dele = data.get("delegation")
        if dele and not db.execute(
                "SELECT 1 FROM delegations WHERE id=?",
                (dele["id"],)).fetchone():
            db.execute(
                "INSERT INTO delegations(id, student_id, guardian_id, delegate_id,"
                " expires_at, revoked_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (dele["id"], dele["student"], dele["guardian"], dele["delegate"],
                 dele["expires_at"], dele.get("revoked_at"),
                 data.get("trip_times", {}).get("departure",
                                                 domain.now_iso())),
            )

        for req in data.get("gate_requests", []):
            if not db.execute("SELECT 1 FROM pickup_requests WHERE id=?",
                              (req["id"],)).fetchone():
                db.execute(
                    "INSERT INTO pickup_requests(id, student_id, collector_id,"
                    " state, registered_at, note) VALUES (?, ?, ?, 'registered', ?, ?)",
                    (req["id"], req["student"], req["collector"],
                     req["registered_at"], "来自演练台账 gate_requests"),
                )

        # 初始照管方：学生均由出发校区照管（幂等）
        for student_id in data.get("students", []):
            if not db.execute(
                    "SELECT 1 FROM custody WHERE student_id=?",
                    (student_id,)).fetchone():
                db.execute(
                    "INSERT INTO custody(student_id, party_type, party_id, since,"
                    " updated_by) VALUES (?, 'site', ?, ?, 'bootstrap')",
                    (student_id, data["sites"][0], domain.now_iso()),
                )

        # 家长侧演示令牌（幂等：同一 collector + request 复用既有令牌）
        for req in data.get("gate_requests", []):
            existing = db.execute(
                "SELECT token FROM collector_tokens WHERE collector_id=?"
                " AND request_id=?",
                (req["collector"], req["id"]),
            ).fetchone()
            if existing:
                issued[f"{req['collector']}:{req['id']}"] = existing["token"]
            else:
                token = "tok_" + secrets.token_urlsafe(24)
                db.execute(
                    "INSERT INTO collector_tokens(token, collector_id, request_id,"
                    " issued_at) VALUES (?,?,?,?)",
                    (token, req["collector"], req["id"], domain.now_iso()),
                )
                issued[f"{req['collector']}:{req['id']}"] = token

        db.execute(
            "INSERT INTO meta(key, value) VALUES('seeded_fixture', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(fixtures_path),),
        )
    return issued
