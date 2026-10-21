"""
/api/data  -  what the dashboard reads.

Protected by DASHBOARD_KEY (a READ key, separate from INGEST_SECRET, which is
a WRITE key). Reads your tables with the Supabase secret key, applies the
interpretation rules, and returns tidy JSON. The page only draws; all the
logic lives here, in one place.
"""
from http.server import BaseHTTPRequestHandler
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
import hmac
import json
import os
import statistics
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# 1. SETTINGS
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")

LOCAL_TZ = timezone(timedelta(hours=4))   # Abu Dhabi. Used to put workouts on the right day.

# Interpretation rules - tune these, they're yours.
PARTIAL_INTAKE_RATIO = 0.6    # intake below 60% of your typical day = "possibly incomplete"
LOW_HR_COVERAGE = 50          # Polar sessions with < 50% real HR signal = "low signal"
COOLDOWN_MERGE_MIN = 60       # a cool-down starting within 60 min of a session joins it

# Database metric name -> short name used by the page
FIELDS = {
    "heart_rate_variability": "hrv",
    "resting_heart_rate": "rhr",
    "walking_heart_rate_average": "walk_hr",
    "step_count": "steps",
    "flights_climbed": "flights",
    "basal_energy_burned": "basal",
    "active_energy": "active",
    "apple_exercise_time": "exercise_min",
    "physical_effort": "effort",
    "dietary_energy": "intake",
    "protein": "protein",
    "carbohydrates": "carbs",
    "total_fat": "fat",
    "fiber": "fibre",
    "sleep_total": "sleep_total",
    "sleep_deep": "sleep_deep",
    "sleep_rem": "sleep_rem",
    "sleep_core": "sleep_core",
    "sleep_awake": "sleep_awake",
}

# Metrics that build up during the day: today's value is incomplete,
# so today is left out of their averages. (HRV, resting HR and sleep are
# overnight readings - today's value is already final.)
CUMULATIVE = {"steps", "flights", "basal", "active", "exercise_min", "intake",
              "protein", "carbs", "fat", "fibre", "total_burn"}
NUTRITION = {"intake", "protein", "carbs", "fat", "fibre"}


# ---------------------------------------------------------------------------
# 2. READ FROM SUPABASE
# ---------------------------------------------------------------------------
def supabase_get(table, query):
    """Read rows, 1,000 at a time (Supabase's page size), until there are no more."""
    rows, offset = [], 0
    while True:
        url = f"{SUPABASE_URL}/rest/v1/{table}?{query}&limit=1000&offset={offset}"
        req = urllib.request.Request(url, headers={"apikey": SUPABASE_KEY})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = json.loads(r.read())
        rows += page
        if len(page) < 1000:
            return rows
        offset += 1000


def to_local(stamp):
    return datetime.fromisoformat(stamp).astimezone(LOCAL_TZ)


def avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


# ---------------------------------------------------------------------------
# 3. DAYS: long rows -> one wide row per day, plus derived numbers
# ---------------------------------------------------------------------------
def build_days(metric_rows, start, end, today):
    by_day = {}
    for r in metric_rows:
        short = FIELDS.get(r["metric"])
        if short:
            by_day.setdefault(r["day"], {})[short] = float(r["value"])

    # "Typical intake" = median of complete-looking past days in the window.
    past_intakes = [m["intake"] for d, m in by_day.items()
                    if "intake" in m and d < today.isoformat()]
    typical_intake = statistics.median(past_intakes) if past_intakes else None

    days, d = [], start
    while d <= end:
        key = d.isoformat()
        m = by_day.get(key, {})
        row = {"day": key, "is_today": d == today}
        row.update({short: m.get(short) for short in FIELDS.values()})

        # Total burn = basal + active, only when BOTH are known (never guess one as 0)
        if row["basal"] is not None and row["active"] is not None:
            row["total_burn"] = round(row["basal"] + row["active"])
        else:
            row["total_burn"] = None

        # Intake status: complete / partial / not_logged / today
        if d == today:
            row["intake_status"] = "today"
        elif row["intake"] is None:
            row["intake_status"] = "not_logged"
        elif typical_intake and row["intake"] < PARTIAL_INTAKE_RATIO * typical_intake:
            row["intake_status"] = "partial"
        else:
            row["intake_status"] = "complete"

        # Net energy only for complete days - otherwise it's a fake deficit
        if row["intake_status"] == "complete" and row["total_burn"] is not None:
            row["net_energy"] = round(row["intake"] - row["total_burn"])
        else:
            row["net_energy"] = None

        days.append(row)
        d += timedelta(days=1)
    return days


def build_summary(days):
    """Averages over the window, with honest counts of how many days fed them."""
    summary = {}
    keys = list(FIELDS.values()) + ["total_burn", "net_energy"]
    for k in keys:
        usable = []
        for row in days:
            if k in CUMULATIVE or k == "net_energy":
                if row["is_today"]:
                    continue
            if k in NUTRITION and row["intake_status"] != "complete":
                usable.append(None)        # counts as a day in the window, but not in the average
                continue
            usable.append(row.get(k))
        known = [v for v in usable if v is not None]
        summary[k] = {"avg": avg(known), "days": len(known), "of": len(usable)}
    return summary


