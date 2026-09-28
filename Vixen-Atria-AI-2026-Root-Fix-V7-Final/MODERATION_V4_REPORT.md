# Vixen Atria — AI Moderation V4 Deep Performance Report

## Scope
Deep root-level upgrade of AI Moderation only. AI Chat remains independent.

## Implemented
- Stateful local moderation engine with bounded memory.
- Unicode NFKC normalization and zero-width character removal.
- Per-user message-window analysis.
- Repeated-message detection.
- Flood detection.
- Mass-mention detection.
- Obvious character-spam detection.
- Short-window duplicate-event suppression.
- High-confidence Fast Path that can intervene without an external AI round trip.
- Semantic Atria moderation path for contextual/linguistic cases.
- Moderation AI concurrency guard (8 in-flight requests).
- Bounded provider timeout (configurable, default 2.5 seconds).
- Stricter AI-ban gate: explicit opt-in plus 0.98 confidence minimum.
- Dashboard controls for Fast Path, AI timeout, detection window, repeat threshold, flood threshold, and mention threshold.
- Existing policy/tool/hierarchy/permission/action-rate safeguards retained.
- Existing SQLite moderation audit retained.

## Why this is a root fix
The old design sent every eligible message directly to the provider. That made obvious floods depend on provider latency and could amplify provider pressure during an attack. V4 moves deterministic high-signal work to a local fast path and reserves AI capacity for semantic decisions.

## Verification
- `py_compile`: PASS
- Dashboard JavaScript syntax (`node --check`): PASS
- New moderation engine + V4 hardening tests: **21 passed, 1 skipped**
- Full suite: collection is blocked by the current execution environment's fake/incomplete `discord` module (`discord.ext` / `discord.app_commands` unavailable). These failures are recorded rather than masked.

## Live integration limitation
Real Discord Gateway, Discord moderation endpoints, and live Atria provider behavior were not exercised in this environment. The release does not claim those as live-tested.
