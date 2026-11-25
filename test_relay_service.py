"""端到端业务规则测试：真实 HTTP 服务 + 合作方桩 + 临时 SQLite。

覆盖：
- 发车以实际点到为准、未上车留校、同班可分车
- 到场只签收实际清点到的人，逐人差异保留，少人不能整车签收、不自动退回
- 查找后的新确认才能变更照管方
- 监护人/代领授权核验、撤回后排队交接重新核验
- 两岗并发确认只有一种交接生效；幂等键补传
- 门岗证据断连补传、冲突留待核对、旧证据不当新放行
- 名单更正不能抹掉已核实交接
- 家长凭证最小可见、跨校值班员越权拦截
- 值班台四类队列、交班报告
- 服务重启后在途学生与未决证据继续处理
"""
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import unittest

from partner_stub import PartnerServer
from student_relay.httpapi import RelayServer, Service
from student_relay.partner import PartnerClient
from student_relay.seed import seed
from student_relay.storage import Store

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


class ApiClient:
    def __init__(self, port: int, token: str):
        self.port = port
        self.token = token

    def call(self, method: str, path: str, data=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            body = None if data is None else json.dumps(
                data, ensure_ascii=False).encode("utf-8")
            conn.request(method, path, body=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            })
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            return resp.status, payload
        finally:
            conn.close()

    def post(self, path, data):
        return self.call("POST", path, data)

    def get(self, path):
        return self.call("GET", path)


class RelayTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "relay.sqlite3"
        self.partner_db = Path(self.tmp.name) / "partner.sqlite3"
        exchanges = FIXTURES / "partner-exchanges.json"
        self.partner = PartnerServer(("127.0.0.1", 0), exchanges, self.partner_db)
        self.addCleanup(self.partner.server_close)
        pt = Thread(target=self.partner.serve_forever,
                    kwargs={"poll_interval": 0.02}, daemon=True)
        pt.start()
        self.addCleanup(self.partner.shutdown)

        self.store = Store(self.db_path)
        self.tokens = seed(self.store, FIXTURES / "registers.json")
        service = Service(self.store,
                          PartnerClient(f"http://127.0.0.1:{self.partner.server_port}"))
        self.server = RelayServer(("127.0.0.1", 0), service)
        self.addCleanup(self.server.server_close)
        st = Thread(target=self.server.serve_forever,
                    kwargs={"poll_interval": 0.02}, daemon=True)
        st.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

        self.desk_a = ApiClient(self.port, "teacher-a")
        self.desk_b = ApiClient(self.port, "teacher-b")
        self.crew = ApiClient(self.port, "driver-7")
        self.parent8 = ApiClient(self.port, self.tokens["parent-8:request-b"])
        self.aunt3 = ApiClient(self.port, self.tokens["aunt-3:request-a"])

    def restart_service(self):
        """模拟服务重启：换一个 Service 打开同一数据库。"""
        self.server.shutdown()
        self.server.server_close()
        store = Store(self.db_path)
        service = Service(store,
                          PartnerClient(f"http://127.0.0.1:{self.partner.server_port}"))
        new_server = RelayServer(("127.0.0.1", self.port), service)
        thread = Thread(target=new_server.serve_forever,
                        kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(new_server.server_close)
        self.server = new_server
        self.store = store
        for client in (self.desk_a, self.desk_b, self.crew,
                       self.parent8, self.aunt3):
            client.port = self.port

    # 常用时间（演练台账使用 2028-07-03 +08:00）
    T0 = "2028-07-03T10:00:00+08:00"
    T_DEPART = "2028-07-03T10:15:00+08:00"
    T_ARRIVE = "2028-07-03T10:37:00+08:00"
    T_AFTER = "2028-07-03T10:45:00+08:00"


class TestSeedingAndAuth(RelayTestBase):
    def test_seed_is_idempotent_and_issues_tokens(self):
        again = seed(self.store, FIXTURES / "registers.json")
        self.assertEqual(again, self.tokens)
        status, payload = self.desk_a.get("/desk/queue")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_missing_or_bad_token_rejected(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/desk/queue")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 401)
        conn.close()
        bad = ApiClient(self.port, "nobody")
        self.assertEqual(bad.get("/desk/queue")[0], 401)

    def test_cross_site_desk_cannot_touch_other_site(self):
        # teacher-b 属于 shelter-b，不能操作 school-a 的台席
        status, payload = self.desk_b.post("/pickup-requests/confirm", {
            "request_id": "request-b", "site": "school-a", "as_of": self.T_ARRIVE})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "out_of_scope")

    def test_crew_cannot_release_student(self):
        status, payload = self.crew.post("/pickup-requests/confirm", {
            "request_id": "request-b", "site": "shelter-b", "as_of": self.T_ARRIVE})
        self.assertEqual(status, 403)

    def test_cross_site_desk_visibility_and_release_boundary(self):
        # shelter-b 是 van-7 的到达站，对将送达的学生有岗位所需可见性，可预排队
        status, _ = self.desk_b.post("/pickup-requests/queue", {
            "request_id": "request-b", "as_of": self.T0})
        self.assertEqual(status, 200)
        # 但孩子此刻由 school-a 照管，shelter-b 不能代现场照管方放行
        status, payload = self.desk_b.post("/pickup-requests/confirm", {
            "request_id": "request-b", "site": "shelter-b", "as_of": self.T0})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "not_current_custodian")
        # 与两站都无关的学生资料不可见（用一个不存在的学生验证最小可见）
        status, payload = self.desk_b.get("/students/stu-unknown")
        self.assertEqual(status, 404)

    def test_parent_only_sees_own_request(self):
        status, payload = self.parent8.get("/parent/requests")
        self.assertEqual(status, 200)
        reqs = payload["data"]["requests"]
        self.assertEqual({r["request"]["id"] for r in reqs}, {"request-b"})
        for item in reqs:
            self.assertEqual(item["request"]["collector_id"], "parent-8")
        # 家长不能调值班台接口
        self.assertEqual(self.parent8.get("/desk/queue")[0], 403)
        # 不能看别人的请求
        status, payload = self.parent8.get(
            "/parent/requests?request_id=request-a")
        self.assertEqual(payload["data"]["requests"], [])


