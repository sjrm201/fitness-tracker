"""
/api/log  -  your daily note (context your devices can't capture).

POST {"day": "2026-10-25", "note": "Travel day, slept badly"}
Send an empty note to remove it. Missing = no row, never an empty string.

Weight is NOT logged here: it arrives automatically from the Eufy scale via
Apple Health, through /api/ingest.

Uses DASHBOARD_KEY: the note form lives in the same browser as the
dashboard, so a separate key would add a step without adding protection.
What this key can do here is limited by the code: one note per day,
within the last 60 days, under 1,000 characters.
"""
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone, date
import hmac
import json
import os
import urllib.request
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET_HOURS", "0"))))

MAX_DAYS_BACK = 60
MAX_NOTE = 1000


class BadInput(Exception):
    pass


def supabase(method, path, rows=None, prefer="return=minimal"):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=json.dumps(rows).encode() if rows is not None else None,
        method=method,
        headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json", "Prefer": prefer},
    )
    urllib.request.urlopen(req, timeout=20)


def check_day(raw):
    try:
        d = date.fromisoformat(str(raw))
    except ValueError:
        raise BadInput("day must look like 2026-10-25")
    today = datetime.now(LOCAL_TZ).date()
    if d > today:
        raise BadInput("day can't be in the future")
    if d < today - timedelta(days=MAX_DAYS_BACK):
        raise BadInput(f"day must be within the last {MAX_DAYS_BACK} days")
    return d.isoformat()


class handler(BaseHTTPRequestHandler):

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        self.reply(405, {"error": "This endpoint only accepts POST"})

    def do_POST(self):
        sent = self.headers.get("Authorization", "")
        if not DASHBOARD_KEY or not hmac.compare_digest(sent.encode(), f"Bearer {DASHBOARD_KEY}".encode()):
            self.reply(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            day = check_day(body.get("day"))
            note = (body.get("note") or "").strip()
            if not note:
                supabase("DELETE", f"daily_notes?day=eq.{day}")
            else:
                if len(note) > MAX_NOTE:
                    raise BadInput(f"note must be under {MAX_NOTE} characters")
                supabase("POST", "daily_notes?on_conflict=day", [{
                    "day": day, "note": note, "updated_at": datetime.now(timezone.utc).isoformat(),
                }], prefer="resolution=merge-duplicates,return=minimal")
        except BadInput as e:
            self.reply(400, {"error": str(e)})
            return
        except json.JSONDecodeError:
            self.reply(400, {"error": "Body is not valid JSON"})
            return
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            print("Supabase error:", e.code, detail)
            self.reply(502, {"error": "Database write failed", "detail": detail})
            return
        except Exception as e:
            print("Log error:", repr(e))
            self.reply(500, {"error": str(e)})
            return
        self.reply(200, {"ok": True, "day": day, "note": note or None})
