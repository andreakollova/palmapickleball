from flask import Flask, render_template, request, jsonify
from collections import defaultdict
from datetime import datetime
import os
import re
import stripe

app = Flask(__name__)

# =========================
# Stripe configuration
# =========================
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")

if not STRIPE_PUBLISHABLE_KEY:
    STRIPE_PUBLISHABLE_KEY = "pk__REDACTED"

if not STRIPE_SECRET_KEY:
    STRIPE_SECRET_KEY = "sk__REDACTED"

# Now set Stripe’s internal API key
stripe.api_key = STRIPE_SECRET_KEY

# =========================
# Booking logic (unchanged)
# =========================
def build_slots():
    out = []
    for h in range(8, 21):  # 08:00..20:30 (end bound 21:00)
        out.append(f"{h:02d}:00")
        out.append(f"{h:02d}:30")
    return out[:-1]


SLOTS_30 = build_slots()  # 26
COURTS = {"1": "Kurt 1", "2": "Kurt 2"}

# demo in-memory storage
bookings = defaultdict(lambda: {"1": set(), "2": set()})


def valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# =========================
# Helpers
# =========================
def parse_amount_to_cents(value) -> int:
    """
    Accepts:
      - int / float -> treated as EUR amount (e.g., 15 -> 1500)
      - '15' or '15.00' or '15,00 €' -> parses to cents
    Returns integer cents (e.g., 1500).
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))

    s = str(value).strip()
    # keep digits, dot, comma
    s = re.sub(r"[^\d.,-]", "", s)
    # if both , and . exist, assume comma is thousands sep and dot is decimal
    if "," in s and "." in s:
        s = s.replace(",", "")
    else:
        # if only comma, use it as decimal sep
        s = s.replace(",", ".")
    try:
        eur = float(s)
        return int(round(eur * 100))
    except Exception:
        return 0


# =========================
# Routes
# =========================
@app.route("/")
def index():
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template("index.html", today=today)


@app.get("/api/availability")
def api_availability():
    date = request.args.get("date")
    if not date or not valid_date(date):
        return jsonify({"ok": False, "error": "Neplatný dátum."}), 400
    return jsonify(
        {
            "ok": True,
            "date": date,
            "slots": SLOTS_30,
            "courts": {
                "1": sorted(list(bookings[date]["1"])),
                "2": sorted(list(bookings[date]["2"])),
            },
        }
    )


@app.post("/api/book")
def api_book():
    data = request.get_json(force=True)
    date = data.get("date")
    court = data.get("court")
    slots = data.get("slots", [])
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()

    if not date or not valid_date(date):
        return jsonify({"ok": False, "error": "Neplatný dátum."}), 400
    if court not in COURTS:
        return jsonify({"ok": False, "error": "Neznámy kurt."}), 400
    if not isinstance(slots, list) or not slots:
        return jsonify({"ok": False, "error": "Nevybrali ste čas."}), 400
    if any(s not in SLOTS_30 for s in slots):
        return jsonify({"ok": False, "error": "Neplatné časové sloty."}), 400

    idxs = sorted(SLOTS_30.index(s) for s in slots)
    if idxs != list(range(min(idxs), max(idxs) + 1)):
        return jsonify({"ok": False, "error": "Výber musí byť súvislý."}), 400

    already = bookings[date][court]
    conflicts = [s for s in slots if s in already]
    if conflicts:
        return jsonify(
            {"ok": False, "error": "Konflikt: obsadené.", "conflicts": conflicts}
        ), 409

    for s in slots:
        already.add(s)

    # IMPORTANT: return JSON (frontend redirects to /checkout)
    return jsonify({"ok": True, "reserved": slots, "court": court, "date": date})


# =========================
# Checkout page (GET)
# =========================
@app.route("/checkout")
def checkout():
    """
    Renders your checkout page (checkout.html).
    Expects query params: date, court, total (e.g., '15,00 €').
    """
    date = request.args.get("date")
    court = request.args.get("court")
    total = request.args.get("total")  # e.g., "15,00 €"
    now = datetime.now()

    return render_template(
        "checkout.html",
        date=date,
        court=court,
        total=total,
        now=now,
        stripe_key=STRIPE_PUBLISHABLE_KEY,  # shown on the frontend
    )


# =========================
# Create PaymentIntent (POST)
# =========================
@app.post("/create-payment-intent")
def create_payment_intent():
    """
    Frontend posts JSON like:
      {
        "amount": "15,00 €",        # or 15, or 15.00
        "currency": "eur",
        "date": "2025-10-13",
        "court": "2",
        "note": "Poznámka ..."
      }
    Returns clientSecret for Stripe.js.
    """
    try:
        data = request.get_json(force=True) or {}
        amount_cents = parse_amount_to_cents(data.get("amount")) or 0
        currency = (data.get("currency") or "eur").lower()

        if amount_cents <= 0:
            return jsonify({"error": "Neplatná suma."}), 400

        # Create the PaymentIntent
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=currency,
            automatic_payment_methods={"enabled": True},
            metadata={
                "date": str(data.get("date") or ""),
                "court": str(data.get("court") or ""),
                "note": str(data.get("note") or ""),
            },
        )
        return jsonify({"clientSecret": intent.client_secret})
    except stripe.error.StripeError as se:
        # Stripe-specific errors
        return jsonify({"error": se.user_message or str(se)}), 402
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================
# Simple success page
# =========================
@app.get("/payment-success")
def payment_success():
    return render_template("payment_success.html")


# =========================
# Run
# =========================
if __name__ == "__main__":
    # For production, run behind a real WSGI server (gunicorn/uvicorn) and disable debug.
    app.run(debug=True)