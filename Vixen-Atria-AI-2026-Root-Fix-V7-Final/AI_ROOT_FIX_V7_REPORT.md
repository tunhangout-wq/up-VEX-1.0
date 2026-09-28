# Vixen / Atria AI 2026 — Root Fix V7 Report

Date: 2026-09-28

## Scope

Root-level fixes for three areas: response truncation/duplication, moderation correctness/performance, and AI chat quality. The implementation preserves the existing project structure and existing setting names; new behavior is introduced through defaults and additive dashboard settings.

## A — Response truncation and duplication

### `bot/services/ai_provider.py`

**Root cause:** streamed output could be treated as complete after the SSE body ended without a terminal marker, and transient transport failures after visible deltas could replay the original request. Continuation also had only a single recovery round and no robust overlap removal.

**Fix:** added explicit `complete`/terminal-state handling for `[DONE]`, `stop`, and `end_turn`; missing terminal state after text now enters continuation. Transport errors after visible text never replay the original request. Continuation is up to four rounds, each using `auto_continue=False`, `CONTINUATION_MAX_OUTPUT_TOKENS=8192`, the accumulated text as an assistant turn, and overlap-aware merging. Failed recovery raises `AIProviderError` with `partial_text` and the user-facing incomplete-response notice. The same terminal/continuation contract is applied to `complete()`.

**Regression tests:** `tests/test_ai_truncation_root.py`; provider concurrency regression tests in `tests/test_root_hardening_v4.py`.

### `bot/services/ai_orchestrator.py` / `bot/services/ai_intent.py`

**Root cause:** routing caps could lower output-token budgets for short/greeting routes even when the configured profile was larger.

**Fix:** profile budgets are flash=6000, balanced=10000, deep=16000. Greeting/thanks have a floor of 300 tokens and casual has a floor of 800; route hints no longer become a hidden hard cap.

**Regression:** updated contract checks in `tests/test_ai_root_hardening_2026.py` and the AI provider/streaming suites.

### `bot/services/ai_chat.py`

**Root cause:** code-fence cleaning corrupted valid code, streaming updates were too frequent, and exception paths could expose partial output as if it were final.

**Fix:** `_clean_answer` removes only `<think>`/`<analysis>` blocks, including an unclosed block, and preserves Markdown code fences. `_split_discord` uses a 1900-character ceiling, semantic boundaries, and balanced code fences without losing characters. Discord streaming edits are throttled to at least 1.2 seconds and HTTP 429 `retry_after` is respected. The final response is synchronized after streaming. Partial failures are marked incomplete, stored per `(channel,user)`, and continuation requests use the stored incomplete answer.

**Regression:** `tests/test_ai_truncation_root.py` verifies code preservation and exact 6000-character reconstruction.

## B — AI quality

### `bot/services/ai_quality.py`

**Root cause:** `simple_prompt_overlong` and `truncated_tail` produced false positives outside social chat.

**Fix:** simple-response length checks apply only to greeting/thanks/casual intents. Tail truncation requires either provider `finish_reason=length` or an explicit recovery path, plus the no-punctuation/connective-ending signal.

### `bot/services/ai_chat.py` — quality repair and system prompt

**Root cause:** quality repair used a fixed `max_tokens` budget that could shorten long valid answers, while the system prompt mixed contradictory length and style directives and allowed untrusted metadata to be inserted too freely.

**Fix:** quality repair is skipped above 7000 input characters; otherwise its token budget scales to at least 1.3× the original answer length, with a 1500-token minimum. Repairs under 70% of the original are rejected. The system prompt is organized into Identity, Language, Context Safety, Length Policy, Safety, and Owner Actions. User-controlled names/server/channel/custom fields are sanitized for newlines and tool markers. Previous conversation remains quoted context rather than instructions.

**Regression:** `tests/test_ai_truncation_root.py`, `tests/test_ai_quality.py`, `tests/test_ai_fast_profile.py`, `tests/test_ai_flash_language_quality.py`.

## C — Moderation

### `bot/services/ai_moderation_engine.py`

**Root cause:** character-spam detection treated normal laughter as spam, flood/repeat thresholds were too aggressive, state cleanup could remove active keys, and several local abuse signals were missing.

**Fix:** laughter/emote patterns are exempted; character spam requires meaningful length and a dominant-character ratio. Defaults are repeat=5 and flood=8 weighted messages per 8 seconds, with very short messages discounted. In-memory buckets and dedup state use a 10-minute TTL and active-key eviction by oldest timestamp. Added configurable local signals for Discord invites, suspicious/shortened links, unauthorized @everyone/@here, long content, attachment/filename abuse, and join-burst raid detection.

**Regression:** `tests/test_root_fix_v7.py`.

### `bot/services/moderation_tools.py`

**Root cause:** moderation actions were not consistently protected by a single trusted-user gate, message cleanup/audit was incomplete, and escalation was not centralized.

