"""Robot dashboard (baymax_app) side of facility mode: staff upload the menu and calendar, and
open requests are emailed to the address configured for their route.

Requests are logged by the Ember runtime (a different process) into the care log. This
process polls the log every 30 s, and emails each open request whose route has an address
(EMBER_ROUTE_EMAIL_<ROUTE>) exactly once, recorded in its own small table. Many work-order
systems accept tickets by email, so that address *is* the integration.
"""
import os
import smtplib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from flask import Blueprint, jsonify, redirect, request

from features.care import dashboard as care_dashboard
from features.care import store as care_store
from .sources import KINDS, Sources
from .routes import ROUTES

blueprint = Blueprint("facility", __name__)
sources = Sources()
LOOKBACK = 24 * 3600  # Turning routing on doesn't email old history.
MAX_ATTEMPTS = 5


@blueprint.before_request
def _facility_mode_only():
    if not care_store.enabled():
        return jsonify({"error": "facility mode is off (EMBER_CARE_ENABLED=0)"}), 404
    user = care_dashboard._hooks["current_user"]() or {}
    if care_dashboard.role(user) != "staff":
        return jsonify({"error": "staff only"}), 403
    return None


def local_zone():
    return datetime.now().astimezone().tzinfo


@blueprint.route("/api/facility/status")
def status():
    return jsonify({"sources": sources.status(local_zone()), "routes": {r: bool(route_email(r)) for r in ROUTES}})


@blueprint.route("/api/facility/upload/<kind>", methods=["POST", "DELETE"])
def upload(kind):
    if kind not in KINDS:
        return jsonify({"error": "kind must be activities or menu"}), 404
    form = request.form.get("from_page") == "1"
    if request.method == "DELETE" or request.form.get("remove") == "1":
        removed = sources.remove_upload(kind)
        return redirect("/care/facility") if form else jsonify({"removed": removed})
    file = request.files.get("file")
    raw = file.read(2_000_001) if file else request.get_data(cache=False)[:2_000_001]
    try:
        text = raw.decode("utf-8-sig")
        result = sources.save_upload(kind, text, local_zone())
    except (UnicodeDecodeError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "The file must be UTF-8 text (CSV or ICS)"
        return (page(error=f"{kind}: {message}"), 400) if form else (jsonify({"error": message}), 400)
    return redirect("/care/facility") if form else jsonify({"saved": True, **result})


@blueprint.route("/care/facility")
def page(error=None):
    """A plain upload form: staff export the files they already make and drop them here."""
    status = sources.status(local_zone())
    def esc(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = ["<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>",
             "<title>Ember menu and calendar</title><body style='font-family:sans-serif;max-width:40em;margin:1em auto;padding:0 1em'>",
             "<h1>Menu and activity calendar</h1>",
             "<p>Ember answers residents' questions about meals and activities only from these files.</p>"]
    if error:
        parts.append(f"<p style='color:#b00020'><b>Not saved.</b> {esc(error)}</p>")
    for kind, hint in (("activities", "An .ics calendar export, or a CSV with columns date, time, title, location"),
                       ("menu", "A CSV with columns date, meal, items (date may be a weekday name for a weekly menu)")):
        info = status[kind]
        state = "Not set" if not info["configured"] else \
            f"From {esc(info['source'] or info['configured'])}, updated {esc(info['updated_at'] or 'never')}"
        if info.get("error"):
            state += f" <b>(last refresh failed; using the copy above)</b>"
        parts += [f"<h2>{kind.title()}</h2><p>{state}</p><p><small>{hint}</small></p>",
                  f"<form method=post action='/api/facility/upload/{kind}' enctype=multipart/form-data>"
                  f"<input type=hidden name=from_page value=1><input type=file name=file required> "
                  f"<button>Upload</button></form>"]
        if info["configured"] == "upload":
            parts.append(f"<form method=post action='/api/facility/upload/{kind}'><input type=hidden name=from_page value=1>"
                         f"<input type=hidden name=remove value=1><button>Remove upload</button></form>")
    return "".join(parts)


# ── Request emails ────────────────────────────────────────────────────────────
def route_email(route):
    return os.getenv(f"EMBER_ROUTE_EMAIL_{route.upper()}", "").strip()


class Outbox:
    """Which care events were emailed. Separate from the care log so its schema stays untouched."""

    def __init__(self, path=None):
        self.explicit = Path(path) if path else None

    @property
    def path(self):
        root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[3]))
        return self.explicit or root / "facility" / "route_emails.sqlite3"

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS emailed (event_id INTEGER NOT NULL, at TEXT NOT NULL, "
                               "route TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                               "PRIMARY KEY (event_id, at))")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def claim(self, event, route):
        """True if this event should be sent now. Marks it 'sending' so a crash can't send it twice."""
        with self.db() as db:
            row = db.execute("SELECT state, attempts FROM emailed WHERE event_id=? AND at=?",
                             (event["id"], event["at"])).fetchone()
            if row and (row[0] in ("sent", "sending") or row[1] >= MAX_ATTEMPTS):
                return False
            db.execute("INSERT OR REPLACE INTO emailed VALUES (?,?,?,'sending',?)",
                       (event["id"], event["at"], route, (row[1] if row else 0) + 1))
            return True

    def finish(self, event, ok):
        with self.db() as db:
            db.execute("UPDATE emailed SET state=? WHERE event_id=? AND at=?",
                       ("sent" if ok else "failed", event["id"], event["at"]))


