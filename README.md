# Railway Telegram Referral Bot with Device-Risk Protection

This package runs a Telegram bot and its Mini App verification page in **one Railway service**. The Mini App validates Telegram's signed `initData` on the server, stores only hashed anti-fraud signals, and prevents a referral from being credited when the same stored Mini App binding is reused by a different Telegram account.

> This is an anti-fraud **risk control**, not a guaranteed physical-device detector. A determined user can reset browser storage, use another browser, or use another device. Do not claim that it detects rooted phones or every multiple-account attempt.

## What the code blocks

| Situation | Result |
|---|---|
| Same phone number on two Telegram accounts | Blocked by the existing phone rule. |
| Same Mini App storage binding used by two Telegram accounts | Second account can use the bot, but its referral is **not credited** when the admin rule is ON. |
| Unique verified binding and valid phone/captcha/channel checks | Referral can be credited. |
| User clears Telegram WebView/browser storage or changes browser/device | A new binding is created; this cannot be reliably treated as the same physical device. |

## 1. Put files on GitHub

Create a new private GitHub repository. Upload **all files and folders from this package**, including `bot.py`, `web/verify.html`, `requirements.txt`, `Procfile`, and `railway.toml`. Do not upload a real `.env` file or the `bot.db` database.

## 2. Create the Railway service

Open Railway, create a new project, choose **Deploy from GitHub repo**, and select the repository. Railway will install packages from `requirements.txt` and start the single web process using `python bot.py`.

Add a Railway Volume and mount it at `/data`. The bot database uses `/data/bot.db`; without this Volume, your users, referral counts, and settings may disappear after a redeploy.

## 3. Generate a public domain

In the Railway service, open **Settings → Networking → Public Networking → Generate Domain**. Copy the resulting HTTPS address, for example:

```text
https://your-service.up.railway.app
```

## 4. Add Railway variables

In Railway **Variables**, add the following values. Railway supplies `PORT` automatically, so do not add it unless you have a specific reason.

```env
BOT_TOKEN=your_bot_token_from_botfather
ADMIN_IDS=your_numeric_telegram_id
DB_PATH=/data/bot.db
PUBLIC_BASE_URL=https://your-service.up.railway.app
DEVICE_BINDING_SECRET=use_a_long_random_secret_at_least_32_characters
```

`DEVICE_VERIFY_WEBAPP_URL` is optional. Leave it empty to use `PUBLIC_BASE_URL/verify`. If you use a custom external verification domain, set it to that exact HTTPS URL instead.

After saving Variables, deploy again. Open this URL in a browser to confirm the server is live:

```text
https://your-service.up.railway.app/health
```

It should return JSON containing `"ok": true`.

## 5. Enable it from Telegram admin panel

Open the bot and send `/admin`, then go to **Verification**. Turn on **Device** and ensure **Same-device referral: BLOCK** is enabled. The same page lets you edit the user-facing device verification message.

The user opens the Telegram Mini App at `/verify`. The page calls `/api/verify`, which validates the signed Telegram session on the Railway server. Only then does it send the completion event to the bot. Referral credit is decided again inside one SQLite transaction, so a client-side page cannot simply send `verified=true` to create a reward.

## Important operating limits

The device verification feature applies to the **master bot** in this package. It is deliberately disabled for clone subprocesses because every clone has a separate Telegram token and cannot safely use the master verification endpoint. Use the feature on a single master bot unless you build a token-isolated endpoint per clone.

The feature stores hashes of an anonymous browser binding, IP address, and user-agent only; it does not store their raw values. Display a privacy notice to users and keep your retention period limited according to your applicable privacy obligations.

## Local checks

Run the following from the package directory before pushing to GitHub:

```bash
pip install -r requirements.txt
python test_antifraud.py
```

The test proves that a second Telegram account using the same binding does not add to the referrer's count, while a distinct verified binding does.