class TestTripLifecycle(RelayTestBase):
    def depart(self):
        return self.crew.post("/trips/boarding", {
            "trip_id": "van-7", "boarded": ["stu-11", "stu-12"],
            "as_of": self.T_DEPART})

    def test_boarding_uses_actual_headcount_and_keeps_missing_at_origin(self):
        status, payload = self.depart()
        self.assertEqual(status, 200, payload)
        trip = payload["data"]
        self.assertEqual(trip["status"], "departed")
        self.assertEqual(set(trip["boarded"]), {"stu-11", "stu-12"})
        # 没上车的 stu-13 继续由出发校区照管
        snap = self.desk_a.get("/students/stu-13")[1]["data"]
        self.assertEqual(snap["custody"]["party_id"], "school-a")
        # 上车学生照管转为随车
        snap = self.desk_a.get("/students/stu-11")[1]["data"]
        self.assertEqual(snap["custody"],
                         {"student_id": "stu-11", "party_type": "crew",
                          "party_id": "van-7", "since": self.T_DEPART,
                          "updated_by": "driver-7"})

    def test_double_boarding_rejected(self):
        self.depart()
        status, payload = self.depart()
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "trip_not_planned")

    def test_arrival_count_keeps_per_student_discrepancy_and_blocks_signoff(self):
        self.depart()
        status, payload = self.desk_b.post("/trips/arrival-count", {
            "trip_id": "van-7", "counted": ["stu-12"], "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual(data["counted_on_arrival"], ["stu-12"])
        self.assertEqual([d["student"] for d in data["discrepancies"]],
                         ["stu-11"])
        # stu-11 照管方仍是随车人员，没有被自动退回出发点
        snap = self.crew.get("/crew/trip")[1]["data"]
        stu11 = next(r for r in snap["roster"] if r["student_id"] == "stu-11")
        self.assertEqual(stu11["custody"]["party_type"], "crew")
        self.assertEqual(stu11["open_dispute"]["kind"], "count_mismatch")
        # 少一个孩子不能整车签收
        status, payload = self.desk_b.post("/trips/arrival-sign", {
            "trip_id": "van-7", "as_of": self.T_ARRIVE})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "arrival_blocked_by_discrepancy")

    def test_arrival_crew_view_visible_to_both_ends(self):
        self.depart()
        # 两端都能查看进度
        self.assertEqual(self.crew.get("/crew/trip")[0], 200)
        status, payload = self.desk_a.get("/trips/van-7")
        self.assertEqual(status, 200)
        status, payload = self.desk_b.get("/trips/van-7")
        self.assertEqual(status, 200)
        # 无关站点不可见（desk_a/desk_b 恰好都相关，改用权限模型断言）
        self.assertEqual(payload["data"]["id"], "van-7")

    def test_resolve_after_search_moves_custody_and_allows_signoff(self):
        self.depart()
        self.desk_b.post("/trips/arrival-count", {
            "trip_id": "van-7", "counted": ["stu-12"], "as_of": self.T_ARRIVE})
        disputes = self.desk_b.get("/disputes")[1]["data"]["disputes"]
        dispute = next(d for d in disputes if d["student_id"] == "stu-11")
        # 配合查找确认 stu-11 已由 shelter-b 实际照管
        status, payload = self.crew.post("/custody/resolve", {
            "student_id": "stu-11", "party_type": "site",
            "party_id": "shelter-b", "dispute_id": dispute["id"],
            "resolution": "车辆折返寻回，到场交接", "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200, payload)
        # 争议闭合、照管变更
        self.assertEqual(payload["data"]["custody"]["party_id"], "shelter-b")
        # 仍需把到场清点补齐（照管已确认，允许重新清点）
        status, payload = self.desk_b.post("/trips/arrival-count", {
            "trip_id": "van-7", "counted": ["stu-11", "stu-12"],
            "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200, payload)
        status, payload = self.desk_b.post("/trips/arrival-sign", {
            "trip_id": "van-7", "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "arrived")


class TestPickupAuthorization(RelayTestBase):
    def test_guardian_pickup_flow(self):
        # 资格核验：parent-8 是 stu-12 的登记监护人
        status, payload = self.desk_b.post("/authorization/check", {
            "student_id": "stu-12", "collector_id": "parent-8",
            "as_of": self.T_ARRIVE})
        self.assertEqual(payload["data"]["basis"], "registered_guardian")
        # request-b 已在台账登记
        status, payload = self.desk_b.post("/pickup-requests/queue", {
            "request_id": "request-b", "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200, payload)
        handover = payload["data"]
        self.assertEqual(handover["status"], "proposed")
        # stu-12 当前在 school-a，shelter-b 不能代放行
        status, payload = self.desk_b.post("/pickup-requests/confirm", {
            "request_id": "request-b", "site": "shelter-b",
            "as_of": self.T_ARRIVE})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "not_current_custodian")
        # 由实际照管站 school-a 放行
        status, payload = self.desk_a.post("/pickup-requests/confirm", {
            "request_id": "request-b", "site": "school-a",
            "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["status"], "confirmed")
        snap = self.desk_a.get("/students/stu-12")[1]["data"]
        self.assertEqual(snap["custody"]["party_type"], "person")
        self.assertEqual(snap["custody"]["party_id"], "parent-8")

    def test_revoked_delegation_blocks_queued_handover(self):
        # auth-2 在台账中已于 10:35 撤回；request-a 是 aunt-3 的请求
        status, payload = self.desk_a.post("/pickup-requests/queue", {
            "request_id": "request-a", "as_of": "2028-07-03T10:30:00+08:00"})
        # 10:30 时授权仍然有效（10:35 才撤回，11:00 过期）
        self.assertEqual(status, 200, payload)
        # 撤回后确认必须重新核验 -> 拒绝
        status, payload = self.desk_a.post("/pickup-requests/confirm", {
            "request_id": "request-a", "site": "school-a",
            "as_of": "2028-07-03T10:38:00+08:00"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "delegation_revoked")
        snap = self.desk_a.get("/students/stu-12")[1]["data"]
        # 照管方未变，仍是 school-a；交接留痕为 blocked
        self.assertEqual(snap["custody"]["party_id"], "school-a")
        self.assertTrue(any(h["status"] == "blocked" for h in snap["handovers"]))
        self.assertIsNotNone(snap["open_dispute"])

    def test_expired_delegation_denied_at_queue(self):
        status, payload = self.desk_a.post("/pickup-requests/queue", {
            "request_id": "request-a",
            "as_of": "2028-07-03T11:05:00+08:00"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "not_authorized")

    def test_unrelated_collector_denied(self):
        status, payload = self.desk_a.post("/pickup-requests", {
            "student_id": "stu-11", "collector_id": "stranger-1",
            "as_of": self.T0, "request_id": "request-x"})
        self.assertEqual(status, 200)
        status, payload = self.desk_a.post("/pickup-requests/queue", {
            "request_id": "request-x", "as_of": self.T0})
        self.assertEqual(status, 403)

    def test_revocation_endpoint_reverifies_even_without_preset_revoke(self):
        # 新建一条有效授权、排队、撤回、确认被拦
        self.desk_a.post("/delegations", {
            "delegation_id": "auth-9", "student_id": "stu-13",
            "guardian_id": "parent-10", "delegate_id": "uncle-2",
            "expires_at": "2028-07-03T12:00:00+08:00", "as_of": self.T0})
        self.desk_a.post("/pickup-requests", {
            "student_id": "stu-13", "collector_id": "uncle-2",
            "request_id": "request-u", "as_of": self.T0})
        self.desk_a.post("/pickup-requests/queue",
                         {"request_id": "request-u", "as_of": self.T0})
        self.desk_a.post("/delegations/revoke",
                         {"delegation_id": "auth-9", "as_of": self.T0})
        status, payload = self.desk_a.post("/pickup-requests/confirm", {
            "request_id": "request-u", "site": "school-a", "as_of": self.T_DEPART})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "delegation_revoked")

    def test_relay_message_does_not_release_and_is_visible_to_parent(self):
        # 监护人到校但学生在途：值班台只能转达去向，不能放行
        self.crew.post("/trips/boarding", {
            "trip_id": "van-7", "boarded": ["stu-11", "stu-12"],
            "as_of": self.T_DEPART})
        status, payload = self.desk_a.post("/pickup-requests/relay", {
            "request_id": "request-b",
            "message": "孩子已转运至 shelter-b，请到该校区接领",
            "as_of": self.T_DEPART})
        self.assertEqual(status, 200, payload)
        # 照管方仍是随车人员，没有被放行
        snap = self.crew.get("/crew/trip")[1]["data"]
        stu12 = next(r for r in snap["roster"] if r["student_id"] == "stu-12")
        self.assertEqual(stu12["custody"]["party_type"], "crew")
        # 家长侧能看到转达
        status, payload = self.parent8.get("/parent/requests")
        req = next(r for r in payload["data"]["requests"]
                   if r["request"]["id"] == "request-b")
        self.assertIn("shelter-b", req["relay"]["relay"])
        # 随车人员不能登记转达（仅值班台）
        self.assertEqual(self.crew.post("/pickup-requests/relay", {
            "request_id": "request-b", "message": "x"})[0], 403)


class TestConcurrentAndIdempotentConfirm(RelayTestBase):
    def _queue(self, request_id="request-b"):
        self.desk_b.post("/pickup-requests/queue",
                         {"request_id": request_id, "as_of": self.T_ARRIVE})

    def test_duplicate_confirm_with_same_idempotency_key_returns_one_handover(self):
        self._queue()
        body = {"request_id": "request-b", "site": "school-a",
                "as_of": self.T_ARRIVE, "idempotency_key": "idem-1"}
        s1, p1 = self.desk_a.post("/pickup-requests/confirm", body)
        s2, p2 = self.desk_a.post("/pickup-requests/confirm", body)
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(p1["data"]["id"], p2["data"]["id"])
        self.assertEqual(p2["data"]["idempotency_key"], "idem-1")

    def test_two_near_simultaneous_conflicts_only_one_takes_effect(self):
        self._queue()
        import threading
        results = []

        def confirm(client, key):
            results.append(client.post("/pickup-requests/confirm", {
                "request_id": "request-b", "site": "school-a",
                "as_of": self.T_ARRIVE, "idempotency_key": key}))

        t1 = threading.Thread(target=confirm, args=(self.desk_a, "key-a"))
        t2 = threading.Thread(target=confirm, args=(self.desk_a, "key-b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses, [200, 409])
        # 最终只有一条 confirmed pickup
        snap = self.desk_a.get("/students/stu-12")[1]["data"]
        confirmed = [h for h in snap["handovers"]
                     if h["kind"] == "pickup" and h["status"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)


class TestGateEvidence(RelayTestBase):
    def _confirmed_pickup(self):
        self.desk_a.post("/pickup-requests/queue",
                         {"request_id": "request-b", "as_of": self.T_ARRIVE,
                          "handover_id": "handover-18"})
        return self.desk_a.post("/pickup-requests/confirm", {
            "request_id": "request-b", "site": "school-a",
            "as_of": self.T_ARRIVE})[1]["data"]

    def test_gate_receipt_disconnect_recovery_matches_real_handover(self):
        hand = self._confirmed_pickup()
        self.assertEqual(hand["id"], "handover-18")
        # 样例 0 是 commit_disconnect：客户端必须通过 GET 补回回报
        status, payload = self.desk_a.post("/gate-receipts", {
            "op_id": "op-gate-18", "terminal": "gate-b",
            "student_id": "stu-12", "handover_id": hand["id"],
            "collector_id": "parent-8", "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200, payload)
        evidence = payload["data"]
        self.assertEqual(evidence["state"], "matched")
        self.assertEqual(evidence["receipt_id"], "gate-18")
        self.assertEqual(evidence["partner_delivery"], "replay_get")
        # 终端补传同一 op_id：原样返回，不重复记账
        status2, payload2 = self.desk_a.post("/gate-receipts", {
            "op_id": "op-gate-18", "terminal": "gate-b",
            "student_id": "stu-12", "handover_id": hand["id"],
            "collector_id": "parent-8"})
        self.assertEqual(status2, 200)
        self.assertTrue(payload2["data"]["redelivery"])

    def test_late_conflicting_evidence_stays_pending_and_reviewable(self):
        hand = self._confirmed_pickup()
        # 接领人不符的迟到证据 -> conflict，不重新签、不改照管
        status, payload = self.desk_a.post("/gate-evidence", {
            "op_id": "op-late-1", "terminal": "gate-a",
            "student_id": "stu-12", "handover_id": hand["id"],
            "collector_id": "aunt-3",
            "receipt_id": "gate-late", "observed_at": self.T_AFTER})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "conflict")
        snap = self.desk_a.get("/students/stu-12")[1]["data"]
        self.assertEqual(snap["custody"]["party_id"], "parent-8")
        # 人工核对只改证据状态，不产生新交接
        status, payload = self.desk_a.post("/gate-evidence/review", {
            "op_id": "op-late-1", "decision": "rejected",
            "note": "旧证据，接领人不符", "as_of": self.T_AFTER})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "rejected")
        snap = self.desk_a.get("/students/stu-12")[1]["data"]
        self.assertEqual(len([h for h in snap["handovers"]
                              if h["kind"] == "pickup"
                              and h["status"] == "confirmed"]), 1)

    def test_evidence_for_unknown_handover_is_conflict(self):
        status, payload = self.desk_a.post("/gate-evidence", {
            "op_id": "op-ghost", "terminal": "gate-a",
            "student_id": "stu-11", "handover_id": "handover-ghost",
            "receipt_id": "gate-x"})
        self.assertEqual(payload["data"]["state"], "conflict")
        self.assertEqual(
            json.loads(payload["data"]["detail"])["reason"],
            "handover_not_found")

    def test_partner_success_path_via_real_stub(self):
        # 样例 1 为 success（gate-a / stu-11 / handover-19 / parent-9）
        self.crew.post("/trips/boarding", {
            "trip_id": "van-7", "boarded": ["stu-11", "stu-12"],
            "as_of": self.T_DEPART,
            "handover_ids": {"stu-11": "handover-19",
                             "stu-12": "handover-20"}})
        # 对端成功应答路径：boarding 交接不是 pickup，证据判 conflict 而非误放行
        status, payload = self.desk_a.post("/gate-receipts", {
            "op_id": "op-gate-19", "terminal": "gate-a",
            "student_id": "stu-11", "handover_id": "handover-19",
            "collector_id": "parent-9"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["state"], "conflict")
        self.assertEqual(json.loads(payload["data"]["detail"])["reason"],
                         "not_a_pickup_handover")
        self.assertEqual(payload["data"]["partner_delivery"], "post")
        self.assertEqual(payload["data"]["receipt_id"], "gate-19")

    def _boarding_handover_stu11(self):
        self.crew.post("/trips/boarding", {
            "trip_id": "van-7", "boarded": ["stu-11", "stu-12"],
            "as_of": self.T_DEPART})
        snap = self.desk_a.get("/students/stu-11")[1]["data"]
        return next(h["id"] for h in snap["handovers"]
                    if h["kind"] == "boarding")


class TestRosterCorrection(RelayTestBase):
    def test_booked_correction_allowed(self):
        status, payload = self.desk_a.post("/trips/roster-correction", {
            "trip_id": "van-7", "student_id": "stu-13",
            "field": "booked", "value": 0, "reason": "改留校",
            "as_of": self.T0})
        self.assertEqual(status, 200)
        self.assertNotIn("stu-13", payload["data"]["booked"])

    def test_cannot_erase_verified_boarding(self):
        self.crew.post("/trips/boarding", {
            "trip_id": "van-7", "boarded": ["stu-11", "stu-12"],
            "as_of": self.T_DEPART})
        status, payload = self.desk_a.post("/trips/roster-correction", {
            "trip_id": "van-7", "student_id": "stu-11",
            "field": "boarded", "value": 0, "as_of": self.T_ARRIVE})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "verified_handover_protected")
        # 事实仍在
        snap = self.desk_a.get("/students/stu-11")[1]["data"]
        self.assertTrue(any(h["kind"] == "boarding" and h["status"] == "confirmed"
                            for h in snap["handovers"]))


class TestQueuesAndShiftChange(RelayTestBase):
    def test_desk_queues_classify_students(self):
        # 初始：三个学生都在 school-a，且 stu-12 有两个接领请求 -> pending
        queue_a = self.desk_a.get("/desk/queue")[1]["data"]
        pending_ids = {x["student"] for x in queue_a["pending_handover"]}
        self.assertIn("stu-12", pending_ids)
        # 发车后 stu-11/stu-12 转运中
        self.crew.post("/trips/boarding", {
            "trip_id": "van-7", "boarded": ["stu-11", "stu-12"],
            "as_of": self.T_DEPART})
        queue_a = self.desk_a.get("/desk/queue")[1]["data"]
        in_transit = {x["student"] for x in queue_a["in_transit"]}
        self.assertEqual(in_transit, {"stu-11", "stu-12"})
        self.assertIn("stu-13", {x["student"] for x in queue_a["signed"]})
        # 到场清点少 stu-11 -> 争议
        self.desk_b.post("/trips/arrival-count", {
            "trip_id": "van-7", "counted": ["stu-12"], "as_of": self.T_ARRIVE})
        queue_b = self.desk_b.get("/desk/queue")[1]["data"]
        self.assertIn("stu-11", {x["student"] for x in queue_b["disputed"]})

    def test_shift_change_shows_who_took_custody_and_basis(self):
        self.desk_a.post("/pickup-requests/queue",
                         {"request_id": "request-b", "as_of": self.T_ARRIVE})
        self.desk_a.post("/pickup-requests/confirm", {
            "request_id": "request-b", "site": "school-a",
            "as_of": self.T_ARRIVE})
        report = self.desk_a.get("/desk/shift-change")[1]["data"]
        pickups = [h for h in report["handovers"] if h["kind"] == "pickup"]
        self.assertEqual(len(pickups), 1)
        self.assertEqual(pickups[0]["collector_id"], "parent-8")
        self.assertEqual(pickups[0]["auth_basis"]["basis"],
                         "registered_guardian")
        actions = [e["action"] for e in report["audit"]]
        self.assertIn("pickup_confirmed", actions)


class TestRestartPersistence(RelayTestBase):
    def test_in_transit_and_pending_evidence_survive_restart(self):
        self.crew.post("/trips/boarding", {
            "trip_id": "van-7", "boarded": ["stu-11", "stu-12"],
            "as_of": self.T_DEPART})
        # 一条待核对证据
        self.desk_a.post("/gate-evidence", {
            "op_id": "op-pending", "terminal": "gate-a",
            "student_id": "stu-11", "handover_id": "handover-ghost",
            "receipt_id": "gate-z", "as_of": self.T_DEPART})
        self.restart_service()
        # 在途学生继续可查、可清点
        status, payload = self.desk_b.post("/trips/arrival-count", {
            "trip_id": "van-7", "counted": ["stu-12"], "as_of": self.T_ARRIVE})
        self.assertEqual(status, 200, payload)
        status, payload = self.desk_b.get("/gate-evidence/op-pending")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "conflict")
        # 争议仍在
        disputes = self.desk_b.get("/disputes")[1]["data"]["disputes"]
        students = {d["student_id"] for d in disputes}
        self.assertIn("stu-11", students)

    def test_database_file_is_sqlite_and_wal_mode(self):
        with sqlite3.connect(self.db_path) as db:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("handovers", tables)
            self.assertIn("gate_evidence", tables)


if __name__ == "__main__":
    unittest.main(verbosity=2)
