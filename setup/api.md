# Cloud API (no install)

For agents that can make HTTPS requests but cannot install the CLI.
Full reference: https://phone-harness.com/docs

All calls send `Authorization: Bearer $PHONE_HARNESS_API_KEY` (see
[cloud.md](cloud.md) for getting a key).

```bash
API=https://api.phone-harness.com
AUTH="Authorization: Bearer $PHONE_HARNESS_API_KEY"

curl -s -H "$AUTH" $API/me                  # account; phones under "available"
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" -d '{"platform":"ios","kind":"device","device_id":"ID","timeout_seconds":900}' \
  $API/sessions                             # start; returns the session (id, state)
curl -s -H "$AUTH" $API/sessions/SID        # poll until "state" is "ready"
curl -s -H "$AUTH" -X DELETE $API/sessions/SID   # stop
```

A ready cloud iPhone session has `control.url` and `control.token`. Drive it
with the control token (not the API key):

```bash
C="Authorization: Bearer $CONTROL_TOKEN"
curl -s -H "$C" $CONTROL_URL/ops                      # supported ops
curl -s -H "$C" -H 'Content-Type: application/json' \
  -d '{"op":"screen.text","kw":{}}' $CONTROL_URL/op   # visible text
curl -s -H "$C" -H 'Content-Type: application/json' \
  -d '{"op":"screen.capture","kw":{}}' $CONTROL_URL/op  # {"png_b64", "bounds"}
curl -s -H "$C" -H 'Content-Type: application/json' \
  -d '{"op":"input.tap","kw":{"x":375,"y":667}}' $CONTROL_URL/op  # tap a point
curl -s -H "$C" -H 'Content-Type: application/json' \
  -d '{"op":"nav.home","kw":{}}' $CONTROL_URL/op      # Home Screen
```

Reserve an iPhone the account does not have yet, with a $20 per phone refundable
deposit. Reservations are for iPhones only (with or without eSIM). Cloud
Android emulators are on the $20 plan and are not reserved. The answer
carries `quote`, which is authoritative, and
`checkout_url`, a Stripe Checkout page where a person pays that deposit.
Nothing is reserved until it is paid (`status` goes from `pending_payment` to
`queued`). Never pay it for the user. An account can have five unpaid
reservations an hour, and cancelling one does not free a place.
`first_name` and `last_name` may be added to put a name other than the
account's on it.

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uuidgen)" \
  -d '{"phone_type":"iphone","plan":"monthly","quantity":1,"use_case":"What it is for"}' \
  $API/reservations                               # phone_type: iphone | iphone_esim
curl -s -H "$AUTH" $API/reservations              # the account's reservations
curl -s -H "$AUTH" $API/reservations/ID           # one: status, position, deposit
curl -s -H "$AUTH" -X POST $API/reservations/ID/cancel   # leave the queue; refunds what is held
```

`GET /me` lists each phone's `id` under `available`. Each `/op` answers `{"result": ...}`. A cloud Android is reached over adb
instead (`POST /sessions/SID/adb` returns host, port and unlock code), so it
needs adb and the CLI is the easier path.
