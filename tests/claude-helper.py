"""Exercise the candidate's real CLI and HTTP client against loopback fixtures."""

import base64
import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location("helper", Path(__file__).parents[1] / "scripts/claude_zooidfund.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

CAMPAIGN = "12345678-1234-4234-8234-123456789abc"
OP = "22345678-1234-4234-8234-123456789abc"
OTHER = "32345678-1234-4234-8234-123456789abc"
PAYER, PAYEE = "0x" + "11" * 20, "0x" + "22" * 20
TX = "0x" + "33" * 32
TOKEN = "fixture-only-agent-credential"
SIGNATURE = "0x" + "44" * 224
PRIVATE = "fixture-private-response-marker"


class HelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.credential = self.root / "credential"
        helper.write_private(self.credential, json.dumps({
            "api_key": TOKEN, "agent_id": OTHER, "wallet_address": PAYER,
        }).encode())
        self.signature = self.root / "signature"
        helper.write_private(self.signature, SIGNATURE.encode())
        self.calls = []
        self.payload = None
        self.offer_change = {}
        self.mode = "success"
        self.bad_receipt = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                test.respond(self)

            def do_POST(self):
                test.respond(self)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(lambda: setattr(helper, "ORIGIN", "https://fcefnmdlggldmfusydix.supabase.co"))
        self.addCleanup(lambda: setattr(helper, "BASE_RPC", "https://mainnet.base.org"))
        helper.ORIGIN = "http://127.0.0.1:" + str(server.server_port)
        helper.BASE_RPC = helper.ORIGIN + "/rpc"

    def respond(self, req):
        self.calls.append((req.path, req.headers.get("Authorization")))
        status, headers = 200, {}
        data = {"debug": PRIVATE}
        if req.path == "/rpc":
            auth = self.payload["payload"]["authorization"]
            payer, payee = ("0x" + x[2:].zfill(64) for x in (PAYER, PAYEE))
            logs = [
                {"address": helper.USDC, "topics": [helper.AUTH_USED, payer, auth["nonce"]]},
                {"address": helper.USDC, "topics": [helper.TRANSFER, payer, payee],
                 "data": "0x" + format(10000, "064x")},
            ]
            if self.bad_receipt:
                logs.pop(0)
            data = {"jsonrpc": "2.0", "id": 1, "result": {
                "status": "0x1", "transactionHash": TX, "logs": logs,
            }}
        elif req.path.startswith("/storage/"):
            data = {"document": PRIVATE}
        elif req.headers.get("Authorization") != "Bearer " + TOKEN:
            status = 401
        elif req.path.endswith("/mcp"):
            envelope = json.loads(req.rfile.read(int(req.headers["Content-Length"])))
            name = envelope["params"]["name"]
            if self.mode == "deny":
                status = 401
            if name == "donate":
                response = {"wallet_address": PAYEE, "amount": 2, "network": helper.NETWORK, "currency": "USDC"}
            elif name == "confirm_donation":
                response = {"status": "completed", "tx_hash": TX, "donation_id": OTHER}
            else:
                endpoint = helper.evidence_urls(CAMPAIGN)[0]
                if self.mode == "foreign":
                    endpoint = helper.ORIGIN + "/must-not-be-called"
                response = {"status": "payment_required", "x402_endpoint": endpoint,
                            "price": 0.01, "currency": "USDC"}
                if self.mode == "ack":
                    response = {"eligibility_status": "not_eligible", "reason": "operator_acknowledgement_required"}
            data = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": response}}
        elif req.headers.get("PAYMENT-SIGNATURE"):
            self.payload = json.loads(base64.b64decode(req.headers["PAYMENT-SIGNATURE"]))
            if self.mode == "disconnect":
                self.close_connection = True
                return
            headers["PAYMENT-RESPONSE"] = self.header({
                "success": True, "transaction": TX, "network": helper.NETWORK,
                "payer": PAYER, "amount": "10000",
            })
            url = helper.ORIGIN + "/storage/v1/object/sign/evidence/sample.pdf?token=" + PRIVATE
            data = {"campaign_id": CAMPAIGN, "evidence_documents": [
                {"status": "available", "signed_url": url},
                {"status": "available", "signed_url": helper.ORIGIN + "/untrusted"},
                {"status": "removed", "signed_url": None},
            ]}
        elif self.mode == "redirect":
            status, headers = 302, {"Location": helper.ORIGIN + "/must-not-be-called"}
        else:
            status = 402
            offer = {"network": helper.NETWORK, "scheme": "exact", "asset": helper.USDC,
                     "payTo": PAYEE, "amount": "10000", "maxTimeoutSeconds": 60,
                     "extra": {"name": "USD Coin", "version": "2"}, **self.offer_change}
            headers["PAYMENT-REQUIRED"] = self.header({
                "x402Version": 2, "resource": {"url": helper.evidence_urls(CAMPAIGN)[1]},
                "accepts": [offer],
            })
        req.send_response(status)
        for key, value in headers.items():
            req.send_header(key, value)
        req.end_headers()
        req.wfile.write(json.dumps(data).encode())

    @staticmethod
    def header(value):
        return base64.b64encode(json.dumps(value).encode()).decode()

    def cli(self, command, *args, root=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            helper.main(["--state", str(root or self.root), "--credential", str(self.credential), command, *args])
        out = stdout.getvalue()
        for hidden in (TOKEN, SIGNATURE, PRIVATE):
            self.assertNotIn(hidden, out)
        return json.loads(out)

    def prepare(self, *, wallet=PAYER):
        return self.cli("prepare", "--operation", OP, "--campaign", CAMPAIGN,
                        "--wallet", wallet, "--payee", PAYEE, "--amount", "0.01")

    def submit(self):
        return self.cli("submit", "--operation", OP, "--wallet", PAYER, "--signature-file", str(self.signature))

    def test_handoff_submit_and_private_documents(self):
        self.assertEqual(self.cli("init")["status"], "ledger_initialized")
        handoff = self.prepare()["typed_data"]
        self.assertEqual(handoff["domain"]["chainId"], 8453)
        self.assertEqual(self.submit()["status"], "paid")
        self.assertEqual(self.payload["payload"], {"signature": SIGNATURE, "authorization": handoff["message"]})
        self.assertEqual(self.submit()["status"], "already_attempted_check_status")
        self.assertEqual(self.cli("fetch-document", "--operation", OP, "--index", "0")["status"], "document_saved")
        self.assertIsNone(self.calls[-1][1])
        self.assertEqual(self.cli("fetch-document", "--operation", OP, "--index", "1")["status"],
                         "untrusted_document_url")
        self.assertEqual(self.cli("fetch-document", "--operation", OP, "--index", "2")["status"],
                         "document_unavailable")
        ledger = (self.root / "evidence.sqlite3").read_bytes()
        for value in (TOKEN, SIGNATURE, PRIVATE):
            self.assertNotIn(value.encode(), ledger)

    def test_uncertain_reconciliation_after_workspace_restore(self):
        self.cli("init")
        self.prepare()
        self.mode = "disconnect"
        self.assertEqual(self.submit()["status"], "payment_uncertain_reconcile_without_resubmitting")
        saved = (self.root / "evidence.sqlite3").read_bytes()
        with tempfile.TemporaryDirectory() as second:
            root = Path(second)
            self.assertTrue(self.cli("status", "--operation", OP, root=root)["status"].startswith("ledger_missing"))
            helper.write_private(root / "evidence.sqlite3", saved)
            self.assertEqual(self.cli("status", "--operation", OP, root=root)["status"], "pending")
            self.bad_receipt = True
            args = ("reconcile", "--operation", OP, "--tx", TX)
            self.assertEqual(self.cli(*args, root=root)["status"], "payment_still_unresolved")
            self.bad_receipt = False
            self.assertEqual(self.cli(*args, root=root)["status"], "paid_content_unavailable")
        self.assertEqual(sum(path.endswith("/" + CAMPAIGN) for path, _ in self.calls), 2)
        self.assertTrue(all(auth is None for path, auth in self.calls if path == "/rpc"))

    def test_auth_and_quote_denials(self):
        self.assertTrue(self.prepare()["status"].startswith("ledger_missing"))
        self.cli("init")
        self.assertEqual(self.prepare(wallet=PAYEE)["status"], "wallet_mismatch")
        self.assertEqual(self.calls, [])
        for mode, expected in (("deny", "credential_rejected"), ("ack", "operator_acknowledgement_required"),
                               ("foreign", "evidence_handoff_mismatch"), ("redirect", "redirect_blocked")):
            with self.subTest(mode=mode):
                self.mode = mode
                self.assertEqual(self.prepare()["status"], expected)
        self.mode = "success"
        for bad in ({"amount": "10001"}, {"network": "eip155:1"}, {"asset": PAYEE},
                    {"payTo": PAYER}, {"maxTimeoutSeconds": True}, {"extra": {"name": "USD Coin"}}):
            with self.subTest(bad=bad):
                self.offer_change = bad
                self.assertEqual(self.prepare()["status"], "evidence_quote_mismatch")
        self.assertIsNone(self.payload)
        self.assertNotIn("/must-not-be-called", [path for path, _ in self.calls])
        self.credential.unlink()
        self.assertEqual(self.prepare()["status"], "credential_missing_restore_existing_identity")

    def test_donation_requests_only_use_existing_mcp(self):
        args = ("--campaign", CAMPAIGN, "--wallet", PAYER, "--recipient", PAYEE,
                "--amount", "2", "--reasoning", "Fixture public reasoning.")
        self.assertEqual(self.cli("donate", *args)["amount_minor_units"], 2000000)
        self.assertEqual(self.cli("confirm-donation", *args, "--tx", TX)["tx_hash"], TX)
        self.assertEqual(self.calls, [(helper.FUNCTIONS + "/mcp", "Bearer " + TOKEN)] * 2)


if __name__ == "__main__":
    unittest.main()