# ---------------------------------------------------------------------------
# 4. SESSIONS: workouts -> training sessions (cool-downs merged in)
# ---------------------------------------------------------------------------
def category(activity_type):
    if activity_type == "martial_arts":
        return "BJJ"
    if "strength" in activity_type:
        return "Strength"
    if "running" in activity_type or activity_type.endswith("_run"):
        return "Run"
    return activity_type.replace("_", " ").title()


def build_sessions(workouts):
    sessions = []
    for w in sorted(workouts, key=lambda w: w["start_at"]):
        start, end = to_local(w["start_at"]), to_local(w["end_at"])
        dur = float(w["duration_min"]) if w["duration_min"] is not None else 0.0
        kcal = float(w["active_kcal"]) if w["active_kcal"] is not None else None
        avg_hr = float(w["avg_hr"]) if w["avg_hr"] is not None else None
        max_hr = float(w["max_hr"]) if w["max_hr"] is not None else None

        if w["activity_type"] == "cool_down" and sessions:
            last = sessions[-1]
            gap = (start - last["_end"]).total_seconds() / 60
            if last["day"] == start.date().isoformat() and -5 <= gap <= COOLDOWN_MERGE_MIN:
                # Merge: time-weighted HR, add minutes and kcal, keep the higher max
                if last["avg_hr"] is not None and avg_hr is not None:
                    total = last["duration_min"] + dur
                    last["avg_hr"] = round((last["avg_hr"] * last["duration_min"] + avg_hr * dur) / total, 1)
                last["duration_min"] = round(last["duration_min"] + dur, 1)
                last["cooldown_min"] = round(last["cooldown_min"] + dur, 1)
                if kcal is not None:
                    last["active_kcal"] = round((last["active_kcal"] or 0) + kcal)
                if max_hr is not None:
                    last["max_hr"] = max(last["max_hr"] or 0, max_hr)
                last["_end"] = end
                continue

        coverage = w.get("hr_coverage_pct")
        sessions.append({
            "day": start.date().isoformat(),
            "time": start.strftime("%H:%M"),
            "category": category(w["activity_type"]),
            "name": w["name"],
            "source": w["source"],
            "duration_min": round(dur, 1),
            "cooldown_min": 0.0,
            "active_kcal": round(kcal) if kcal is not None else None,
            "avg_hr": round(avg_hr, 1) if avg_hr is not None else None,
            "max_hr": max_hr,
            "hr_coverage_pct": float(coverage) if coverage is not None else None,
            "low_signal": coverage is not None and float(coverage) < LOW_HR_COVERAGE,
            "_end": end,
        })
    for s in sessions:
        s.pop("_end")
    return sessions


def attach_training(days, sessions):
    by_day = {}
    for s in sessions:
        by_day.setdefault(s["day"], []).append(s)
    for row in days:
        todays = by_day.get(row["day"], [])
        row["training"] = [s["category"] for s in todays]
        row["training_min"] = round(sum(s["duration_min"] for s in todays)) if todays else 0


# ---------------------------------------------------------------------------
# 5. THE ENDPOINT
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")     # health data: never cache
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        sent = self.headers.get("Authorization", "")
        expected = f"Bearer {DASHBOARD_KEY}"
        if not DASHBOARD_KEY or not hmac.compare_digest(sent.encode(), expected.encode()):
            self.reply(401, {"error": "unauthorized"})
            return

        try:
            params = parse_qs(urlparse(self.path).query)
            n_days = max(1, min(int(params.get("days", ["30"])[0]), 730))
            today = datetime.now(LOCAL_TZ).date()
            start = today - timedelta(days=n_days - 1)
            fetch_from = (start - timedelta(days=1)).isoformat()   # 1-day buffer for timezones

            metric_rows = supabase_get(
                "daily_metrics",
                f"select=day,metric,value&day=gte.{start.isoformat()}&order=day.asc")
            workout_rows = supabase_get(
                "workouts",
                f"select=*&start_at=gte.{fetch_from}&order=start_at.asc")

            # Don't count days before your data begins as "missing"
            if metric_rows:
                first_day = date.fromisoformat(min(r["day"] for r in metric_rows))
                start = max(start, first_day)

            days = build_days(metric_rows, start, today, today)
            sessions = [s for s in build_sessions(workout_rows) if s["day"] >= start.isoformat()]
            attach_training(days, sessions)

        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            print("Supabase error:", e.code, detail)
            self.reply(502, {"error": "Database read failed", "detail": detail})
            return
        except Exception as e:
            print("Data error:", repr(e))
            self.reply(500, {"error": str(e)})
            return

        self.reply(200, {
            "generated_at": datetime.now(LOCAL_TZ).isoformat(timespec="minutes"),
            "range": {"start": start.isoformat(), "end": today.isoformat(), "days": n_days},
            "days": days,
            "sessions": sessions,
            "summary": build_summary(days),
            "rules": {
                "partial_intake_ratio": PARTIAL_INTAKE_RATIO,
                "low_hr_coverage": LOW_HR_COVERAGE,
                "cooldown_merge_min": COOLDOWN_MERGE_MIN,
            },
        })
