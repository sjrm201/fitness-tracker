"""
/api/ingest  -  the letterbox.

Receives JSON from Health Auto Export, checks the password on the envelope,
keeps a copy of the delivery (the receipt), then saves clean rows into
daily_metrics and workouts. Every save is an upsert: SET, never ADD.
"""
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone
import hmac
import json
import os
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# 1. SETTINGS - read from environment variables (.env on your Mac, Vercel live)
#    Never type a secret into this file.
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
INGEST_SECRET = os.environ.get("INGEST_SECRET", "")

# Only metrics on your wishlist get stored. Anything else is reported back
# as "ignored" so you can see it, rather than silently thrown away.
WISHLIST = {
    "heart_rate_variability", "resting_heart_rate", "walking_heart_rate_average",
    "step_count", "flights_climbed", "basal_energy_burned", "active_energy",
    "apple_exercise_time", "physical_effort",
    "dietary_energy", "protein", "carbohydrates", "total_fat", "fiber",
    "sleep_analysis",
}

# Polar strap dropouts: when the strap loses skin contact it records 0 bpm
# (or very low values). During training your HR is never below this, so any
# minute under it is "no signal", not effort. Your resting HR is ~58.
# Tune this if needed.
HR_DROPOUT_FLOOR = 70

# Sleep arrives as one bundle; we flatten it into separate metrics.
SLEEP_PARTS = {
    "totalSleep": "sleep_total",
    "deep": "sleep_deep",
    "rem": "sleep_rem",
    "core": "sleep_core",
    "awake": "sleep_awake",
}


# ---------------------------------------------------------------------------
# 2. SMALL HELPERS
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def qty(obj):
    """Auto Export wraps numbers like {"qty": 350, "units": "kcal"}. Unwrap."""
    return obj.get("qty") if isinstance(obj, dict) else None


def to_kcal(value, unit):
    """Energy can arrive in kilojoules depending on phone settings. Store kcal, always."""
    if value is not None and unit and unit.lower() == "kj":
        return round(value / 4.184, 1), "kcal"
    return value, unit


def energy(obj):
    """Unwrap a workout energy figure like {"qty": 1470, "units": "kJ"} into kcal."""
    if not isinstance(obj, dict):
        return None
    return to_kcal(obj.get("qty"), obj.get("units"))[0]


def first(*values):
    """Return the first value that is not None. (0 counts as a real value.)"""
    for v in values:
        if v is not None:
            return v
    return None


def to_iso(stamp):
    """'2026-10-15 10:11:04 +0400' -> '2026-10-15T10:11:04+04:00'"""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z").isoformat()


def source_name(src):
    """Source can be a plain string or {"name": ...}. Return a string or None."""
    if isinstance(src, dict):
        return src.get("name")
    return src


def supabase_post(table, rows, upsert_on=None):
    """Send rows to a Supabase table. With upsert_on, a row whose key already
    exists is REPLACED (set), never duplicated or added to."""
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    prefer = "return=minimal"
    if upsert_on:
        url += f"?on_conflict={upsert_on}"
        prefer = "resolution=merge-duplicates,return=minimal"
    req = urllib.request.Request(
        url,
        data=json.dumps(rows).encode(),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,          # the master key - server side only
            "Content-Type": "application/json",
            "Prefer": prefer,
        },
    )
    urllib.request.urlopen(req, timeout=20)


# ---------------------------------------------------------------------------
# 3. TRANSLATE: Auto Export's shape  ->  your table's shape
# ---------------------------------------------------------------------------
def metric_rows(metrics):
    rows = {}        # keyed by (day, metric): one delivery can't hold duplicates
    ignored = set()
    for m in metrics:
        name = m.get("name")
        if name not in WISHLIST:
            ignored.add(name)
            continue
        for point in m.get("data", []):
            day = str(point.get("date", ""))[:10]      # '2026-10-15 00:00:00 +0400' -> '2026-10-15'
            if not day:
                continue
            source = source_name(point.get("source"))

            if name == "sleep_analysis":
                for field, metric in SLEEP_PARTS.items():
                    if point.get(field) is not None:
                        rows[(day, metric)] = {
                            "day": day, "metric": metric, "value": point[field],
                            "unit": "hr", "source": source, "updated_at": now_iso(),
                        }
            elif point.get("qty") is not None:         # no number -> no row. Never invent a 0.
                value, unit = to_kcal(point["qty"], m.get("units"))
                rows[(day, name)] = {
                    "day": day, "metric": name, "value": value,
                    "unit": unit, "source": source, "updated_at": now_iso(),
                }
    return list(rows.values()), sorted(i for i in ignored if i)


