"""`phone-harness cloud reserve` and `cloud reservations` against a fake reservation API.

No network, no Stripe, no money. The fake answers in the shapes phone-cloud's
reservation API gives (its docs/api.md) and keeps every request the CLI sent,
so a test can check the body, the bearer and the Idempotency-Key.

    python -m unittest discover tests -p test_cloud_reserve.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
SUPPORT = str(Path(__file__).resolve().parent / "support")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if SUPPORT not in sys.path:
    sys.path.insert(0, SUPPORT)
import browser_recorder
browser_recorder.install()

from phone_harness import cloud, run  # noqa: E402

KEY = "pck_reserve_test"
USE_CASE = "Testing our listings app on a real iPhone."
CHECKOUT = "https://checkout.stripe.com/c/pay/cs_test_"
DEPOSIT = 2000
# (standard, intro) cents for one phone for one billing period. For a short
# while the API took a reservation for a cloud Android, before it had a price.
# It refuses one now, and this CLI never asks, but one made then can still be
# on the account.
PRICES = {("iphone", "monthly"): (8000, 5000), ("iphone", "annual"): (76800, 48000),
          ("iphone_esim", "monthly"): (10000, 6500), ("iphone_esim", "annual"): (96000, 62400),
          ("android", "monthly"): (None, None), ("android", "annual"): (None, None)}
DETAIL = {
    "pending_payment": "This reservation is not finished. Pay the deposit to hold a place.",
    "queued": "You are in the queue. Cancel any time and the deposit comes back in full.",
    "cancelling": "You have left the queue. Your deposit is on its way back to your card.",
    "granted": "Your phone is yours. The deposit went towards your first bill.",
    "ended": "The plan for this phone has ended.",
    "cancelled": "You left the queue and the deposit was refunded in full.",
}
GRANTED_REFUSAL = ("Your phone has been handed over, and its deposit went towards your "
                   "first bill. Manage the plan from your account.")


def rid(n):
    return "rsv_" + f"{n:024x}"


FAR = 0xa1 << 88           # an id that shares no leading digits with rid(1), rid(2), ...


def reservation(n, phone_type="iphone", plan="monthly", quantity=1, status="pending_payment", *,
                position=None, granted=0, paid_days_ago=None, intro=True):
    """One reservation as the API shows it, in whichever state `status` names."""
    standard, intro_cents = PRICES[(phone_type, plan)]
    lines = []
    if standard is not None:
        rest = quantity
        if intro and intro_cents is not None:
            lines.append({"tier": "intro", "quantity": 1, "unit_cents": intro_cents})
            rest -= 1
        if rest:
            lines.append({"tier": "standard", "quantity": rest, "unit_cents": standard})
    first = sum(line["quantity"] * line["unit_cents"] for line in lines) if lines else None
    total = DEPOSIT * quantity
    unpaid = status == "pending_payment" or (status == "cancelled" and paid_days_ago is None)
    credited = 0 if unpaid else DEPOSIT * granted
    held = total - credited if status in ("queued", "cancelling") else 0
    refunded = total - credited if status in ("cancelled", "granted", "ended") and not unpaid else 0
    pending = status == "pending_payment"
    return {
        "reservation_id": rid(n), "status": status, "phone_type": phone_type, "plan": plan,
        "quantity": quantity, "created_at": time.time() - 86400 * (paid_days_ago or 0) - 60,
        "paid_at": None if unpaid else time.time() - 86400 * (paid_days_ago or 0),
        "position": position if status == "queued" else None,
        "queued": quantity - granted if status == "queued" else 0,
        "granted": 0 if unpaid else granted,
        "ended": granted if status == "ended" else 0,      # phones whose plan has since ended
        "deposit": {"paid_cents": 0 if unpaid else total, "refundable_cents": held,
                    "credited_cents": credited, "refunded_cents": refunded, "currency": "usd"},
        "can_cancel": status in ("pending_payment", "queued"),
        "checkout_url": CHECKOUT + f"{n:06d}" if pending else None,
        "expires_at": time.time() + 86400 if pending else None,
        "quote": {"currency": "usd", "interval": "month" if plan == "monthly" else "year",
                  "deposit": {"per_phone_cents": DEPOSIT, "phones": quantity, "total_cents": total,
                              "refundable": True},
                  "priced": standard is not None, "lines": lines, "first_period_cents": first,
                  "due_at_handover_cents": first - total if first is not None else None},
        "detail": ("This reservation was never paid for, so nothing was charged and no place is held."
                   if status == "cancelled" and unpaid else DETAIL[status]),
    }


class FakeApi(BaseHTTPRequestHandler):
    """POST/GET /reservations, GET /reservations/{id}, POST /reservations/{id}/cancel."""
    valid = set()
    rows = {}                 # reservation_id -> reservation, in the order made
    keys = {}                 # Idempotency-Key -> (body, reservation_id)
    requests = []             # (method, path, headers with lowercase names, body)
    fail = None               # (status, body, headers): the next request answers this instead
    drop = 0                  # the next N creates are made, and then never answered
    live = True               # False: an API from before reservations; every route under it is a 404
    account_name = "Ada King"  # the name on file; None for an account that has none
    names = {}                # reservation_id -> [first, last] as the API would store them
    checkout = None           # set: every new reservation's checkout_url is this instead

    def log_message(self, *a):
        pass

    def _send(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        c = FakeApi
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        path, method = self.path.split("?")[0], self.command
        c.requests.append((method, path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.headers.get("Authorization", "")[7:] not in c.valid:
            return self._send(401, {"error": "bad or missing bearer token"})
        if c.fail:
            (status, payload, headers), c.fail = c.fail, None
            return self._send(status, payload, headers)
        parts = path.strip("/").split("/")
        if parts[0] != "reservations" or not c.live:
            return self._send(404, {"error": f"no route {method} {path}"})
        if len(parts) == 1 and method == "POST":
            return self._create(body or {})
        if len(parts) == 1 and method == "GET":
            return self._send(200, {"reservations": list(reversed(list(c.rows.values())))})
        row = c.rows.get(parts[1])
        if row is None:
            return self._send(404, {"error": "No such reservation"})
        if len(parts) == 2 and method == "GET":
            return self._send(200, row)
        if len(parts) == 3 and parts[2] == "cancel" and method == "POST":
            return self._cancel(row)
        return self._send(404, {"error": f"no route {method} {path}"})

    def _create(self, body):
        c = FakeApi
        key = self.headers.get("Idempotency-Key")
        if set(body) - {"first_name", "last_name", "email", "use_case", "phone_type", "plan", "quantity"}:
            return self._send(400, {"error": "Unknown field"})
        if body.get("phone_type") == "android":
            return self._send(400, {"error": "Cloud Android cannot be reserved. Choose a phone: iphone, iphone_esim",
                                    "code": "phone_not_reservable"})
        if (body.get("phone_type"), body.get("plan")) not in PRICES:
            return self._send(400, {"error": "Choose a phone: iphone, iphone_esim"})
        if type(body.get("quantity")) is not int or not 1 <= body["quantity"] <= 50:
            return self._send(400, {"error": "Choose between 1 and 50 phones"})
        if key in c.keys:
            sent, made = c.keys[key]
            if sent != body:
                return self._send(409, {"error": "Idempotency-Key was already used with a different request",
                                        "code": "idempotency_conflict"})
            status, row = 200, c.rows[made]
        else:
            row = reservation(len(c.rows) + 1, body["phone_type"], body["plan"], body["quantity"])
            if c.checkout:
                row["checkout_url"] = c.checkout
            c.rows[row["reservation_id"]] = row
            # A signed-in account need not say who it is: a name in the body is
            # used, else the one on file, else none at all.
            first, _, last = (c.account_name or "").partition(" ")
            c.names[row["reservation_id"]] = [body.get("first_name") or first or None,
                                              body.get("last_name") or last or None]
            if key:
                c.keys[key] = (body, row["reservation_id"])
            status = 201
        if c.drop:
            c.drop -= 1
            self.close_connection = True
            return None                     # made, and the answer never arrives
        return self._send(status, {**row, "status_url": "https://phone-harness.com/reservation?id="
                                                          + row["reservation_id"]})

    def _cancel(self, row):
        status, deposit = row["status"], row["deposit"]
        if status in ("granted", "ended"):
            return self._send(409, {"error": GRANTED_REFUSAL, "code": "already_granted"})
        refunded = 0
        if status in ("queued", "cancelling"):
            refunded = deposit["refundable_cents"]
            deposit.update(refundable_cents=0, refunded_cents=deposit["refunded_cents"] + refunded)
            handed = row["granted"]
            row.update(status="granted" if handed else "cancelled", position=None, queued=0,
                       can_cancel=False,
                       detail=(f"{handed} phone handed over. You left the queue for the other "
                               f"{row['quantity'] - handed}, and that part of the deposit was refunded."
                               if handed else DETAIL["cancelled"]))
        elif status == "pending_payment":
            row.update(status="cancelled", can_cancel=False, checkout_url=None, expires_at=None,
                       detail="This reservation was never paid for, so nothing was charged "
                              "and no place is held.")
        return self._send(200, {**row, "refunded_cents": refunded})

    do_GET = do_POST = _handle


class ReserveCase(unittest.TestCase):
    """A fake API, a throwaway home, and an API key in the environment."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        FakeApi.valid, FakeApi.rows, FakeApi.keys, FakeApi.requests = {KEY}, {}, {}, []
        FakeApi.fail, FakeApi.drop, FakeApi.checkout = None, 0, None
        FakeApi.account_name, FakeApi.names, FakeApi.live = "Ada King", {}, True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api = f"http://127.0.0.1:{self.server.server_port}"
        self.env = {**os.environ, "PYTHONPATH": SUPPORT + os.pathsep + SRC,
                    "PHONE_HARNESS_HOME": self.home.name, "PHONE_HARNESS_TELEMETRY": "0",
                    "PHONE_HARNESS_NO_UPDATE_CHECK": "1", "PHONE_HARNESS_CLOUD_API": self.api,
                    "PHONE_HARNESS_CLOUD_OAUTH_ISSUER": self.api, "PHONE_HARNESS_API_KEY": KEY}
        for name in ("PHONE_HARNESS_SESSION", "BROWSER", "NO_COLOR"):
            self.env.pop(name, None)
        browser_recorder.recorder.reset()

    def run_cli(self, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, "-m", "phone_harness.run", "cloud", *args], input=stdin,
                              capture_output=True, text=True, env={**self.env, **(env or {})})

    def reserve(self, *extra, kind="iphone", plan="monthly", env=None):
        return self.run_cli("reserve", "--type", kind, "--plan", plan,
                            "--use-case", USE_CASE, *extra, env=env)

    def sent(self, method=None, suffix=""):
        return [r for r in FakeApi.requests if (method is None or r[0] == method) and r[1].endswith(suffix)]

    def hold(self, *rows):
        """Put reservations on the account, oldest first."""
        for row in rows:
            FakeApi.rows[row["reservation_id"]] = row
        return rows[0] if len(rows) == 1 else rows


