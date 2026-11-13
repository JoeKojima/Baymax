"""Staff routes for documentation drafts on the robot's dashboard, and the drafts/family-update loop.

Every route here is staff-only (paired accounts in EMBER_STAFF_EMAILS), on top of
baymax_app's sign-in and pairing checks. Uses the hooks care.dashboard.register stored.
"""
import html
import threading
import time
from datetime import date, datetime

from flask import Blueprint, Response, jsonify, request, send_from_directory

from . import dashboard, family_digest, store
from .drafts import DraftError, KINDS, drafts, enabled, export_text, run_schedule

blueprint = Blueprint("care_drafts", __name__)


@blueprint.before_request
def _staff_only():
    if not enabled():
        return jsonify({"error": "drafts are off (EMBER_CARE_ENABLED=0 or EMBER_CARE_DRAFTS=0)"}), 404
    if dashboard.role(dashboard._user()) != "staff":
        return jsonify({"error": "staff only"}), 403
    return None


def _name(user):
    return user.get("username") or user.get("email")


def _found(action):
    try:
        return jsonify(action())
    except KeyError:
        return jsonify({"error": "no draft with that ID"}), 404
    except (DraftError, ValueError) as error:
        return jsonify({"error": str(error)}), 409 if isinstance(error, DraftError) else 400


@blueprint.route("/care/drafts")
def page():
    return send_from_directory(dashboard._hooks["static_dir"], "care_drafts.html")


@blueprint.route("/api/care/drafts")
def list_drafts():
    return jsonify({"kinds": KINDS, "drafts": drafts.list(
        kind=request.args.get("kind"), state=request.args.get("state"),
        all_versions=request.args.get("all") == "1")})


@blueprint.route("/api/care/drafts/generate", methods=["POST"])
def generate():
    """On demand: {"day": "2026-11-12"} for the day's drafts, or {"fall_event_id": 12}."""
    body = request.get_json(silent=True) or {}
    if body.get("fall_event_id") is not None:
        return _found(lambda: [drafts.generate_fall(int(body["fall_event_id"]))])
    try:
        day = date.fromisoformat(body["day"]) if body.get("day") else \
            datetime.fromtimestamp(store.log.clock()).date()
    except (TypeError, ValueError):
        return jsonify({"error": "day must be YYYY-MM-DD"}), 400
    return _found(lambda: drafts.generate_day(day))


@blueprint.route("/api/care/drafts/<int:draft_id>")
def view(draft_id):
    def action():
        draft = drafts.get(draft_id)
        ids = set(draft["source_event_ids"])
        start = datetime.fromisoformat(draft["period_start"]).timestamp()
        end = datetime.fromisoformat(draft["period_end"]).timestamp()
        draft["sources"] = [e for e in store.log.events(start, end, limit=5000) if e["id"] in ids]
        return draft
    return _found(action)


@blueprint.route("/api/care/drafts/<int:draft_id>/edit", methods=["POST"])
def edit(draft_id):
    body = request.get_json(silent=True) or {}
    return _found(lambda: drafts.edit(draft_id, body.get("body"), _name(dashboard._user())))


@blueprint.route("/api/care/drafts/<int:draft_id>/sign", methods=["POST"])
def sign(draft_id):
    """{"name": "Maria Lopez, LPN"}: the signer's name as it should appear on the record."""
    user = dashboard._user()
    body = request.get_json(silent=True) or {}
    return _found(lambda: drafts.sign(draft_id, body.get("name") or _name(user), user.get("email")))


def _filename(draft, extension):
    return f"ember-{draft['key'].replace(':', '-')}-v{draft['version']}.{extension}"


@blueprint.route("/api/care/drafts/<int:draft_id>/export.txt")
def export_txt(draft_id):
    try:
        draft = drafts.get(draft_id)
    except KeyError:
        return jsonify({"error": "no draft with that ID"}), 404
    return Response(export_text(draft, dashboard._hooks["device"]), mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{_filename(draft, "txt")}"'})


@blueprint.route("/api/care/drafts/<int:draft_id>/print")
def printable(draft_id):
    """A clean page to print or save as PDF from the browser, for the facility's EHR."""
    try:
        draft = drafts.get(draft_id)
    except KeyError:
        return jsonify({"error": "no draft with that ID"}), 404
    text = html.escape(export_text(draft, dashboard._hooks["device"]))
    page = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{html.escape(draft['title'])}</title>"
            "<style>body{margin:2cm;font:11pt/1.5 Georgia,serif;color:#000;background:#fff}"
            "pre{white-space:pre-wrap;font:inherit}@page{margin:2cm}</style></head>"
            f"<body><pre>{text}</pre></body></html>")
    return Response(page, mimetype="text/html")


# ── Loop: morning drafts, fall timelines, weekly family update ───────────────
def family_emails():
    staff = dashboard.staff_emails()
    return [e for e in dashboard._hooks["paired_emails"]() if e.lower() not in staff]


def tick(now=None):
    run_schedule(drafts, now)
    family_digest.send_if_due(dashboard._hooks, family_emails, now)


def _loop():
    while True:
        try:
            tick()
        except Exception as error:
            print(f"[CARE] Drafts/family update check failed: {type(error).__name__}", flush=True)
        time.sleep(60)


def register(app, start_loop=True):
    """Called by baymax_app after care.dashboard.register (it reuses that module's hooks)."""
    app.register_blueprint(blueprint)
    if start_loop and store.enabled():
        threading.Thread(target=_loop, name="care-drafts", daemon=True).start()