outbox = Outbox()


def smtp_send(to, subject, body):
    """Synchronous Gmail SMTP with the dashboard's existing sender, so success is known before marking."""
    sender, password = os.getenv("BAYMAX_EMAIL_FROM", ""), os.getenv("BAYMAX_EMAIL_PASSWORD", "")
    message = MIMEText(body, "plain")
    message["From"], message["To"], message["Subject"] = sender, to, subject
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
        server.login(sender, password)
        server.sendmail(sender, [to], message.as_string())


def email_for(event, device):
    """(subject, body). The summary and route only: never the resident's words."""
    data = event.get("data") or {}
    route = data["route"]
    name = device.get("name") or device.get("serial") or "Ember"
    urgent = "URGENT " if data.get("urgent") else ""
    subject = f"{urgent}Ember request ({ROUTES[route][2]}): {name}"
    when = datetime.fromisoformat(event["at"]).strftime("%a %d %b %H:%M")
    body = "\n".join([
        f"Request from the resident with Ember {name}.", "",
        f"What: {event['summary']}", f"Route: {ROUTES[route][2]}", f"Logged: {when}", f"Reference: {name} #{event['id']}",
        "",
        "Ember passed this on from conversation; please follow up as usual. Ember is not an emergency system"
        + (": the resident was told to use their pendant or pull cord." if data.get("urgent") else "."),
        "Mark it handled on the facility page or the robot's dashboard (/care).",
    ])
    return subject, body


def send_route_emails(log=None, device=None, send=None, clock=time.time):
    """Email open requests that have a route address. Returns how many were sent."""
    if not care_store.enabled():
        return 0
    if send is None:
        if not (os.getenv("BAYMAX_EMAIL_FROM") and os.getenv("BAYMAX_EMAIL_PASSWORD")):
            return 0  # Not configured: leave requests unsent so they go once email is set up.
        send = smtp_send
    log = log or care_store.log
    device = device or care_dashboard._hooks.get("device") or {}
    sent = 0
    for event in log.events(clock() - LOOKBACK, kinds=["request", "help_request"], audience="staff"):
        route = (event.get("data") or {}).get("route")
        address = route_email(route) if route in ROUTES else ""
        if event["status"] != "open" or not address or not outbox.claim(event, route):
            continue
        subject, body = email_for(event, device)
        try:
            send(address, subject, body)
            outbox.finish(event, True)
            sent += 1
        except Exception as error:
            outbox.finish(event, False)
            print(f"[FACILITY] Could not email request #{event['id']} to {route}: {type(error).__name__}", flush=True)
    return sent


def _route_loop():
    while True:
        try:
            send_route_emails()
        except Exception as error:
            print(f"[FACILITY] Request email check failed: {type(error).__name__}", flush=True)
        time.sleep(30)


def register(app, start_loop=True):
    """Called by baymax_app after the care dashboard registers (it reuses its sign-in hooks)."""
    app.register_blueprint(blueprint)
    if start_loop and care_store.enabled() and any(route_email(r) for r in ROUTES):
        threading.Thread(target=_route_loop, name="facility-route-email", daemon=True).start()