class Reserve(ReserveCase):
    def test_reserve_sends_the_four_fields_the_bearer_and_a_key_of_its_own(self):
        r = self.reserve("--qty", "3", kind="iphone-esim", plan="annual")
        self.assertEqual(r.returncode, 0, r.stderr)
        [(method, path, headers, body)] = FakeApi.requests
        self.assertEqual((method, path), ("POST", "/reservations"))
        self.assertEqual(body, {"phone_type": "iphone_esim", "plan": "annual", "quantity": 3,
                                "use_case": USE_CASE})
        self.assertEqual(headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertRegex(headers["idempotency-key"], r"^[A-Za-z0-9_-]{16,128}$")
        again = self.reserve("--qty", "3", kind="iphone-esim", plan="annual")
        self.assertEqual(again.returncode, 0, again.stderr)
        first, second = (req[2]["idempotency-key"] for req in FakeApi.requests)
        self.assertNotEqual(first, second)                 # one key for each invocation
        self.assertEqual(len(FakeApi.rows), 2)

    def test_quantity_defaults_to_one_and_the_api_spelling_of_a_type_is_taken_too(self):
        spelled = self.run_cli("reserve", "--type=iphone_esim", "--plan=monthly", "--use-case=Two words")
        self.assertEqual(spelled.returncode, 0, spelled.stderr)
        self.assertEqual(self.reserve("--quantity", "2", kind="iphone-esim").returncode, 0)
        self.assertEqual([req[3] for req in FakeApi.requests], [
            {"phone_type": "iphone_esim", "plan": "monthly", "quantity": 1, "use_case": "Two words"},
            {"phone_type": "iphone_esim", "plan": "monthly", "quantity": 2, "use_case": USE_CASE}])
        twice = self.reserve("--qty", "1", "--qty", "2")               # said twice is not guessed at
        self.assertEqual(twice.returncode, 1)
        self.assertIn("Usage: phone-harness cloud reserve", twice.stderr)
        self.assertEqual(len(FakeApi.requests), 2)

    def test_the_key_can_be_given_so_a_retry_is_the_same_reservation(self):
        key = "retry-key_0123456789"
        first = self.reserve("--idempotency-key", key, "--json")
        again = self.reserve("--idempotency-key", key, "--json")
        self.assertEqual((first.returncode, again.returncode), (0, 0), first.stderr + again.stderr)
        self.assertEqual([req[2]["idempotency-key"] for req in FakeApi.requests], [key, key])
        self.assertEqual(json.loads(first.stdout)["reservation_id"],
                         json.loads(again.stdout)["reservation_id"])
        self.assertEqual(len(FakeApi.rows), 1)
        other = self.reserve("--idempotency-key", key, "--qty", "2")
        self.assertEqual(other.returncode, 1)
        self.assertIn("already used for a different request", other.stderr)
        self.assertEqual(len(FakeApi.rows), 1)

    def test_bad_arguments_are_refused_before_anything_is_sent(self):
        use = ["--use-case", "A phone to test on."]
        cases = [
            (["--type", "iphone", "--plan", "monthly", "--qty", "0", *use], "between 1 and 50"),
            (["--type", "iphone", "--plan", "monthly", "--qty", "51", *use], "between 1 and 50"),
            (["--type", "iphone", "--plan", "monthly", "--qty", "-3", *use], "between 1 and 50"),
            (["--type", "iphone", "--plan", "monthly", "--qty", "two", *use], "--qty must be a number"),
            (["--type", "iphone", "--plan", "monthly", "--qty", "1.5", *use], "--qty must be a number"),
            (["--type", "ipad", "--plan", "monthly", *use], "--type must be iphone or iphone-esim, not 'ipad'"),
            (["--plan", "monthly", *use], "--type is required"),
            (["--type", "iphone", "--plan", "weekly", *use], "--plan must be monthly or annual"),
            (["--type", "iphone", *use], "--plan is required"),
            (["--type", "iphone", "--plan", "monthly"], "--use-case is required"),
            (["--type", "iphone", "--plan", "monthly", "--use-case", "   "], "--use-case is required"),
            (["--type", "iphone", "--plan", "monthly", "--use-case", "x" * 2001], "the most is 2000"),
            (["--type", "iphone", "--plan", "monthly", "--idempotency-key", "short", *use], "16 to 128"),
            (["--type", "iphone", "--plan", "monthly", "--idempotency-key", "has spaces in the key!", *use],
             "16 to 128"),
            (["--type", "iphone", "--plan", "monthly", "extra", *use], "Usage: phone-harness cloud reserve"),
        ]
        for args, needle in cases:
            with self.subTest(args=args[:6]):
                r = self.run_cli("reserve", *args)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(needle, r.stderr)
                self.assertEqual(r.stdout, "")
        unknown = self.run_cli("reserve", "--type", "iphone", "--plan", "monthly", "--pay-now", *use)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unrecognized arguments: --pay-now", unknown.stderr)
        self.assertEqual(FakeApi.requests, [])
        self.assertEqual(browser_recorder.recorder.urls(), [])

    def test_an_android_is_not_reserved_and_nothing_is_sent(self):
        said = ("Reservations are for iPhones only (with or without eSIM). "
                "Cloud Android emulators are on the $20 plan and are not reserved.")
        self.assertEqual(cloud.IPHONES_ONLY, said)
        for kind in ("android", "Android", "android-emulator"):
            with self.subTest(kind=kind):
                r = self.reserve("--qty", "2", "--json", "--open", kind=kind)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(r.stderr.splitlines()[0], said)
                self.assertIn("Usage: phone-harness cloud reserve --type iphone|iphone-esim --plan", r.stderr)
                self.assertEqual(r.stdout, "")
        self.assertEqual(FakeApi.requests, [])
        self.assertEqual(browser_recorder.recorder.urls(), [])
        # Nothing the command says about itself offers one either.
        self.assertEqual(sorted(set(cloud.RESERVE_TYPES.values())), ["iphone", "iphone_esim"])
        self.assertNotIn("android", cloud.RESERVE_USAGE.lower())
        self.assertIn("cloud reserve --type iphone|iphone-esim --plan monthly|annual", cloud.CLI_USAGE)
        helped = self.run_cli("reserve", "--help")
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn(said, " ".join(helped.stdout.split()))
        self.assertNotRegex(helped.stdout, r"(?i)\|android|or android")

    def test_the_quote_and_the_checkout_link_are_printed_and_nothing_is_called_reserved(self):
        r = self.reserve("--qty", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn(f"Reservation {rid(1)} started: 3 x iPhone, monthly.", out)
        self.assertIn("holds no place in the queue until the deposit is paid", out)
        self.assertIn("deposit      $60.00 due now (3 x $20.00), refundable in full while queued", out)
        self.assertIn("first month  1 x $50.00 intro price, once per customer", out)
        self.assertIn("2 x $80.00 standard price", out)
        self.assertIn("$210.00 in all; $150.00 due at handover, after the deposit", out)
        self.assertIn("pay          " + CHECKOUT + "000001", out)
        self.assertIn("the link works for another 24 hours", out)
        self.assertIn(f"phone-harness cloud reservations {rid(1)}", out)
        self.assertNotRegex(out, r"(?i)\breserved\b|\bposition\b|you are in the queue")
        self.assertEqual(browser_recorder.recorder.urls(), [])       # only --open opens anything
        self.assertNotIn("\x1b", out)

    def test_an_annual_quote(self):
        annual = self.reserve("--qty", "2", kind="iphone-esim", plan="annual")
        self.assertIn("2 x iPhone with eSIM, annual", annual.stdout)
        self.assertIn("first year   1 x $624.00 intro price, once per customer", annual.stdout)
        self.assertIn("1 x $960.00 standard price", annual.stdout)
        self.assertIn("$1,584.00 in all; $1,544.00 due at handover, after the deposit", annual.stdout)

    def test_json_prints_what_the_api_answered(self):
        r = self.reserve("--qty", "2", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        made = json.loads(r.stdout)
        self.assertEqual(made["reservation_id"], rid(1))
        self.assertEqual(made["status"], "pending_payment")
        self.assertEqual(made["checkout_url"], CHECKOUT + "000001")
        self.assertEqual(made["quote"]["deposit"], {"per_phone_cents": 2000, "phones": 2,
                                                    "total_cents": 4000, "refundable": True})
        self.assertEqual(made["status_url"], "https://phone-harness.com/reservation?id=" + rid(1))

    def test_open_opens_the_checkout_link_in_the_browser(self):
        r = self.reserve("--open")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(browser_recorder.recorder.urls(), [CHECKOUT + "000001"])
        self.assertIn("opened in your browser", r.stdout)
        self.assertIn(CHECKOUT + "000001", r.stdout)                 # still printed: it may be relayed
        browser_recorder.recorder.reset()
        quiet = self.reserve("--open", "--json", env={"PHONE_HARNESS_CLOUD_WATCH": "0"})
        self.assertEqual(json.loads(quiet.stdout)["reservation_id"], rid(2))
        self.assertEqual(browser_recorder.recorder.urls(), [CHECKOUT + "000002"])   # --open is explicit

    def test_a_link_that_is_not_stripe_checkout_is_neither_shown_nor_opened(self):
        for bad in ("https://checkout.stripe.com.example.net/pay", "http://checkout.stripe.com/c/pay/x",
                    "https://checkout.stripe.com@example.net/pay", "file:///etc/passwd",
                    "https://example.net/?next=https://checkout.stripe.com/"):
            with self.subTest(bad=bad):
                FakeApi.checkout = bad
                r = self.reserve("--open")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertNotIn(bad, r.stdout + r.stderr)
                self.assertIn("is not a Stripe Checkout page", r.stdout)
                self.assertNotIn("opened in your browser", r.stdout)
        self.assertEqual(browser_recorder.recorder.urls(), [])

    def test_a_name_is_optional_and_overrides_the_accounts_when_given(self):
        plain = self.reserve()                                  # nothing said: the account's name
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(FakeApi.requests[-1][3], {"phone_type": "iphone", "plan": "monthly", "quantity": 1,
                                                   "use_case": USE_CASE})
        self.assertEqual(FakeApi.names[rid(1)], ["Ada", "King"])
        named = self.reserve("--first-name", " Dana ", "--last-name", "Okafor")
        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertEqual(FakeApi.requests[-1][3], {
            "phone_type": "iphone", "plan": "monthly", "quantity": 1, "use_case": USE_CASE,
            "first_name": "Dana", "last_name": "Okafor"})
        self.assertEqual(FakeApi.names[rid(2)], ["Dana", "Okafor"])
        half = self.reserve("--last-name=Lovelace", "--first-name", "  ")     # one of the two; a blank is not sent
        self.assertEqual(half.returncode, 0, half.stderr)
        self.assertEqual(FakeApi.requests[-1][3], {
            "phone_type": "iphone", "plan": "monthly", "quantity": 1, "use_case": USE_CASE,
            "last_name": "Lovelace"})
        self.assertEqual(FakeApi.names[rid(3)], ["Ada", "Lovelace"])
        FakeApi.account_name = None                             # no name on file, and none is needed
        bare = self.reserve()
        self.assertEqual(bare.returncode, 0, bare.stderr)
        self.assertIn(f"Reservation {rid(4)} started", bare.stdout)
        self.assertEqual(FakeApi.names[rid(4)], [None, None])
        for request in FakeApi.requests:
            self.assertNotIn("email", request[3])              # the account's address is the API's to fill in

    def test_a_key_that_was_already_paid_for_shows_where_it_stands(self):
        key = "paid-already_0123456789"
        row = self.hold(reservation(1, quantity=2, status="queued", position=4, paid_days_ago=2))
        FakeApi.keys[key] = ({"phone_type": "iphone", "plan": "monthly", "quantity": 2,
                              "use_case": USE_CASE}, row["reservation_id"])
        r = self.reserve("--qty", "2", "--idempotency-key", key, "--open")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Reservation {rid(1)} already exists for this key", r.stdout)
        self.assertIn("status       queued", r.stdout)
        self.assertIn("position     4 in the queue", r.stdout)
        self.assertNotIn(f"Reservation {rid(1)} started", r.stdout)
        self.assertNotIn("checkout.stripe.com", r.stdout)
        self.assertEqual(browser_recorder.recorder.urls(), [])        # nothing left to pay


class ReserveErrors(ReserveCase):
    def test_not_signed_in_sends_nothing(self):
        r = self.reserve(env={"PHONE_HARNESS_API_KEY": ""})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not signed in. Run: phone-harness cloud login", r.stderr)
        for args in (["reservations"], ["reservations", rid(1)], ["reservations", "cancel", rid(1), "--yes"]):
            r = self.run_cli(*args, env={"PHONE_HARNESS_API_KEY": ""})
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("phone-harness cloud login", r.stderr)
        self.assertEqual(FakeApi.requests, [])                # never an anonymous reservation

    def test_the_cli_sign_in_is_the_bearer_and_a_refused_one_says_to_sign_in(self):
        auth = Path(self.home.name) / "config" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"access_token": "at-cli", "expires_at": time.time() + 3600}))
        FakeApi.valid = {"at-cli"}
        r = self.reserve(env={"PHONE_HARNESS_API_KEY": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeApi.requests[0][2]["authorization"], "Bearer at-cli")
        FakeApi.valid = set()
        refused = self.reserve(env={"PHONE_HARNESS_API_KEY": ""})
        self.assertEqual(refused.returncode, 1)
        self.assertIn("signed out. Run: phone-harness cloud login", refused.stderr)
        listing = self.run_cli("reservations")
        self.assertEqual(listing.returncode, 1)
        self.assertIn("phone-harness cloud login", listing.stderr)

    def test_api_refusals_are_said_in_the_apis_words(self):
        cases = [
            ((403, {"error": "Reservations are not open yet.", "code": "reservations_closed"}, None),
             ["Reservations are not open yet."]),
            ((403, {"error": "That phone and plan cannot be reserved yet.", "code": "plan_unavailable"}, None),
             ["That phone and plan cannot be reserved yet.", "another --type or --plan"]),
            ((429, {"error": "Too many unfinished reservations. Finish one, or try again in an hour.",
                    "code": "rate_limited", "retry_after_seconds": 3600}, {"Retry-After": "3600"}),
             ["Too many unfinished reservations", "Cancelling one does not free a place; paying for one does.",
              "phone-harness cloud reservations"]),
            ((400, {"error": "A first name is too long"}, None), ["A first name is too long"]),
            ((400, {"error": "Choose between 1 and 50 phones"}, None), ["Choose between 1 and 50 phones"]),
            # What the API says of a phone it does not reserve, should this CLI ever offer one it has dropped.
            ((400, {"error": "Cloud Android cannot be reserved. Choose a phone: iphone, iphone_esim",
                    "code": "phone_not_reservable"}, None),
             ["Cloud Android cannot be reserved. Choose a phone: iphone, iphone_esim"]),
            ((403, {"error": "this sign-in can drive phones, not manage the account; use the dashboard "
                             "or an API key", "code": "scope_limited"}, None), ["use the dashboard or an API key"]),
            ((403, {"error": "account is blocked", "code": "account_blocked"}, None), ["blocked"]),
            ((500, {"error": "The phone service could not complete this request."}, None),
             ["could not complete this request", "HTTP 500"]),
        ]
        for fail, needles in cases:
            with self.subTest(code=fail[1].get("code") or fail[0]):
                FakeApi.fail = fail
                r = self.reserve()
                self.assertEqual(r.returncode, 1, r.stdout)
                for needle in needles:
                    self.assertIn(needle, r.stderr)
                # A refusal to reserve is not "your account is not enabled for phones".
                self.assertNotIn("isn't enabled for phones", r.stderr)
                self.assertNotIn("source=cli", r.stderr)
                self.assertEqual(r.stdout, "")
        self.assertEqual(FakeApi.rows, {})

    def test_an_api_from_before_reservations_says_so_from_every_command(self):
        """Signed in, a path the API does not have is a 404. Until reservations are live that is all
        of them, and "no reservation of yours has that id" would be the wrong thing to say."""
        FakeApi.live = False
        for args in (["reserve", "--type", "iphone", "--plan", "monthly", "--use-case", USE_CASE],
                     ["reservations"], ["reservations", "--json"], ["reservations", rid(1)],
                     ["reservations", "rsv_0000"], ["reservations", "cancel", rid(1)],
                     ["reservations", "cancel", rid(1), "--yes"]):
            with self.subTest(args=args[:3]):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(f"{self.api} does not take reservations yet", r.stderr)
                self.assertIn("Nothing was changed.", r.stderr)
                for wrong in ("No reservation", "no route", "404", "matches 0", "Traceback"):
                    self.assertNotIn(wrong, r.stderr)
                self.assertEqual(r.stdout, "")
        self.assertEqual(self.sent("POST", "/cancel"), [])            # the read came first and said enough

    def test_a_404_for_one_id_on_an_api_that_has_reservations_is_that_id(self):
        self.hold(reservation(1, status="queued", position=1, paid_days_ago=1))
        unknown = self.run_cli("reservations", rid(9))
        self.assertIn(f"No reservation {rid(9)} on this account", unknown.stderr)
        self.assertEqual(len(FakeApi.requests), 1)             # the API's own "No such reservation" needs no second look
        FakeApi.fail = (404, {"error": "no route GET /reservations/" + rid(9)}, None)     # a 404 in other words
        odd = self.run_cli("reservations", rid(9))
        self.assertEqual(odd.returncode, 1)
        self.assertIn(f"No reservation {rid(9)} on this account", odd.stderr)
        self.assertNotIn("does not take reservations yet", odd.stderr)
        self.assertEqual([req[:2] for req in FakeApi.requests[1:]],
                         [("GET", "/reservations/" + rid(9)), ("GET", "/reservations")])

    def test_a_stripe_outage_says_how_to_retry_the_same_reservation(self):
        FakeApi.fail = (502, {"error": "Stripe could not complete this request; try again shortly.",
                              "code": "payments_unavailable"}, None)
        r = self.reserve()
        self.assertEqual(r.returncode, 1)
        key = FakeApi.requests[0][2]["idempotency-key"]
        self.assertIn("Stripe could not complete this request", r.stderr)
        self.assertIn("Nothing was charged", r.stderr)
        self.assertIn(f"--idempotency-key {key}", r.stderr)

    def test_a_lost_answer_is_asked_for_again_under_the_same_key(self):
        FakeApi.drop = 1
        r = self.reserve("--qty", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        first, second = FakeApi.requests
        self.assertEqual(first[2]["idempotency-key"], second[2]["idempotency-key"])
        self.assertEqual(first[3], second[3])
        self.assertEqual(len(FakeApi.rows), 1)                        # one reservation, not two
        self.assertIn(f"Reservation {rid(1)} started", r.stdout)

    def test_an_answer_that_is_not_a_reservation_is_not_printed_as_one(self):
        for answer in ({}, {"ok": True}, []):
            with self.subTest(answer=answer):
                FakeApi.fail = (200, answer, None)
                r = self.reserve()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("answered with something that is not a reservation", r.stderr)
                self.assertIn("--idempotency-key " + FakeApi.requests[-1][2]["idempotency-key"], r.stderr)
                self.assertEqual(r.stdout, "")
        FakeApi.fail = (200, [], None)                         # and a list that is not the list
        listing = self.run_cli("reservations")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("No reservations.", listing.stdout)

    def test_an_unreachable_api_says_so_and_how_to_continue(self):
        FakeApi.drop = 2
        r = self.reserve()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"Could not reach {self.api}", r.stderr)
        key = FakeApi.requests[0][2]["idempotency-key"]
        self.assertIn(f"--idempotency-key {key}", r.stderr)
        self.assertEqual(r.stdout, "")
        down = self.run_cli("reservations", env={"PHONE_HARNESS_CLOUD_API": "http://127.0.0.1:1"})
        self.assertEqual(down.returncode, 1)
        self.assertIn("Could not reach http://127.0.0.1:1", down.stderr)


class Reservations(ReserveCase):
    def test_the_list_has_every_column_and_lines_up(self):
        self.hold(
            reservation(1, "iphone", "monthly", 3, "queued", position=4, paid_days_ago=2),
            reservation(2, "iphone_esim", "annual", 1, "pending_payment"),
            reservation(3, "android", "monthly", 2, "cancelled", paid_days_ago=5),
            reservation(4, "iphone", "monthly", 3, "queued", position=1, granted=1, paid_days_ago=9),
            reservation(5, "iphone", "annual", 1, "granted", granted=1, paid_days_ago=30),
            reservation(6, "iphone", "monthly", 2, "cancelling", paid_days_ago=1),
            reservation(7, "iphone", "monthly", 1, "cancelled"),
            reservation(8, "iphone", "monthly", 1, "ended", granted=1, paid_days_ago=90),
        )
        r = self.run_cli("reservations")
        self.assertEqual(r.returncode, 0, r.stderr)
        header, *rows, note = r.stdout.splitlines()
        self.assertEqual(header.split(), ["ID", "TYPE", "PLAN", "QTY", "STATUS", "POSITION", "DEPOSIT", "PAID"])
        self.assertEqual([row.split()[0] for row in rows], [rid(n) for n in (8, 7, 6, 5, 4, 3, 2, 1)])  # newest first
        cells = {row.split()[0]: [row[header.index(name):].split("  ")[0].strip() for name in header.split()]
                 for row in rows}
        self.assertEqual(cells[rid(1)], [rid(1), "iphone", "monthly", "3", "queued", "4", "$60.00 paid",
                                         "2 days ago"])
        self.assertEqual(cells[rid(2)], [rid(2), "iphone-esim", "annual", "1", "pending_payment", "-",
                                         "$20.00 due", "-"])
        # A phone this CLI does not reserve, made elsewhere: the API's own word for it.
        self.assertEqual(cells[rid(3)][1:], ["android", "monthly", "2", "cancelled", "-", "$40.00 refunded",
                                             "5 days ago"])
        self.assertEqual(cells[rid(4)][4:], ["queued", "1", "$60.00 paid, $40.00 refundable", "9 days ago"])
        self.assertEqual(cells[rid(5)][4:], ["granted", "-", "$20.00 credited", "30 days ago"])
        self.assertEqual(cells[rid(6)][4:], ["cancelling", "-", "$40.00 refund pending", "1 day ago"])
        self.assertEqual(cells[rid(7)][4:], ["cancelled", "-", "-", "-"])
        self.assertEqual(cells[rid(8)][4:], ["ended", "-", "$20.00 credited", "90 days ago"])
        self.assertIn("pending_payment holds no place until its deposit is paid", note)
        self.assertNotIn(CHECKOUT, r.stdout)                  # a link is shown with one reservation, not in the list
        [(method, path, headers, _body)] = FakeApi.requests
        self.assertEqual((method, path, headers["authorization"]), ("GET", "/reservations", f"Bearer {KEY}"))

    def test_an_empty_list_and_the_list_as_json(self):
        empty = self.run_cli("reservations")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn("No reservations. `phone-harness cloud reserve` starts one.", empty.stdout)
        self.assertEqual(json.loads(self.run_cli("reservations", "--json").stdout), [])
        self.hold(reservation(1, status="queued", position=2, paid_days_ago=1), reservation(2))
        listed = json.loads(self.run_cli("reservations", "--json").stdout)
        self.assertEqual([row["reservation_id"] for row in listed], [rid(2), rid(1)])
        self.assertEqual(listed[1]["deposit"]["paid_cents"], 2000)
        self.assertEqual(listed[0]["checkout_url"], CHECKOUT + "000002")

    def test_one_reservation_by_id_or_by_a_unique_prefix(self):
        self.hold(reservation(1, quantity=3, status="queued", position=4, paid_days_ago=2),
                  reservation(2, "iphone_esim", "annual", 1),
                  reservation(FAR, quantity=3, status="queued", position=1, granted=1, paid_days_ago=9))
        r = self.run_cli("reservations", rid(1))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeApi.requests[-1][:2], ("GET", "/reservations/" + rid(1)))
        for line in (f"reservation  {rid(1)}", "status       queued",
                     "You are in the queue. Cancel any time and the deposit comes back in full.",
                     "phones       3 x iPhone, monthly", "position     4 in the queue",
                     "deposit      $60.00 paid", "paid         2 days ago",
                     "first month  1 x $50.00 intro price, once per customer",
                     "$210.00 in all; $150.00 due at handover, after the deposit",
                     f"cancel       phone-harness cloud reservations cancel {rid(1)}"):
            self.assertIn(line, r.stdout)
        self.assertNotIn("checkout.stripe.com", r.stdout)

        unpaid = self.run_cli("reservations", rid(2))
        self.assertIn("status       pending_payment", unpaid.stdout)
        self.assertIn("deposit      $20.00 due now (1 x $20.00), refundable in full while queued", unpaid.stdout)
        self.assertIn("pay          " + CHECKOUT + "000002", unpaid.stdout)

        partly = self.run_cli("reservations", "rsv_a1")               # a prefix: looked up in the list
        self.assertEqual(partly.returncode, 0, partly.stderr)
        self.assertEqual(FakeApi.requests[-1][:2], ("GET", "/reservations"))
        self.assertIn("handed over  1 of 3; 2 still waiting", partly.stdout)
        self.assertNotIn("the plan has ended", partly.stdout)
        self.assertIn("deposit      $60.00 paid, $40.00 refundable", partly.stdout)
        self.assertIn("paid         9 days ago", partly.stdout)

        as_json = self.run_cli("reservations", rid(2), "--json")
        self.assertEqual(json.loads(as_json.stdout)["reservation_id"], rid(2))

        several = self.run_cli("reservations", "rsv_0000")
        self.assertEqual(several.returncode, 1)
        self.assertIn("'rsv_0000' matches 2 reservations", several.stderr)
        self.assertIn(rid(2), several.stderr)
        for unknown in (rid(99), "rsv_ffff", "nope"):
            missing = self.run_cli("reservations", unknown)
            self.assertEqual(missing.returncode, 1, unknown)
            self.assertIn("phone-harness cloud reservations` lists them", missing.stderr)
        self.assertIn(f"No reservation {rid(99)} on this account", self.run_cli("reservations", rid(99)).stderr)

    def test_a_reservation_for_a_phone_this_cli_does_not_reserve_is_still_shown_and_cancelled(self):
        # One made while an Android was still offered stays on the account.
        # It has no price, so its quote is the deposit alone.
        self.hold(reservation(1, "android", "monthly", 2, "queued", position=3, paid_days_ago=1))
        one = self.run_cli("reservations", rid(1))
        self.assertEqual(one.returncode, 0, one.stderr)
        for line in ("status       queued", "phones       2 x android, monthly", "position     3 in the queue",
                     "deposit      $40.00 paid", "first month  price confirmed before handover"):
            self.assertIn(line, one.stdout)
        self.assertNotIn("in all", one.stdout)
        gone = self.run_cli("reservations", "cancel", rid(1), "--yes")
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertIn(f"Reservation {rid(1)} cancelled. $40.00 is on its way back to the card that paid.",
                      gone.stdout)

    def test_usage_for_words_it_does_not_take(self):
        for args in (["reservations", rid(1), rid(2)], ["reservations", "cancel"],
                     ["reservations", "cancel", rid(1), rid(2)], ["reservations", "--yes"],
                     ["reservations", rid(1), "--yes"]):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("Usage: phone-harness cloud reservations [ID] [--json]", r.stderr)
        self.assertEqual(FakeApi.requests, [])


