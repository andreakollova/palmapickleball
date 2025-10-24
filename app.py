from flask import Flask, render_template, request, jsonify, redirect, url_for
from collections import defaultdict
from datetime import datetime, timedelta
import os
import re
import stripe
from dotenv import load_dotenv  # <-- add this at the top, with your other imports
from urllib.parse import urlencode

load_dotenv()  # loads variables from a .env file into os.environ


app = Flask(__name__)

# =========================
# Stripe configuration
# =========================
STRIPE_PUBLISHABLE_KEY = os.environ["STRIPE_PUBLISHABLE_KEY"]
STRIPE_SECRET_KEY     = os.environ["STRIPE_SECRET_KEY"]

if not STRIPE_PUBLISHABLE_KEY or not STRIPE_SECRET_KEY:
    raise RuntimeError(
        "Missing Stripe keys. Set STRIPE_PUBLISHABLE_KEY and STRIPE_SECRET_KEY in your environment (or .env)."
    )

stripe.api_key = STRIPE_SECRET_KEY

# =========================
# Booking logic (unchanged API, internals add expiry)
# =========================
def build_slots():
    out = []
    for h in range(8, 21):  # 08:00..20:30 (end bound 21:00)
        out.append(f"{h:02d}:00")
        out.append(f"{h:02d}:30")
    return out   # <-- bolo out[:-1], to odstráni 20:30 a spôsobí chybu


SLOTS_30 = build_slots()  # 26
COURTS = {"1": "Kurt 1", "2": "Kurt 2"}

# demo in-memory storage
# Predtým: {"1": set(), "2": set()}
# Teraz:   {"1": {slot: {"held_at": datetime}}, "2": {...}}
bookings = defaultdict(lambda: {"1": {}, "2": {}})

# Koľko minút držíme “hold” (automaticky uvoľníme po čase)
HOLD_MINUTES = 10
HOLD_DELTA = timedelta(minutes=HOLD_MINUTES)


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


def cleanup_expired(now=None):
    """
    Vymaže z 'bookings' všetky holdy staršie než HOLD_MINUTES.
    Volá sa pri každom /api/availability a /api/book.
    """
    if now is None:
        now = datetime.utcnow()

    cutoff = now - HOLD_DELTA
    # bookings: { date: { "1": {slot: {"held_at": dt}}, "2": {...} } }
    for date, courts in list(bookings.items()):
        for court_id, slot_map in courts.items():
            # zozbieraj sloty na vymazanie
            to_delete = [slot for slot, meta in slot_map.items()
                         if not meta or meta.get("held_at") is None or meta["held_at"] < cutoff]
            # vymaž expirované
            for slot in to_delete:
                del slot_map[slot]


def court_busy_slots_for_date(date: str, court_id: str):
    """
    Vracia set platných (neexpirovaných) slotov pre daný deň + kurt.
    """
    cleanup_expired()
    slot_map = bookings[date][court_id]  # dict slot -> {"held_at": dt}
    return set(slot_map.keys())


# ---------- NEW: helper to compute “blocked because past or within 30 min” (today only)
def slots_blocked_today(now_local=None, bumper_min=30):
    """
    Return list of slot strings (e.g. '16:30') that should NOT be bookable today
    because they start <= now + bumper (default 30 minutes).
    Uses local server time for the facility.
    """
    if now_local is None:
        now_local = datetime.now()
    cutoff = now_local + timedelta(minutes=bumper_min)
    blocked = []
    for s in SLOTS_30:
        hh, mm = map(int, s.split(":"))
        slot_dt = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot_dt <= cutoff:
            blocked.append(s)
    return blocked
# ---------- /NEW


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

    # vyčisti expirované a vráť iba aktívne holdy
    cleanup_expired()

    busy_1 = sorted(list(court_busy_slots_for_date(date, "1")))
    busy_2 = sorted(list(court_busy_slots_for_date(date, "2")))

    # ---------- NEW: add “blocked” field for today (past + 30 min bumper)
    today_str = datetime.now().strftime("%Y-%m-%d")
    blocked = slots_blocked_today() if date == today_str else []
    # ---------- /NEW

    return jsonify(
        {
            "ok": True,
            "date": date,
            "slots": SLOTS_30,
            "courts": {
                "1": busy_1,
                "2": busy_2,
            },
            "blocked": blocked,  # NEW: client can gray these out
        }
    )


