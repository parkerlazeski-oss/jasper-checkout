"""22 Day Reset checkout backend.

Two-step checkout for the22dayreset.com:
  POST /lead    -> capture lead, notify via SMS, return Stripe Checkout URL
  POST /webhook -> Stripe checkout.session.completed -> SALE notifications
  GET  /health  -> warmup ping (frontend calls this on page load)

All secrets come from environment variables — nothing sensitive lives in this repo.
"""
import json
import os
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests
import stripe
from flask import Flask, jsonify, redirect, request

app = Flask(__name__)

stripe.api_key = os.environ["STRIPE_KEY"]
PRICE_ID = os.environ["STRIPE_PRICE_ID"]
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
TWILIO_SID = os.environ["TWILIO_SID"]
TWILIO_TOKEN = os.environ["TWILIO_TOKEN"]
TWILIO_FROM = os.environ["TWILIO_FROM"]
# US numbers must go out through the A2P 10DLC messaging service (+1 661 number);
# Canadian numbers keep using TWILIO_FROM (the 431 number).
TWILIO_MG_US = os.environ.get("TWILIO_MG_US", "")
CA_AREA = {"204", "226", "236", "249", "250", "263", "289", "306", "343", "354", "365", "367", "368",
           "382", "403", "416", "418", "428", "431", "437", "438", "450", "468", "474", "506", "514",
           "519", "548", "579", "581", "584", "587", "600", "604", "613", "639", "647", "672", "683",
           "705", "709", "742", "753", "778", "780", "782", "807", "819", "825", "867", "873", "879",
           "902", "905"}
OPTIN_SITE = "https://sms.parkerjay.com"
OPTIN_NOTIFY_SMS = os.environ.get("OPTIN_NOTIFY_SMS", "+13068817888")
OPTIN_NOTIFY_EMAIL = os.environ.get("OPTIN_NOTIFY_EMAIL", "parkerlazeski@gmail.com")
OPTIN_CONFIRM = (
    "Parker Lazeski Alerts: You're subscribed to website alerts and reminders. "
    "Msg frequency varies. Msg & data rates may apply. Reply HELP for help, STOP to opt out."
)
SITE = os.environ.get("SITE", "https://the22dayreset.com")
ALLOWED_ORIGINS = {SITE, "https://www.the22dayreset.com"}

# comma-separated lists so recipients can change without a code push
HIT_SMS_TO = [n.strip() for n in os.environ.get("HIT_SMS_TO", "").split(",") if n.strip()]
SALE_SMS_TO = [n.strip() for n in os.environ.get("SALE_SMS_TO", "").split(",") if n.strip()]
HIT_EMAIL_TO = [e.strip() for e in os.environ.get("HIT_EMAIL_TO", "").split(",") if e.strip()]
SALE_EMAIL_TO = [e.strip() for e in os.environ.get("SALE_EMAIL_TO", "").split(",") if e.strip()]
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.mail.me.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)


def send_email(recipients, subject, body):
    if not (recipients and SMTP_USER and SMTP_PASS):
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"The 22 Day Reset <{EMAIL_FROM}>"
        msg["To"] = ", ".join(recipients)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(EMAIL_FROM, recipients, msg.as_string())
        return True
    except Exception as e:
        print(f"EMAIL FAIL: {type(e).__name__}: {e}", flush=True)
        return False


def send_sms(to, body):
    data = {"To": to, "Body": body}
    if TWILIO_MG_US and to.startswith("+1") and to[2:5] not in CA_AREA:
        data["MessagingServiceSid"] = TWILIO_MG_US
    else:
        data["From"] = TWILIO_FROM
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data=data,
            timeout=10,
        )
        return r.status_code < 300
    except Exception:
        return False


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/health")
def health():
    return "ok"


