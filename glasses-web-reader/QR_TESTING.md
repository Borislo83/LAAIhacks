# Testing on real glasses: tunnel + QR code

## Quick start (every time)

```bash
cd ~/Code/lahacks/glasses-web-reader
npm run dev    # terminal 1: leave running (server :8787 + Vite :5173)
npm run qr     # terminal 2: starts/reuses the tunnel, checks it, prints the QR
```

Open the **Even app** on the phone, scan the QR (also saved to `qr.png`), and the app
loads on the glasses. If the tunnel restarted, the URL changes: run `npm run qr` again
and rescan.

## One-time setup (new laptop or fresh clone)

| Need | How to get it | Check |
|---|---|---|
| Node.js 24+ (npm 11) | `brew install node` | `node -v` |
| Project packages | `npm install` (in this folder) | `ls node_modules` |
| Chromium for Playwright (server page rendering) | `npx playwright install chromium` | `ls ~/Library/Caches/ms-playwright` |
| cloudflared (the tunnel) | `brew install cloudflared` | `cloudflared --version` |
| `.env` with Claude key (required) | `cp .env.example .env`, then set `ANTHROPIC_API_KEY=...` | `grep -c ANTHROPIC_API_KEY .env` |
| Phone with the **Even app** (version 2.2.10+), G2 glasses paired | App Store / Play Store | glasses show up in the app |

No Cloudflare account is needed. Quick tunnels are free and anonymous. Without an API
key, pages won't load: the glasses show "Smart view is not set up".

Last known working versions (2026-10-17): Node 24.11.0, npm 11.6.3, cloudflared 2026.10.1,
evenhub-cli 0.1.12, Even Hub SDK 0.0.16.

## Why a tunnel

School/venue Wi-Fi isolates clients, so the phone can't reach the laptop's LAN IP
(`npx evenhub qr --url http://<lan-ip>:5173` won't load). A Cloudflare quick tunnel gives
the laptop a public HTTPS URL that forwards to `localhost:5173`. Vite proxies `/api` to
the server on `:8787`, so one tunnel covers both. On a home network without isolation,
the LAN-IP QR works too and no tunnel is needed.

```
phone (Even app) → https://xxx.trycloudflare.com → Cloudflare → cloudflared on laptop
  → Vite :5173 → (/api) → condense server :8787
```

## What `scripts/qr.sh` does

1. Checks Vite answers on `localhost:5173` (otherwise it tells you to run `npm run dev`).
2. Asks cloudflared's metrics endpoint for the live tunnel hostname:
   `curl http://127.0.0.1:20241/quicktunnel` → `{"hostname":"xxx.trycloudflare.com"}`.
   If nothing is running, it starts
   `cloudflared tunnel --url http://localhost:5173 --metrics 127.0.0.1:20241`
   in the background (log: `/tmp/cf-tunnel.log`).
3. Retries `https://<host>/api/health` until it returns 200 (new tunnels take a few seconds).
4. Prints the QR with `npx evenhub qr --url <url>`, writes `qr.png`, and saves the URL to
   `/tmp/cf-url.txt`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Vite isn't running on :5173" | Start `npm run dev` first. |
| QR scans but the page won't load | The tunnel died or restarted with a new URL. Run `npm run qr` again and rescan. |
| curl shows `000` | No response. Right after startup this is normal, so wait. If it persists, Vite is down or the tunnel points at the wrong port. |
| Error `1033` when scanning | That tunnel is dead (cloudflared stopped or restarted), so the QR points to a URL that no longer exists. Run `npm run qr` and scan the **new** QR. |
| `502` from Cloudflare | The tunnel is up but nothing is on `:5173`. Restart `npm run dev`. |
| Vite "Blocked request. This host is not allowed" | `server.allowedHosts` in `vite.config.ts` needs `.trycloudflare.com` (already set). |
| Page loads but articles fail | Check terminal 1 for server errors. Smart view needs `ANTHROPIC_API_KEY` in `.env`. |
| Port already in use | An old dev server is still running: `lsof -nP -iTCP:5173 -sTCP:LISTEN`, then `kill <pid>`. |
| Tunnel URL from an old log doesn't work | **Never trust `/tmp/cf-tunnel.log` or `/tmp/cf-url.txt` alone.** They can be left over from a dead tunnel. Ask the running process: `curl http://127.0.0.1:20241/quicktunnel`. If cloudflared was started without `--metrics`, find its port with `lsof -nP -iTCP -sTCP:LISTEN \| grep cloudflar`. |

Tip: open the tunnel URL in a normal phone browser first. If it loads there, the network
side is fine and any remaining problem is in the Even app or on the glasses.

## Stopping everything

```bash
# Ctrl+C in terminal 1 stops the server + Vite
pkill cloudflared        # stops the tunnel
```

## Manual commands (no script)

```bash
cloudflared tunnel --url http://localhost:5173 --metrics 127.0.0.1:20241
curl -s http://127.0.0.1:20241/quicktunnel                            # get hostname
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/api/health    # expect 200
npx evenhub qr --url https://<host>                                   # print QR
```

## Notes

- The tunnel URL is public: anyone with it can use the app while it runs. Stop the tunnel
  when you're done.
- For a URL that never changes, use a named Cloudflare tunnel (needs an account and a
  domain) or deploy the app. For a packaged build instead of dev mode, see
  `npm run pack` in the README. Packaged builds call the server URL in the `app.json`
  network whitelist, which still points to a placeholder.
