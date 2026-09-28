# Vixen / Atria — 2026 Root Engineering V4

Release date: 2026-09-28

## Scope

This release is a root-cause hardening pass over the existing Vixen/Atria Discord bot + aiohttp Dashboard. It keeps the previously requested AI Chat, Dashboard, economy, staff, loans, and reliability work and does not add the previously excluded expanded AI Moderation feature.

## Root fixes implemented

### AI provider and streaming
- Reworked the shared Atria HTTP session lifecycle with a single creation path, explicit active-request accounting, and deterministic async shutdown.
- Preserved bounded concurrency, adaptive transient recovery, circuit breaking, partial-response preservation, and automatic continuation.
- Removed a nested request-gate deadlock in auto-continuation: continuation now starts only after the outer provider slot is released.
- Continued responses preserve natural boundary whitespace instead of stripping content at chunk boundaries.
- Provider failures can carry partial text so a useful answer is not discarded merely because the final recovery attempt fails.

### AI Chat lifecycle and persistence
- Bounded the in-process context cache with LRU-style eviction.
- Moved retention pruning into service-owned background tasks that consume exceptions and are cancelled during service shutdown.
- Added deterministic external message IDs for persisted AI turns so duplicate/retried Discord events do not create duplicate history rows.
- Preserved short micro-social replies and the language/register routing already present in V4.

### Data integrity
- Existing users now persist newly introduced default fields instead of only receiving them in memory.
- Added a durable `financial_transaction.json` journal for operations that must update both users and loans.
- Loan funding and repayment now commit balance changes and loan-state changes as one coordinated transaction rather than relying on a best-effort rollback across two independent files.
- Recovery is idempotent and can replay the intended snapshot after a process interruption between file replacements.

### Dashboard and configuration integrity
- Dashboard Presence changes, AI Chat settings, and AI Moderation settings now use atomic settings read-modify-write updates.
- Fixed a real functional defect where the Dashboard AI Moderation settings endpoint updated in-memory data and runtime synchronization but did not persist the configuration to disk.
- Preserved owner-only protection for sensitive rank policy and `OWNER_ID`.

### Versioning and documentation
- Updated the active release label to `2026.09.28-root-engineering-v4`.
- Updated README documentation for the new root-engineering guarantees.
- Removed the misleading V3.2 test-count claim from the release payload.

## Verification performed

### Passing verification

- Full pytest run with `--continue-on-collection-errors`: **109 passed**.
- Root-hardening regression suite: **16 passed, 1 skipped**.
- Python AST parse over all `bot/**/*.py`: **0 syntax errors**.
- `python3 -m compileall -q bot tests`: **PASS**.
- `node --check dashboard/assets/app.js`: **PASS**.
- Dangerous construct scan for `shell=True`, `os.system(`, `eval(`, and `exec(` in bot/dashboard/tests: **CLEAN**.
- Legacy user-facing Atria cooldown/countdown/"shorten the message" phrase scan: **CLEAN**.
- Final package excludes Python bytecode, `__pycache__`, `.pytest_cache`, and `.env` secrets.

### Dependency/environment boundary

The execution environment for this audit is Python **3.13.5**, while the project targets Python **3.12**. The environment does not contain the real `discord.py` package and outbound package installation was unavailable.

Because of that boundary:

- 7 test modules cannot be collected because `discord.py` / `discord.app_commands` / `discord.ext` are unavailable.
- 3 additional test failures are the same environment problem surfacing at runtime where the unrelated installed `discord` module lacks real `discord.py` APIs.
- The release does **not** use a Discord compatibility stub to turn those cases into false PASS results.
- No live Discord Gateway test was performed.
- No live Atria account/API test was performed.

The project requirements retain the real runtime dependency declarations; install them in the intended Python 3.12 runtime before production execution.

## Release boundary

All local/static verification above was performed against the actual V4 source tree. External service behavior remains subject to the user's Discord bot token, Discord permissions/intents, Atria credentials, account limits, and network environment.
