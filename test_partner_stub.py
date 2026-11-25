from http.client import HTTPConnection, RemoteDisconnected
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from partner_stub import PartnerServer

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class PartnerContractTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = FIXTURES / "partner-exchanges.json"
        self.registers = json.loads((FIXTURES / "registers.json").read_text())
        self.exchanges = json.loads(path.read_text())
        self.server = PartnerServer(("127.0.0.1", 0), path, Path(directory.name) / "journal.sqlite3")
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, data=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            conn.request(method, path, None if data is None else json.dumps(data),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_fixture_can_exchange_and_query_every_counterparty_example(self):
        self.assertIsInstance(self.registers, dict)
        self.assertTrue(self.registers)
        self.assertTrue(any(e["outcome"] == "commit_disconnect" for e in self.exchanges))
        for index, example in enumerate(self.exchanges):
            with self.subTest(operation=example["operation"], case=index):
                identity = f"sample-{index}"
                url = "/operations/" + identity
                data = {field: example[field] for field in ("operation", "request")}
                data["id"] = identity
                expected = {"id": identity, "reply": example["reply"]}
                self.assertEqual(self.request("GET", url)[0], 404)
                if example["outcome"] == "commit_disconnect":
                    with self.assertRaises(RemoteDisconnected):
                        self.request("POST", "/operations", data)
                else:
                    self.assertEqual(self.request("POST", "/operations", data), (200, expected))
                self.assertEqual(self.request("GET", url), (200, expected))
                self.assertEqual(self.request("POST", "/operations", data), (200, expected))
                modified = {**data, "request": {**data["request"], "another_case": True}}
                self.assertEqual(self.request("POST", "/operations", modified)[0], 409)


if __name__ == "__main__":
    unittest.main()
