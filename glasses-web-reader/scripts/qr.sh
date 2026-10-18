#!/usr/bin/env bash
# Print a QR code that opens the dev app on real G2 glasses through a
# Cloudflare quick tunnel. Starts the tunnel if one isn't running.
# Usage: npm run qr   (needs `npm run dev` running first)
set -euo pipefail
cd "$(dirname "$0")/.."

METRICS=127.0.0.1:20241
LOG=/tmp/cf-tunnel.log

tunnel_host() {
  curl -s -m 3 "http://$METRICS/quicktunnel" | sed -nE 's/.*"hostname":"([^"]+)".*/\1/p'
}

if ! curl -s -m 5 -o /dev/null http://localhost:5173/; then
  echo "Vite isn't running on :5173. Start it with: npm run dev" >&2
  exit 1
fi

HOST=$(tunnel_host || true)
if [ -z "$HOST" ]; then
  echo "No tunnel running, starting cloudflared..."
  nohup cloudflared tunnel --url http://localhost:5173 --metrics "$METRICS" \
    --no-autoupdate >"$LOG" 2>&1 &
  for _ in $(seq 1 40); do
    HOST=$(tunnel_host || true)
    [ -n "$HOST" ] && grep -q "Registered tunnel connection" "$LOG" && break
    sleep 1
  done
fi
[ -n "$HOST" ] || { echo "Tunnel didn't come up. See $LOG" >&2; exit 1; }
URL="https://$HOST"

# New tunnels take a few seconds before Cloudflare routes to them.
for _ in $(seq 1 20); do
  [ "$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$URL/api/health")" = 200 ] && break
  sleep 2
done
CODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$URL/api/health" || true)
[ "$CODE" = 200 ] || { echo "Tunnel $URL not answering (HTTP $CODE)" >&2; exit 1; }

echo "$URL" >/tmp/cf-url.txt
node -e "require('qr-image').image(process.argv[1],{size:10}).pipe(require('fs').createWriteStream('qr.png'))" "$URL"
npx evenhub qr --url "$URL"
echo "Tunnel OK: $URL   (QR also saved to qr.png)"