@app.post("/api/book")
def api_book():
    """
    Verzia A: okamžite zablokuje (HOLD) vybrané sloty na 5 minút.
    Po uplynutí 5 minút sa automaticky uvoľnia (cleanup_expired()).
    """
    try:
        data = request.get_json(force=True) or {}
        date  = data.get("date")
        court = data.get("court")
        slots = data.get("slots", [])
        name  = (data.get("name")  or "").strip()
        email = (data.get("email") or "").strip()

        if not date or not valid_date(date):
            return jsonify({"ok": False, "error": "Neplatný dátum."}), 400
        if court not in COURTS:
            return jsonify({"ok": False, "error": "Neznámy kurt."}), 400
        if not isinstance(slots, list) or not slots:
            return jsonify({"ok": False, "error": "Nevybrali ste čas."}), 400
        if any(s not in SLOTS_30 for s in slots):
            return jsonify({"ok": False, "error": "Neplatné časové sloty."}), 400

        # ---------- NEW: hard validation to disallow past / too-soon slots for today
        today_str = datetime.now().strftime("%Y-%m-%d")
        if date == today_str:
            now_local = datetime.now()
            cutoff = now_local + timedelta(minutes=30)
            for s in slots:
                hh, mm = map(int, s.split(":"))
                slot_dt = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if slot_dt <= cutoff:
                    return jsonify(
                        {"ok": False, "error": "Nie je možné rezervovať minulé alebo príliš skoré sloty."}
                    ), 400
        # ---------- /NEW

        # vyčisti expirované pred kontrolou konfliktov
        cleanup_expired()

        # kontrola súvislého výberu
        idxs = sorted(SLOTS_30.index(s) for s in slots)
        if idxs != list(range(min(idxs), max(idxs) + 1)):
            return jsonify({"ok": False, "error": "Výber musí byť súvislý."}), 400

        # existujúce platné holdy
        slot_map = bookings[date][court]  # dict slot -> {"held_at": dt}

        # konflikty = slot je držaný a neexpiroval
        conflicts = [s for s in slots if s in slot_map]
        if conflicts:
            return jsonify({"ok": False, "error": "Konflikt: obsadené.", "conflicts": conflicts}), 409

        # nastav hold s aktuálnym časom
        now = datetime.utcnow()
        for s in slots:
            slot_map[s] = {"held_at": now}

        # vyrátaj expiráciu a pošli ju klientovi (ISO8601 UTC)
        expires_at = (now + HOLD_DELTA).isoformat(timespec="seconds") + "Z"

        return jsonify({
            "ok": True,
            "reserved": slots,
            "court": court,
            "date": date,
            "expires_at": expires_at,   # klient si uloží a zobrazí timer
            "hold_seconds": HOLD_DELTA.seconds
        }), 200

    except Exception as e:
        # nech to nikdy nevráti None – vždy JSON s chybou
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# Checkout page (GET)
# =========================
@app.route("/checkout")
def checkout():
    date = request.args.get("date")
    court = request.args.get("court")
    total = request.args.get("total")
    time_str = request.args.get("time", "")        # <— read
    now = datetime.now()

    return render_template(
        "checkout.html",
        date=date,
        court=court,
        total=total,
        time_str=time_str,                          # <— pass
        now=now,
        stripe_key=STRIPE_PUBLISHABLE_KEY,
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
                "time": str(data.get("time") or ""),  # <— add this
            },
        )
        return jsonify({"clientSecret": intent.client_secret})
    except stripe.error.StripeError as se:
        # Stripe-specific errors
        return jsonify({"error": se.user_message or str(se)}), 402
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/adminpanel", methods=["GET"])
def adminpanel():
    # frontend-only placeholder
    return render_template("adminpanel_login.html")

@app.route("/admin/dashboard")
def admin_dashboard():
    # odovzdá šablóne rovnaké dáta ako JSON feed nižšie
    return render_template("admin_dashboard.html", reservations=sample_reservations())

# >>> PRIDANÉ: customers stránka <<<
@app.route("/admin/customers")
def admin_customers():
    # zatiaľ len frontend demo: pošleme sample_reservations,
    # aby šablóna mala nejaké dáta
    return render_template(
        "admin_customers.html",
        reservations=sample_reservations()
    )
# <<< KONIEC PRIDANÉHO >>>

