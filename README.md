# 临时安置点的学生接领与在途交接

本地演练资料和对端协议桩使用合成编号。gate-receipt 是独立门岗终端上传的实物交接记录示例。终端断线不影响原有凭条。清点差异和委托的有效时点来自校方，门岗回执不会验证两者。

`fixtures/registers.json` 保存模拟业务台账。trip 的 booked、boarded 和 counted_on_arrival 分别来自预安排、发车和到场清点。gate_requests 是待处理请求，未证明实际接领。

`fixtures/partner-exchanges.json` 给出合成对端调用的请求和回报示例。每个条目独立，文件顺序没有业务含义。请求正文按匹配的 `operation` 和 `request` 选择示例。对端只模拟设备或合作方的应答，不处理业务服务的规则。

使用 Python 3.11.2 和标准库，无需安装依赖。运行以下命令核验台账与对端联调协议。

```sh
python3 -m unittest -v
python3 partner_stub.py --port 8765
```

对端只监听本机。向 `POST /operations` 提交 JSON 对象，含字符串 `id`、字符串 `operation` 和对象 `request`。首次接单记入对端日志，正常返回 200，格式为 `{"id": "...", "reply": {...}}`。条目配置为 `commit_disconnect` 时，接单会写入对端日志，但首次 HTTP 连接没有响应。按该 ID 调用 `GET /operations/<id>` 查询回报，未接单返回 404。同一个 ID 和内容的重试返回原回报，复用 ID 提交其他内容返回 409。无对应条目的请求返回 422。可通过 `--exchanges` 传入同格式的其他联调资料。

重启业务服务进行联调时可保持对端进程运行。若也需要重启桩，可通过 `--journal` 指定临时 SQLite 文件保留对端接单记录。

## 业务服务 student_relay

在对端协议之上实现两校值班台与随车人员共用的转运/接领后端：照管状态机、
逐人清点差异、接领资格与撤回重验、并发交接唯一性、门岗证据断连补传与
冲突待核对，全部落 SQLite，重启可继续处理在途学生与未决证据。详见
[`student_relay/README.md`](student_relay/README.md)。

```sh
python3 -m student_relay.server --port 8080 \
    --db data/relay.sqlite3 --partner http://127.0.0.1:8765
python3 -m unittest test_relay_service test_partner_stub -v
```
