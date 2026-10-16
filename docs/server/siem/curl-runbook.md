# SIEM Delivery: Operator curl Runbook

The SIEM delivery lifecycle as shell commands: log in, elevate, configure,
run and confirm the canary, operate, recover and decommission. What each step
means, and when to use it, is in
[SIEM Delivery: CIDX Operations](operations.md); the Google side is in the
[Google SecOps guide](secops-guide.md).

Requirements: `bash`, `curl` and `python3` on the operator workstation, an
admin account on the CIDX server, and an `https` front door (the Web UI session
cookie is `Secure`, see [CIDX Operations](operations.md#real-tenant-enablement-checklist)).
Replace every `example` value with your own; never paste real hosts, keys or
passwords into a shared document.

Steps 1, 2 and 3 each consume TOTP codes when the account has MFA. A code is
single-use per 30-second step for the account, so every code you type must
come from a different 30-second step: wait for the next code between them.

Two front doors are used:

- the REST API with a Bearer token (canary, statistics, recovery);
- the Web UI forms with a session cookie (settings, key and CA uploads, which
  exist only as Web UI forms).

## 0. Variables and scratch directory

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
case "$CIDX_URL" in https://*) ;; *) echo "CIDX_URL must use https" >&2;; esac
```

Keep the password out of the process list and the shell history: read it
once and write it to files (JSON-encoded, so any character is safe).

```bash
python3 -c "import getpass,json,os,sys;w=sys.argv[1];p=getpass.getpass('admin password: ');open(w+'/pass.txt','w').write(p);json.dump({'username':os.environ['ADMIN_USER'],'password':p},open(w+'/login.json','w'))" "$W"
```

## 1. REST login (Bearer token), with MFA if the account has it

```bash
curl -sS -X POST "$CIDX_URL/auth/login" -H 'Content-Type: application/json' \
  --data-binary @"$W/login.json" -o "$W/login.out" -w 'HTTP %{http_code}\n'
# MFA-enabled account: the body has "mfa_required": true and an mfa_token; answer it.
if python3 -c "import json,sys;sys.exit(0 if json.load(open('$W/login.out')).get('mfa_required') else 1)"; then
  MFA_TOKEN=$(python3 -c "import json;print(json.load(open('$W/login.out'))['mfa_token'])")
  read -r -p "TOTP code: " CODE
  printf '{"mfa_token":"%s","totp_code":"%s"}' "$MFA_TOKEN" "$CODE" > "$W/mfa.json"
  curl -sS -X POST "$CIDX_URL/auth/mfa/verify" -H 'Content-Type: application/json' \
    --data-binary @"$W/mfa.json" -o "$W/login.out" -w 'HTTP %{http_code}\n'
fi
python3 -c "import json;print('Authorization: Bearer '+json.load(open('$W/login.out'))['access_token'])" > "$W/auth.hdr"
rm -f "$W/login.out" "$W/mfa.json"
```

The token lives for the `jwt_expiration_minutes` server setting (default 10
minutes). The login response does not state it; the token's own `exp` claim
does:

```bash
python3 -c "import base64,json,time;p=open('$W/auth.hdr').read().split()[-1].split('.')[1];d=json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4)));print('expires in',int(d['exp']-time.time()),'s')"
```

Repeat this step when calls start returning 401. Do not retry a rejected
login in a loop: repeated failures for one username are throttled with
growing waits (HTTP 429 with `Retry-After`). See
[Login and elevation](../auth/login-and-elevation.md).

## 2. Web session (for the settings forms and the key and CA uploads)

The `session` cookie carries the `Secure` attribute whenever the server's
`host` setting is not a loopback address, so curl's cookie jar sends it back
only over `https`. Over plain `http` every form call answers 303 to `/login`.

```bash
curl -sS -c "$W/jar" -b "$W/jar" "$CIDX_URL/login" -o "$W/page.html"
CSRF=$(python3 -c "import re;print(re.search(r'name=\"csrf_token\"\s+value=\"([^\"]+)',open('$W/page.html').read()).group(1))")
curl -sS -c "$W/jar" -b "$W/jar" -o "$W/page.html" -X POST "$CIDX_URL/login" \
  --data-urlencode "username=$ADMIN_USER" --data-urlencode "password@$W/pass.txt" \
  --data-urlencode "csrf_token=$CSRF" -w 'HTTP %{http_code}\n'
# MFA-enabled account: the 200 page carries a challenge_token; answer it with a
# code from a NEW 30-second step (not the one used in step 1).
CH=$(python3 -c "import re;m=re.search(r'name=.challenge_token.[^>]*value=.([^\"\x27]+)',open('$W/page.html').read());print(m.group(1) if m else '')")
if [ -n "$CH" ]; then read -r -p "TOTP code: " CODE
  curl -sS -c "$W/jar" -b "$W/jar" -o /dev/null -X POST "$CIDX_URL/admin/mfa/challenge/verify" \
    --data-urlencode "challenge_token=$CH" --data-urlencode "totp_code=$CODE" -w 'HTTP %{http_code}\n'; fi
# A fresh form CSRF token comes from the Config page (repeat before EVERY form post):
cfg_csrf() { curl -sS -c "$W/jar" -b "$W/jar" "$CIDX_URL/admin/config" -o "$W/page.html";
  CSRF=$(python3 -c "import re;print(re.search(r'name=\"csrf_token\"\s+value=\"([^\"]+)',open('$W/page.html').read()).group(1))"); }
# Print a form result (success or error banner):
msg() { python3 -c "import re,html;t=open('$W/page.html').read();print([' '.join(html.unescape(re.sub('<[^>]+>',' ',m)).split()) for m in re.findall(r'<article class=\"message-(?:success|error)\">(.*?)</article>',t,re.S)])"; }
```

## 3. TOTP elevation (only when `elevation_enforcement_enabled` is on)

With enforcement on, every write below needs an elevation window, or it
answers `403 {"detail":{"error":"elevation_required"}}`. The web session and
the Bearer token are elevated SEPARATELY.

A TOTP code is single-use per 30-second step for the account: elevate the two
with codes from two DIFFERENT 30-second steps, or the second call fails with
`401 elevation_failed`.

```bash
read -r -p "TOTP code: " CODE; printf '{"totp_code":"%s"}' "$CODE" > "$W/elev.json"
curl -sS -X POST "$CIDX_URL/auth/elevate" -c "$W/jar" -b "$W/jar" -H 'Content-Type: application/json' \
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

4.1 Service-account key:

```bash
cfg_csrf
curl -sS -c "$W/jar" -b "$W/jar" -o "$W/page.html" -w 'HTTP %{http_code}\n' \
  -X POST "$CIDX_URL/admin/config/siem_delivery/credential" \
  -F "csrf_token=$CSRF" -F "service_account_file=@$SA_KEY_FILE;type=application/json"; msg
# expect: "SIEM Delivery service-account credential set: <client_email> (key id <id>)"
```

4.2 Additional trusted CA (skip on a direct connection to Google):

```bash
cfg_csrf
curl -sS -c "$W/jar" -b "$W/jar" -o "$W/page.html" -w 'HTTP %{http_code}\n' \
  -X POST "$CIDX_URL/admin/config/siem_delivery/trusted_ca" \
  -F "csrf_token=$CSRF" -F "trusted_ca_file=@$CA_PEM_FILE;type=application/x-pem-file"; msg
# expect: "SIEM Delivery trusted CA set (SHA-256 <fingerprint>)"
```

4.3 Destination fields, saved with `enabled=false`. Delivery is enabled only
after the canary is confirmed (step 6).

```bash
siem_save() {  # $1 = true|false
  cfg_csrf
  curl -sS -c "$W/jar" -b "$W/jar" -o "$W/page.html" -w 'HTTP %{http_code}\n' \
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
cfg_csrf; python3 -c "import re,html;t=open('$W/page.html').read();m=re.search(r'id=\"siem-delivery-trusted-ca\">(.*?)</table>',t,re.S);print(' '.join(html.unescape(re.sub('<[^>]+>',' ',m.group(1))).split()) if m else 'trusted CA table not found')"
```

Every process must show `probe_result` `ok` before arming can happen.

## 5. Canary (REST, Bearer)

```bash
curl -sS -X POST "$CIDX_URL/api/admin/siem-delivery/canary" -H @"$W/auth.hdr" -o "$W/canary.json" -w 'HTTP %{http_code}\n'
python3 -c "import json;d=json.load(open('$W/canary.json'));print(d['canary_run_id'],d['result'],d['event_count'],d['mapping_version'],len(d['expected_product_log_ids']))"
# expect: result "accepted", event_count 34, mapping_version 3
```

| Answer | Meaning |
|--------|---------|
| `"result": "rejected"` | SecOps refused the canary; `result_signature` is `status\|google status\|class\|field path`. |
| `409 "no SIEM destination is configured"` | Step 4.3 not done. |
| `409 "the service-account credential changed during the canary; run it again"` | The key was replaced while the canary was being sent. |
| `409 "canary run is stale: ..."` | The configuration lifetime changed, or a newer canary was recorded; run it again. |
| `503 "cannot mint a SecOps token: <reason>"` | The token exchange failed (section 9). |

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
does not arm; send the full list afterwards. `409 "canary run is stale, for
another destination, or not accepted"` means the canary no longer matches the
configuration: run it again.

Now enable delivery:

```bash
siem_save true
```

The capture state goes to `awaiting process readiness`, then `armed`, within
one or two loop cycles. Re-run step 4.4 to watch it.

## 7. Operate

```bash
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats"              # fleet counts, capture, halt, probes
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/quarantine?limit=100"
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/system/health" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'],d.get('failure_reasons'))"
curl -sS "$CIDX_URL/healthz"                                                         # status only
```

The health reasons are explained in
[CIDX Operations](operations.md#health).

## 8. Recovery actions (REST, Bearer, elevated when enforcement is on)

| `stats.halt.class` | Action |
|---|---|
| `duplicate_response` (SecOps answered 409) | Check SecOps for the batch's events. Present: acknowledge. Absent: rebatch. |
| `credential`, `request_rejection`, `unclassified`, `row_rejection_burst`, `local_validation_burst` | Fix the cause, then `resume` (or wait for the 15-minute probe). |
| no halt, events pending | Transient failures (5xx, timeouts) and 429 need no action: events drain automatically. |

```bash
BATCH=$(curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats" | python3 -c "import sys,json;print(json.load(sys.stdin)['halt']['batch_id'])")
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/batches/$BATCH/rebatch"      # send again
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/batches/$BATCH/acknowledge"  # mark delivered
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/resume"                     # clear any halt now
curl -sS -X POST -H @"$W/auth.hdr" -H 'Content-Type: application/json' \
  "$CIDX_URL/api/admin/siem-delivery/quarantine/requeue" --data '{"event_uuids":["<event-uuid>"]}'  # at most 500 ids
```

Run either `rebatch` or `acknowledge` for a batch, not both.

## 9. Troubleshooting the token exchange

| `probe_result` / canary 503 reason | Check |
|---|---|
| `credential_missing` | Step 4.1 not done, or the key was removed. |
| `credential_invalid` | The stored key no longer loads; upload it again. |
| `credential_key_mismatch` | The server's encryption key changed since the key was stored; upload it again. |
| `token_uri_not_allowed` | The key's `token_uri` must be `https://oauth2.googleapis.com/token`. |
| `token_rejected` | Google refused the token request: the key is disabled or deleted, or belongs to another service account. |
| `token_endpoint_unreachable` | DNS, egress or proxy, or TLS trust: on a proxy or emulator setup, confirm the CA is still listed (step 4.4). |

## 10. Decommission

```bash
# 0. resolve any open halt while the destination is still configured (section 8)
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats" | python3 -c "import sys,json;print(json.load(sys.stdin)['halt']['class'])"
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/resume"   # when the class above is not None
# 1. disable and clear the destination
cfg_csrf
curl -sS -c "$W/jar" -b "$W/jar" -o "$W/page.html" -X POST "$CIDX_URL/admin/config/siem_delivery" \
  --data-urlencode "csrf_token=$CSRF" --data-urlencode enabled=false --data-urlencode region= \
  --data-urlencode api_version=v1 --data-urlencode project_id= --data-urlencode location= \
  --data-urlencode instance_id= --data-urlencode max_batch_events=1000 \
  --data-urlencode source_instance_label= --data-urlencode harness_endpoint=; msg
# 2. remove the CA and the key
cfg_csrf; curl -sS -c "$W/jar" -b "$W/jar" -o "$W/page.html" -X POST "$CIDX_URL/admin/config/siem_delivery/trusted_ca/remove" --data-urlencode "csrf_token=$CSRF"; msg
cfg_csrf; curl -sS -c "$W/jar" -b "$W/jar" -o "$W/page.html" -X POST "$CIDX_URL/admin/config/siem_delivery/credential/remove" --data-urlencode "csrf_token=$CSRF"; msg
# 3. abandon the events left for the removed destination (the clearing save itself leaves one)
curl -sS -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/stats" | python3 -c "import sys,json;print(json.load(sys.stdin)['fleet']['unconfigured_destinations'])"
curl -sS -X POST -H @"$W/auth.hdr" "$CIDX_URL/api/admin/siem-delivery/destinations/<destination_key>/abandon"
# 4. after about a minute: stats pending 0, unconfigured [], /api/system/health without SIEM reasons
rm -rf "$W"
```

Test-emulator setups only: also remove any test-emulator DNS redirection on
every node.
