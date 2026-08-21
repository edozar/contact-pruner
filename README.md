# Contact Pruner

`ADM · Ops Control · Contact Pruner`

Two-phase, Telegram-approved CRM hygiene. Identifies unresponsive HubSpot contacts and either archives them or tags them UNQUALIFIED — nothing happens until you approve via Telegram reply.

## Two candidate groups

| Group | Criteria | Action |
|-------|----------|--------|
| **Ghosts** | `subscription_level=Free`, 90+ days old, never opened a marketing email, never filled a form, never logged into member area | HubSpot soft-archive (90-day restore window) |
| **Watchers** | `subscription_level=Free`, 90+ days old, opened emails but never converted (no form fill, no member login) | Set `hs_lead_status=UNQUALIFIED` |

Hard guards always applied — skip contacts with: paid subscription (`$1.00`/`Fee`), protected lifecycle stage (customer/opportunity/SQL/MQL), active member access, advisor verified, any associated deal, sales email replied, or already UNQUALIFIED.

## Usage

**Phase 1 — scan:**
```bash
cd ~/contact-pruner
python3 prune.py --scan
```
Sends two CSVs to `@IPO_CLUB_Status_bot`. Reply to that Telegram message with any emails you want to keep (one per line or comma-separated).

**Phase 2 — execute:**
```bash
python3 prune.py --execute <run-id>
```
Reads your Telegram reply, shows final counts, asks for confirmation, then executes. Run state is in `output/scan_<run-id>.json`; execute log in `output/execute_<run-id>.json`.

## Configuration

`.env` (not committed):
```
HUBSPOT_TOKEN=pat-eu1-...
TELEGRAM_STATUS_BOT_TOKEN=...
TELEGRAM_STATUS_CHAT_ID=...
```

No launchd. Manual trigger only — run quarterly or before large campaigns.

## Notes

- Archived contacts disappear from all HubSpot views and exports automatically.
- UNQUALIFIED contacts: add `Lead status is not equal to Unqualified` to any HubSpot view used for campaign exports.
- `squarespace_user_id NOT_HAS_PROPERTY` returns 0 results — all Free contacts have a Squarespace user ID. `last_member_page_login` is the reliable member-area activity signal.