**Fix:** owner/server-owner/OWNER_ID/staff/moderation-permission users and configurable whitelist users/roles are skipped with `skipped_trusted`. Spam/flood/scam cleanup deletes the offending message and recent same-author messages when Manage Messages is available, and logs the deletion. A persistent escalation counter implements first=warning, second=short timeout, third=long timeout. Auto-ban remains opt-in and requires confidence >=0.98. Persistent moderation logs use `mod_log_channel` when configured.

**Regression:** `tests/test_root_fix_v7.py`.

### `bot/cogs/atria.py`

**Root cause:** semantic moderation timeout was too short for contextual classification, JSON output was insufficiently constrained, edits were not rechecked, empty-text messages with attachments/embeds/stickers were under-inspected, and moderation/chat could race.

**Fix:** default semantic moderation timeout is 6 seconds and the timeout metric is exposed. Classifier input is placed inside `<message>` delimiters and treated strictly as data. JSON shape/types/enums are validated; malformed or injected output results in allow. `on_message_edit` reuses moderation inspection. Attachment/embed/sticker signals are inspected even when text is empty, and message content is evaluated up to 4000 characters. Moderation runs before chat for the same event; an actioned message does not receive an AI chat reply. `/ai moderate` is permission-gated; `/ai chat` has a user cooldown; verdicts use Discord splitting rather than fixed slicing.

**Regression:** `tests/test_root_fix_v7.py`.

### Persistence/config/dashboard

`bot/utils/ai_store.py` gained persistent recent-moderation action counting for escalation. `bot/utils/data_manager.py` adds new moderation defaults without renaming existing settings. `bot/web/server.py`, `dashboard/index.html`, and `dashboard/assets/app.js` expose and persist the new moderation controls and metrics.

## Test evidence

### Targeted root-fix suite

`python -m pytest -q tests/test_ai_truncation_root.py tests/test_root_fix_v7.py tests/test_root_hardening_v4.py tests/test_ai_provider.py tests/test_ai_streaming.py tests/test_ai_quality.py tests/test_ai_2026_upgrade.py -o addopts=''`

Result: **65 passed, 1 skipped**.

The skipped test is explicitly conditioned on the absence of the real `discord.py` package in this analysis runtime.

### Exact requested commands

`python -m compileall bot tests` — **PASS** (exit code 0).

`python -m pytest -q` — **BLOCKED in this runtime during test collection**, because the environment has no real `discord.py` package. Eight integration modules fail to import `discord`, `discord.ext`, or `discord.app_commands`. This is an environment/dependency boundary, not a test failure produced by the V7 changes. No compatibility stub was installed into the production tree to manufacture a green result.

### Additional checks

- `node --check dashboard/assets/app.js` — PASS.
- Dangerous construct scan (`shell=True`, `os.system(`, `eval(`, `exec(`) — CLEAN.
- Dashboard duplicate HTML IDs — 0.
- Final response split checks preserve all characters for 6000-character input and keep chunks <=1900.

## Baseline / no-fake validation note

The A response-truncation regressions were executed against the pre-fix behavior and initially produced failures before the production changes were applied. The new B/C root regression tests validate the new contracts after implementation; their original pre-fix behavior was not re-run across every case in this dependency-limited environment, so no false claim of full before/after execution is made.

## Files changed

### Production

- `bot/services/ai_provider.py`
- `bot/services/ai_chat.py`
- `bot/services/ai_orchestrator.py`
- `bot/services/ai_intent.py`
- `bot/services/ai_quality.py`
- `bot/services/ai_moderation_engine.py`
- `bot/services/moderation_tools.py`
- `bot/cogs/atria.py`
- `bot/utils/ai_store.py`
- `bot/utils/data_manager.py`
- `bot/web/server.py`
- `dashboard/assets/app.js`
- `dashboard/index.html`

### Tests / contracts

- `tests/test_ai_truncation_root.py` (new)
- `tests/test_root_fix_v7.py` (new)
- `tests/test_root_hardening_v4.py`
- `tests/test_ai_provider.py`
- `tests/test_ai_streaming.py`
- `tests/test_ai_quality.py`
- `tests/test_ai_2026_upgrade.py`
- `tests/test_ai_root_hardening_2026.py`
- `tests/test_ai_fast_profile.py`
- `tests/test_ai_flash_language_quality.py`
- `tests/test_ai_ultra_v3.py`

## Design decisions

1. Never replay the original prompt after visible streaming text. Recovery always continues from the accumulated assistant text.
2. A successful non-SSE continuation body without a finish reason is treated as terminal for backwards-compatible transports; explicit `length` continues.
3. Moderation is ordered before chat on the same Discord event to avoid answering a message after taking action on it.
4. High-confidence local moderation can act without an external AI round trip, while contextual classification uses the provider with a bounded six-second timeout.
5. Automatic bans remain opt-in and confidence-gated at 0.98.
6. No production compatibility stub, placeholder provider, or fake Discord integration was added.

## External integration boundary

No live Discord Gateway/API action and no live Atria account/API request was performed in this runtime. Production verification still requires the target Python 3.12 environment with the declared dependencies and real credentials.
