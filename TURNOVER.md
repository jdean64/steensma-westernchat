# WestChat Turnover — May 29, 2026

## What This App Does
Flask-based AI parts ordering assistant for Steensma Lawn & Power. Customers chat to identify Western snowplow parts, the AI collects name/email/phone, and at EXECUTE phase fires two SES emails: one to the shop (fulfillment copy) and one to the customer (order confirmation + payment link).

Live at: `https://coresteensma.com` (nginx reverse proxy → gunicorn on 127.0.0.1:8087)

---

## Session Summary — What Was Fixed

### 1. Order Summary Email Was Never Firing
**Root cause:** AI was not reliably setting `send_order_summary: true` in its JSON response.  
**Fix (commit c9639d1):** Added server-side trigger — if `phase == EXECUTE` AND name + email (with `@`) are present, the email fires regardless of the AI flag:
```python
should_send = ai.get("send_order_summary") or (phase_execute and contact_complete)
```

### 2. Gunicorn Not Capturing Python Logs
**Root cause:** Gunicorn workers were not forwarding Python `logging` output.  
**Fix:** Added `--capture-output` to the gunicorn command in `/etc/systemd/system/westchat.service`.  
Logs now visible at `/var/log/westchat/error.log`.  
Look for: `Triggering order summary`, `Order summary sent to Jeff`, `Order summary sent to customer`.

### 3. Session Dedup Blocking Re-Tests
**Root cause:** Dedup key `order_sent_{session_key}` was set on the first email attempt. If user gave a different email in the same session, the dedup blocked the second send.  
**Fix (this session, committed):** Key is now email-scoped:
```python
safe_email = contact.get("email", "").lower().strip()
sent_key = f"order_sent_{key}_{safe_email}"
```

### 4. DKIM Not Configured — Emails Going to Spam
**Root cause:** `steensmalawn.com` DNS is controlled by an unresponsive external IT/MSP. Could not add DKIM CNAME records.  
**Fix:** Switched sending domain to `coresteensma.com` (user controls GoDaddy DNS directly).

### 5. Email From Address Changed
**Old:** `jeffd@steensmalawn.com`  
**New:** `westchat@coresteensma.com` (send-only, no inbox needed — domain identity is what SES uses)  
**Reply-To:** `jeffd@steensmalawn.com` on both emails so customer/staff replies route correctly.  
Config in `/var/www/westchat/.env` — `SES_FROM=westchat@coresteensma.com`.

---

## Current Infrastructure State

### AWS SES (us-east-1)
| Identity | Type | Status |
|---|---|---|
| `jdean64@gmail.com` | Email | Verified |
| `jeffd@steensmalawn.com` | Email | Verified |
| `coresteensma.com` | Domain | **DKIM SUCCESS** (RSA-2048) |
| `steensmalawn.com` | Domain | DKIM Pending (blocked — external IT) |

**Sandbox mode:** Still active. Can only send to verified addresses until production access approved.  
**Production access request:** Submitted May 29, 2026. AWS case open. Expect approval within 24h.  
Once approved: any customer email address will work with no changes to the code.

### DNS — coresteensma.com (GoDaddy: ns45/ns46.domaincontrol.com)
Records added this session:
```
TXT  @                                           v=spf1 include:amazonses.com ~all
CNAME gnupvcswm7nf44k2cmeiaia3nl4oqmmx._domainkey  → gnupvcswm7nf44k2cmeiaia3nl4oqmmx.dkim.amazonses.com
CNAME vacc2m4yofgori72vgmbb67hzl7vjzw3._domainkey  → vacc2m4yofgori72vgmbb67hzl7vjzw3.dkim.amazonses.com
CNAME e7ci7myo3uj5ffxftvyoo43ltb5q2exk._domainkey  → e7ci7myo3uj5ffxftvyoo43ltb5q2exk.dkim.amazonses.com
```

### Server
- **Service:** `westchat.service` (systemd), WorkingDirectory `/var/www/westchat`
- **Gunicorn:** 2 workers, 120s timeout, `--capture-output`, error log `/var/log/westchat/error.log`
- **Logs:** `sudo tail -f /var/log/westchat/error.log`
- **Restart:** `sudo systemctl restart westchat`
- **Env file:** `/var/www/westchat/.env` (not in git — contains secrets)

### Transaction Log
`/var/log/westchat/interactions.jsonl` — append-only JSONL, one record per chat turn.  
Fields include `send_order_summary: bool` so you can audit which turns triggered an email.

---

## .env Reference (server only — never commit)
```
FLASK_SECRET_KEY=...
OPENAI_API_KEY=...
WESTCHAT_MODEL=gpt-4o
SES_FROM=westchat@coresteensma.com
ESCALATION_EMAIL=jdean64@gmail.com
PAYMENT_PORTAL_URL=https://steensmalawn.com/checkout
```

---

## Pending Items

### Immediate (waiting on AWS)
- [ ] **SES production access approval** — AWS case open, ~24h. No code changes needed when approved.
- [ ] After approval: run full end-to-end test with a non-verified customer email.

### Security (decided: come back to)
- [ ] **SSH port 2222** open to internet via UFW — needs decision: restrict to known IP range or close entirely.

### Next Development Priorities (in order)
1. **Auditable order backend** — SQLite/Postgres `orders` table + `/admin/orders` staff view. Replace reliance on JSONL + email as sole record of orders.
2. **Multi-item cart** — Session `cart[]` array instead of single `part_identified`. Customer adds parts, AI accumulates, order summary fires on explicit checkout or "that's everything." High impact for current selling season.
3. **PDF diagram number ↔ part number mapping** — Western exploded-view PDFs have position numbers (①②③) that are NOT part numbers. No lookup table currently exists. Solution: extract item-number→part-number tables from PDFs at index time. Also: AI cannot see what the browser user is viewing on the PDF — this gap affects part identification accuracy.
4. **Billing component** — Payment processing beyond the current static payment link. Future phase, after order backend is stable.

---

## How to Verify Email Is Working
```bash
# Check SES identity status
python3 -c "
import boto3
ses = boto3.client('sesv2', region_name='us-east-1')
r = ses.get_email_identity(EmailIdentity='coresteensma.com')
print('DKIM:', r['DkimAttributes']['Status'])
"

# Check production access status
python3 -c "
import boto3
ses = boto3.client('sesv2', region_name='us-east-1')
r = ses.get_account()
print('Production:', r.get('ProductionAccessEnabled'))
"

# Watch live logs during a test
sudo tail -f /var/log/westchat/error.log
# Look for: "Triggering order summary", "Order summary sent to Jeff", "Order summary sent to customer"
```

---

## Repository
GitHub: `jdean64/steensma-westchat` (branch: main)  
Deployed from: `/var/www/westchat/` on EC2 instance (us-east-1)  
IAM role `EC2-SSM-Role` grants SES send permission — no credentials stored in code or .env for AWS.
