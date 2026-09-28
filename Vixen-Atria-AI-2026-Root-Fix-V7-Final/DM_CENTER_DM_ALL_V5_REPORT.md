# Vixen / Atria — DM Center + DM All Operations V5

Release date: 2026-09-28

## Scope

Deep upgrade of the Dashboard DM subsystem: single-recipient DM Center, persistent DM templates, bulk DM All campaigns, dashboard UX, permissions, lifecycle safety, and regression coverage. Existing AI Chat and AI Moderation remain separate subsystems.

## Production changes

### DM Center
- Canonical message model supports plain Discord content + Embed together.
- Server-side validation for Discord content/embed limits, URLs, fields and empty messages.
- Per-recipient variables are rendered server-side: `{user}`, `{mention}`, `{user_id}`, `{username}`, `{display_name}`, `{server}`, `{server_id}`, `{member_count}`, `{moderator}`, `{reason}`, `{warnings}`, `{roles}`, `{is_bot}`, `{timestamp}`, `{date}`, `{time}`, `{server_icon}`, `{bot_avatar}`, `{bot}`, `{primary_color}`, `{accent_color}`.
- Persistent template metadata: description, category, favorite flag.
- Template search/category/favorite filtering, duplicate/delete/favorite/install starter library.
- Starter library expanded to 12 concrete templates across Community, Events, Operations, Moderation and Staff.
- DM history persists both content and embed data and supports status/search filtering.
- One-time preview tokens remain sender + guild + recipient bound. Virtual Dashboard sessions are additionally bound to their staff code.

### DM All / Bulk Campaigns
- New persistent SQLite campaign/job tables with per-recipient item state.
- Exact audience snapshot is created during preview; the start operation cannot silently recalculate the audience.
- Audience modes: all members, selected roles, selected member IDs. Include/exclude role filters are supported. Bots, staff, self and the bot account can be excluded.
- Server Members Intent/cache completeness is verified before bulk preview to prevent partial-audience sends.
- Hard safety cap: 5,000 recipients per campaign.
- Explicit preview + exact count confirmation required before starting a campaign.
- Configurable pacing: 5–60 messages/minute.
- Persistent queue with progress counters: success, failed, cancelled, uncertain.
- Pause, resume and cancel controls.
- Campaign details endpoint returns per-recipient delivery state.
- Campaign controls are creator-bound; real sessions must match the creating user, virtual sessions must match the creating staff code, and the configured owner can manage jobs.
- Shutdown/restart safety: active or queued campaigns are marked `interrupted`; an in-flight send becomes `uncertain` instead of being automatically retried, preventing duplicate DMs.
- Resume is blocked when uncertain sends exist; the operator must review the campaign first.
- Bulk warning-count database lookups are skipped unless `{warnings}` is actually present in the campaign payload.

### Dashboard UX / design
- Dedicated DM All navigation and responsive campaign workspace.
- DM Center KPI/header area, searchable template toolbar, richer composer, real message preview and delivery audit table.
- DM All has audience controls, safety banner, matched-recipient counter, message preview, explicit start action and live campaign queue.
- Preview now shows sample recipient names rather than only opaque IDs.
- Duplicate HTML IDs were removed from the DM pages, including the template category filter and DM All preview control.
- DM All page title is correctly mapped in the dashboard shell.

## Root fixes found during verification

- Fixed `resolve_name()` to tolerate lightweight/non-Discord bot objects instead of crashing DM history responses.
- Fixed event deduplication so real repeated messages are not suppressed when different Discord event IDs are available.
- Fixed `DMBulkManager._variables()` lifecycle bug caused by an invalid static method using instance state.
- Fixed `DMBulkManager.stop()` so completed-task callbacks cannot erase the job ID before the durable `interrupted` transition.
- Added explicit uncertain-send accounting for shutdown/process interruption.
- Added virtual-session code binding for DM preview tokens.
- Added backend validation preventing accidental "all members" behavior when Role/Selected-ID modes are empty.

## Database migrations

Existing `dm_templates` and `dm_history` tables are upgraded non-destructively with missing columns. New `dm_bulk_jobs` and `dm_bulk_items` tables are created with indexes for job/status lookup. Existing data is retained.

## Tests and verification

- Full repository test suite with the available local Discord API test harness: **149 passed, 2 skipped**.
- DM + bulk + root targeted regression suite: **45 passed** before the final test-harness cleanup; the final full run remained green for all exercised application paths.
- DM Dashboard static contract checks: duplicate IDs **0**; required DM routes and controls present.
- Python `py_compile` for changed production modules: PASS.
- `compileall` over `bot/` and `tests/`: PASS.
- Dashboard JavaScript `node --check`: PASS.
- Security static scan: no `shell=True`, `os.system(`, `eval(` or `exec(` in production bot/dashboard paths.
- Release archive integrity: verified with `unzip -t`.

## Environment boundary

The audit environment runs Python 3.13 and does not contain the real `discord.py` 2.7 runtime. The project targets Python 3.12 and keeps the declared production dependency set. The two skipped startup-banner tests are intentionally skipped when the installed `discord` object does not expose the real `discord.py` Bot constructor; they are not treated as live Discord verification.

No live Discord Gateway, Discord REST moderation/DM endpoint, or live Atria provider request was exercised from this environment. Those require the target runtime, valid credentials, Discord intents/permissions, and network access.
