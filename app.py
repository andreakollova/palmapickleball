from flask import Flask, render_template, request, jsonify
from collections import defaultdict
from datetime import datetime


app = Flask(__name__)

def build_slots():
    out = []
    for h in range(8, 21):
        out.append(f"{h:02d}:00")
        out.append(f"{h:02d}:30")
    return out[:-1]

SLOTS_30 = build_slots()
COURTS = {"1": "Kurt 1", "2": "Kurt 2"}
bookings = defaultdict(lambda: {"1": set(), "2": set()})

def valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False

@app.route("/")
def index():
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template("index.html", today=today)

@app.get("/api/availability")
def api_availability():
    date = request.args.get("date")
    if not date or not valid_date(date):
        return jsonify({"ok": False, "error": "Neplatný dátum."}), 400
    return jsonify({
        "ok": True,
        "date": date,
        "slots": SLOTS_30,
        "courts": {
            "1": sorted(list(bookings[date]["1"])),
            "2": sorted(list(bookings[date]["2"]))
        }
    })

@app.post("/api/book")
def api_book():
    data  = request.get_json(force=True)
    date  = data.get("date")
    court = data.get("court")
    slots = data.get("slots", [])
    name  = (data.get("name") or "").strip()
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
        return jsonify({"ok": False, "error": "Konflikt: obsadené.", "conflicts": conflicts}), 409

    for s in slots:
        already.add(s)

    # IMPORTANT: still return JSON (do NOT redirect here)
    return jsonify({"ok": True, "reserved": slots, "court": court, "date": date})

# New checkout page (simple GET)
@app.route("/checkout")
def checkout():
    date = request.args.get("date")
    court = request.args.get("court")
    total = request.args.get("total")
    return render_template("checkout.html", date=date, court=court, total=total)

if __name__ == "__main__":
    app.run(debug=True)
