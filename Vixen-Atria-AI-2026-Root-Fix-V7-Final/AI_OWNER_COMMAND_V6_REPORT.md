# Vixen / Atria — AI Owner Command Hardening V6

Date: 2026-09-28

## Scope

This pass targets the natural-language AI command path in the Discord bot. The goal is to make direct admin requests such as `timeout`, `kick`, `ban`, `warn`, `mute`, `untimeout`, and `unban` reliable while preventing questions, examples, quotations, technical discussion, and hypothetical text from becoming executable actions.

## Root problems found

1. The intent router only recognized a narrow subset of action wording and could miss several Arabic/Tunisian/English forms.
2. The direct action executor had no deterministic parser for clearly unambiguous owner commands, so simple commands unnecessarily depended on a model response and its private marker formatting.
3. A model response containing only `<VIXEN_ACTION>...</VIXEN_ACTION>` could be rejected as "no visible answer" before the action executor ran.
4. Quoted/example text such as `I wrote "kick @user" as an example` could be classified as an action because the action pattern was found inside the quoted text.
5. A direct action path that successfully executed an action but failed while sending its result could return `False` and fall back into the normal AI generation path.

## Implemented fixes

### Intent routing

- Expanded high-confidence action recognition across English, Arabic, Tunisian and common Discord wording.
- Added explicit question guards so `how do I kick...`, `كيفاش نعمل kick...؟`, and `شنوة command متاع timeout؟` stay conversational/question routes.
- Added non-execution guards for examples, quotations, and hypothetical wording.
- Kept technical text such as `timeout في aiohttp` away from `owner_action` routing.

### Deterministic owner command fast path

- Added a conservative direct parser used only for the verified OWNER_ID.
- Requires an explicit Discord member mention or a numeric user ID for member moderation actions.
- Supports `kick`, `ban`, `timeout`, `mute`, `warn`, `untimeout`, and `unban`.
- Maps `mute` to Discord's timeout action because that is the actual supported executor operation.
- Parses timeout duration values expressed in seconds/minutes/hours/days and clamps them to Discord's supported application range.
- Extracts a short explicit reason after `because`, `for reason`, `بسبب`, or `على خاطر`.
- The parser returns `None` instead of guessing when the target is ambiguous.

### Model action-marker path

- Pure action-marker responses are now valid even when their visible text is empty.
- Streaming no longer creates a fake `...` answer solely because the model returned an action marker.
- Action reporting failures do not re-enter the model path after execution, preventing duplicate action attempts.

### Security and execution gates preserved

- Verified Owner check remains application-side.
- Discord permission checks remain application-side.
- Target member hierarchy checks remain application-side.
- Role/channel protections and audit logging remain application-side.
- The model cannot directly bypass these application checks.

## Tests executed

### AI command regression suite

`tests/test_ai_owner_commands.py`: **7 passed**

Coverage includes:
- action routing for kick/ban/timeout/warn/untimeout/mute/unban
- Arabic/Tunisian forms
- timeout duration parsing
- question rejection
- example/quotation/hypothetical rejection
- technical timeout text rejection
- pure model action-marker parsing
- no-fallback behavior when an executed action's result message fails

### Related AI regression suite

Combined run:

`tests/test_ai_chat_ultimate_quality.py tests/test_ai_owner_commands.py tests/test_ai_root_hardening_2026.py tests/test_ai_contracts.py tests/test_ai_2026_upgrade.py`

Result: **40 passed**

### Static/runtime checks

- `python -m compileall -q bot tests`: PASS
- `node --check dashboard/assets/app.js`: PASS
- Legacy Atria countdown/short-message scan: CLEAN
- `shell=True` / `os.system` / `eval` / `exec` scan: CLEAN

## Full-suite environment boundary

A full repository run in this analysis environment currently reports:

- **131 passed**
- **5 failed**
- **2 skipped**
- **8 collection errors**

The collection/runtime failures are environment/test-harness related: this runtime does not contain the real `discord.py` package and some tests share a minimal `discord` import stub. Several failures therefore surface as missing `app_commands`, `Forbidden`, or other real Discord APIs. These are not represented as successful live Discord tests.

No live Discord Gateway, Discord moderation REST request, or live Atria provider request was executed here.

## Production behavior expectation

In the intended Python 3.12 + declared `discord.py` runtime, direct owner commands can now be handled without an unnecessary Atria round-trip when the syntax is explicit and the target is unambiguous. Ambiguous or non-command language continues through the normal AI conversation path instead of being executed locally.
