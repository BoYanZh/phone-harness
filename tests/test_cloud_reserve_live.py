"""`cloud reserve` and `cloud reservations` against a real phone-cloud running on this machine.

Skipped unless PHONE_CLOUD_LOCAL_STATE names the state file that phone-cloud's
tests/local_reservations.py writes. That script is the real API process on a
throwaway PostgreSQL with Stripe faked, so nothing here reaches Stripe, a card
or a phone. Beside the API it gives a "control" address that does what only
Stripe, or a person at Stripe's page, can do: pay a Checkout, let one run out,
end a plan. test_cloud_reserve.py is the same commands against a fake API.

    # in a phone-cloud checkout that has the reservation API
    python3 tests/local_reservations.py --port 8793 --control 8794 --state /tmp/local-cloud.json
    # here
    PHONE_CLOUD_LOCAL_STATE=/tmp/local-cloud.json \\
        python -m unittest discover tests -p test_cloud_reserve_live.py

Every amount asserted is read from the API's own answers; this file names none.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
SUPPORT = str(Path(__file__).resolve().parent / "support")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if SUPPORT not in sys.path:
    sys.path.insert(0, SUPPORT)
import browser_recorder
browser_recorder.install()

from phone_harness.cloud import _amount  # noqa: E402

STATE = os.environ.get("PHONE_CLOUD_LOCAL_STATE")
USE_CASE = "An integration run of the phone-harness CLI."


class Local:
    """The local phone-cloud: its API, its operator, and the stand-in for Stripe's side."""

    def __init__(self, path):
        self.state = json.loads(Path(path).read_text())
        self.api, self.control = self.state["api"], self.state["control"]

    @staticmethod
    def http(method, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def operator(self, method, path, body=None, key=None):
        headers = {"Authorization": "Bearer " + self.state["operator_token"],
                   "X-Phone-Cloud-Actor": self.state["operator_actor"]}
        if key:
            headers["Idempotency-Key"] = key
        return self.http(method, self.api + path, body, headers)

    def account(self, name=None):
        """A new account and an API key for it. `name` None is an account with no name on file."""
        # An address of its own, at the made-up domain this local cloud's accounts use.
        domain = self.state["accounts"][0]["email"].partition("@")[2]
        code, made = self.operator("POST", "/admin/users", {
            "email": f"cli-{uuid.uuid4().hex[:12]}@{domain}", "name": name})
        assert code == 201, (code, made)
        code, key = self.operator("POST", f"/admin/users/{made['uid']}/keys", {"label": "cli integration"})
        assert code == 201, (code, key)
        return key["key"]

    def pay(self, rid):
        """A person pays the deposit on Stripe's page, and Stripe's webhook lands."""
        code, paid = self.http("POST", f"{self.control}/pay/{rid}")
        assert (code, paid.get("webhook")) == (200, 200), (code, paid)
        return paid["reservation"]

    def expire(self, rid):
        code, gone = self.http("POST", f"{self.control}/expire/{rid}")
        assert (code, gone.get("webhook")) == (200, 200), (code, gone)

    def queue_row(self, rid):
        code, listed = self.operator("GET", "/admin/reservations")
        assert code == 200, (code, listed)
        return next(row for row in listed["queue"] if row["id"] == rid)

    def grant(self, rid, phones=None):
        """The operator hands phones over: the plan starts and their deposit comes off its first bill."""
        body = {"confirm": self.queue_row(rid)["confirm"], "dry_run": False}
        if phones is not None:
            body["phones"] = phones
        code, done = self.operator("POST", f"/admin/reservations/{rid}/grant", body,
                                   key="cli-grant-" + uuid.uuid4().hex)
        assert code == 200 and done.get("granted") is True, (code, done)
        return done

    def end_plan(self, subscription):
        code, ended = self.http("POST", f"{self.control}/subscription/{subscription}/cancel")
        assert (code, ended.get("webhook")) == (200, 200), (code, ended)

    def refunds(self, rid):
        code, stripe = self.http("GET", self.control + "/stripe")
        assert code == 200, code
        return [r for r in stripe["refunds"] if (r.get("metadata") or {}).get("reservation_id") == rid]


@unittest.skipUnless(STATE, "set PHONE_CLOUD_LOCAL_STATE to the state file of a local phone-cloud "
                            "(its tests/local_reservations.py)")
class LiveReservations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.local = Local(STATE)
        code, health = cls.local.http("GET", cls.local.api + "/health")
        assert (code, health.get("ok")) == (200, True), "the local phone-cloud is not answering"

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.key = self.local.account("Dana Okafor")
        browser_recorder.recorder.reset()

    def run_cli(self, *args, key=None, env=None):
        full = {**os.environ, "PYTHONPATH": SUPPORT + os.pathsep + SRC, "PHONE_HARNESS_HOME": self.home.name,
                "PHONE_HARNESS_TELEMETRY": "0", "PHONE_HARNESS_NO_UPDATE_CHECK": "1",
                "PHONE_HARNESS_CLOUD_API": self.local.api,
                "PHONE_HARNESS_API_KEY": self.key if key is None else key, **(env or {})}
        for name in ("PHONE_HARNESS_SESSION", "BROWSER", "NO_COLOR"):
            full.pop(name, None)
        return subprocess.run([sys.executable, "-m", "phone_harness.run", "cloud", *args],
                              capture_output=True, text=True, env=full, stdin=subprocess.DEVNULL)

    def reserve(self, *extra, kind="iphone", plan="monthly", key=None):
        r = self.run_cli("reserve", "--type", kind, "--plan", plan, "--use-case", USE_CASE, "--json", *extra,
                         key=key)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def read(self, rid, key=None):
        r = self.run_cli("reservations", rid, "--json", key=key)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_reserve_pay_list_and_cancel_with_the_deposit_refunded(self):
        made = self.reserve("--qty", "2")
        rid = made["reservation_id"]
        quote, deposit = made["quote"], made["quote"]["deposit"]
        total = deposit["total_cents"]
        self.assertEqual(made["status"], "pending_payment")
        self.assertTrue(made["checkout_url"].startswith("https://checkout.stripe.com/"), made["checkout_url"])
        self.assertEqual((made["phone_type"], made["plan"], made["quantity"]), ("iphone", "monthly", 2))
        self.assertGreater(total, 0)
        self.assertEqual(total, deposit["per_phone_cents"] * 2)
        self.assertEqual(sum(line["quantity"] for line in quote["lines"]), 2)
        self.assertEqual(quote["first_period_cents"],
                         sum(line["quantity"] * line["unit_cents"] for line in quote["lines"]))
        self.assertEqual(quote["due_at_handover_cents"], quote["first_period_cents"] - total)
        self.assertIsNone(made["paid_at"])
        self.assertIsNone(made["position"])

        # Unpaid, it holds no place: no position anywhere, and the link is still shown.
        shown = self.run_cli("reservations", rid)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("status       pending_payment", shown.stdout)
        self.assertIn(f"deposit      {_amount(total)} due now", shown.stdout)
        self.assertIn("pay          " + made["checkout_url"], shown.stdout)
        self.assertNotIn("position", shown.stdout)
        listed = json.loads(self.run_cli("reservations", "--json").stdout)
        self.assertEqual([(row["reservation_id"], row["status"], row["position"]) for row in listed],
                         [(rid, "pending_payment", None)])

        # A person pays on Stripe's page; only then is there a place in the queue.
        self.local.pay(rid)
        queued = self.read(rid)
        self.assertEqual(queued["status"], "queued")
        self.assertIsInstance(queued["position"], int)
        self.assertGreaterEqual(queued["position"], 1)
        self.assertIsNotNone(queued["paid_at"])
        self.assertEqual(queued["deposit"]["paid_cents"], total)
        self.assertEqual(queued["deposit"]["refundable_cents"], total)
        self.assertIsNone(queued["checkout_url"])
        table = self.run_cli("reservations")
        row = next(line for line in table.stdout.splitlines() if line.startswith(rid))
        self.assertEqual(row.split()[1:6], ["iphone", "monthly", "2", "queued", str(queued["position"])])
        self.assertIn(f"{_amount(total)} paid", row)

        # Cancelling asks first, and changes nothing until it is confirmed.
        asked = self.run_cli("reservations", "cancel", rid)
        self.assertEqual(asked.returncode, 1)
        self.assertIn(f"refunds {_amount(total)} to the card that paid", asked.stderr)
        self.assertIn(f"phone-harness cloud reservations cancel {rid} --yes", asked.stderr)
        self.assertEqual(self.read(rid)["status"], "queued")
        self.assertEqual(self.local.refunds(rid), [])

        done = self.run_cli("reservations", "cancel", rid, "--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"Reservation {rid} cancelled. {_amount(total)} is on its way back", done.stdout)
        after = self.read(rid)
        self.assertEqual((after["status"], after["position"], after["can_cancel"]), ("cancelled", None, False))
        self.assertEqual(after["deposit"]["refunded_cents"], total)
        self.assertEqual([refund["amount"] for refund in self.local.refunds(rid)], [total])

        again = self.run_cli("reservations", "cancel", rid, "--yes")           # twice refunds once
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("was already cancelled", again.stdout)
        self.assertEqual(len(self.local.refunds(rid)), 1)

    def test_the_printed_quote_and_open(self):
        r = self.run_cli("reserve", "--type", "iphone-esim", "--plan", "annual", "--qty", "3",
                         "--use-case", USE_CASE, "--open")
        self.assertEqual(r.returncode, 0, r.stderr)
        [made] = json.loads(self.run_cli("reservations", "--json").stdout)
        quote = made["quote"]
        self.assertEqual((made["phone_type"], made["plan"], made["quantity"]), ("iphone_esim", "annual", 3))
        self.assertIn(f"Reservation {made['reservation_id']} started: 3 x iPhone with eSIM, annual.", r.stdout)
        self.assertIn("holds no place in the queue until the deposit is paid", r.stdout)
        self.assertIn(f"deposit      {_amount(quote['deposit']['total_cents'])} due now "
                      f"(3 x {_amount(quote['deposit']['per_phone_cents'])}), refundable in full while queued",
                      r.stdout)
        for line in quote["lines"]:
            self.assertIn(f"{line['quantity']} x {_amount(line['unit_cents'])} {line['tier']} price", r.stdout)
        self.assertEqual({line["tier"] for line in quote["lines"]}, {"intro", "standard"})
        self.assertIn(f"{_amount(quote['first_period_cents'])} in all; "
                      f"{_amount(quote['due_at_handover_cents'])} due at handover", r.stdout)
        self.assertIn("pay          " + made["checkout_url"], r.stdout)
        self.assertEqual(browser_recorder.recorder.urls(), [made["checkout_url"]])
        self.assertIn("opened in your browser", r.stdout)

    def test_a_cancel_after_the_grant_is_refused_and_an_ended_plan_says_so(self):
        made = self.reserve()
        rid, total = made["reservation_id"], made["quote"]["deposit"]["total_cents"]
        self.local.pay(rid)
        granted = self.local.grant(rid)
        after = self.read(rid)
        self.assertEqual((after["status"], after["granted"], after["queued"]), ("granted", 1, 0))
        self.assertEqual(after["deposit"]["credited_cents"], total)
        self.assertEqual((after["deposit"]["refundable_cents"], after["can_cancel"]), (0, False))

        refused = self.run_cli("reservations", "cancel", rid, "--yes")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Not cancelled.", refused.stderr)
        self.assertIn("handed over", refused.stderr)                  # the API's own sentence
        self.assertEqual(refused.stdout, "")
        self.assertEqual(self.read(rid)["status"], "granted")
        self.assertEqual(self.local.refunds(rid), [])

        self.local.end_plan(granted["subscription"])
        ended = self.read(rid)
        self.assertEqual((ended["status"], ended.get("ended")), ("ended", 1))
        shown = self.run_cli("reservations", rid)
        self.assertIn("status       ended", shown.stdout)
        self.assertIn("the plan has ended for 1", shown.stdout)
        still = self.run_cli("reservations", "cancel", rid, "--yes")
        self.assertEqual(still.returncode, 1)
        self.assertIn("Not cancelled.", still.stderr)
        self.assertEqual(self.local.refunds(rid), [])

    def test_part_handed_over_refunds_only_the_phones_still_waiting(self):
        made = self.reserve("--qty", "3")
        rid, each = made["reservation_id"], made["quote"]["deposit"]["per_phone_cents"]
        self.local.pay(rid)
        self.local.grant(rid, phones=1)
        partly = self.read(rid)
        self.assertEqual((partly["status"], partly["granted"], partly["queued"]), ("queued", 1, 2))
        self.assertEqual(partly["deposit"]["refundable_cents"], each * 2)
        asked = self.run_cli("reservations", "cancel", rid)
        self.assertIn(f"gives up the 2 still waiting and refunds {_amount(each * 2)}", asked.stderr)
        done = self.run_cli("reservations", "cancel", rid, "--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"Left the queue for the phones still waiting on {rid}.", done.stdout)
        self.assertIn(f"{_amount(each * 2)} is on its way back", done.stdout)
        after = self.read(rid)
        self.assertEqual((after["status"], after["granted"], after["queued"]), ("granted", 1, 0))
        self.assertEqual((after["deposit"]["refunded_cents"], after["deposit"]["credited_cents"]), (each * 2, each))
        self.assertEqual([refund["amount"] for refund in self.local.refunds(rid)], [each * 2])

    def test_the_same_key_is_the_same_reservation(self):
        key = "cli-live-" + uuid.uuid4().hex
        first = self.reserve("--qty", "2", "--idempotency-key", key)
        again = self.reserve("--qty", "2", "--idempotency-key", key)
        self.assertEqual(first["reservation_id"], again["reservation_id"])
        self.assertEqual(first["checkout_url"], again["checkout_url"])
        self.assertEqual(len(json.loads(self.run_cli("reservations", "--json").stdout)), 1)
        other = self.run_cli("reserve", "--type", "iphone", "--plan", "monthly", "--qty", "5",
                             "--use-case", USE_CASE, "--idempotency-key", key)
        self.assertEqual(other.returncode, 1)
        self.assertIn("already used for a different request", other.stderr)
        # Paid, the same key says where it stands and offers nothing more to pay.
        self.local.pay(first["reservation_id"])
        paid = self.run_cli("reserve", "--type", "iphone", "--plan", "monthly", "--qty", "2",
                            "--use-case", USE_CASE, "--idempotency-key", key, "--open")
        self.assertEqual(paid.returncode, 0, paid.stderr)
        self.assertIn("already exists for this key", paid.stdout)
        self.assertIn("status       queued", paid.stdout)
        self.assertNotIn("checkout.stripe.com", paid.stdout)
        self.assertEqual(browser_recorder.recorder.urls(), [])

    def test_a_name_is_optional_and_an_override_is_used(self):
        nameless = self.local.account(None)
        bare = self.reserve(key=nameless)                       # no name on file and none given: still made
        self.assertEqual(bare["status"], "pending_payment")
        named = self.reserve("--first-name", "Grace", "--last-name", "Hopper", key=nameless)
        mine = self.reserve()                                   # this account has "Dana Okafor" on file
        half = self.reserve("--first-name", "Ada")              # one of the two; the other stays the account's
        for made in (bare, named, mine, half):
            self.local.pay(made["reservation_id"])
        rows = [self.local.queue_row(made["reservation_id"]) for made in (bare, named, mine, half)]
        self.assertEqual([row["name"] for row in rows],         # what the operator reads
                         [None, "Grace Hopper", "Dana Okafor", "Ada Okafor"])
        self.assertEqual({row["use_case"] for row in rows}, {USE_CASE})

    def test_one_account_cannot_read_or_cancel_anothers(self):
        made = self.reserve()
        rid = made["reservation_id"]
        self.local.pay(rid)
        stranger = self.local.account("Bo Stranger")
        self.assertEqual(json.loads(self.run_cli("reservations", "--json", key=stranger).stdout), [])
        for args in (["reservations", rid], ["reservations", "cancel", rid, "--yes"]):
            r = self.run_cli(*args, key=stranger)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(f"No reservation {rid} on this account", r.stderr)
        self.assertEqual(self.read(rid)["status"], "queued")
        self.assertEqual(self.local.refunds(rid), [])

    def test_too_many_unfinished_reservations_are_refused_until_one_is_paid(self):
        made, refused = [], None
        for _ in range(12):
            r = self.run_cli("reserve", "--type", "iphone", "--plan", "monthly", "--use-case", USE_CASE, "--json")
            if r.returncode:
                refused = r
                break
            made.append(json.loads(r.stdout)["reservation_id"])
        self.assertIsNotNone(refused, "twelve unpaid reservations were all accepted")
        self.assertGreaterEqual(len(made), 2)
        self.assertIn("`phone-harness cloud reservations` lists the unfinished ones", refused.stderr)
        self.assertEqual(refused.stdout, "")
        self.assertEqual(len(json.loads(self.run_cli("reservations", "--json").stdout)), len(made))
        # Giving one up is not a way back in. Paying for one is.
        gone = self.run_cli("reservations", "cancel", made[0], "--yes")
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertIn("Nothing had been charged", gone.stdout)
        still = self.run_cli("reserve", "--type", "iphone", "--plan", "monthly", "--use-case", USE_CASE, "--json")
        self.assertEqual(still.returncode, 1, still.stdout)
        self.local.pay(made[1])
        self.reserve()

    def test_an_android_is_not_reserved_and_the_api_is_never_asked(self):
        r = self.run_cli("reserve", "--type", "android", "--plan", "monthly", "--use-case", USE_CASE)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stderr.splitlines()[0],
                         "Reservations are for iPhones only (with or without eSIM). "
                         "Cloud Android emulators are on the $20 plan and are not reserved.")
        self.assertEqual(r.stdout, "")
        # Refused here, before the API: the account has no reservation, not even an unpaid one.
        self.assertEqual(json.loads(self.run_cli("reservations", "--json").stdout), [])

    def test_a_checkout_that_ran_out_is_cancelled_with_nothing_charged(self):
        made = self.reserve()
        rid = made["reservation_id"]
        self.local.expire(rid)
        gone = self.read(rid)
        self.assertEqual((gone["status"], gone["paid_at"], gone["checkout_url"]), ("cancelled", None, None))
        self.assertEqual(gone["deposit"]["paid_cents"], 0)
        r = self.run_cli("reservations", "cancel", rid, "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("was already cancelled. Nothing to refund.", r.stdout)
        self.assertEqual(self.local.refunds(rid), [])

    def test_no_sign_in_and_a_key_the_api_refuses(self):
        for args in (["reserve", "--type", "iphone", "--plan", "monthly", "--use-case", USE_CASE],
                     ["reservations"]):
            missing = self.run_cli(*args, key="")
            self.assertEqual(missing.returncode, 1, args)
            self.assertIn("Not signed in. Run: phone-harness cloud login", missing.stderr)
            refused = self.run_cli(*args, key="pck_not_a_real_key")
            self.assertEqual(refused.returncode, 1, args)
            self.assertIn("signed out. Run: phone-harness cloud login", refused.stderr)


if __name__ == "__main__":
    unittest.main()
