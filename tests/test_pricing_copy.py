"""The prices these docs quote are the ones Phone Harness Cloud charges.

phone-cloud owns the numbers: production's settings, and tests/test_pricing_drift.py
there, which also checks this repository when PHONE_HARNESS_DIR names a checkout.
This holds the docs still between those runs. Change a price there first.

    python -m unittest discover tests -p test_pricing_copy.py
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from phone_harness.cloud import IPHONES_ONLY  # noqa: E402

DOCS = ("README.md", "SKILL.md", "install.md", "onboarding.md", "setup/cloud.md",
        "integrations/hermes/README.md")

RATE, GRANT, TRIAL_MINUTES = "5¢", "$5", "100"
IPHONE = {"$80", "$100", "$768", "$960"}
# What reserving a phone costs up front. The service reads the amount from
# Stripe and sends it in every quote, so the docs state it once each and say
# the quote is what counts.
DEPOSIT = "$20"
# Which phones are reserved: IPHONES_ONLY, the sentence `cloud reserve` answers
# a request for an Android with. Every doc that explains reserving says it in
# those words. It names a price of its own, the Android plan's.
ANDROID_PLAN = "$20"
RESERVING = ("README.md", "SKILL.md", "install.md", "onboarding.md", "setup/cloud.md", "setup/api.md")
FIGURES = {RATE, GRANT, *IPHONE, DEPOSIT, ANDROID_PLAN}
FIGURE = re.compile(r"(?<![\w$])\$\d+(?:,\d{3})*(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?¢")


def read(name):
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


class PricingCopy(unittest.TestCase):
    def test_every_figure_is_a_published_price(self):
        for name in DOCS:
            with self.subTest(doc=name):
                self.assertEqual(set(FIGURE.findall(read(name))) - FIGURES, set())

    def test_the_starting_credit_and_the_minutes_it_buys(self):
        for name in ("install.md", "onboarding.md", "setup/cloud.md"):
            text = read(name)
            self.assertIn(f"{GRANT} of credit", text, name)
            self.assertRegex(text, rf"\b{TRIAL_MINUTES} (?:Android )?minutes", name)

    def test_a_cloud_iphone_is_a_subscription_and_not_billed_by_the_minute(self):
        for name in ("SKILL.md", "install.md", "onboarding.md", "setup/cloud.md",
                     "integrations/hermes/README.md"):
            text = read(name)
            self.assertIn("flat subscription", text, name)
            self.assertNotRegex(text, r"(?i)iphone[^.|]*\b(not charged|is free)\b", name)
        self.assertLessEqual(IPHONE, set(FIGURE.findall(read("setup/cloud.md"))))

    def test_the_deposit_is_per_phone_and_refundable_and_the_quote_is_what_counts(self):
        for name in RESERVING:
            text = read(name).replace(IPHONES_ONLY, "")                    # that figure is the Android plan's
            self.assertEqual(text.count(DEPOSIT), 1, name)                 # said once, so it changes in one place
            self.assertIn(f"{DEPOSIT} per phone refundable deposit", text, name)
            self.assertRegex(text, r"quote\b[^.]*\bauthoritative", name)
        for name in set(DOCS) - set(RESERVING):
            self.assertNotIn(DEPOSIT, read(name), name)

    def test_only_iphones_are_reserved_and_every_doc_says_so_in_the_same_words(self):
        self.assertIn(f"on the {ANDROID_PLAN} plan", IPHONES_ONLY)
        for name in RESERVING:
            text = read(name)
            self.assertEqual(text.count(IPHONES_ONLY), 1, name)
            # and none lists an Android beside the iPhones as a phone to reserve
            self.assertNotRegex(text, r"(?i)iphone[-_]esim`?(?: \([^)]*\))?,? (?:or|\|) `?android|--type android",
                                name)
        for name in set(DOCS) - set(RESERVING):
            self.assertNotIn("Reservations are for", read(name), name)

    def test_the_limit_on_unpaid_reservations_is_stated_where_reserving_is_explained(self):
        for name in ("README.md", "SKILL.md", "setup/cloud.md", "setup/api.md"):
            text = read(name).lower()
            self.assertRegex(text, r"five unpaid reservations an hour", name)
            self.assertRegex(text, r"cancelling one does\s+not free a place", name)


if __name__ == "__main__":
    unittest.main()
