#!/usr/bin/env bash
# Focal glasses demo stack on this laptop, as in the "Focal glasses: demo flows and
# test guide" doc.
#
#   scripts/demo.sh start [--sim] [--open URL] [--tunnel] [--pair]
#                                              start everything (and the simulator, opened on URL);
#                                              --pair: tunnel + a QR the Even app scans to use real glasses
#   scripts/demo.sh status                     what is running, with health checks
#   scripts/demo.sh stop                       stop everything this script started
#   scripts/demo.sh logs [name]                tail a log (agents, orchestrator, glasses, watcher, sim, tunnel)
#   scripts/demo.sh cart [clear]               open the cart window (FOCAL CART) to watch adds live; clear empties it
#
# Started: agents :8000, orchestrator :8001, glasses app (Vite :5180 + condense server
# :8797), the action watcher, and optionally the Even Hub simulator and a Cloudflare
# tunnel. Settings come from each service's .env (services/agents/.env sets the cart to
# live guest mode; set AMAZON_CART_DRY_RUN=1 in your shell for a dry run).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/.runtime/demo"
LOGS="$RUN/logs"
mkdir -p "$LOGS"

VITE_PORT=5180
CONDENSE_PORT=8797
NAMES=(agents orchestrator glasses watcher sim tunnel)

urlencode() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

pidfile() { echo "$RUN/$1.pid"; }
running() { local f; f=$(pidfile "$1"); [[ -f $f ]] && kill -0 "$(cat "$f")" 2>/dev/null; }

launch() { # name, command...
  local name=$1; shift
  if running "$name"; then echo "  $name already running"; return; fi
  # setsid-like: a new process group so stop can take the whole tree down.
  ( set -m; nohup "$@" >"$LOGS/$name.log" 2>&1 & echo $! >"$(pidfile "$name")" )
  echo "  started $name (log: .runtime/demo/logs/$name.log)"
}

wait_for() { # label, url, seconds
  local label=$1 url=$2 limit=${3:-60} i=0
  until curl -s -m 2 -o /dev/null "$url"; do
    sleep 1; i=$((i + 1))
    if (( i >= limit )); then echo "  ! $label did not answer at $url (see logs)"; return 1; fi
  done
  echo "  $label ready"
}

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

start() {
  local sim=0 tunnel=0 open="" pair=0
  while (( $# )); do
    case $1 in
      --sim) sim=1 ;;
      --tunnel) tunnel=1 ;;
      --pair) tunnel=1; pair=1 ;;
      --open) shift; open=${1:-}; sim=1 ;;
      *) echo "unknown option $1"; exit 2 ;;
    esac
    shift
  done
  for port in 8000 8001 $VITE_PORT $CONDENSE_PORT; do
    if port_busy "$port" && ! running agents && ! running orchestrator && ! running glasses; then
      echo "Port $port is already in use by another process. Stop it first (lsof -nP -iTCP:$port)."; exit 1
    fi
  done

  echo "Starting the Focal demo stack"
  launch agents uv run --directory "$ROOT/services/agents" laai-agents
  launch orchestrator uv run --directory "$ROOT/services/orchestrator" laai-orchestrator
  wait_for "orchestrator" "http://127.0.0.1:8001/health" 90

  # The tunnel comes first so the glasses gateway can allow its origin.
  if (( tunnel )); then start_tunnel; fi
  # The glasses gateway may sign the simulator in on this machine only (never over a
  # tunnel or Wi-Fi); the orchestrator's DEMO_AUTH_MODE accepts demo-* from loopback.
  # With --pair, a phone that scanned this run's QR (secret in the link) is signed in too.
  local origins="http://localhost:$VITE_PORT,http://127.0.0.1:$VITE_PORT" secret=""
  [[ -f $RUN/tunnel.url ]] && origins="$origins,$(cat "$RUN/tunnel.url")"
  if (( pair )); then
    secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
    echo "$secret" >"$RUN/pair.secret"; chmod 600 "$RUN/pair.secret"
  fi
  launch glasses env VITE_PORT=$VITE_PORT CONDENSE_PORT=$CONDENSE_PORT \
    GATEWAY_DEV_TOKEN=demo-all GATEWAY_PAIR_SECRET="$secret" ORCHESTRATOR_ALLOWED_ORIGINS="$origins" \
    npm --prefix "$ROOT/glasses-web-reader" run dev
  wait_for "glasses app" "http://127.0.0.1:$VITE_PORT/api/health" 90
  wait_for "agents" "http://127.0.0.1:8000/" 120 || true

  mkdir -p "$HOME/.local/state/laai"
  launch watcher env GLASSES_API_URL=http://127.0.0.1:8001 GLASSES_WEB_TOKEN=demo-web \
    GLASSES_AGENT_TOKEN=demo-agent GLASSES_EXECUTOR_TOKEN=demo-executor \
    uv run --directory "$ROOT/services/agents" laai-glasses-executor \
    --journal "$HOME/.local/state/laai/watch.json" watch

  if (( pair )) && [[ -f $RUN/tunnel.url ]]; then pair_qr "$(cat "$RUN/tunnel.url")/?pair=$secret"; fi
  if (( sim )); then
    launch sim npx --prefix "$ROOT/glasses-web-reader" evenhub-simulator --automation-port 9898 \
      --aid coreaudio:BuiltInMicrophoneDevice "http://localhost:$VITE_PORT${open:+/?open=$(urlencode "$open")}"
  fi

  echo
  echo "Ready."
  echo "  Glasses app (phone UI):  http://localhost:$VITE_PORT"
  echo "  Lens Monitor:            http://localhost:$VITE_PORT/sim.html"
  (( sim )) || echo "  Simulator:               scripts/demo.sh start --sim (or npx evenhub-simulator ...)"
  echo "  Cart mode:               $(cart_mode)"
}

