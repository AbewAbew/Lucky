#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

RUN_DIR="$PROJECT_DIR/.run"
TOOLS_DIR="$PROJECT_DIR/.tools"
PYTHON="$PROJECT_DIR/.venv/bin/python"
CLOUDFLARED="$TOOLS_DIR/cloudflared"
LOCAL_URL="http://127.0.0.1:8000"

mkdir -p "$RUN_DIR" "$TOOLS_DIR"

WEB_PID=""
TUNNEL_PID=""

stop_recorded_process() {
  local name="$1"
  local pid_file="$RUN_DIR/$name.pid"
  local pid=""

  [[ -f "$pid_file" ]] || return 0
  pid="$(tr -dc '0-9' < "$pid_file")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    # Only stop a recorded process that was launched from this project.
    local process_dir=""
    process_dir="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    if [[ "$process_dir" == "$PROJECT_DIR" ]]; then
      kill "$pid" 2>/dev/null || true
      for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
      done
    fi
  fi
  rm -f "$pid_file"
}

cleanup() {
  trap - EXIT INT TERM
  echo
  echo "Stopping Lucky testing services..."
  for pid in "$TUNNEL_PID" "$WEB_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  rm -f "$RUN_DIR/web.pid" "$RUN_DIR/tunnel.pid"
}
trap cleanup EXIT INT TERM

for service in tunnel web; do
  stop_recorded_process "$service"
done

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add BOT_TOKEN and the account settings, then run this command again."
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

if ! "$PYTHON" -c "import fastapi, httpx, psycopg, uvicorn, dotenv" >/dev/null 2>&1; then
  echo "Installing Python dependencies..."
  "$PYTHON" -m pip install -e .
fi

if ! grep -Eq '^BOT_TOKEN=.+$' .env; then
  echo "BOT_TOKEN is missing from .env. Configure it and run this command again."
  exit 1
fi

if ! grep -Eq '^DATABASE_URL=.+$' .env; then
  echo "DATABASE_URL is missing from .env. Add a Postgres connection string" \
    "(e.g. from a free Neon project) and run this command again."
  exit 1
fi

if [[ ! -x "$CLOUDFLARED" ]]; then
  case "$(uname -m)" in
    x86_64|amd64) cloudflared_asset="cloudflared-linux-amd64" ;;
    aarch64|arm64) cloudflared_asset="cloudflared-linux-arm64" ;;
    armv7l) cloudflared_asset="cloudflared-linux-arm" ;;
    *) echo "Unsupported CPU architecture: $(uname -m)"; exit 1 ;;
  esac

  echo "Downloading Cloudflare Tunnel into .tools (one-time setup)..."
  curl -fL --retry 3 \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/$cloudflared_asset" \
    -o "$CLOUDFLARED.download"
  chmod +x "$CLOUDFLARED.download"
  "$CLOUDFLARED.download" --version >/dev/null
  mv "$CLOUDFLARED.download" "$CLOUDFLARED"
fi

if ss -ltn 2>/dev/null | grep -Eq '127\.0\.0\.1:8000[[:space:]]'; then
  echo "Port 8000 is already in use. Stop the old Lucky server, then run this command again."
  exit 1
fi

: > "$RUN_DIR/web.log"
: > "$RUN_DIR/tunnel.log"

echo "Starting the local web server..."
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 \
  > "$RUN_DIR/web.log" 2>&1 &
WEB_PID=$!
printf '%s\n' "$WEB_PID" > "$RUN_DIR/web.pid"

web_ready=false
for _ in {1..60}; do
  if curl -fsS "$LOCAL_URL/health" >/dev/null 2>&1; then
    web_ready=true
    break
  fi
  kill -0 "$WEB_PID" 2>/dev/null || break
  sleep 0.25
done
if [[ "$web_ready" != true ]]; then
  echo "The web server did not start. See $RUN_DIR/web.log"
  tail -n 30 "$RUN_DIR/web.log" || true
  exit 1
fi

echo "Creating a fresh Cloudflare HTTPS address..."
"$CLOUDFLARED" tunnel --url "$LOCAL_URL" --no-autoupdate \
  > "$RUN_DIR/tunnel.log" 2>&1 &
TUNNEL_PID=$!
printf '%s\n' "$TUNNEL_PID" > "$RUN_DIR/tunnel.pid"

PUBLIC_URL=""
for _ in {1..120}; do
  PUBLIC_URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN_DIR/tunnel.log" | head -n 1 || true)"
  [[ -n "$PUBLIC_URL" ]] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || break
  sleep 0.25
done
if [[ -z "$PUBLIC_URL" ]]; then
  echo "Cloudflare did not create an address. See $RUN_DIR/tunnel.log"
  tail -n 30 "$RUN_DIR/tunnel.log" || true
  exit 1
fi

# Replace only PUBLIC_URL; preserve every secret and other setting in .env.
ENV_TEMP="$RUN_DIR/env.updated"
if grep -q '^PUBLIC_URL=' .env; then
  awk -v url="$PUBLIC_URL" '
    /^PUBLIC_URL=/ { print "PUBLIC_URL=" url; next }
    { print }
  ' .env > "$ENV_TEMP"
else
  cp .env "$ENV_TEMP"
  printf '\nPUBLIC_URL=%s\n' "$PUBLIC_URL" >> "$ENV_TEMP"
fi
mv "$ENV_TEMP" .env

# Settings (including PUBLIC_URL) are read once when Python starts, so restart
# the web process with the new URL. The Telegram bot lives inside this same
# process and registers its webhook automatically on startup — there is no
# separate bot process to launch.
kill "$WEB_PID" 2>/dev/null || true
wait "$WEB_PID" 2>/dev/null || true
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 \
  > "$RUN_DIR/web.log" 2>&1 &
WEB_PID=$!
printf '%s\n' "$WEB_PID" > "$RUN_DIR/web.pid"

web_ready=false
for _ in {1..60}; do
  if curl -fsS "$LOCAL_URL/health" >/dev/null 2>&1; then
    web_ready=true
    break
  fi
  kill -0 "$WEB_PID" 2>/dev/null || break
  sleep 0.25
done
if [[ "$web_ready" != true ]]; then
  echo "The web server did not restart with the new address. See $RUN_DIR/web.log"
  tail -n 30 "$RUN_DIR/web.log" || true
  exit 1
fi

bot_ready=false
for _ in {1..40}; do
  if grep -q 'Telegram webhook registered' "$RUN_DIR/web.log"; then
    bot_ready=true
    break
  fi
  grep -q 'Could not configure Telegram bot' "$RUN_DIR/web.log" && break
  kill -0 "$WEB_PID" 2>/dev/null || break
  sleep 0.25
done
if [[ "$bot_ready" != true ]]; then
  echo "The Telegram bot did not register its webhook. See $RUN_DIR/web.log"
  tail -n 30 "$RUN_DIR/web.log" || true
  exit 1
fi

echo
echo "Lucky is ready."
echo "Public Mini App: $PUBLIC_URL"
echo "Local health:    $LOCAL_URL/health"
echo "Logs:            $RUN_DIR"
echo
echo "Send /start and /admin to the bot again. Old Telegram buttons keep their old URL."
echo "Keep this terminal open. Press Ctrl+C once to stop both services."

while kill -0 "$WEB_PID" 2>/dev/null \
  && kill -0 "$TUNNEL_PID" 2>/dev/null; do
  sleep 2
done

echo "One of the two services stopped unexpectedly. Recent logs:"
tail -n 12 "$RUN_DIR/web.log" "$RUN_DIR/tunnel.log" || true
exit 1