class Cancel(ReserveCase):
    def test_cancel_without_yes_says_what_it_would_do_and_does_nothing(self):
        self.hold(reservation(1, quantity=2, status="queued", position=4, paid_days_ago=2))
        r = self.run_cli("reservations", "cancel", rid(1))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"Reservation {rid(1)}: 2 x iPhone, monthly, position 4 in the queue.", r.stderr)
        self.assertIn("gives up that place and refunds $40.00 to the card that paid", r.stderr)
        self.assertIn(f"Not cancelled. To confirm, run: phone-harness cloud reservations cancel {rid(1)} --yes",
                      r.stderr)
        self.assertEqual(self.sent("POST"), [])
        self.assertEqual(FakeApi.rows[rid(1)]["status"], "queued")
        as_json = self.run_cli("reservations", "cancel", rid(1), "--json")     # --json never asks
        self.assertEqual((as_json.returncode, as_json.stdout), (1, ""))
        self.assertEqual(self.sent("POST"), [])

    def test_cancel_refunds_the_deposit_and_says_how_much(self):
        self.hold(reservation(1, quantity=2, status="queued", position=4, paid_days_ago=2))
        r = self.run_cli("reservations", "cancel", rid(1), "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        [(method, path, headers, body)] = self.sent("POST")
        self.assertEqual((method, path, body), ("POST", f"/reservations/{rid(1)}/cancel", None))
        self.assertEqual(headers["authorization"], f"Bearer {KEY}")
        self.assertIn(f"Reservation {rid(1)} cancelled. $40.00 is on its way back to the card that paid.",
                      r.stdout)
        self.assertIn("You left the queue and the deposit was refunded in full.", r.stdout)
        again = self.run_cli("reservations", "cancel", rid(1), "--yes")          # twice refunds once
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(f"Reservation {rid(1)} was already cancelled. Nothing to refund.", again.stdout)

    def test_cancel_by_prefix_and_as_json(self):
        self.hold(reservation(1), reservation(FAR, quantity=1, status="queued", position=1, paid_days_ago=1))
        r = self.run_cli("reservations", "cancel", "rsv_a1", "--yes", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        done = json.loads(r.stdout)
        self.assertEqual((done["reservation_id"], done["status"], done["refunded_cents"]),
                         (rid(FAR), "cancelled", 2000))
        self.assertEqual([req[:2] for req in FakeApi.requests],
                         [("GET", "/reservations"), ("POST", f"/reservations/{rid(FAR)}/cancel")])
        self.assertEqual(FakeApi.rows[rid(1)]["status"], "pending_payment")     # the other one is untouched

    def test_an_unpaid_reservation_is_cancelled_with_nothing_to_refund(self):
        self.hold(reservation(1, quantity=3))
        asked = self.run_cli("reservations", "cancel", rid(1))
        self.assertIn("deposit not paid", asked.stderr)
        self.assertIn("Nothing was charged, so nothing is refunded", asked.stderr)
        r = self.run_cli("reservations", "cancel", rid(1), "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Reservation {rid(1)} cancelled. Nothing had been charged.", r.stdout)
        self.assertNotIn("on its way back", r.stdout)

    def test_a_cancel_after_the_grant_is_refused_in_the_apis_words(self):
        self.hold(reservation(1, status="granted", granted=1, paid_days_ago=20))
        r = self.run_cli("reservations", "cancel", rid(1))        # nothing to confirm: the API decides
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.sent("POST", "/cancel")), 1)
        self.assertIn("Not cancelled. " + GRANTED_REFUSAL, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(FakeApi.rows[rid(1)]["status"], "granted")
        self.assertEqual(FakeApi.rows[rid(1)]["deposit"]["refunded_cents"], 0)

    def test_a_plan_that_has_ended_says_so_and_cannot_be_cancelled(self):
        self.hold(reservation(1, quantity=2, status="ended", granted=2, paid_days_ago=90))
        shown = self.run_cli("reservations", rid(1))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        for line in ("status       ended", "The plan for this phone has ended.",
                     "handed over  2 of 2; the plan has ended for 2", "deposit      $40.00 credited"):
            self.assertIn(line, shown.stdout)
        self.assertNotIn("cancel       phone-harness", shown.stdout)
        self.assertNotIn("still waiting", shown.stdout)
        r = self.run_cli("reservations", "cancel", rid(1))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not cancelled. " + GRANTED_REFUSAL, r.stderr)

    def test_a_grant_in_progress_and_a_refund_stripe_did_not_confirm(self):
        self.hold(reservation(1, quantity=2, status="queued", position=1, paid_days_ago=3))
        busy = (409, {"error": "Your phone is being handed over right now. Try again in a few minutes.",
                      "code": "grant_in_progress", "retry_after_seconds": 120}, {"Retry-After": "120"})
        unconfirmed = (502, {**reservation(1, quantity=2, status="cancelling", paid_days_ago=3),
                             "code": "refund_unconfirmed",
                             "error": "You have left the queue, but the refund could not be started. "
                                      "Try again in a few minutes."}, None)
        outage = (502, {"error": "Stripe could not complete this request; try again shortly.",
                        "code": "payments_unavailable"}, None)
        for fail, needles in ((busy, ["Not cancelled. Your phone is being handed over right now."]),
                              (unconfirmed, ["the refund could not be started", "never refunds twice",
                                             f"phone-harness cloud reservations cancel {rid(1)} --yes"]),
                              (outage, ["Stripe could not complete this request", "Nothing changed",
                                        f"phone-harness cloud reservations cancel {rid(1)} --yes"])):
            with self.subTest(code=fail[1]["code"]):
                r = self._cancel_failing(fail)
                self.assertEqual(r.returncode, 1, r.stdout)
                for needle in needles:
                    self.assertIn(needle, r.stderr)
                self.assertEqual(r.stdout, "")

    def _cancel_failing(self, fail):
        """A confirmed cancel whose POST answers `fail`. The read before it still works."""
        original = FakeApi._cancel

        def refused(handler, row):
            return handler._send(*fail)
        FakeApi._cancel = refused
        try:
            return self.run_cli("reservations", "cancel", rid(1), "--yes")
        finally:
            FakeApi._cancel = original

    def test_part_handed_over_refunds_only_the_phones_still_waiting(self):
        self.hold(reservation(1, quantity=3, status="queued", position=1, granted=1, paid_days_ago=9))
        asked = self.run_cli("reservations", "cancel", rid(1))
        self.assertIn("gives up the 2 still waiting and refunds $40.00", asked.stderr)
        self.assertIn("What was already handed over (1 of 3) is not affected.", asked.stderr)
        r = self.run_cli("reservations", "cancel", rid(1), "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Left the queue for the phones still waiting on {rid(1)}.", r.stdout)
        self.assertIn("$40.00 is on its way back to the card that paid.", r.stdout)
        self.assertIn("1 phone handed over. You left the queue for the other 2", r.stdout)

    def test_a_refund_still_owed_is_retried_without_asking_again(self):
        self.hold(reservation(1, quantity=2, status="cancelling", paid_days_ago=3))
        r = self.run_cli("reservations", "cancel", rid(1))            # they already said yes once
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.sent("POST", "/cancel")), 1)
        self.assertIn("cancelled. $40.00 is on its way back", r.stdout)

    def test_cancelling_someone_elses_or_nobodys_reservation(self):
        r = self.run_cli("reservations", "cancel", rid(42), "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"No reservation {rid(42)} on this account", r.stderr)
        self.assertEqual(self.sent("POST"), [])


class NotFound(BaseHTTPRequestHandler):
    """A stub of an API from before reservations: every request is a 404."""
    # What that 404 can look like. The first is phone-cloud's own (a signed-in
    # request for a path it does not have); the rest are what a proxy or an
    # older server in front of it might say instead.
    KINDS = {
        "the API's own": (b'{"error": "no route %s %s"}', "application/json"),
        "a page": (b"<html><body><h1>404 Not Found</h1></body></html>", "text/html"),
        "nothing": (b"", "text/plain"),
        "other JSON": (b'{"detail": "Not Found"}', "application/json"),
    }
    kind = "the API's own"
    seen = []

    def log_message(self, *a):
        pass

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        NotFound.seen.append((self.command, self.path))
        body, content_type = NotFound.KINDS[NotFound.kind]
        raw = body % (self.command.encode(), self.path.encode()) if b"%s" in body else body
        self.send_response(404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_DELETE = _answer


class NoReservationsYet(ReserveCase):
    """Until reservations are live, every path under /reservations is a 404. Each command has to
    say that, and never "no reservation of yours has that id"."""

    def setUp(self):
        super().setUp()
        stub = ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        self.stub = f"http://127.0.0.1:{stub.server_port}"
        self.env["PHONE_HARNESS_CLOUD_API"] = self.stub

    def test_every_command_says_the_api_does_not_take_reservations_yet(self):
        said = f"{self.stub} does not take reservations yet, so a phone cannot be reserved, listed or cancelled through it. Nothing was changed."
        commands = {
            "reserve": (["reserve", "--type", "iphone", "--plan", "monthly", "--use-case", USE_CASE],
                        [("POST", "/reservations")]),
            "reserve --json --open": (["reserve", "--type", "iphone-esim", "--plan", "annual", "--qty", "3",
                                       "--use-case", USE_CASE, "--json", "--open"], [("POST", "/reservations")]),
            "reservations": (["reservations"], [("GET", "/reservations")]),
            "reservations --json": (["reservations", "--json"], [("GET", "/reservations")]),
            "reservations ID": (["reservations", rid(1)],
                                [("GET", "/reservations/" + rid(1)), ("GET", "/reservations")]),
            "reservations PREFIX": (["reservations", "rsv_00"], [("GET", "/reservations")]),
            "cancel ID": (["reservations", "cancel", rid(1)],
                          [("GET", "/reservations/" + rid(1)), ("GET", "/reservations")]),
            "cancel ID --yes": (["reservations", "cancel", rid(1), "--yes"],
                                [("GET", "/reservations/" + rid(1)), ("GET", "/reservations")]),
            "cancel ID --yes --json": (["reservations", "cancel", rid(1), "--yes", "--json"],
                                       [("GET", "/reservations/" + rid(1)), ("GET", "/reservations")]),
        }
        for kind in NotFound.KINDS:
            for name, (args, asked) in commands.items():
                with self.subTest(kind=kind, command=name):
                    NotFound.kind, NotFound.seen = kind, []
                    r = self.run_cli(*args)
                    self.assertEqual(r.returncode, 1, r.stdout)
                    self.assertEqual(r.stderr.strip(), said)
                    self.assertEqual(r.stdout, "")
                    for wrong in ("No reservation", "on this account", "no route", "404", "Not Found"):
                        self.assertNotIn(wrong, r.stderr)
                    self.assertEqual(NotFound.seen, asked)       # a 404 for one id: is it the id, or the API?
        self.assertEqual(browser_recorder.recorder.urls(), [])
        self.assertEqual(FakeApi.requests, [])                   # nothing went anywhere else


class AtATerminal(unittest.TestCase):
    """The confirmation a person gets. In-process: a subprocess has no terminal."""

    def setUp(self):
        self.row = reservation(1, quantity=2, status="queued", position=4, paid_days_ago=2)
        self.calls = []

    def api(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        if method == "GET":
            return self.row
        return {**reservation(1, quantity=2, status="cancelled", paid_days_ago=2), "refunded_cents": 4000}

    def cancel(self, answer, *args):
        terminal = mock.Mock()
        terminal.isatty.return_value = True
        out = io.StringIO()
        with mock.patch.object(cloud, "_api", self.api), mock.patch.object(sys, "stdin", terminal), \
                mock.patch("builtins.input", return_value=answer) as asked, contextlib.redirect_stdout(out):
            code = cloud.cli(["reservations", "cancel", rid(1), *args])
        return code, out.getvalue(), asked

    def test_no_is_the_default_and_leaves_it_alone(self):
        for answer in ("", "n", "no", "maybe"):
            self.calls = []
            code, out, asked = self.cancel(answer)
            self.assertEqual(code, 1, answer)
            asked.assert_called_once_with("Cancel this reservation? [y/N] ")
            self.assertIn("refunds $40.00 to the card that paid", out)
            self.assertIn("Not cancelled.", out)
            self.assertEqual(self.calls, [("GET", "/reservations/" + rid(1))])

    def test_yes_cancels(self):
        for answer in ("y", "YES"):
            self.calls = []
            code, out, _asked = self.cancel(answer)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.calls, [("GET", "/reservations/" + rid(1)),
                                          ("POST", f"/reservations/{rid(1)}/cancel")])
            self.assertIn("$40.00 is on its way back", out)

    def test_the_flag_skips_the_question(self):
        code, _out, asked = self.cancel("n", "--yes")
        self.assertEqual(code, 0)
        asked.assert_not_called()
        self.assertEqual(len(self.calls), 2)


class Shapes(unittest.TestCase):
    """What is printed for answers the fake does not make on its own."""

    def test_money_is_whatever_currency_the_api_names(self):
        self.assertEqual(cloud._amount(2000), "$20.00")
        self.assertEqual(cloud._amount(201600, "usd"), "$2,016.00")
        self.assertEqual(cloud._amount(2000, "eur"), "20.00 EUR")
        self.assertEqual(cloud._amount(None), "?")

    def test_a_quote_is_only_what_the_api_sent(self):
        row = reservation(1, quantity=2, intro=False)                  # the intro phone was used before
        self.assertEqual(cloud._quote_rows(row), [
            ("deposit", "$40.00 due now (2 x $20.00), refundable in full while queued"),
            ("first month", "2 x $80.00 standard price"),
            ("", "$160.00 in all; $120.00 due at handover, after the deposit")])
        bare = {"quote": {"currency": "usd", "interval": "month", "priced": False, "lines": [],
                          "deposit": {"per_phone_cents": None, "phones": 1, "total_cents": None}}}
        self.assertEqual(cloud._quote_rows(bare), [("first month", "price confirmed before handover")])
        self.assertEqual(cloud._quote_rows({}), [("first period", "price confirmed before handover")])

    def test_a_checkout_link_has_to_be_stripes(self):
        good = "https://checkout.stripe.com/c/pay/cs_live_a1B2#fragment"
        self.assertEqual(cloud._checkout_url({"checkout_url": good}), good)
        for bad in (None, "", 7, "https://checkout.stripe.com/c/pay/x\n", "https://CHECKOUT.stripe.com.evil.example/",
                    "https://user@checkout.stripe.com/pay", "javascript:alert(1)", "https://[bad"):
            self.assertIsNone(cloud._checkout_url({"checkout_url": bad}), bad)

    def test_a_deposit_in_a_few_words_follows_the_money_not_the_status(self):
        later = {**reservation(1, quantity=2, status="granted", granted=2, paid_days_ago=3), "status": "paused"}
        self.assertEqual(cloud._deposit_words(later), "$40.00 credited")       # a status this CLI has not met
        odd = {"status": "paused", "deposit": {"paid_cents": 4000, "currency": "usd"}}
        self.assertEqual(cloud._deposit_words(odd), "$40.00 paid")
        self.assertEqual(cloud._deposit_words({"status": "queued"}), "-")
        self.assertEqual(cloud._deposit_words({"status": "pending_payment"}), "due")

    def test_how_far_off_a_time_is(self):
        now = time.time()
        self.assertEqual(cloud._in(now + 86400), "24 hours")
        self.assertEqual(cloud._in(now + 30 * 60), "30 min")
        self.assertEqual(cloud._in(now + 5 * 86400), "5 days")
        self.assertEqual(cloud._in(now - 5), "expired")
        self.assertEqual(cloud._in(None), "?")

    def test_an_expired_link_says_to_reserve_again(self):
        row = {**reservation(1), "expires_at": time.time() - 60}
        self.assertEqual(cloud._pay_rows(row), [
            ("pay", row["checkout_url"]), ("", "the link has expired; if it no longer opens, reserve again")])
        self.assertIn("no payment link yet", cloud._pay_rows({**row, "checkout_url": None})[0][1])


class Telemetry(unittest.TestCase):
    def test_nothing_a_person_typed_about_a_reservation_is_recorded(self):
        argv = run._telemetry_argv([
            "cloud", "reserve", "--type", "iphone-esim", "--plan", "annual", "--qty", "3",
            "--use-case", "Our secret launch", "--first-name", "Dana", "--last-name=Okafor",
            "--idempotency-key", "retry-key_0123456789", "--open", "--json"])
        self.assertEqual(argv, [
            "cloud", "reserve", "--type", "iphone-esim", "--plan", "annual", "--qty", "3",
            "--use-case", "<redacted>", "--first-name", "<redacted>", "--last-name=<redacted>",
            "--idempotency-key", "<redacted>", "--open", "--json"])
        self.assertEqual(run._telemetry_argv(["cloud", "reservations", "cancel", rid(7), "--yes"]),
                         ["cloud", "reservations", "cancel", "<redacted>", "--yes"])
        self.assertEqual(run._telemetry_argv(["--session", "s1", "cloud", "reservations", "rsv_0000"]),
                         ["cloud", "reservations", "<redacted>"])
        self.assertEqual(run._telemetry_argv(["cloud", "reserve", "--type=iphone_esim", "--qty=2", "-secret", "x=y"]),
                         ["cloud", "reserve", "--type=iphone_esim", "--qty=2", "<redacted>", "<redacted>"])
        # A type these commands do not take is not one of their fixed words.
        self.assertEqual(run._telemetry_argv(["cloud", "reserve", "--type", "android", "--type=android"]),
                         ["cloud", "reserve", "--type", "<redacted>", "--type=<redacted>"])
        # Other commands are recorded as they were.
        self.assertEqual(run._telemetry_argv(["cloud", "start", "Saved phone"]), ["cloud", "start", "Saved phone"])

    def test_every_flag_these_commands_take_is_one_telemetry_keeps(self):
        parsers = cloud._cloud_parsers()
        for name in ("reserve", "reservations"):
            for action in parsers[name]._actions:
                for flag in action.option_strings:
                    self.assertIn(flag, run._RESERVATION_FLAGS, f"{name} {flag}")


if __name__ == "__main__":
    unittest.main()
