#!/usr/bin/env python3
"""Fixed-origin zooidfund HTTP client for an external Base wallet signer."""

import argparse
import base64
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ORIGIN = "https://fcefnmdlggldmfusydix.supabase.co"
FUNCTIONS = "/functions/v1"
BASE_RPC = "https://mainnet.base.org"
NETWORK = "eip155:8453"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AUTH_USED = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ADDRESS = r"0x[0-9a-fA-F]{40}"
HASH = r"0x[0-9a-fA-F]{64}"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
MAX_BODY = 2 * 1024 * 1024


class Stop(Exception):
    """A fixed, safe status."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http(url, *, token=None, body=None, payment=None, limit=MAX_BODY):
    headers = {"Accept": "application/json", "User-Agent": "zooidfund-skill/claude-helper"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if payment:
        headers["PAYMENT-SIGNATURE"] = base64.b64encode(json.dumps(payment).encode()).decode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, None if body is None else json.dumps(body).encode(), headers)
    try:
        response = build_opener(NoRedirect()).open(req, timeout=15)
    except HTTPError as exc:
        response = exc
    with response:
        if 300 <= response.status < 400:
            raise Stop("redirect_blocked")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise Stop("response_too_large")
        return response.status, response.headers, data


def rpc(method, params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def checked(value, pattern, status="invalid_input_or_response"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise Stop(status)
    return value


def minor(value):
    return int(Decimal(checked(str(value), r"[0-9]{1,9}(?:\.[0-9]{1,6})?", "invalid_amount")) * 1_000_000)


def approved_amount(value):
    amount = minor(value)
    if not amount:
        raise Stop("invalid_amount")
    return amount


def private(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or (
        os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise Stop("unsafe_private_path")
    return path


def read_private(path, limit):
    with private(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Stop("private_input_too_large")
    return data


def write_private(path, data):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def credential(path):
    if path is None or not path.exists():
        raise Stop("credential_missing_restore_existing_identity")
    record = json.loads(read_private(path, 4096))
    return (
        checked(record.get("api_key"), r"[A-Za-z0-9_-]{16,256}", "invalid_credential"),
        checked(record.get("agent_id"), UUID),
        checked(record.get("wallet_address"), ADDRESS).lower(),
    )


def mcp(name, args, token):
    status, _, data = http(ORIGIN + FUNCTIONS + "/mcp", token=token,
                          body=rpc("tools/call", {"name": name, "arguments": args}))
    if status in (401, 403):
        raise Stop("credential_rejected")
    if status != 200:
        raise Stop("mcp_response_uncertain")
    envelope = json.loads(data, parse_float=Decimal)
    if envelope.get("id") != 1 or envelope.get("jsonrpc") != "2.0":
        raise Stop("mcp_response_uncertain")
    result = envelope["result"]
    payload = result.get("structuredContent")
    if payload is None:
        content = result["content"]
        if len(content) != 1 or content[0].get("type") != "text":
            raise Stop("mcp_response_uncertain")
        payload = json.loads(content[0]["text"], parse_float=Decimal)
    if result.get("isError") is True:
        if payload.get("error") == "Unauthorized. Provide Authorization: Bearer <agent_api_key>.":
            raise Stop("credential_rejected")
        if name == "confirm_donation" and payload.get("error") == "tx_hash has already been recorded":
            details = payload.get("data", {})
            if checked(details.get("tx_hash"), HASH).lower() == args["tx_hash"]:
                return {"duplicate_donation_id": checked(details.get("donation_id"), UUID)}
        raise Stop("mcp_tool_rejected_check_existing_flow")
    return payload


def evidence_urls(campaign):
    endpoint = ORIGIN + FUNCTIONS + "/evidence-x402"
    return endpoint + "/" + campaign, endpoint + "?campaign_id=" + campaign


def payment_header(headers, name):
    if len(headers.get_all(name) or []) != 1:
        raise Stop("missing_or_duplicate_payment_header")
    return json.loads(base64.b64decode(headers[name], validate=True))


def evidence_offer(campaign, token, payee, amount):
    handoff = mcp("get_evidence", {"campaign_id": campaign}, token)
    if handoff.get("eligibility_status") == "not_eligible":
        if handoff.get("reason") == "missing_or_invalid_agent_authorization":
            raise Stop("credential_rejected")
        if handoff.get("reason") == "operator_acknowledgement_required":
            raise Stop("operator_acknowledgement_required")
        raise Stop("evidence_not_eligible")
    endpoint, canonical = evidence_urls(campaign)
    if (handoff.get("status") != "payment_required" or handoff.get("x402_endpoint") != endpoint
            or handoff.get("currency") != "USDC" or minor(handoff.get("price")) != amount):
        raise Stop("evidence_handoff_mismatch")
    status, headers, _ = http(endpoint, token=token)
    if status != 402:
        raise Stop("evidence_challenge_unavailable")
    challenge = payment_header(headers, "PAYMENT-REQUIRED")
    if (challenge.get("x402Version") != 2 or challenge["resource"].get("url") != canonical
            or "payment-identifier" in challenge.get("extensions", {}) or len(challenge["accepts"]) != 1):
        raise Stop("unsupported_evidence_challenge")
    offer = challenge["accepts"][0]
    extra = offer.get("extra", {})
    timeout = offer.get("maxTimeoutSeconds")
    if (offer.get("scheme") != "exact" or offer.get("network") != NETWORK
            or checked(offer.get("asset"), ADDRESS).lower() != USDC
            or checked(offer.get("payTo"), ADDRESS).lower() != payee or offer.get("amount") != str(amount)
            or type(timeout) is not int or not 1 <= timeout <= 300
            or extra.get("name") != "USD Coin" or extra.get("version") != "2"
            or extra.get("assetTransferMethod", "eip3009") != "eip3009"
            or set(extra) - {"name", "version", "assetTransferMethod"}):
        raise Stop("evidence_quote_mismatch")
    accepted = {key: offer[key] for key in ("scheme", "network", "asset", "payTo", "amount", "maxTimeoutSeconds", "extra")}
    return accepted, {"url": canonical}


def typed_data(auth):
    domain = {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": USDC}
    fields = [("from", "address"), ("to", "address"), ("value", "uint256"),
              ("validAfter", "uint256"), ("validBefore", "uint256"), ("nonce", "bytes32")]
    return {"domain": domain, "types": {"TransferWithAuthorization": [
        {"name": key, "type": kind} for key, kind in fields
    ]}, "primaryType": "TransferWithAuthorization", "message": auth}


def ledger(root, create=False):
    path = root / "evidence.sqlite3"
    if create:
        write_private(path, b"")
    elif not path.exists():
        raise Stop("ledger_missing_restore_or_reconcile_before_initializing")
    db = sqlite3.connect(private(path))
    if create:
        with db:
            db.execute("CREATE TABLE attempts (id TEXT PRIMARY KEY, intent TEXT, status TEXT, tx TEXT)")
            db.execute("CREATE UNIQUE INDEX one_active ON attempts ((1)) WHERE status IN ('prepared','pending')")
    return db


def attempt(db, operation):
    row = db.execute("SELECT intent,status,tx FROM attempts WHERE id=?", (operation,)).fetchone()
    if row is None:
        raise Stop("unknown_operation")
    return json.loads(row[0]), row[1], row[2]


def transition(db, operation, old, new, tx=None):
    with db:
        result = db.execute("UPDATE attempts SET status=?,tx=? WHERE id=? AND status=?",
                            (new, tx, operation, old))
        if result.rowcount != 1:
            raise Stop("attempt_state_changed")


def prepare(db, args, operation, identity):
    token, agent, payer = identity
    if db.execute("SELECT 1 FROM attempts WHERE id=? OR status IN ('prepared','pending')",
                  (operation,)).fetchone():
        raise Stop("existing_attempt_check_status")
    campaign = checked(args.campaign, UUID)
    payee, amount = checked(args.payee, ADDRESS).lower(), approved_amount(args.amount)
    offer, resource = evidence_offer(campaign, token, payee, amount)
    now = int(time.time())
    auth = {"from": payer, "to": payee, "value": str(amount), "validAfter": "0",
            "validBefore": str(now + offer["maxTimeoutSeconds"]), "nonce": "0x" + secrets.token_hex(32)}
    intent = {"agent_id": agent, "campaign": campaign, "auth": auth, "offer": offer, "resource": resource}
    with db:
        try:
            db.execute("INSERT INTO attempts VALUES (?,?,?,NULL)", (operation, json.dumps(intent), "prepared"))
        except sqlite3.IntegrityError:
            raise Stop("existing_attempt_check_status") from None
    return {"status": "prepared", "typed_data": typed_data(auth)}


def submit(db, root, args, operation, identity):
    token, agent, payer = identity
    intent, state, _ = attempt(db, operation)
    auth = intent["auth"]
    if state != "prepared":
        raise Stop("already_attempted_check_status")
    if intent["agent_id"] != agent or auth["from"] != payer:
        raise Stop("wallet_mismatch")
    if int(time.time()) >= int(auth["validBefore"]):
        raise Stop("authorization_expired")
    signature = checked(read_private(args.signature_file, 8192).decode().strip(),
                        r"0x(?:[0-9a-fA-F]{2}){65,4095}", "invalid_signature")
    payment = {"x402Version": 2, "resource": intent["resource"], "accepted": intent["offer"],
               "payload": {"authorization": auth, "signature": signature}}
    transition(db, operation, "prepared", "pending")
    try:
        status, headers, data = http(evidence_urls(intent["campaign"])[0], token=token, payment=payment)
        receipt = payment_header(headers, "PAYMENT-RESPONSE")
        tx = checked(receipt.get("transaction"), HASH).lower()
        if (receipt.get("success") is not True or receipt.get("network") not in (NETWORK, "base")
                or checked(receipt.get("payer"), ADDRESS).lower() != payer
                or receipt.get("amount", auth["value"]) != auth["value"]):
            return {"status": "payment_uncertain_reconcile_without_resubmitting", "transaction_hint": tx}
        transition(db, operation, "pending", "paid", tx)
    except (Stop, HTTPException, OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        raise Stop("payment_uncertain_reconcile_without_resubmitting") from None
    if status == 200:
        try:
            response = json.loads(data)
            docs = response.get("evidence_documents")
            if response.get("campaign_id") == intent["campaign"] and isinstance(docs, list):
                write_private(root / (operation + ".evidence.json"), json.dumps(docs).encode())
                return {"status": "paid", "transaction": tx, "document_count": len(docs)}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return {"status": "paid_content_unavailable", "transaction": tx}


def fetch_document(db, root, operation, index):
    _, state, _ = attempt(db, operation)
    if state != "paid" or index < 0:
        raise Stop("document_unavailable")
    docs = json.loads(read_private(root / (operation + ".evidence.json"), MAX_BODY * 2))
    if index >= len(docs) or docs[index].get("status") != "available":
        raise Stop("document_unavailable")
    url = docs[index].get("signed_url")
    if not isinstance(url, str) or not url.startswith(ORIGIN + "/storage/v1/object/sign/evidence/"):
        raise Stop("untrusted_document_url")
    parsed = urlsplit(url)
    suffix = parsed.path.removeprefix("/storage/v1/object/sign/evidence/")
    if (parsed.fragment or not suffix or any(segment in (".", "..", "") for segment in suffix.split("/"))
            or not re.fullmatch(r"[a-zA-Z0-9._/%-]+", suffix)
            or any(code in suffix.lower() for code in ("%2e", "%2f", "%5c", "%25"))
            or not re.fullmatch(r"token=[a-zA-Z0-9._-]+", parsed.query)):
        raise Stop("untrusted_document_url")
    target = root / (operation + "." + str(index) + ".document")
    if target.exists():
        raise Stop("document_already_saved")
    status, _, data = http(url, limit=25 * 1024 * 1024)
    if status != 200:
        raise Stop("document_unavailable")
    write_private(target, data)
    return {"status": "document_saved", "index": index}


def base_read(method, params):
    status, _, raw = http(BASE_RPC, body=rpc(method, params))
    envelope = json.loads(raw)
    if status != 200 or envelope.get("jsonrpc") != "2.0" or envelope.get("id") != 1 or "error" in envelope:
        raise Stop("base_verification_unavailable")
    return envelope.get("result")


def close_unpaid(db, operation):
    intent, state, _ = attempt(db, operation)
    if state != "pending":
        raise Stop("no_submitted_payment")
    auth = intent["auth"]
    finalized = base_read("eth_getBlockByNumber", ["finalized", False])
    if not finalized:
        raise Stop("base_verification_unavailable")
    timestamp = checked(finalized["timestamp"], r"0x[0-9a-fA-F]{1,16}")
    if int(timestamp, 16) <= int(auth["validBefore"]):
        raise Stop("payment_still_unresolved")
    block_hash = checked(finalized["hash"], HASH)
    call = {"to": USDC, "data": "0xe94a0102" + auth["from"][2:].zfill(64) + auth["nonce"][2:]}
    # authorizationState(address,bytes32) is true after use or cancellation.
    used = base_read("eth_call", [call, {"blockHash": block_hash, "requireCanonical": True}])
    if used != "0x" + "0" * 64:
        raise Stop("payment_still_unresolved")
    transition(db, operation, "pending", "expired_unpaid")
    return {"status": "expired_unpaid"}


def matching_receipt(tx):
    receipt = base_read("eth_getTransactionReceipt", [tx])
    if not receipt or receipt.get("transactionHash", "").lower() != tx or receipt.get("status") != "0x1":
        raise Stop("matching_base_receipt_unavailable")
    return receipt["logs"]


def usdc_event(log, topics, data=None):
    return (log.get("address", "").lower() == USDC and log.get("removed") is not True
            and log.get("topics") == topics and (data is None or log.get("data") == data))


def transfer_topics(payer, payee):
    return [TRANSFER, "0x" + payer[2:].zfill(64), "0x" + payee[2:].zfill(64)]


def donation_candidates(logs, amount):
    # The provider can accept a one-minor-unit difference. Refuse a receipt
    # with multiple candidates so a changed campaign wallet cannot select another transfer.
    candidates = []
    for log in logs:
        topics, data = log.get("topics") or [], log.get("data", "")
        if (log.get("address", "").lower() == USDC and log.get("removed") is not True
                and topics and topics[0].lower() == TRANSFER and re.fullmatch(r"0x[0-9a-fA-F]+", data)
                and abs(int(data, 16) - amount) <= 1):
            candidates.append(log)
    return candidates


def reconcile(db, operation, tx):
    intent, state, existing = attempt(db, operation)
    if state == "paid":
        return {"status": "paid", "transaction": existing}
    if state != "pending":
        raise Stop("no_submitted_payment")
    auth = intent["auth"]
    payer = "0x" + auth["from"][2:].zfill(64)
    topics = transfer_topics(auth["from"], auth["to"])
    amount = "0x" + format(int(auth["value"]), "064x")
    logs = matching_receipt(tx)
    if not any(
        usdc_event(authorization, [AUTH_USED, payer, auth["nonce"]])
        and usdc_event(transfer, topics, amount)
        for authorization, transfer in zip(logs, logs[1:])
    ):
        raise Stop("payment_still_unresolved")
    transition(db, operation, "pending", "paid", tx)
    return {"status": "paid_content_unavailable", "transaction": tx}


def donation(args, token, payer):
    campaign, amount = checked(args.campaign, UUID), approved_amount(args.amount)
    recipient = checked(args.recipient, ADDRESS).lower()
    payload = {"campaign_id": campaign, "amount": amount / 1_000_000, "reasoning": args.reasoning}
    if args.command == "confirm-donation":
        tx = checked(args.tx, HASH).lower()
        topics = transfer_topics(payer, recipient)
        candidates = donation_candidates(matching_receipt(tx), amount)
        if len(candidates) != 1 or not usdc_event(candidates[0], topics, "0x" + format(amount, "064x")):
            raise Stop("donation_receipt_does_not_match_reviewed_transfer")
        result = mcp("confirm_donation", {**payload, "tx_hash": tx}, token)
        if "duplicate_donation_id" in result:
            return {"status": "duplicate_check_campaign_history", **result}
        if result.get("status") != "completed" or checked(result.get("tx_hash"), HASH).lower() != tx:
            raise Stop("confirmation_uncertain_check_campaign_history")
        return {"status": "completed", "donation_id": checked(result.get("donation_id"), UUID), "tx_hash": tx}
    result = mcp("donate", payload, token)
    if (checked(result.get("wallet_address"), ADDRESS).lower() != recipient
            or minor(result.get("amount")) != amount or result.get("network") != NETWORK
            or result.get("currency") != "USDC"):
        raise Stop("donation_instructions_mismatch")
    return {"status": "instructions", "wallet_address": recipient, "network": NETWORK,
            "currency": "USDC", "asset": USDC, "amount_minor_units": amount}


def run(args):
    root = private(args.state, directory=True)
    if args.command == "init":
        ledger(root, create=True).close()
        return {"status": "ledger_initialized"}
    if args.command in ("donate", "confirm-donation", "prepare", "submit"):
        identity = credential(args.credential)
        if checked(args.wallet, ADDRESS).lower() != identity[2]:
            raise Stop("wallet_mismatch")
        if args.command in ("donate", "confirm-donation"):
            return donation(args, identity[0], identity[2])
    operation = checked(args.operation, UUID)
    db = ledger(root)
    try:
        if args.command == "prepare":
            return prepare(db, args, operation, identity)
        if args.command == "submit":
            return submit(db, root, args, operation, identity)
        if args.command == "quarantine-restored":
            transition(db, operation, "prepared", "pending")
            return {"status": "pending"}
        if args.command == "close-unpaid":
            return close_unpaid(db, operation)
        if args.command == "fetch-document":
            return fetch_document(db, root, operation, args.index)
        if args.command == "reconcile":
            return reconcile(db, operation, checked(args.tx, HASH).lower())
        intent, state, tx = attempt(db, operation)
        if args.command == "status":
            return {"status": state, "campaign_id": intent["campaign"], "transaction": tx}
        if state != "prepared" or int(time.time()) < int(intent["auth"]["validBefore"]):
            raise Stop("cannot_close_attempt")
        transition(db, operation, "prepared", "expired")
        return {"status": "expired"}
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--credential", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    for name in ("donate", "confirm-donation", "prepare", "submit", "status", "reconcile",
                 "close-expired", "fetch-document", "quarantine-restored", "close-unpaid"):
        command = commands.add_parser(name)
        if name in ("donate", "confirm-donation"):
            fields = ["campaign", "recipient", "amount", "wallet", "reasoning"]
        else:
            fields = ["operation"]
        if name in ("confirm-donation", "reconcile"):
            fields += ["tx"]
        if name == "prepare":
            fields += ["campaign", "payee", "amount", "wallet"]
        if name == "fetch-document":
            command.add_argument("--index", type=int, required=True)
        if name == "submit":
            fields += ["wallet"]
            command.add_argument("--signature-file", required=True, type=Path)
        for field in fields:
            command.add_argument("--" + field, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args)))
        return 0
    except Stop as exc:
        print(json.dumps({"status": str(exc)}))
    except (HTTPException, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
        print(json.dumps({"status": "request_or_local_state_failed_check_status"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
