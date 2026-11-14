# Documentation drafts, fall timelines and the family update

Saves staff documentation time. **Ember drafts; staff review, edit and sign.** Nothing is
final until a staff member signs it. Drafts stay non-clinical: they are built only from care
events, which can't hold medication, diagnoses, vitals or assessments.

Part of facility mode: nothing here runs unless `EMBER_CARE_ENABLED=1`.

## Falls in the care log
Each fall is recorded once, as a `fall` event with status `attention`. Where it is recorded
depends on the runtime path, because the runtime and the dashboard are separate processes:

| Path | Who records |
|---|---|
| v8 / MVP: `features/toolkits/fall_detection.py` (never posts `/api/fall`) | The toolkit, in the runtime. After Ember's check-in reaches Gemini it adds a `note` linked by `data.fall_event_id`. |
| A runtime that posts `POST /api/fall` to the dashboard (v10 local mode) | `baymax_app.report_fall`. |

`data` holds non-clinical facts only: `source`, `detected_at` and, for the dashboard,
whether an alert email was configured. Pose angles stay out of the care log.

## Drafts
Module `drafts.py` (storage, templates, schedule) and `drafts_dashboard.py` (routes, loop).

| Kind | Key | Built from |
|---|---|---|
| `daily_note` | `daily_note:<day>` | The day's staff-visible events, in sections: safety, check-ins, requests and how they were handled, activities, meals, night activity, notes, flags |
| `activity_log` | `activity_log:<day>` | The day's `activity` events |
| `service_log` | `service_log:<day>` | `request` and `help_request` events with who handled them, when, and minutes to handle |
| `fall_timeline` | `fall_timeline:<event id>` | The fall, Ember's linked check-in, when staff marked it handled, and check-ins, help requests, night activity and further falls for 72 hours |

- **Deterministic:** plain templating over events. No model call, so what staff read is what
  was logged. Every line ends with its source event (`[#12]`) and each draft stores
  `source_event_ids`.
- **Summaries only:** drafts never contain the resident's own words (quotes), and never
  private non-safety events (they use the staff view of the log).
- **States:** `draft` → `edited` → `signed` (signer's name as typed, their account email and
  the time). A signed row is final: SQLite triggers refuse to update or delete it. Editing a
  signed draft creates version *n+1* (`supersedes` points at the signed one); only the newest
  version can be edited.
- **Regeneration** refreshes a draft only while it is still in `draft` state. Staff edits
  and signatures are never overwritten.
- **Storage:** table `care_drafts` in the care log's own SQLite file
  (`BAYMAX_DATA_DIR/care_log.sqlite3`). Drafts are not pruned with events: they are the
  record staff signed.

### Schedule
The dashboard runs a loop next to the morning digest (thread `care-drafts`, every minute):
- each morning after `EMBER_CARE_DRAFT_TIME` (default `07:00`), yesterday's daily note,
  activity log and service log, once a day (the date is saved in the care log's settings,
  so a restart doesn't repeat it);
- every tick, timelines for falls from the last 4 days, refreshed until edited or signed.

Staff can also generate on demand.

### Staff routes (robot dashboard, staff accounts only)
| Route | Does |
|---|---|
| `GET /care/drafts` | Review page: list, edit, sign, print, download |
| `GET /api/care/drafts?kind=&state=&all=1` | Newest version of each draft (`all=1`: every version) |
| `POST /api/care/drafts/generate` | `{"day": "YYYY-MM-DD"}` (default today) or `{"fall_event_id": 12}` |
| `GET /api/care/drafts/<id>` | The draft plus its source events |
| `POST /api/care/drafts/<id>/edit` | `{"body": "..."}`; on a signed draft, starts a new version |
| `POST /api/care/drafts/<id>/sign` | `{"name": "Maria Lopez, LPN"}` (default: the account's username). 409 if already signed |
| `GET /api/care/drafts/<id>/export.txt` | Plain text download for the EHR |
| `GET /api/care/drafts/<id>/print` | Printable HTML (print or "save as PDF" in the browser) |

Exports end with the document name, version, unit, source event IDs and either the
signature with a SHA-256 fingerprint of the text, or `UNSIGNED DRAFT`.

## Family update (weekly email)
`family_digest.py`. Set `EMBER_CARE_FAMILY_DIGEST="sun 18:00"` (off when unset).
- **Recipients:** paired accounts that are not staff.
- **Consent:** only kinds the resident agreed to share (`care.log.family_kinds()`), never
  private events, never quotes. **Nothing is sent before consent is recorded**, or when there
  is nothing to share.
- **Content:** activities, meals and notes listed by summary ("Joined bingo (twice)"). Other
  consented kinds appear only as counts. It closes by pointing family to the care team and
  saying Ember is not a medical device.
- **Once per ISO week:** at or after the configured day and time (it catches up later in the
  week if the dashboard was down). The week is saved in the care log *before* sending, so a
  crash can skip a week but never send twice.

## Inputs, outputs, dependencies
- **Inputs:** care events (`features/care/store.py`), staff edits and signatures.
- **Outputs:** `care_drafts` rows, exports, emails, and forwarding to the facility page.
- **Dependencies:** Flask (already used by the dashboard), the standard library. No new packages.

## External data flows
- **tadashirobotics.com:** each new draft, refresh, edit and signature is queued with
  `POST /api/device/care-draft` through `baymax_cloud` (`docs/facility/SITE_CONTRACT.md` §1).
  It needs `BAYMAX_DEVICE_KEY` and respects `EMBER_CARE_CLOUD=0`.
- **Gmail SMTP** (the dashboard's existing settings): the weekly family update.
- **Nothing goes to Gemini.** Optional Gemini prose polishing is not implemented.

## Enabling and disabling
```
EMBER_CARE_ENABLED=1                 # facility mode (required)
EMBER_CARE_DRAFTS=1                  # default; 0 turns off drafts and their routes
EMBER_CARE_DRAFT_TIME=07:00          # morning drafts for yesterday
EMBER_CARE_FAMILY_DIGEST=sun 18:00   # weekly family email; unset = off
EMBER_CARE_RESIDENT_NAME=Rose        # name used in drafts and the family email
```
The loop runs where the morning digest runs (`python baymax_app.py`, or
`EMBER_CARE_DIGEST_IN_PROCESS=1`).

## Tests
- `python -m unittest tests.test_care_drafts`: each draft type from fixture events,
  versioning and signed immutability (including direct SQLite writes), schedule, exports,
  forwarding payloads, staff-only routes, a fall posted to the dashboard recorded once,
  family consent filtering and once-per-week sending.
- `tests/test_toolkit_fall_detection.py`: a toolkit-detected fall is recorded once, with
  the linked check-in note.

All offline: no network, Gemini or devices.
