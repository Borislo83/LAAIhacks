#!/usr/bin/env bash
# Builds the Even Hub package (.ehpk) for the G2 glasses, pointed at the laptop's HTTPS
# address. Even Hub only lets a packaged app reach the HTTPS origins in app.json's network
# whitelist, so the address is baked into both the build and the whitelist.
#
#   scripts/pack-evenhub.sh [https://your-address]   default: this run's tunnel (.runtime/demo/tunnel.url)
#
# Output: demo/focal-beta.ehpk. Upload it in the Even Hub developer portal
# (https://hub.evenrealities.com) and move the build to Test to share it as a Beta.
# A quick tunnel's address changes on every start: re-pack and re-upload after a restart.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/glasses-web-reader"
BASE=${1:-$(cat "$ROOT/.runtime/demo/tunnel.url" 2>/dev/null || true)}
if [[ ! $BASE =~ ^https://[A-Za-z0-9.-]+$ ]]; then
  echo "Give the laptop's HTTPS address (start the stack with scripts/demo.sh start --pair first)."; exit 2
fi

python3 - "$APP/app.json" "$BASE" <<'EOF'
import json, sys
path, base = sys.argv[1], sys.argv[2]
app = json.load(open(path))
for permission in app["permissions"]:
    if permission["name"] == "network":
        permission["whitelist"] = [base]
json.dump(app, open(path, "w"), indent=2)
open(path, "a").write("\n")
EOF

rm -rf "$APP/dist"
(cd "$APP" && VITE_API_BASE="$BASE" npm run build >/dev/null)
(cd "$APP" && npx evenhub pack app.json dist -o "$ROOT/demo/focal-beta.ehpk")
echo "Packed demo/focal-beta.ehpk for $BASE"