def workout_hr(w):
    """Return (avg_hr, max_hr, coverage_pct) for one workout.

    Polar sessions: recalculate from the minute-by-minute data, skipping
    dropout minutes (below HR_DROPOUT_FLOOR). Coverage = % of minutes with a
    real signal, so the dashboard can grey out sessions it shouldn't trust.
    Everything else: use Auto Export's own summary, unchanged.
    """
    source = (source_name(w.get("source")) or "").lower()
    minutes = [p for p in (w.get("heartRateData") or []) if p.get("Avg") is not None]

    if "polar" in source and minutes:
        good = [p for p in minutes if p["Avg"] >= HR_DROPOUT_FLOOR]
        if not good:
            return None, None, 0                     # no usable signal at all
        avg_hr = round(sum(p["Avg"] for p in good) / len(good), 1)
        max_hr = max(first(p.get("Max"), p["Avg"]) for p in good)
        coverage = round(100 * len(good) / len(minutes))
        return avg_hr, max_hr, coverage

    hr = w.get("heartRate") or {}
    avg_hr = first(qty(hr.get("avg")), qty(w.get("avgHeartRate")))
    max_hr = first(qty(hr.get("max")), qty(w.get("maxHeartRate")))
    return avg_hr, max_hr, (100 if avg_hr is not None else None)


def workout_rows(workouts):
    rows = []
    for w in workouts:
        if not w.get("id"):
            continue
        name = w.get("name") or "Unknown"

        km = qty(w.get("distance"))
        if km is not None and (w.get("distance") or {}).get("units") == "mi":
            km = km * 1.60934                              # miles -> km

        avg_hr, max_hr, coverage = workout_hr(w)

        rows.append({
            "id": w["id"],
            "start_at": to_iso(w["start"]),
            "end_at": to_iso(w["end"]),
            "activity_type": name.lower().replace(" ", "_"),   # 'Martial Arts' -> 'martial_arts'
            "name": name,
            "duration_min": round(w["duration"] / 60, 1) if w.get("duration") is not None else None,
            "active_kcal": energy(w.get("activeEnergyBurned")),
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "hr_coverage_pct": coverage,
            "distance_km": round(km, 2) if km is not None else None,
            "source": source_name(w.get("source")),
            "updated_at": now_iso(),
        })
    return rows


# ---------------------------------------------------------------------------
# 4. THE LETTERBOX ITSELF
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        # Someone visited in a browser. This letterbox only accepts deliveries.
        self.reply(405, {"error": "This endpoint only accepts POST"})

    def do_POST(self):
        # --- Step 1: check the password on the envelope -------------------
        sent = self.headers.get("Authorization", "")
        expected = f"Bearer {INGEST_SECRET}"
        if not INGEST_SECRET or not hmac.compare_digest(sent.encode(), expected.encode()):
            self.reply(401, {"error": "unauthorized"})
            return

        # --- Step 2: open the envelope -----------------------------------
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.reply(400, {"error": "Body is not valid JSON"})
            return

        try:
            # --- Step 3: keep the receipt, BEFORE anything can go wrong --
            supabase_post("raw_payloads", [{
                "automation": self.headers.get("automation-name"),
                "payload": body,
            }])

            # --- Step 4: translate and file ------------------------------
            data = body.get("data", {})
            m_rows, ignored = metric_rows(data.get("metrics", []))
            w_rows = workout_rows(data.get("workouts", []))
            supabase_post("daily_metrics", m_rows, upsert_on="day,metric")
            supabase_post("workouts", w_rows, upsert_on="id")

        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            print("Supabase error:", e.code, detail)          # shows in Vercel logs
            self.reply(502, {"error": "Database write failed", "detail": detail})
            return
        except Exception as e:
            print("Ingest error:", repr(e))
            self.reply(500, {"error": str(e)})
            return

        # --- Step 5: sign for the delivery ---------------------------------
        self.reply(200, {
            "ok": True,
            "metric_rows": len(m_rows),
            "workouts": len(w_rows),
            "ignored_metrics": ignored,
        })
