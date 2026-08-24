# jasper-checkout

Backend for the22dayreset.com — 2-step checkout (lead capture -> Stripe) + SMS notifications.
No secrets in this repo; all config via environment variables:
STRIPE_KEY, STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET, TWILIO_SID, TWILIO_TOKEN,
TWILIO_FROM, HIT_SMS_TO, SALE_SMS_TO, SITE

Run: gunicorn app:app
