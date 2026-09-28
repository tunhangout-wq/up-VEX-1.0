# Vixen Discord EDR Control Center

نظام إدارة Discord فعلي يجمع بين Bot + Dashboard + Economy + Staff + Atria Dawn.

## التشغيل

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

املأ `.env`:

- `DISCORD_TOKEN`
- `OWNER_ID`
- `DASHBOARD_PASSWORD`
- `ATRIA_API_KEY` مضبوط في `.env` لتشغيل Atria Dawn Preview مباشرةً
- `AI_API_KEY` اسم قديم مدعوم للتوافق مع الإصدارات السابقة
- `GUILD_ID` اختياري لتسريع مزامنة Slash Commands لسيرفر محدد

ثم:

```bash
python -m bot.main
```

Dashboard: `http://127.0.0.1:8080` افتراضيًا، أو المنفذ الموجود في `DASHBOARD_PORT` عند التشغيل المحلي.

## Control Center

كل أوامر Control Center التنفيذية متاحة كـ `/slash` وPrefix، وتشمل:

- Server: lockdown/unlockdown, name, icon, verification, slowmode, systemchannel, features
- Channel: create/delete/rename/topic/slowmode/clone/purge/lock/unlock
- Member: timeout/untimeout/kick/ban/unban/deafen/undeafen/mute/unmute/move/dm
- Role: create/delete/add/remove/rename/list
- Security: bans/invites/createinvite/deleteinvite/webhooks/deletewebhooks
- Utility: ping/members/emoji/sticker/event/events/prune/botinfo

الـDashboard يقرأ هذه الأوامر مباشرة من Cog ويعرض معاملات كل أمر ويستدعي نفس callback والصلاحيات، لذلك لا توجد أزرار mock.

## Lockdown

`/server lockdown` يحفظ حالة `@everyone` السابقة لكل قناة ويكمل عند `Forbidden/HTTPException` مع delay صغير لتقليل ضغط Discord. `unlockdown` يستعيد الحالة المحفوظة فقط.

## DM

إرسال DM يحتاج صلاحية Vixen `dm_members` فقط، وليس `moderate_members`. قائمة أعضاء الـDashboard تستخدم pagination بدل حد ثابت 200.

## Staff

`setrank` و`removestaff` يحترمان hierarchy: لا يمكن للعضو منح أو إزالة رتبة مساوية/أعلى من رتبته، والمالك محمي.

## Economy

حد الإضافة اليومي يحسب المبلغ الذي أُضيف فعليًا، مع احترام quota والحدود القصوى للمحفظة والبنك.

## Loans

`/loan` أصبح Hybrid Group: يعمل Slash وPrefix (`!loan request`, `!loan repay`, `!loan my`).

## Atria Dawn

التكامل يستخدم **Atria Dawn Preview مباشرةً** server-side فقط. نقطة الاتصال الرسمية هي `https://api.atria-asi.ai/v1/chat/completions` والموديل هو `Atria-Dawn-Preview` (وحالة الأحرف مهمة). يستخدم الطلب `Authorization: Bearer <ATRIA_API_KEY>`. لا تضع المفتاح في JavaScript أو HTML أو Git.

Dashboard: صفحة Atria AI مع Chat وModeration يدوي وتلقائي قابل للتفعيل.
Bot: `/ai chat`.

يمكن تفعيل moderation من `settings.json` عبر `atria.moderation_enabled`. عند `moderation_mode=all` يفحص الرسائل الجديدة تلقائيًا، وعند `prefix` يفحص الرسائل التي تبدأ بالبادئة المحددة. كما يوجد `/ai moderate` للفحص اليدوي.

## حالة التشغيل على Replit

الـWorkflow يشغل البوت والـDashboard في عملية واحدة. إذا لم يتم ضبط `DISCORD_TOKEN` بعد، يبقى الـDashboard متاحًا في وضع الإعداد بدل أن تنتهي العملية؛ لا يمكن تنفيذ أوامر Discord أو تسجيل دخول المالك حتى تتم إضافة `DISCORD_TOKEN` و`OWNER_ID`.

## V4 Root Engineering

- AI provider requests use bounded concurrency, adaptive transient recovery, circuit breaking, safe continuation, and deterministic shutdown.
- AI Chat context caching is bounded and its background maintenance tasks are owned and cancelled cleanly during shutdown.
- Discord AI turn persistence uses deterministic external IDs so retried/duplicated events do not create duplicate turns.
- User and loan state updates use atomic read-modify-write operations; loan funding and repayment now commit balances and loan state through a durable journal that can recover after an interrupted multi-file write.
- Dashboard Presence, AI Chat settings, and AI Moderation settings use atomic settings updates; AI Moderation configuration is persisted before runtime synchronization.
- Owner identity is sourced from `OWNER_ID` and protected from mutable dashboard rank settings.

## التنظيف والاختبار

النسخة النهائية لا تحتوي `__pycache__`, `*.pyc`, `.pytest_cache`, أو ملفات مؤقتة.

قبل التشغيل:

```bash
python -m compileall -q bot
```

ثم شغّل البوت واختبر Slash sync والـDashboard ضد سيرفر Discord فعلي.

