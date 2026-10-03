# SIEM Delivery to Google SecOps: Operator curl Runbook

The whole SIEM delivery lifecycle from a shell: log in, elevate, configure,
run and confirm the canary, operate, recover and decommission. It is the
command-line companion of the [Google SecOps guide](siem-secops-guide.md)
(section 4 explains the arming steps; section 7 explains health and halts).
It was written from an operator walkthrough on a solo (SQLite) deployment
and a clustered (PostgreSQL) deployment.

Placeholders only: replace every `example` value with your own. Never paste
real hosts, keys or passwords into a shared document.

## 0. Variables and secret files

```bash
export CIDX_URL="https://cidx.example.com"     # front door, no trailing slash
export ADMIN_USER="admin"
export SA_KEY_FILE="$HOME/secops/sa-key.json"  # service-account JSON key (chmod 600)
export CA_PEM_FILE="$HOME/secops/extra-ca.pem" # ONLY for a TLS-inspecting proxy or a test emulator
export PROJECT="example-secops-project"
export LOCATION="us"
export INSTANCE="00000000-0000-0000-0000-000000000000"   # SecOps customer ID
export REGION="us"
export LABEL="cidx-example-1"                  # source_instance_label -> additional.cidx_instance
umask 077; W=$(mktemp -d)                      # scratch dir for cookies and tokens; deleted at the end
```

Keep secrets out of the process list: write the password to a file once.

```bash
read -rs -p "admin password: " P; printf '{"username":"%s","password":"%s"}' "$ADMIN_USER" "$P" > "$W/login.json"
printf '%s' "$P" > "$W/pass.txt"; unset P
```

## 1. REST login (Bearer token), with MFA if the account has it

```bash
curl -sS -X POST "$CIDX_URL/auth/login" -H 'Content-Type: application/json' \
  --data-binary @"$W/login.json" -o "$W/login.out" -w 'HTTP %{http_code}\n'
# If the body has "access_token": done. If it has "mfa_required": true, complete MFA:
MFA_TOKEN=$(python3 -c "import json;print(json.load(open('$W/login.out'))['mfa_token'])")
read -r -p "TOTP code: " CODE
printf '{"mfa_token":"%s","totp_code":"%s"}' "$MFA_TOKEN" "$CODE" > "$W/mfa.json"
curl -sS -X POST "$CIDX_URL/auth/mfa/verify" -H 'Content-Type: application/json' \
  --data-binary @"$W/mfa.json" -o "$W/login.out" -w 'HTTP %{http_code}\n'
python3 -c "import json;print('Authorization: Bearer '+json.load(open('$W/login.out'))['access_token'])" > "$W/auth.hdr"
rm -f "$W/login.out" "$W/mfa.json"
```

The token lives 10 minutes; repeat this step when calls start returning 401.
Never retry a rejected login in a loop: repeated failures lock the account.

## 2. Web session (the credential and CA uploads exist ONLY as Web UI forms)

