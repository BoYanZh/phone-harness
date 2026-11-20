# Cloud phones

Phone Harness Cloud hosts an Android or an iPhone. No hardware, any OS.

## Needs

| | Cloud iPhone | Cloud Android |
| --- | --- | --- |
| Python 3.10+ and phone-harness | yes | yes |
| HTTPS to `api.phone-harness.com` and `clerk.phone-harness.com` | yes | yes |
| HTTPS to the phone's control URL | yes | no |
| adb installed | no | yes |
| Outbound TCP to the phone's adb host and port (not HTTPS) | no | yes |
| Billing | flat subscription, not per minute | 5¢ per minute, from account credit |

A sandbox that only allows HTTPS can drive a cloud iPhone but not a cloud
Android. If installing is not possible at all, use the [API](api.md).

## Sign in

- **Someone can open a browser (any device):** `phone-harness cloud login`
  prints a URL and a code. The user approves in any browser; the command
  waits up to about 10 minutes, so run it in the background if your commands
  time out sooner. `--no-browser` skips opening a browser here.
- **No one to approve (CI, unattended agents):** set `PHONE_HARNESS_API_KEY`.
  Mint one with `phone-harness cloud keys create LABEL` from a signed-in
  machine; it is shown once.

Never sign up, join or approve for the user.

## Use

```bash
phone-harness cloud ls             # phones on the account
phone-harness cloud start PHONE    # an id or label from `cloud ls`; `ios`/`android` when only one matches
phone-harness <<'PY'               # checks the attached phone
print(ensure_device())
PY
phone-harness cloud stop           # ends the session
```

New accounts get $5 of credit (100 Android minutes). A cloud Android emulator
bills 5¢ a minute from that credit. A cloud iPhone is a real phone on a flat
subscription: from $80 a month, $100 with an eSIM (a real number), or 20% off
billed annually ($768 or $960 a year). It does not bill by the minute, so it
can stay running.
A cloud iPhone is a separate phone from the user's own, with its own apps
and logins; if an app asks for an Apple ID or any sign-in, ask the user.

## Reserve a phone

An account with no cloud iPhone yet, or one that needs more of them, reserves
them:

```bash
phone-harness cloud reserve --type iphone --plan monthly --qty 2 \
  --use-case "What the phones are for"
phone-harness cloud reservations              # status and place in the queue
phone-harness cloud reservations cancel ID    # leave the queue; the deposit is refunded
```

`--type` is `iphone` or `iphone-esim`; `--plan` is `monthly` or
`annual`; `--qty` is 1 to 50. Reserving takes a $20 per phone refundable
deposit. The command prints a quote, which is authoritative, and a Stripe
Checkout link. The user opens the link and pays (`--open` opens it for them).
That payment is what holds a place in the queue: until it is made, nothing is
reserved. The plan starts when a phone is handed over, and the deposit comes
off its first bill. The reservation carries the account's name;
`--first-name` and `--last-name` put another one on it.

- Reservations are for iPhones only (with or without eSIM). Cloud Android
  emulators are on the $20 plan and are not reserved.
- These commands need a signed-in account (`cloud login`, or
  `PHONE_HARNESS_API_KEY`). Without one, reserve at phone-harness.com/get-a-phone.
- The queue is in the order deposits were paid. Nothing moves a place up.
- Cancelling refunds the deposit still held automatically, to the card that
  paid; a reservation that is part handed over refunds only the phones still
  waiting, and a phone that was handed over has had its deposit credited to
  its first bill.
- The website has no list of reservations: `cloud reservations` is the list,
  and the website shows one reservation's status page from its link.
- An account can have five unpaid reservations an hour. Cancelling one does not
  free a place; paying for one does.

Never pay, or fill in a payment page, for the user. Cancel a reservation only
when they ask.
