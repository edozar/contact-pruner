#!/usr/bin/env python3
"""
Contact pruner — two-phase, human-approved.

Phase 1 — scan (identify candidates, send to Telegram for review):
    python prune.py --scan

Phase 2 — execute (read your Telegram reply, apply exclusions, prune):
    python prune.py --execute <run-id>

Nothing is deleted or tagged until you approve in Telegram.
Reply to the scan message with emails to KEEP (one per line or comma-separated).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
OUTPUT = HERE / "output"
OUTPUT.mkdir(exist_ok=True)

# ── env ────────────────────────────────────────────────────────────────────────
def _env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        # try .env in this directory
        env_path = HERE / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, val = line.partition("=")
                if k.strip() == key:
                    return val.strip()
    return v

HUBSPOT_TOKEN  = _env("HUBSPOT_TOKEN")
BOT_TOKEN      = _env("TELEGRAM_STATUS_BOT_TOKEN")
CHAT_ID        = _env("TELEGRAM_STATUS_CHAT_ID")

HS_BASE   = "https://api.hubapi.com"
TG_BASE   = f"https://api.telegram.org/bot{BOT_TOKEN}"

HS_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}

# ── hard guards ────────────────────────────────────────────────────────────────
PROTECTED_SUB      = {"$1.00", "fee"}          # paid or intent-declared
PROTECTED_STAGES   = {"customer", "opportunity", "salesqualifiedlead",
                      "marketingqualifiedlead", "evangelist"}
PROTECTED_DOMAINS  = {"ipo.club"}              # own records

PROPS = [
    "email", "firstname", "lastname", "createdate",
    "subscription_level", "squarespace_user_id", "last_member_page_login",
    "hs_email_last_open_date", "hs_email_open",
    "recent_conversion_date", "recent_conversion_event_name",
    "company", "ipo_club_member_acces", "advisor_verified",
    "num_associated_deals", "lifecyclestage", "hs_lead_status",
    "hs_sales_email_last_replied",
]

# ── helpers ────────────────────────────────────────────────────────────────────
def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def _days_ago_ms(days: int) -> int:
    return _now_ms() - days * 86_400_000

def _epoch_to_iso(ms_str: str | None) -> str:
    if not ms_str:
        return ""
    try:
        return datetime.fromtimestamp(int(ms_str) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ms_str

def _is_protected(p: dict) -> str | None:
    """Return a reason string if contact must never be pruned, else None."""
    email = (p.get("email") or "").lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in PROTECTED_DOMAINS:
        return "own_domain"
    sub = (p.get("subscription_level") or "").strip().lower()
    if sub in PROTECTED_SUB:
        return f"subscription_level={sub}"
    stage = (p.get("lifecyclestage") or "").lower()
    if stage in PROTECTED_STAGES:
        return f"lifecyclestage={stage}"
    if (p.get("ipo_club_member_acces") or "").lower() == "active":
        return "member_active"
    if (p.get("advisor_verified") or "").lower() == "true":
        return "advisor_verified"
    try:
        if int(float(p.get("num_associated_deals") or 0)) >= 1:
            return "has_deal"
    except (ValueError, TypeError):
        pass
    if p.get("hs_sales_email_last_replied"):
        return "replied_to_sales"
    if (p.get("hs_lead_status") or "").upper() == "UNQUALIFIED":
        return "already_unqualified"
    return None

# ── HubSpot search ─────────────────────────────────────────────────────────────
def _hs_search(filter_groups: list[dict]) -> dict[str, dict]:
    """Paginate through a HubSpot contact search. Returns {id: properties}."""
    url = f"{HS_BASE}/crm/v3/objects/contacts/search"
    results: dict[str, dict] = {}
    after = None
    while True:
        body: dict = {
            "filterGroups": filter_groups,
            "properties": PROPS,
            "limit": 200,
        }
        if after:
            body["after"] = after
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data,
                                     headers=HS_HEADERS, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        for rec in d.get("results", []):
            results[rec["id"]] = rec.get("properties", {})
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.12)  # stay under 10 req/s
    return results


def _fetch_ghosts() -> dict[str, dict]:
    """
    Free contacts, 90+ days old, never opened a marketing email, never filled a form,
    never logged into the member area.
    Note: squarespace_user_id is NOT filtered — all Free contacts have one (Squarespace
    issues a user ID on newsletter signup regardless of tier).
    """
    age_cutoff = str(_days_ago_ms(90))
    return _hs_search([
        {
            "filters": [
                {"propertyName": "subscription_level",      "operator": "EQ",              "value": "Free"},
                {"propertyName": "createdate",              "operator": "LT",              "value": age_cutoff},
                {"propertyName": "hs_email_last_open_date", "operator": "NOT_HAS_PROPERTY"},
                {"propertyName": "recent_conversion_date",  "operator": "NOT_HAS_PROPERTY"},
                {"propertyName": "last_member_page_login",  "operator": "NOT_HAS_PROPERTY"},
            ]
        }
    ])


def _fetch_watchers() -> dict[str, dict]:
    """
    Free contacts, 90+ days old, have opened at least one email, but never filled a form
    and never logged into the member area.
    """
    age_cutoff = str(_days_ago_ms(90))
    return _hs_search([
        {
            "filters": [
                {"propertyName": "subscription_level",      "operator": "EQ",              "value": "Free"},
                {"propertyName": "createdate",              "operator": "LT",              "value": age_cutoff},
                {"propertyName": "hs_email_last_open_date", "operator": "HAS_PROPERTY"},
                {"propertyName": "recent_conversion_date",  "operator": "NOT_HAS_PROPERTY"},
                {"propertyName": "last_member_page_login",  "operator": "NOT_HAS_PROPERTY"},
            ]
        }
    ])

# ── CSV generation ─────────────────────────────────────────────────────────────
def _build_ghost_csv(contacts: dict[str, dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name", "created", "subscription_level", "id"])
    for cid, p in sorted(contacts.items(), key=lambda kv: kv[1].get("createdate") or ""):
        w.writerow([
            p.get("email", ""),
            f"{p.get('firstname', '')} {p.get('lastname', '')}".strip(),
            _epoch_to_iso(p.get("createdate")),
            p.get("subscription_level", ""),
            cid,
        ])
    return buf.getvalue()


def _build_watcher_csv(contacts: dict[str, dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name", "company", "created", "last_open", "total_opens", "id"])
    for cid, p in sorted(contacts.items(),
                          key=lambda kv: kv[1].get("hs_email_last_open_date") or "",
                          reverse=True):
        w.writerow([
            p.get("email", ""),
            f"{p.get('firstname', '')} {p.get('lastname', '')}".strip(),
            p.get("company", ""),
            _epoch_to_iso(p.get("createdate")),
            _epoch_to_iso(p.get("hs_email_last_open_date")),
            p.get("hs_email_open", ""),
            cid,
        ])
    return buf.getvalue()

# ── Telegram ───────────────────────────────────────────────────────────────────
def _tg_get_updates(offset: int | None = None) -> list[dict]:
    params = {"limit": 100, "timeout": 0}
    if offset is not None:
        params["offset"] = offset
    url = f"{TG_BASE}/getUpdates?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read()).get("result", [])


def _tg_send_message(text: str, parse_mode: str = "HTML") -> dict:
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"{TG_BASE}/sendMessage", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read()).get("result", {})


def _tg_send_document(filename: str, csv_data: str, caption: str) -> None:
    boundary = "----PruneBoundary"
    body_parts = []

    def _field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    body_parts.append(_field("chat_id", str(CHAT_ID)))
    body_parts.append(_field("caption", caption))
    body_parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode() + csv_data.encode() + b"\r\n"
    )
    body_parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(body_parts)

    req = urllib.request.Request(
        f"{TG_BASE}/sendDocument", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        print(f"  WARNING: sendDocument failed: {resp}")

# ── scan ───────────────────────────────────────────────────────────────────────
def scan() -> None:
    print("Fetching ghost candidates from HubSpot...")
    raw_ghosts = _fetch_ghosts()
    print(f"  raw ghosts: {len(raw_ghosts)}")

    print("Fetching watcher candidates from HubSpot...")
    raw_watchers = _fetch_watchers()
    print(f"  raw watchers: {len(raw_watchers)}")

    # Apply hard guards
    ghosts:   dict[str, dict] = {}
    watchers: dict[str, dict] = {}
    skipped_g = skipped_w = 0

    for cid, p in raw_ghosts.items():
        reason = _is_protected(p)
        if reason:
            skipped_g += 1
        else:
            ghosts[cid] = p

    for cid, p in raw_watchers.items():
        if cid in ghosts:
            continue  # already classified
        reason = _is_protected(p)
        if reason:
            skipped_w += 1
        else:
            watchers[cid] = p

    print(f"  ghosts after guards: {len(ghosts)} ({skipped_g} protected)")
    print(f"  watchers after guards: {len(watchers)} ({skipped_w} protected)")

    if not ghosts and not watchers:
        print("Nothing to prune. Exiting.")
        return

    # Get current Telegram update offset so execute() can find replies
    updates = _tg_get_updates()
    last_update_id = updates[-1]["update_id"] if updates else 0

    # Send the scan message
    ghost_count   = len(ghosts)
    watcher_count = len(watchers)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    text = (
        f"<b>Contact Pruner — Scan {run_id}</b>\n\n"
        f"<b>Ghosts</b> ({ghost_count}): Free, 90+ days old, never opened, never joined, never converted.\n"
        f"  Action if approved: <b>archived</b> (soft delete, 90d restore window)\n\n"
        f"<b>Watchers</b> ({watcher_count}): Free, 90+ days old, opened emails, but never joined or converted.\n"
        f"  Action if approved: <b>tagged UNQUALIFIED</b> (kept in CRM, excluded from campaigns)\n\n"
        f"Review the attached CSVs.\n"
        f"<b>Reply here with any emails to KEEP</b> (one per line or comma-separated).\n"
        f"Then run:\n<code>python prune.py --execute {run_id}</code>"
    )

    print("Sending scan message to Telegram...")
    msg = _tg_send_message(text)
    message_id = msg.get("message_id")
    print(f"  message_id: {message_id}")

    # Send CSV files
    if ghosts:
        ghost_csv = _build_ghost_csv(ghosts)
        _tg_send_document(f"ghosts_{run_id}.csv", ghost_csv,
                          f"Ghosts ({ghost_count}) — candidates for archive")
        print(f"  sent ghost CSV ({ghost_count} rows)")

    if watchers:
        watcher_csv = _build_watcher_csv(watchers)
        _tg_send_document(f"watchers_{run_id}.csv", watcher_csv,
                          f"Watchers ({watcher_count}) — candidates for UNQUALIFIED tag")
        print(f"  sent watcher CSV ({watcher_count} rows)")

    # Save run state
    run_data = {
        "run_id": run_id,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "message_id": message_id,
        "last_update_id_at_scan": last_update_id,
        "ghost_ids": {cid: p.get("email", "") for cid, p in ghosts.items()},
        "watcher_ids": {cid: p.get("email", "") for cid, p in watchers.items()},
        "counts": {"ghosts": ghost_count, "watchers": watcher_count},
    }
    run_path = OUTPUT / f"scan_{run_id}.json"
    run_path.write_text(json.dumps(run_data, indent=2))
    print(f"\nRun saved: {run_path}")
    print(f"\nReview Telegram, then:\n  python prune.py --execute {run_id}")

# ── execute ────────────────────────────────────────────────────────────────────
def execute(run_id: str) -> None:
    run_path = OUTPUT / f"scan_{run_id}.json"
    if not run_path.exists():
        print(f"ERROR: no scan file found for run_id={run_id}")
        print(f"Expected: {run_path}")
        sys.exit(1)

    run_data = json.loads(run_path.read_text())
    message_id       = run_data["message_id"]
    last_update_id   = run_data["last_update_id_at_scan"]
    ghost_ids: dict  = run_data["ghost_ids"]   # {contact_id: email}
    watcher_ids: dict = run_data["watcher_ids"]

    # Fetch Telegram updates since the scan
    print(f"Checking Telegram for replies to message {message_id}...")
    updates = _tg_get_updates(offset=last_update_id + 1)
    print(f"  {len(updates)} updates since scan")

    # Find replies to our message from our chat
    keep_emails: set[str] = set()
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or {}
        if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
            continue
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to != message_id:
            continue
        text = msg.get("text", "")
        # Parse all email addresses from the reply
        found = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
        for e in found:
            keep_emails.add(e.lower())

    if keep_emails:
        print(f"  Keep emails from your reply: {sorted(keep_emails)}")
    else:
        print("  No keep-emails found in replies (proceeding with full list).")

    # Build final action sets
    ghosts_to_archive:  list[str] = []   # contact IDs
    watchers_to_tag:    list[str] = []

    for cid, email in ghost_ids.items():
        if email.lower() in keep_emails:
            print(f"  KEPT (your reply): {email}")
        else:
            ghosts_to_archive.append(cid)

    for cid, email in watcher_ids.items():
        if email.lower() in keep_emails:
            print(f"  KEPT (your reply): {email}")
        else:
            watchers_to_tag.append(cid)

    print(f"\nFinal: archive {len(ghosts_to_archive)} ghosts, "
          f"tag {len(watchers_to_tag)} watchers as UNQUALIFIED")

    if not ghosts_to_archive and not watchers_to_tag:
        print("Nothing to do.")
        return

    confirm = input("\nProceed? [yes/no]: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    archived = _archive_contacts(ghosts_to_archive)
    tagged   = _tag_disqualified(watchers_to_tag)

    # Send confirmation
    lines = [f"<b>Contact Pruner — Execute {run_id}</b>\n"]
    lines.append(f"Archived (deleted) ghosts: <b>{archived}</b>")
    lines.append(f"Tagged watchers UNQUALIFIED: <b>{tagged}</b>")
    if keep_emails:
        lines.append(f"Kept per your reply: {len(keep_emails)} emails")
    _tg_send_message("\n".join(lines))

    # Save execute log
    log = {
        "run_id": run_id,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "archived": archived,
        "tagged": tagged,
        "kept_emails": sorted(keep_emails),
        "ghost_ids_archived": ghosts_to_archive,
        "watcher_ids_tagged": watchers_to_tag,
    }
    log_path = OUTPUT / f"execute_{run_id}.json"
    log_path.write_text(json.dumps(log, indent=2))
    print(f"\nLog saved: {log_path}")
    print(f"Done. {archived} archived, {tagged} tagged UNQUALIFIED.")


def _archive_contacts(ids: list[str]) -> int:
    if not ids:
        return 0
    url = f"{HS_BASE}/crm/v3/objects/contacts/batch/archive"
    deleted = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        payload = json.dumps({"inputs": [{"id": c} for c in chunk]}).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers=HS_HEADERS, method="POST")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    status = r.status
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                print(f"  ERROR archive chunk: {e.code} {e.read()[:200]}")
                status = e.code
                break
        if status in (200, 204):
            deleted += len(chunk)
            print(f"  archived {deleted}/{len(ids)}")
        time.sleep(0.12)
    return deleted


def _tag_disqualified(ids: list[str]) -> int:
    if not ids:
        return 0
    url = f"{HS_BASE}/crm/v3/objects/contacts/batch/update"
    tagged = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        payload = json.dumps({
            "inputs": [{"id": c, "properties": {"hs_lead_status": "UNQUALIFIED"}}
                       for c in chunk]
        }).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers=HS_HEADERS, method="POST")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    status = r.status
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                print(f"  ERROR tag chunk: {e.code} {e.read()[:200]}")
                status = e.code
                break
        if status in (200, 201):
            tagged += len(chunk)
            print(f"  tagged {tagged}/{len(ids)}")
        time.sleep(0.12)
    return tagged

# ── main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true",
                    help="Identify candidates and send CSVs to Telegram")
    ap.add_argument("--execute", metavar="RUN_ID",
                    help="Read Telegram reply and execute pruning for given run ID")
    args = ap.parse_args()

    if not HUBSPOT_TOKEN:
        sys.exit("ERROR: HUBSPOT_TOKEN not set")
    if not BOT_TOKEN:
        sys.exit("ERROR: TELEGRAM_STATUS_BOT_TOKEN not set")
    if not CHAT_ID:
        sys.exit("ERROR: TELEGRAM_STATUS_CHAT_ID not set")

    if args.scan:
        scan()
    elif args.execute:
        execute(args.execute)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