The `session` cookie carries the `Secure` attribute whenever the server is
not bound to localhost. Browsers and curl's cookie jar send it back only over
`https`, so a plain-`http` deployment needs a TLS front door for browser and
curl web-form use (public issue #2004). Over plain `http` curl silently drops
the cookie and every form call answers 303 to `/login`; the hand-built
`Cookie` header below works over both.

```bash
curl -sS -c "$W/jar" -b "$W/jar" "$CIDX_URL/login" -o "$W/page.html"
CSRF=$(python3 -c "import re;print(re.search(r'name=\"csrf_token\"\s+value=\"([^\"]+)',open('$W/page.html').read()).group(1))")
curl -sS -c "$W/jar" -b "$W/jar" -D "$W/h.txt" -o "$W/page.html" -X POST "$CIDX_URL/login" \
  --data-urlencode "username=$ADMIN_USER" --data-urlencode "password@$W/pass.txt" \
  --data-urlencode "csrf_token=$CSRF" -w 'HTTP %{http_code}\n'
# MFA-enabled account: the 200 page carries a challenge_token; answer it now.
CH=$(python3 -c "import re;m=re.search(r'name=.challenge_token.[^>]*value=.([^\"\x27]+)',open('$W/page.html').read());print(m.group(1) if m else '')")
if [ -n "$CH" ]; then read -r -p "TOTP code: " CODE
  curl -sS -c "$W/jar" -b "$W/jar" -D "$W/h.txt" -o /dev/null -X POST "$CIDX_URL/admin/mfa/challenge/verify" \
    --data-urlencode "challenge_token=$CH" --data-urlencode "totp_code=$CODE" -w 'HTTP %{http_code}\n'; fi
# Build a Cookie header from the jar and Set-Cookie (http AND https; keeps a load-balancer cookie too)
python3 - "$W" <<'PY'
import re,sys
w=sys.argv[1]; c={}
for l in open(w+"/jar"):
    l=l.replace("#HttpOnly_","",1)
    f=l.rstrip("\n").split("\t")
    if len(f)>=7 and not l.startswith("#"): c[f[5]]=f[6]
for l in open(w+"/h.txt"):
    m=re.match(r"(?i)set-cookie:\s*([^=]+)=([^;]*)",l)
    if m: c[m.group(1)]=m.group(2)
open(w+"/cookie.hdr","w").write("Cookie: "+"; ".join(k+"="+v for k,v in c.items())+"\n")
PY
# A fresh form CSRF token comes from the Config page (repeat before EVERY form post):
cfg_csrf() { curl -sS -H @"$W/cookie.hdr" "$CIDX_URL/admin/config" -o "$W/page.html";
  CSRF=$(python3 -c "import re;print(re.search(r'name=\"csrf_token\"\s+value=\"([^\"]+)',open('$W/page.html').read()).group(1))"); }
# Print a form result (success or error banner):
msg() { python3 -c "import re,html;t=open('$W/page.html').read();print([' '.join(html.unescape(re.sub('<[^>]+>',' ',m)).split()) for m in re.findall(r'<article class=\"message-(?:success|error)\">(.*?)</article>',t,re.S)])"; }
```

## 3. TOTP elevation (only when `elevation_enforcement_enabled` is Yes)

With enforcement on, every write below needs an elevation window, or it
answers `403 {"detail":{"error":"elevation_required"}}`. The web session
(keyed by its cookie) and the Bearer token (keyed by its token id) are
elevated SEPARATELY.

A TOTP code is single-use per 30-second step for the account. Elevating the
web session and the Bearer token with codes from the SAME step makes the
second call fail with `401 elevation_failed "Invalid or expired code."`. Use
codes from two DIFFERENT 30-second steps: wait for the next code before the
second call.

```bash
read -r -p "TOTP code: " CODE; printf '{"totp_code":"%s"}' "$CODE" > "$W/elev.json"
curl -sS -X POST "$CIDX_URL/auth/elevate" -H @"$W/cookie.hdr" -H 'Content-Type: application/json' \
  --data-binary @"$W/elev.json" -w '\nHTTP %{http_code}\n'      # web session window
# ...wait for the NEXT code (a different 30 s step), then:
read -r -p "next TOTP code: " CODE; printf '{"totp_code":"%s"}' "$CODE" > "$W/elev.json"
curl -sS -X POST "$CIDX_URL/auth/elevate" -H @"$W/auth.hdr" -H 'Content-Type: application/json' \
  --data-binary @"$W/elev.json" -w '\nHTTP %{http_code}\n'      # Bearer token window
rm -f "$W/elev.json"
```

With enforcement off, `/auth/elevate` answers 503
`elevation_enforcement_disabled`; skip this step.

## 4. Configure (delivery stays disabled)

Every configuration save applies its change to the latest committed
configuration, whichever process or node serves it, so saves made one after
another never revert each other and need no waiting in between.

4.1 Service-account key (stored encrypted in the database; only its identity
is ever shown):

```bash
cfg_csrf
curl -sS -H @"$W/cookie.hdr" -o "$W/page.html" -w 'HTTP %{http_code}\n' \
  -X POST "$CIDX_URL/admin/config/siem_delivery/credential" \
  -F "csrf_token=$CSRF" -F "service_account_file=@$SA_KEY_FILE;type=application/json"; msg
# expect: "SIEM Delivery service-account credential set: <client_email> (key id <id>)"
```

4.2 Additional trusted CA (skip on a direct connection to Google):

```bash
cfg_csrf
curl -sS -H @"$W/cookie.hdr" -o "$W/page.html" -w 'HTTP %{http_code}\n' \
  -X POST "$CIDX_URL/admin/config/siem_delivery/trusted_ca" \
  -F "csrf_token=$CSRF" -F "trusted_ca_file=@$CA_PEM_FILE;type=application/x-pem-file"; msg
```

4.3 Destination fields, saved with `enabled=false`. Delivery is enabled only
after the canary is confirmed (step 6).

```bash
siem_save() {  # $1 = true|false
  cfg_csrf
  curl -sS -H @"$W/cookie.hdr" -o "$W/page.html" -w 'HTTP %{http_code}\n' \
    -X POST "$CIDX_URL/admin/config/siem_delivery" \
    --data-urlencode "csrf_token=$CSRF" --data-urlencode "enabled=$1" \
    --data-urlencode "region=$REGION" --data-urlencode api_version=v1 \
    --data-urlencode "project_id=$PROJECT" --data-urlencode "location=$LOCATION" \
    --data-urlencode "instance_id=$INSTANCE" --data-urlencode max_batch_events=1000 \
    --data-urlencode "source_instance_label=$LABEL" --data-urlencode harness_endpoint=; msg; }
siem_save false
```

4.4 Read back (identity only; the key is never returned):

```bash
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats" | python3 -c "
import sys,json;d=json.load(sys.stdin);c=d['capture']
print('credential',d['credential']);print('state',c['state'],'|',c['status'])
print('probes',[(p['node_id'],p['probe_result']) for p in c['processes']])"
cfg_csrf; python3 -c "import re,html;t=' '.join(html.unescape(re.sub('<[^>]+>',' ',open('$W/page.html').read())).split());i=t.find('Additional trusted CA');print(t[i:i+200])"
```

Every process must show `probe_result` `ok` before arming can happen. The
Config page shows the configuration as the process that served the page
last loaded it; on a cluster another node's page can lag a save by up to
about 30 seconds.

## 5. Canary (REST, Bearer)

The canary runs while delivery is still disabled, as long as a destination
is configured.

```bash
curl -sS -X POST "$CIDX_URL/api/admin/siem-delivery/canary" -H @"$W/auth.hdr" -o "$W/canary.json" -w 'HTTP %{http_code}\n'
python3 -c "import json;d=json.load(open('$W/canary.json'));print(d['canary_run_id'],d['result'],d['event_count'],len(d['expected_product_log_ids']))"
# expect: result "accepted", event_count 34 (mapping version 2)
```

`503 "cannot mint a SecOps token: <reason>"` means the token exchange failed
(section 9). `409 "the service-account credential changed during the
canary; run it again"` means the key was replaced while the canary was being
sent.

## 6. Verify in SecOps, confirm, then enable

In SecOps SIEM Search, for each expected id:
`metadata.vendor_name = "CIDX" AND metadata.product_log_id = "<id>"`, or the
whole run: `additional.fields["correlation_id"] = "canary-<canary_run_id>"`.
With a test emulator instead of a real tenant, use the emulator's own search
or received-events view for the same ids.

Confirm with ALL ids:

```bash
python3 -c "import json;d=json.load(open('$W/canary.json'));json.dump({'canary_run_id':d['canary_run_id'],'visible_product_log_ids':d['expected_product_log_ids']},open('$W/confirm.json','w'))"
curl -sS -X POST "$CIDX_URL/api/admin/siem-delivery/canary/confirm-visible" -H @"$W/auth.hdr" \
  -H 'Content-Type: application/json' --data-binary @"$W/confirm.json" -w '\nHTTP %{http_code}\n'
# expect {"confirmed":true,"expected_count":34,"confirmed_count":34,"missing_action_types":[]}
```

A partial list answers `"confirmed": false` with `missing_action_types` and
does not arm; send the full list afterwards. `409 "canary run is stale, ..."`
means the configuration lifetime changed after the canary (see below): run
the canary again.

Now enable delivery:

```bash
siem_save true
```

Capture state goes `awaiting process readiness`, then `armed`, within one
or two loop cycles. Enabling does not invalidate the confirmed canary.

A canary confirmation is valid only for the configuration lifetime that
produced it. Disabling or clearing the destination, removing or replacing
the service-account key, changing the trusted CA, or moving the destination
to other coordinates ends that lifetime: delivery disarms (or cannot arm) and
shows `awaiting canary` until steps 5 and 6 are repeated.

## 7. Operate

```bash
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats"            # fleet counts, capture, halt, probes
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/quarantine?limit=100"
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/system/health" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'],d.get('failure_reasons'))"
curl -sS "$CIDX_URL/healthz"                                                       # status only
```

SIEM reasons appear in the authenticated `GET /api/system/health` as
`failure_reasons` (they only ever make the node DEGRADED). The public
`/healthz` shows the resulting status only (DEGRADED still answers HTTP 200).
`GET /health` does not include them. The fleet snapshot can lag an admin
action by one refresh (about a minute).

## 8. Recovery actions (REST, Bearer, elevated when enforcement is on)

| Symptom (`stats.halt.class`) | Action |
|---|---|
| `duplicate_response` (SecOps answered 409) | Check SecOps for the batch's events. Present: acknowledge. Absent: rebatch. |
| `credential`, `request_rejection`, `unclassified` | Fix the cause, then `resume` (or wait for the 15-minute probe). |
| transient 5xx, 502 or timeout | No halt: events stay pending and drain automatically. |

```bash
BATCH=$(curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats" | python3 -c "import sys,json;print(json.load(sys.stdin)['halt']['batch_id'])")
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/batches/$BATCH/rebatch"      # send again
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/batches/$BATCH/acknowledge"  # mark delivered
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/resume"                     # clear any halt now
curl -sS -X POST -H @"$W/auth.hdr" -H 'Content-Type: application/json' \
  "$CIDX_URL/api/admin/siem-delivery/quarantine/requeue" --data '{"event_uuids":["<uuid>"]}'
```

## 9. Troubleshooting the token exchange

| `probe_result` / canary 503 reason | Check |
|---|---|
| `credential_missing` | Step 4.1 not done, or the key was removed. |
| `token_uri_not_allowed` | The key's `token_uri` must be `https://oauth2.googleapis.com/token`. |
| `token_rejected` | The key is disabled or deleted, or it belongs to the wrong service account. |
| `token_endpoint_unreachable` | DNS, egress or proxy, or TLS trust: on a proxy or emulator setup, confirm the CA is still listed (step 4.4). |

## 10. Decommission

```bash
# 1. disable and clear the destination
cfg_csrf
curl -sS -H @"$W/cookie.hdr" -o "$W/page.html" -X POST "$CIDX_URL/admin/config/siem_delivery" \
  --data-urlencode "csrf_token=$CSRF" --data-urlencode enabled=false --data-urlencode region= \
  --data-urlencode api_version=v1 --data-urlencode project_id= --data-urlencode location= \
  --data-urlencode instance_id= --data-urlencode max_batch_events=1000 \
  --data-urlencode source_instance_label= --data-urlencode harness_endpoint=; msg
# 2. remove the CA and the key
cfg_csrf; curl -sS -H @"$W/cookie.hdr" -o "$W/page.html" -X POST "$CIDX_URL/admin/config/siem_delivery/trusted_ca/remove" --data-urlencode "csrf_token=$CSRF"; msg
cfg_csrf; curl -sS -H @"$W/cookie.hdr" -o "$W/page.html" -X POST "$CIDX_URL/admin/config/siem_delivery/credential/remove" --data-urlencode "csrf_token=$CSRF"; msg
# 3. abandon the events left for the removed destination (the clearing save itself leaves one)
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats" | python3 -c "import sys,json;print(json.load(sys.stdin)['fleet']['unconfigured_destinations'])"
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/destinations/<destination_key>/abandon"
# 4. after about a minute: stats pending 0, unconfigured [], /api/system/health without SIEM reasons
rm -rf "$W"
```

Re-enabling the same destination later starts a new configuration lifetime:
repeat steps 4 to 6, including a fresh canary and confirmation.

Test-emulator setups only: also remove any hosts-file override for
`chronicle.<region>.rep.googleapis.com` and `oauth2.googleapis.com` on every
node, and confirm with `getent ahosts oauth2.googleapis.com`.
