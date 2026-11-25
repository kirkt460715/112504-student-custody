"""模拟合作方的本地 HTTP 接口。"""
import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def encode(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PartnerServer(ThreadingHTTPServer):
    def __init__(self, address, exchanges, journal):
        self.journal = journal
        self.examples = {}
        for example in json.loads(Path(exchanges).read_text(encoding="utf-8")):
            key = (example["operation"], encode(example["request"]))
            if key in self.examples or example["outcome"] not in {"success", "commit_disconnect"}:
                raise ValueError("请求样例重复或连接结果未知")
            self.examples[key] = example
        with closing(self.connection()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS journal (
                id TEXT PRIMARY KEY, operation TEXT, request TEXT, reply TEXT)""")
        super().__init__(address, Handler)

    def connection(self):
        return sqlite3.connect(self.journal, timeout=5)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, value):
        payload = encode(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        prefix = "/operations/"
        if not self.path.startswith(prefix) or not ID.fullmatch(self.path[len(prefix):]):
            self.send_json(404, {"error": "not_found"})
            return
        identity = self.path[len(prefix):]
        with closing(self.server.connection()) as db:
            row = db.execute("SELECT reply FROM journal WHERE id=?", (identity,)).fetchone()
        if row is None:
            self.send_json(404, {"error": "unknown_operation"})
            return
        self.send_json(200, {"id": identity, "reply": json.loads(row[0])})

    def do_POST(self):
        if self.path != "/operations":
            self.send_json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            identity, operation, request = data["id"], data["operation"], data["request"]
            if not isinstance(identity, str) or not ID.fullmatch(identity):
                raise ValueError
            if not isinstance(operation, str) or not isinstance(request, dict):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            self.send_json(400, {"error": "invalid_request"})
            return

        body = encode(request)
        with closing(self.server.connection()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT operation,request,reply FROM journal WHERE id=?", (identity,)).fetchone()
            if previous:
                if previous[:2] != (operation, body):
                    self.send_json(409, {"error": "operation_id_conflict"})
                    return
                reply = json.loads(previous[2])
                drop = False
            else:
                example = self.server.examples.get((operation, body))
                if example is None:
                    self.send_json(422, {"error": "unsupported_example"})
                    return
                reply = example["reply"]
                drop = example["outcome"] == "commit_disconnect"
                db.execute("INSERT INTO journal VALUES (?, ?, ?, ?)", (identity, operation, body, encode(reply)))
        if drop:
            self.close_connection = True
        else:
            self.send_json(200, {"id": identity, "reply": reply})

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--exchanges", type=Path, default=ROOT / "fixtures" / "partner-exchanges.json")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="partner-") as directory:
        journal = args.journal or Path(directory) / "journal.sqlite3"
        with PartnerServer(("127.0.0.1", args.port), args.exchanges, journal) as server:
            print(f"联调服务监听 http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()


if __name__ == "__main__":
    main()
