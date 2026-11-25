# 学生转运与接领后端服务（student_relay）

面向暴雨预警下两校值班台与随车人员共用的后端。仅使用 Python 3.11 标准库，
数据落在 SQLite（WAL），服务重启后在途学生、未决门岗证据和争议都能继续处理。

## 运行

```sh
# 1) 启动门岗/合作方对端桩（项目自带，可保持运行、可独立重启）
python3 partner_stub.py --port 8765 --journal /tmp/partner.sqlite3

# 2) 启动业务服务（默认装载 fixtures/registers.json）
python3 -m student_relay.server --port 8080 \
    --db data/relay.sqlite3 \
    --partner http://127.0.0.1:8765
```

启动时会打印家长侧演示令牌。值班台/随车人员的 Bearer 令牌即其岗位编号
（演练台账中为 `teacher-a`、`teacher-b`、`driver-7`）。`--no-seed` 可开空库，
`--partner ""` 可禁用对端（此时 `/gate-receipts` 返回 503，但 `/gate-evidence`
仍可登记终端离线证据）。

## 角色与最小可见

| 调用方 | 令牌 | 可见/可操作范围 |
| --- | --- | --- |
| 值班台 desk | `teacher-a` / `teacher-b` | 本站当前照管、或经停本站班次涉及的学生；只有**当前照管站点**能放行 |
| 随车 crew | `driver-7` | 仅本车班次的点名与进度；不能代值班台放行 |
| 家长/亲属 collector | `tok_...`（绑定接领请求） | 只能查看与**自己接领请求**有关的状态和转达信息 |

所有写接口要求 `Authorization: Bearer <token>`，越权返回 `403 out_of_scope`。

## 核心不变量（与业务规则对应）

1. **唯一当前照管方**：每个学生在 `custody` 中至多一行，照管转移只能由
   已核实的现场交接驱动。写事务经全局写锁 + `BEGIN IMMEDIATE` 串行化，
   两个岗位几乎同时确认同一学生时，先提交者生效，后到者读到新照管方后
   得到 `409`，最终只有一种交接生效。
2. **交接只追加**：`handovers` 状态为 proposed/confirmed/rejected/superseded/
   blocked，名单更正不能删除或改写已核实交接（返回
   `409 verified_handover_protected`）。
3. **发车以实际点到为准**：同班可分车；没上车的学生照管方保持出发校区。
4. **到场只签收实际清点到的人**：差异逐人保留在 `trip_roster` + `disputes`；
   少一个孩子整车签收被拒（`arrival_blocked_by_discrepancy`）；系统**不会**
   自动把失配学生退回出发点，其照管方保持随车人员，必须经查找后的新确认
   （`/custody/resolve`）才能变更。
5. **接领资格**：登记监护人，或“有时限且指定该孩子”的代领授权。排队时
   记录授权快照；确认交接时按交接时刻重新核验，撤回/过期后排入队列的交接
   也会被拦（`delegation_revoked` / `delegation_expired`），并留 blocked 记录
   与争议，照管方不变。
6. **现场确认才放行**：值班台可登记接领请求、转达去向（`/pickup-requests/relay`），
   但非当前照管方的放行尝试只留审计、不改变交接状态
   （`409 not_current_custodian`）。
7. **门岗证据独立**：终端实物回执经合作方接口收发，与业务记录分离。
   - 对端先落日志再应答；`commit_disconnect` 时客户端按同一操作 ID 走
     `GET /operations/<id>` 补回回报，返回里带 `partner_delivery=replay_get`。
   - 同一 `op_id` 补传原样返回（`redelivery=true`），可辨清是哪一次实际交接。
   - 证据与已核实记录冲突（接领人不符、交接不存在/被拒/非接领交接）时
     留在 `conflict` 待核对并开争议，不重新签、不把旧证据当新放行、不改照管。
   - 人工核对（`/gate-evidence/review`）只改证据状态，不补签交接。
8. **持久化**：全部状态在 SQLite；重启后用同一数据库即可继续在途清点、
   证据核对与争议处理。

## 接口一览（均为 JSON）

### 班次与转运
- `POST /trips` 建班（booked 名单）
- `POST /trips/boarding` 发车点到（crew；`boarded` 实际点到，可选
  `handover_ids` 指定交接编号、`idempotency_key`）
- `POST /trips/arrival-count` 到场清点（到达站 desk；返回逐人 `discrepancies`）
- `POST /trips/arrival-sign` 整车签收（有未决差异时 409）
- `POST /trips/roster-correction` 名单更正（受已核实交接保护）
- `GET /trips/<id>`、`GET /crew/trip` 两端进度查看

### 接领资格与交接
- `POST /delegations`、`POST /delegations/revoke` 代领授权与撤回
- `POST /authorization/check` 按时刻核验资格
- `POST /pickup-requests` 登记到校请求（请求≠接领）
- `POST /pickup-requests/queue` 资格核验后排入交接队列（可选 `handover_id`）
- `POST /pickup-requests/confirm` 现场确认（带 `site`、可选 `idempotency_key`）
- `POST /pickup-requests/reject`、`POST /pickup-requests/relay` 拒绝/转达去向

### 争议、照管与门岗证据
- `GET /disputes` 未决争议
- `POST /custody/resolve` 查找后的照管确认（闭合争议）
- `POST /gate-receipts` 经合作方接口提交门岗回执（自动断连补传）
- `POST /gate-evidence` 直接登记离线门岗证据
- `GET /gate-evidence/<op_id>`、`POST /gate-evidence/review`

### 视图
- `GET /desk/queue` 值班台四类工作台：`pending_handover` / `in_transit` /
  `signed` / `disputed`
- `GET /desk/shift-change` 交班报告：谁、凭什么授权依据、何时接走/转入
- `GET /students/<id>` 学生全量快照（按岗位过滤）
- `GET /parent/requests` 家长侧（仅本人请求，可加 `?request_id=`）

## 测试

```sh
python3 -m unittest test_relay_service test_partner_stub -v
```

30 个用例覆盖：分车点到、逐人清点差异、整车签收拦截、查找后变更照管、
授权撤回重验、并发确认唯一性、幂等补传、门岗断连补传与冲突待核对、
名单更正保护、最小可见与越权、重启持久化。