# === Registrácia po objednávke (POST) ===
@app.post("/registracia-po-objednavke")
def registracia_po_objednavke():
    """
    Vytvorí účet na základe e-mailu z objednávky a zvoleného hesla.
    Po úspechu presmeruje späť na /rezervacia-uspesna?registered=1
    a ponechá v URL aj info o rezervácii (date, time, court, total).
    """
    email      = (request.form.get("email") or "").strip().lower()
    password   = request.form.get("password") or ""
    next_path  = request.form.get("next_path") or url_for("rezervacia_uspesna")

    # doplnkové info kvôli spätnému zobrazeniu
    params = {
        "registered": "1",
        "date":  request.form.get("date")  or "",
        "time":  request.form.get("time")  or "",
        "court": request.form.get("court") or "",
        "total": request.form.get("total") or "",
        "email": email or "",
    }

    # TODO: tu si sprav reálnu registráciu používateľa v DB:
    # - validácia (už existuje? dĺžka hesla? atď.)
    # - hash hesla (napr. werkzeug.security.generate_password_hash)
    # - uloženie do DB
    # - (voliteľne) prihlásenie: login_user(user)
    #
    # Pre demo nič nerobíme a len redirectneme späť.

    return redirect(f"{next_path}?{urlencode(params)}")

@app.get("/payment-success")
def payment_success():
    return render_template("payment_success.html")

@app.route("/rezervacia-uspesna")
def rezervacia_uspesna():
    # optional query parameters passed from checkout
    date  = request.args.get("date")
    time  = request.args.get("time")
    court = request.args.get("court")
    total = request.args.get("total")
    email = request.args.get("email")  # <-- NEW

    return render_template(
        "rezervacia_uspesna.html",
        date=date,
        time=time,
        court=court,
        total=total,
        email=email   # <-- NEW
    )

