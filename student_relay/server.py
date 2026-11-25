"""服务启动入口。

用法：
    python3 -m student_relay.server --db data/relay.sqlite3 \
        --fixtures fixtures/registers.json --port 8080 \
        --partner http://127.0.0.1:8765

不传 --fixtures 时打开空库（适合自行建班）；不传 --partner 时
/gate-receipts 返回 503，但 /gate-evidence 仍可登记终端离线证据。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .httpapi import RelayServer, Service
from .partner import PartnerClient
from .seed import seed
from .storage import Store

ROOT = Path(__file__).resolve().parent.parent


def build_service(db_path: str | Path, fixtures: str | Path | None,
                  partner_url: str | None) -> tuple[Service, dict]:
    store = Store(db_path)
    tokens: dict = {}
    if fixtures is not None:
        tokens = seed(store, fixtures)
    partner = PartnerClient(partner_url) if partner_url else None
    return Service(store, partner), tokens


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "relay.sqlite3")
    parser.add_argument("--fixtures", type=Path,
                        default=ROOT / "fixtures" / "registers.json")
    parser.add_argument("--no-seed", action="store_true",
                        help="不装载演练台账，打开空库")
    parser.add_argument("--partner", default="http://127.0.0.1:8765",
                        help="门岗/合作方对端地址，传空串禁用")
    args = parser.parse_args()

    fixtures = None if args.no_seed else args.fixtures
    service, tokens = build_service(args.db, fixtures, args.partner or None)
    server = RelayServer((args.host, args.port), service)
    print(f"业务服务监听 http://{args.host}:{server.server_port}", flush=True)
    print(f"数据库: {args.db}", flush=True)
    if tokens:
        print("家长侧演示令牌:", json.dumps(tokens, ensure_ascii=False, indent=2),
              flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.store.close()


if __name__ == "__main__":
    main()
