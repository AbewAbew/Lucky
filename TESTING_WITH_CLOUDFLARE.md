# Testing Lucky Bingo with Telegram and Cloudflare

Lucky testing uses two long-running processes:

1. The FastAPI web server runs the Mini App, the game engine, and the Telegram
   bot (as a webhook handler at `/telegram/webhook`) together on port 8000.
2. Cloudflare Tunnel gives the local web server a temporary public HTTPS address.

Keep each process open in its own terminal. Press `Ctrl+C` in its terminal to stop it.

## WSL or Linux — one command

Open a terminal in the project folder:

```bash
cd /home/solskjaer/bingo
```

Then run:

```bash
./start-testing.sh
```

This single launcher:

- downloads `cloudflared` into the persistent project folder `.tools/` if it is missing;
- starts the FastAPI web/game server;
- starts a Cloudflare Quick Tunnel and reads its fresh HTTPS address;
- changes only `PUBLIC_URL` in `.env`;
- restarts the web server so it reads that address, which also re-registers the
  Telegram webhook and updates Telegram's menu button;
- writes service logs to `.run/`.

Keep that terminal open. Press `Ctrl+C` once to stop both services. On the
first run, internet access is required to download `cloudflared`. Later runs reuse
the copy at `.tools/cloudflared`; they do not depend on `/tmp` or a global install.

Every launch creates a new temporary address. After the launcher prints
`Lucky is ready`, send both commands to the bot again:

```bash
/start
/admin
```

Use the newly generated buttons. Telegram cannot change a button inside an older
message, so an old button can still point to an expired address.

If WSL says `Permission denied` the first time, run this once and retry:

```bash
chmod +x start-testing.sh
```

### Opening the admin board

Open the admin board through the bot's new `/admin` button. A normal browser tab
does not contain Telegram's signed login information and is denied intentionally.
To use local browser-only development, temporarily set `ALLOW_DEV_AUTH=true` and
open `http://127.0.0.1:8000/admin`; do not enable that setting on a public app.

### Manual commands, if troubleshooting

The one-command launcher is recommended. Its equivalent components are:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.tools/cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate
```

When using the manual method, copy the generated tunnel address into `PUBLIC_URL`
in `.env`, then restart the web process so it reads the new address and
re-registers the Telegram webhook. The automatic launcher handles that ordering
for you.

## Native Windows (not WSL)

`start.bat` starts the web server, which also runs the Telegram bot. Cloudflare
remains the second process.

1. Close any old Lucky web-server windows.
2. Double-click `start.bat`.
3. Open another Command Prompt in the project folder.
4. Start the tunnel:

```bat
cloudflared.exe tunnel --url http://127.0.0.1:8000
```

5. Copy the generated HTTPS address into `PUBLIC_URL` in `.env`.
6. Close and reopen `start.bat` so the web app reads the new address and
   re-registers the Telegram webhook.
7. Send `/start` to the Telegram bot again.

## Quick checks

- Local health: `http://127.0.0.1:8000/health` should show `{"status":"ok"}`.
- Public health: `https://your-address.trycloudflare.com/health` should show the same result.
- Player Mini App: send `/start`, then tap **Play Lucky**.
- Admin Mini App: send `/admin`, then tap **Lucky Admin**.
- If Telegram opens an old page, close the Mini App completely and use the newest bot button.

## Common problems

### Cloudflare says the origin refused the connection

The web server in Terminal 1 is not running on `127.0.0.1:8000`. Start it, then retry the public address.

### `/tmp/cloudflared` or `cloudflared` is missing

Run `./start-testing.sh`. It installs a persistent copy at
`.tools/cloudflared`, so `/tmp/cloudflared` and a global `cloudflared` command are
not required.

### The admin page says Access denied

Send `/admin` to the bot and use the newest **Lucky Admin** button. Opening the
public `/admin` URL directly in another browser has no Telegram signature and is
correctly rejected.

### Telegram opens an expired tunnel

Update `PUBLIC_URL`, restart the web process, and send `/start` again. A button inside an old Telegram message does not update itself.

### The bot does not answer

Check `.run/web.log` (or the terminal running `start.bat`) for `Telegram webhook
registered` — if it instead shows `Could not configure Telegram bot`, `PUBLIC_URL`
is not a reachable `https://` address yet, or `TELEGRAM_WEBHOOK_SECRET` /
`BOT_TOKEN` is missing or wrong. Only one running web process should hold the
webhook for this bot token; if you also ran `setWebhook` manually elsewhere or
started a second copy of the app against the same `BOT_TOKEN`, Telegram will only
deliver updates to whichever `setWebhook` call happened last.

### Testing balance does not decrease

`ENABLE_REAL_MONEY=false` intentionally prevents cartela debits and payouts. Keep it disabled until all three administrator IDs and production safeguards are ready.

Cloudflare Quick Tunnels are intended for development and testing, not permanent production hosting. See the [official Cloudflare Quick Tunnel documentation](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).