## Dashboard command catalog
الـCommand Center يكتشف كل leaf commands من جميع الـCogs (وليس Control فقط) ويعرض المعاملات وينفذ الـcallback الحقيقي مع فحص Vixen permissions. المصدر الحالي يحتوي 117 leaf commands.

## Final Control Center / AI notes
- Dashboard login supports Owner credentials and one-use Vixen staff codes.
- Unbound staff-code sessions use a negative, isolated Dashboard principal; they are never treated as the Discord guild owner.
- Command Center discovers executable leaf commands from all loaded Cogs, not only the Control cog.
- Discord application-command metadata is exposed to the dashboard, including choices; the dashboard renders selects for choices and converts Discord objects such as Member/Role/Channel before invoking the same Cog callback.
- Atria Chat and Atria Moderation use `ATRIA_API_KEY` server-side only (`AI_API_KEY` remains a legacy fallback). AI Chat has no Vixen-side RPM/cooldown limiter; automated moderation keeps its separate safety action quota.
- Live Discord Gateway/API validation still requires running the project with a real Discord token, guild ID, and bot permissions.


### AI credentials
ضع مفتاح Atria الجديد في `ATRIA_API_KEY` داخل `.env` المحلي فقط. لا تضع secrets في `.env.example` أو HTML أو JavaScript أو Git. يظل `AI_API_KEY` مدعومًا كاسم قديم للتوافق.
## Discord AI message access

AI Chat وAI Moderation يعتمدان على قراءة محتوى الرسائل. في Discord Developer Portal افتح التطبيق → Bot → Privileged Gateway Intents، ثم فعّل **Message Content Intent** واحفظ التغيير. المشروع يطلب هذا الـIntent برمجيًا، لكن Discord يتطلب تفعيله أيضًا من لوحة التطبيق.

من Dashboard: AI Moderation → اختر القنوات وأنواع الكشف والإجراءات → حفظ الإعدادات. ولـAI Chat: اختر القنوات → فعّل AI Chat → اجعل Response Mode = Every Message أو Mention Only حسب السلوك المطلوب → حفظ. الإعدادات تُحفظ في `bot/data/settings.json` داخل قسم `ai` وتُقرأ مباشرة عند وصول الرسالة.


## Atria AI Chat Ultra

AI Chat uses `Atria-Dawn-Preview` with live SSE streaming in Discord and Dashboard. The Chat context is configurable up to 100 stored messages, with relevance selection and per-user context. The response ceiling is set to Atria’s documented per-request maximum of 65,536 output tokens; a larger account quota is consumed across many requests, not as one 100-million-token response. Owner natural-language actions are enabled only for the exact configured `OWNER_ID`, and every action is revalidated against Discord permissions and bot role hierarchy before execution.

## Atria authentication troubleshooting

`ATRIA_API_KEY` is the primary server-side credential and `AI_API_KEY` is only a legacy fallback. The runtime trims `ATRIA_API_BASE_URL` and `ATRIA_MODEL`, and removes accidental whitespace/newlines from pasted API keys before constructing the Bearer header. The Dashboard provider status exposes only the environment-variable name used for the key, never the key itself. A `401` from Atria indicates an invalid or revoked API key; create a fresh key in the Atria console and keep it only in the server environment.

## AI Moderation V4 Deep Performance Layer

AI Moderation now uses a two-stage low-latency pipeline:

1. **Local Fast Path** — deterministic detection for message floods, repeated messages, mass mentions, and obvious character spam. High-confidence signals can intervene without waiting for the AI provider.
2. **Semantic AI Path** — contextual cases go through Atria with a bounded timeout and an eight-request moderation concurrency guard, keeping moderation responsive without flooding the provider.

Additional safeguards include Unicode/zero-width normalization, per-user deduplication, bounded in-memory state, configurable thresholds, stricter AI-ban confidence gating, and Dashboard controls for Fast Path, AI timeout, detection window, repeat/flood/mention thresholds.


## AI Owner Command Hardening V6

Natural-language Discord admin commands now have a conservative deterministic fast path for verified owners, in addition to the existing model-generated action-marker path. Core member actions recognized locally include `kick`, `ban`, `timeout`/`mute`, `warn`, `untimeout`, and `unban`, with Discord mentions or numeric IDs as explicit targets and timeout duration parsing for seconds/minutes/hours/days. Questions, technical discussion, examples, quotations, and hypothetical wording are blocked from direct execution.

The model-action path also accepts an action-only `<VIXEN_ACTION>` response; it no longer requires a visible prose answer before the real application executor can run. Reporting failures after an action no longer fall back into the model path, preventing accidental duplicate execution. Owner verification, Discord permissions, role hierarchy, target resolution, and audit logging remain application-side gates.

## DM Center + DM All Operations V5

The Dashboard now includes a production-oriented DM Center and persistent DM All campaign system. Single DMs support content + Embed templates, server-side variables, preview-bound one-time sends and delivery history. DM All uses an exact preview-time audience snapshot, role/ID targeting, safety exclusions, 5,000-recipient cap, 5–60 messages/minute pacing, persistent per-recipient status, pause/resume/cancel controls, creator-bound authorization, and uncertain-send protection across shutdown/restart. The dashboard includes responsive composer, template search/favorites/categories, concrete preview samples, campaign progress and audit views.