cart_mode() {
  local env="$ROOT/services/agents/.env" dry guest
  dry=${AMAZON_CART_DRY_RUN:-$(grep -E '^AMAZON_CART_DRY_RUN=' "$env" 2>/dev/null | tail -1 | cut -d= -f2)}
  guest=${AMAZON_CART_GUEST:-$(grep -E '^AMAZON_CART_GUEST=' "$env" 2>/dev/null | tail -1 | cut -d= -f2)}
  if [[ $dry != 0 ]]; then echo "dry run (nothing is added)"
  elif [[ $guest == 1 ]]; then echo "live, guest cart (no Amazon sign-in)"
  else echo "live, signed-in Amazon account"; fi
}

start_tunnel() {
  command -v cloudflared >/dev/null || { echo "  ! cloudflared not installed (brew install cloudflared)"; return; }
  launch tunnel cloudflared tunnel --url "http://localhost:$VITE_PORT" --metrics 127.0.0.1:20241 --no-autoupdate
  local host="" i=0
  until [[ -n $host ]] || (( i >= 30 )); do
    sleep 1; i=$((i + 1))
    # Not up yet is normal here: under set -e a failed curl must not end the script.
    host=$(curl -s -m 2 http://127.0.0.1:20241/quicktunnel 2>/dev/null | sed -nE 's/.*"hostname":"([^"]+)".*/\1/p' || true)
  done
  if [[ -z $host ]]; then echo "  ! tunnel did not come up"; return; fi
  echo "https://$host" >"$RUN/tunnel.url"
  echo "  tunnel: https://$host"
}

pair_qr() { # url: print it as a QR in the terminal and save it as an image
  local url=$1
  (cd "$ROOT/glasses-web-reader" && node -e "require('qr-image').image(process.argv[1],{size:10}).pipe(require('fs').createWriteStream(process.argv[2]))" "$url" "$RUN/pair-qr.png")
  echo
  echo "Pair the glasses: in the Even app on your phone, scan this QR (also saved as .runtime/demo/pair-qr.png)."
  echo "It opens the app through the tunnel and signs it in for this run only."
  (cd "$ROOT/glasses-web-reader" && npx evenhub qr --url "$url" 2>/dev/null) || true
}

stop() {
  echo "Stopping the Focal demo stack"
  for name in "${NAMES[@]}"; do
    local f; f=$(pidfile "$name")
    if running "$name"; then
      local pid; pid=$(cat "$f")
      kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
      pkill -P "$pid" 2>/dev/null || true
      echo "  stopped $name"
    fi
    rm -f "$f"
  done
  rm -f "$RUN/tunnel.url" "$RUN/pair.secret" "$RUN/pair-qr.png"
}

status() {
  for name in "${NAMES[@]}"; do
    if running "$name"; then printf "  %-13s running (pid %s)\n" "$name" "$(cat "$(pidfile "$name")")"
    else printf "  %-13s stopped\n" "$name"; fi
  done
  check() { curl -s -m 3 -o /dev/null -w '%{http_code}' "$2" | grep -qE '^[23]' && echo "  $1 ok" || echo "  $1 not answering"; }
  check "orchestrator /health" "http://127.0.0.1:8001/health"
  check "glasses app /api/health" "http://127.0.0.1:$VITE_PORT/api/health"
  echo "  cart mode: $(cart_mode)"
}

case ${1:-} in
  start) shift; start "$@" ;;
  stop) stop ;;
  restart) shift; stop; start "$@" ;;
  status) status ;;
  logs) tail -n 60 -f "$LOGS/${2:-watcher}.log" ;;
  cart)
    token=$(grep -E '^AMAZON_CART_EXECUTOR_TOKEN=' "$ROOT/services/agents/.env" | tail -1 | cut -d= -f2-)
    curl -s -m 120 -X POST "http://127.0.0.1:8000/cart/${2:-show}"  -H 'Content-Type: application/json' \
      -d "{\"executor_token\":\"$token\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["summary"])' ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