@app.route("/lead", methods=["POST", "OPTIONS"])
def lead():
    if request.method == "OPTIONS":
        return "", 204
    d = request.get_json(silent=True) or {}
    required = ["first", "last", "email", "phone", "city"]
    missing = [f for f in required if not str(d.get(f, "")).strip()]
    if missing:
        return jsonify(error=f"missing: {', '.join(missing)}"), 400

    first = str(d["first"]).strip()[:60]
    last = str(d["last"]).strip()[:60]
    email = str(d["email"]).strip()[:120]
    phone = str(d["phone"]).strip()[:30]
    city = str(d["city"]).strip()[:80]
    contact = str(d.get("contact", "")).strip()[:20]
    heard = str(d.get("heard", "")).strip()[:200]
    note = str(d.get("note", "")).strip()[:500]

    hit = (
        f"\U0001f525 Reset checkout hit\n{first} {last}\n{phone} · {email}\n"
        f"{city}" + (f"\nHeard via: {heard}" if heard else "")
    )
    def notify_hit():
        for n in HIT_SMS_TO:
            send_sms(n, hit)
        send_email(HIT_EMAIL_TO, f"🔥 Reset checkout hit — {first} {last}", hit)
    threading.Thread(target=notify_hit, daemon=True).start()

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            customer_email=email,
            success_url=f"{SITE}/welcome/",
            cancel_url=f"{SITE}/#reserve",
            metadata={
                "first": first, "last": last, "phone": phone, "city": city,
                "contact_pref": contact, "heard": heard, "note": note,
            },
        )
    except Exception:
        return jsonify(error="checkout unavailable, try again in a moment"), 500
    return jsonify(url=session.url)


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception:
        return "bad signature", 400

    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        m = s.get("metadata") or {}
        amount = (s.get("amount_total") or 0) / 100
        name = f"{m.get('first', '')} {m.get('last', '')}".strip() or "Someone"
        email = s.get("customer_email") or s.get("customer_details", {}).get("email", "")
        sale = (
            f"\U0001f4b0 SALE — 22 Day Reset\n{name} just paid ${amount:.0f}\n"
            f"{m.get('phone', '')} · {email}\n{m.get('city', '')}"
        )
        for n in SALE_SMS_TO:
            send_sms(n, sale)
        send_email(SALE_EMAIL_TO, f"💰 SALE — {name} joined the 22 Day Reset", sale)
    return "ok"


_optin_hits = {}


def normalize_phone(raw):
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) == 11 and digits[0] == "1":
        return "+" + digits
    return None


@app.route("/sms-optin", methods=["POST"])
def sms_optin():
    """Opt-in form for Parker Lazeski Alerts (A2P 10DLC consent record)."""
    f = request.form
    if f.get("website"):  # honeypot
        return redirect(f"{OPTIN_SITE}/thanks.html", 303)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    recent = [t for t in _optin_hits.get(ip, []) if now - t < 3600]
    if len(recent) >= 5:
        return redirect(f"{OPTIN_SITE}/?error=limit", 303)
    _optin_hits[ip] = recent + [now]

    name = str(f.get("name", "")).strip()[:80]
    email = str(f.get("email", "")).strip()[:120]
    if not name or "@" not in email:
        return redirect(f"{OPTIN_SITE}/?error=fields", 303)
    wants_sms = f.get("consent") == "yes"
    phone = normalize_phone(str(f.get("phone", ""))) if wants_sms else None
    if wants_sms and not phone:
        return redirect(f"{OPTIN_SITE}/?error=phone", 303)

    record = {
        "event": "alerts_signup",
        "program": "Parker Lazeski Alerts",
        "name": name,
        "email": email,
        "sms_opt_in": wants_sms,
        "phone": phone,
        "consent_text": str(f.get("consent_text", ""))[:600] if wants_sms else "",
        "form_version": str(f.get("form_version", ""))[:20],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ip": ip,
        "user_agent": request.headers.get("User-Agent", "")[:200],
    }
    print(json.dumps(record), flush=True)

    def after():
        kind = "SMS + email" if wants_sms else "email only"
        if wants_sms:
            send_sms(phone, OPTIN_CONFIRM)
        send_sms(OPTIN_NOTIFY_SMS, f"Alerts sign-up ({kind}): {name} {email} {phone or ''}".strip())
        send_email([OPTIN_NOTIFY_EMAIL], f"Alerts sign-up ({kind}): {name}",
                   json.dumps(record, indent=2))
    threading.Thread(target=after, daemon=True).start()
    return redirect(f"{OPTIN_SITE}/thanks.html?sms={1 if wants_sms else 0}", 303)


if __name__ == "__main__":
    app.run(port=5111)
