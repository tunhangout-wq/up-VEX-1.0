# Vixen Discord EDR

## التشغيل

- الـWorkflow الرئيسي: `DASHBOARD_HOST=0.0.0.0 DASHBOARD_PORT=5000 python -m bot.main`
- تثبيت الاعتماديات: `pip install -r requirements.txt`
- فحص Python: `python -m compileall -q bot`

## الإعداد المطلوب

انسخ `.env.example` إلى `.env` واضبط `DISCORD_TOKEN` و`OWNER_ID` و`DASHBOARD_PASSWORD`. يمكن ترك `GUILD_ID` اختياريًا. لا ترفع `.env` ولا تضع مفاتيح Atria داخل JavaScript أو HTML.

إذا كان `DISCORD_TOKEN` ناقصًا، يشغّل التطبيق الـDashboard في وضع الإعداد فقط ويعرض الواجهة، لكن الاتصال بـDiscord وتنفيذ الأوامر وتسجيل دخول المالك يحتاجان التوكن وآيدي المالك.

## البنية

- `bot/main.py`: تحميل الـCogs وتشغيل Discord والـDashboard في نفس العملية.
- `bot/cogs/`: أوامر البوت الحقيقية، وتظهر تلقائيًا في مركز الأوامر.
- `bot/web/server.py`: API المصادق عليه وتنفيذ الأوامر من الـDashboard.
- `dashboard/`: الواجهة العربية RTL.