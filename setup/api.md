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
  -d '{"platform":"ios","kind":"device","device_id":"ID","timeout_seconds":900}' \
  $API/sessions                             # start; returns the session
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
  -d '{"op":"screen.capture","kw":{}}' $CONTROL_URL/op  # {"png_b64": ...}
```

Each `/op` answers `{"result": ...}`. A cloud Android is reached over adb
instead (`POST /sessions/SID/adb` returns host, port and unlock code), so it
needs adb and the CLI is the easier path.