@app.route("/registracia", methods=["GET","POST"])
def registracia():
    return render_template("registracia.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # for now, frontend only – no login logic yet
        email = request.form.get("email")
        password = request.form.get("password")
        # (optional) add a fake error if you want to test the message
        # return render_template("login.html", error_message="Nesprávny email alebo heslo.")

    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    sample_reservations = [
        {"date": "2025-10-16", "time": "17:00 – 18:00", "court": "Kurt 1", "total": "30,00 €", "video_url": ""},
        {"date": "2025-10-20", "time": "18:30 – 19:30", "court": "Kurt 2", "total": "30,00 €", "video_url": "https://example.com/video123.mp4"},
    ]
    return render_template(
        "dashboard.html",
        user_name="Andrea",
        user_photo=url_for("static", filename="images/profile1.png"),
        reservations=sample_reservations,
    )

@app.route("/profil")
def profil():
    return render_template(
        "profil.html",
        user_name="Andrea",
        user_email="andrea@palmapickleball.sk",
        user_photo=url_for("static", filename="images/profile1.png"),
        masked_password="••••••••"
    )

@app.post("/api/release")
def api_release():
    """
    Uvoľní (zruší HOLD) pre zadané sloty.
    Očakáva JSON: { "date": "YYYY-MM-DD", "court": "1", "slots": ["16:00","16:30"] }
    """
    data = request.get_json(force=True) or {}
    date = data.get("date")
    court = str(data.get("court") or "")
    slots = data.get("slots") or []

    if not date or not valid_date(date):
        return jsonify({"ok": False, "error": "Neplatný dátum."}), 400
    if court not in COURTS:
        return jsonify({"ok": False, "error": "Neznámy kurt."}), 400
    if not isinstance(slots, list) or not slots:
        return jsonify({"ok": False, "error": "Chýbajú sloty."}), 400
    if any(s not in SLOTS_30 for s in slots):
        return jsonify({"ok": False, "error": "Neplatné časové sloty."}), 400

    # odstráň expirované a uvoľni požadované sloty
    cleanup_expired()
    slot_map = bookings[date][court]  # dict slot -> {"held_at": dt}

    released = []
    for s in slots:
        if s in slot_map:
            del slot_map[s]
            released.append(s)

    return jsonify({"ok": True, "released": released})


# ===== ADDED: jednotný zdroj dát pre admin dashboard (rozšírené o level, registered_at) =====
def sample_reservations():
    return [
        {
            "created_at":    "2025-10-19 18:05",
            "registered_at": "2025-09-28 10:12",
            "date": "2025-10-25",
            "time": "18:00 – 19:00",
            "court": "Kurt 1",
            "name": "Lucia Hrivíková",
            "email": "lucia@example.com",
            "phone": "+421 903 444 555",
            "level": "Mierne pokročilý",
            "addons": {"rackets": 1, "camera": 0},
            "total": "23,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "clen",            # člen
        },
        {
            "created_at":    "2025-10-19 10:10",
            "registered_at": "2025-09-15 09:41",
            "date": "2025-10-19",
            "time": "15:00 – 16:00",
            "court": "Kurt 2",
            "name": "Juraj Kováč",
            "email": "juraj@example.com",
            "phone": "+421 905 123 321",
            "level": "Profesionál",
            "addons": {"rackets": 0, "camera": 1},
            "total": "28,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "staff",           # staff
        },
        {
            "created_at":    "2025-10-18 14:22",
            "registered_at": "2025-10-18 14:05",
            "date": "2025-10-24",
            "time": "17:00 – 18:00",
            "court": "Kurt 2",
            "name": "Peter Novák",
            "email": "peter@example.com",
            "phone": "+421 911 222 333",
            "level": "Začiatočník",
            "addons": {"rackets": 0, "camera": 1},
            "total": "28,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-18 09:55",
            "registered_at": "2025-08-30 18:22",
            "date": "2025-10-19",
            "time": "19:30 – 20:30",
            "court": "Kurt 1",
            "name": "Zuzana Mrázová",
            "email": "zuzka@example.com",
            "phone": "+421 904 888 123",
            "level": "Stredne pokročilý",
            "addons": {"rackets": 1, "camera": 0},
            "total": "23,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "clen",            # člen
        },
        {
            "created_at":    "2025-10-17 10:40",
            "registered_at": "2025-07-12 12:01",
            "date": "2025-10-23",
            "time": "19:30 – 20:30",
            "court": "Kurt 1",
            "name": "Andrea Kollová",
            "email": "andrea@palmapickleball.sk",
            "phone": "+421 900 000 000",
            "level": "Profesionál",
            "addons": {"rackets": 2, "camera": 1},
            "total": "30,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "staff",           # staff (trénerka / interný tím)
        },
        {
            "created_at":    "2025-10-17 08:10",
            "registered_at": "2025-10-17 08:01",
            "date": "2025-10-21",
            "time": "06:30 – 07:30",
            "court": "Kurt 2",
            "name": "Viktor Bača",
            "email": "viktor@example.com",
            "phone": "+421 903 111 555",
            "level": "Začiatočník",
            "addons": {"rackets": 0, "camera": 0},
            "total": "20,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-16 18:11",
            "registered_at": "2025-10-01 17:44",
            "date": "2025-10-28",
            "time": "20:00 – 21:00",
            "court": "Kurt 2",
            "name": "Michaela Hrúzová",
            "email": "miska@example.com",
            "phone": "+421 902 987 456",
            "level": "Mierne pokročilý",
            "addons": {"rackets": 1, "camera": 0},
            "total": "23,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-16 08:11",
            "registered_at": "2025-09-10 09:30",
            "date": "2025-10-22",
            "time": "07:30 – 08:30",
            "court": "Kurt 2",
            "name": "Marek Hruška",
            "email": "marek@example.com",
            "phone": "+421 902 123 456",
            "level": "Mierne pokročilý",
            "addons": {"rackets": 0, "camera": 0},
            "total": "20,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-15 12:31",
            "registered_at": "2025-06-02 14:22",
            "date": "2025-10-20",
            "time": "16:00 – 17:00",
            "court": "Kurt 1",
            "name": "Eva Kováčová",
            "email": "eva@example.com",
            "phone": "+421 944 777 222",
            "level": "Stredne pokročilý",
            "addons": {"rackets": 1, "camera": 0},
            "total": "23,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "clen",            # člen
        },
        {
            "created_at":    "2025-10-15 09:00",
            "registered_at": "2025-10-15 08:55",
            "date": "2025-10-26",
            "time": "09:00 – 10:00",
            "court": "Kurt 1",
            "name": "Filip Oravec",
            "email": "filip@example.com",
            "phone": "+421 948 000 123",
            "level": "Začiatočník",
            "addons": {"rackets": 0, "camera": 0},
            "total": "20,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-14 19:03",
            "registered_at": "2025-05-20 11:40",
            "date": "2025-10-27",
            "time": "18:00 – 19:00",
            "court": "Kurt 2",
            "name": "Karin Liptáková",
            "email": "karin@example.com",
            "phone": "+421 903 555 000",
            "level": "Profesionál",
            "addons": {"rackets": 2, "camera": 1},
            "total": "32,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "staff",           # staff
        },
        {
            "created_at":    "2025-10-14 09:03",
            "registered_at": "2025-10-14 08:50",
            "date": "2025-10-17",
            "time": "17:00 – 18:00",
            "court": "Kurt 2",
            "name": "Roman Bielik",
            "email": "roman@example.com",
            "phone": "+421 948 222 111",
            "level": "Mierne pokročilý",
            "addons": {"rackets": 0, "camera": 0},
            "total": "20,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-13 21:15",
            "registered_at": "2025-07-07 15:00",
            "date": "2025-10-30",
            "time": "21:00 – 22:00",
            "court": "Kurt 1",
            "name": "Samuel Duda",
            "email": "samuel@example.com",
            "phone": "+421 905 333 789",
            "level": "Začiatočník",
            "addons": {"rackets": 1, "camera": 0},
            "total": "23,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-13 10:31",
            "registered_at": "2025-04-01 09:10",
            "date": "2025-10-19",
            "time": "08:00 – 09:00",
            "court": "Kurt 1",
            "name": "Kristína Holá",
            "email": "kika@example.com",
            "phone": "+421 903 741 852",
            "level": "Stredne pokročilý",
            "addons": {"rackets": 0, "camera": 0},
            "total": "20,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "clen",            # člen
        },
        {
            "created_at":    "2025-10-13 08:40",
            "registered_at": "2025-09-02 12:45",
            "date": "2025-10-31",
            "time": "16:30 – 17:30",
            "court": "Kurt 2",
            "name": "Jana Kmeťová",
            "email": "jana@example.com",
            "phone": "+421 917 654 123",
            "level": "Mierne pokročilý",
            "addons": {"rackets": 1, "camera": 1},
            "total": "33,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-12 16:45",
            "registered_at": "2025-10-12 16:30",
            "date": "2025-10-25",
            "time": "06:00 – 07:00",
            "court": "Kurt 1",
            "name": "Tomáš Švec",
            "email": "tomas@example.com",
            "phone": "+421 905 456 654",
            "level": "Začiatočník",
            "addons": {"rackets": 0, "camera": 0},
            "total": "20,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-12 12:05",
            "registered_at": "2025-08-11 13:50",
            "date": "2025-10-29",
            "time": "12:00 – 13:00",
            "court": "Kurt 2",
            "name": "Adam Kubiš",
            "email": "adam@example.com",
            "phone": "+421 915 321 789",
            "level": "Začiatočník",
            "addons": {"rackets": 2, "camera": 0},
            "total": "26,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-12 08:22",
            "registered_at": "2025-05-29 18:20",
            "date": "2025-10-19",
            "time": "12:00 – 13:00",
            "court": "Kurt 1",
            "name": "Monika Tóthová",
            "email": "monika@example.com",
            "phone": "+421 907 111 222",
            "level": "Mierne pokročilý",
            "addons": {"rackets": 1, "camera": 0},
            "total": "23,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "clen",            # člen
        },
        {
            "created_at":    "2025-10-11 18:33",
            "registered_at": "2025-03-18 20:55",
            "date": "2025-11-01",
            "time": "18:00 – 19:00",
            "court": "Kurt 2",
            "name": "Patrik Maier",
            "email": "patrik@example.com",
            "phone": "+421 944 333 222",
            "level": "Stredne pokročilý",
            "addons": {"rackets": 0, "camera": 1},
            "total": "28,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "clen",            # člen
        },
        {
            "created_at":    "2025-10-11 09:02",
            "registered_at": "2025-10-11 08:40",
            "date": "2025-10-18",
            "time": "18:30 – 19:30",
            "court": "Kurt 2",
            "name": "Peter Novák st.",
            "email": "peter.st@example.com",
            "phone": "+421 911 000 333",
            "level": "Začiatočník",
            "addons": {"rackets": 0, "camera": 0},
            "total": "20,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
        {
            "created_at":    "2025-10-10 20:20",
            "registered_at": "2025-02-02 14:11",
            "date": "2025-10-20",
            "time": "20:00 – 21:00",
            "court": "Kurt 1",
            "name": "Natália Uhríková",
            "email": "natalia@example.com",
            "phone": "+421 903 258 369",
            "level": "Profesionál",
            "addons": {"rackets": 1, "camera": 1},
            "total": "33,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": "clen",            # člen
        },
        {
            "created_at":    "2025-10-10 12:14",
            "registered_at": "2025-10-10 12:00",
            "date": "2025-10-17",
            "time": "17:00 – 18:00",
            "court": "Kurt 1",
            "name": "Andrea Kollová (2)",
            "email": "andrea2@palmapickleball.sk",
            "phone": "+421 900 000 001",
            "level": "Začiatočník",
            "addons": {"rackets": 2, "camera": 1},
            "total": "30,00 €",
            "avatar": url_for('static', filename='images/profile1.png'),
            "status": None,              # normal
        },
    ]

@app.get("/admin/api/reservations")
def admin_api_reservations():
    """Jednoduchý JSON feed pre admin UI (filter/stránkovanie/hlavička)."""
    return jsonify({"ok": True, "reservations": sample_reservations()})
# ===== /ADDED =====


# =========================
# Run
# =========================r
if __name__ == "__main__":
    # For production, run behind a real WSGI server (gunicorn/uvicorn) and disable debug.
    app.run(debug=True)
