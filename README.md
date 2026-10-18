# Focal (LAAIhacks)

Smart glasses that read the web page you're looking at and run Fetch.ai agents to act on it:
compare prices, summarise reviews, add to an Amazon cart, play YouTube or Spotify, send an
iMessage, search the web and read the news, all by voice ("Hey Focal, ...") on Even Realities
G2 glasses. The phone only connects the glasses; everything runs on your laptop.

- One-page user guide (gestures and things to say): [demo/focal-glasses-quick-guide.pdf](demo/focal-glasses-quick-guide.pdf)
- Every demo flow, phase by phase: the "Focal glasses: demo flows and test guide" doc

## Install

Focal runs on a Mac (Messages, Spotify and the laptop actions use macOS).

### 1. Prerequisites

| Tool | Version | Install |
| --- | --- | --- |
| macOS | 14+ | |
| Node.js | 22+ | `brew install node` |
| uv (Python 3.12+) | 0.5+ | `brew install uv` |
| Google Chrome | any | [google.com/chrome](https://www.google.com/chrome/) (the Amazon cart window uses it) |
| cloudflared | any | `brew install cloudflared` (only for real glasses) |
| Spotify app | any | signed in, for Spotify control |
| Messages | | signed in to iMessage, for sending messages |

Accounts and keys:

- **Anthropic API key**: reads pages into glasses screens ([console.anthropic.com](https://console.anthropic.com)).
- **StepFun API key** (or an OpenAI key): speech-to-text for the glasses mic.
- **Supabase project**: stores sessions, actions and settings ([supabase.com](https://supabase.com)).
- **ASI:One API key** (optional): LLM routing and summaries ([asi1.ai](https://asi1.ai)).
- **Even Realities G2 glasses and the Even app** on your phone, for real glasses. Without them,
  everything runs on the Even Hub simulator on your laptop.

### 2. Get the code and install dependencies

```bash
git clone https://github.com/Borislo83/LAAIhacks.git
cd LAAIhacks
npm --prefix glasses-web-reader install
uv sync --directory services/orchestrator
uv sync --directory services/agents
```

### 3. Set up Supabase

Create a Supabase project, then apply the migrations in `supabase/migrations/` in order
(with the Supabase CLI: `supabase link --project-ref <ref>` then `supabase db push`, or paste
each file into the SQL editor). Create one Auth user (Authentication > Users > Add user) and
copy its user id: the local demo signs in as that user.

### 4. Configure

Copy each example file and fill it in. Each file explains every setting.

```bash
cp glasses-web-reader/.env.example glasses-web-reader/.env
cp services/orchestrator/.env.example services/orchestrator/.env
cp services/agents/.env.example services/agents/.env
```

The settings that matter:

- `glasses-web-reader/.env`: `ANTHROPIC_API_KEY`, `STEPFUN_API_KEY`,
  `ORCHESTRATOR_URL=http://127.0.0.1:8001`.
- `services/orchestrator/.env`:
  - Supabase: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`.
  - Agents: `AGENTS_URL=http://127.0.0.1:8000`.
  - Local demo sign-in: `DEMO_AUTH_MODE=true` and `DEMO_USER_ID=<the Auth user id>`.
- `services/agents/.env`:
  - Contacts: `IMESSAGE_CONTACTS` (for example `Alex=+15551234567`) and
    `IMESSAGE_DEFAULT_CONTACT` (who glasses messages go to).
  - Real sends: `IMESSAGE_DRY_RUN=0`.
  - Live guest Amazon cart: `AMAZON_CART_DRY_RUN=0`, `AMAZON_CART_GUEST=1`.
- **Executor tokens.** Generate each one with
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` and put the same value in
  both `services/orchestrator/.env` and `services/agents/.env`:
  `SPOTIFY_EXECUTOR_TOKEN`, `IMESSAGE_EXECUTOR_TOKEN`, `LAPTOP_EXECUTOR_TOKEN`,
  `AMAZON_CART_EXECUTOR_TOKEN`.

### 5. Run it

One command starts the agents (:8000), the orchestrator (:8001), the glasses app (:5180), the
action watcher and the Even Hub simulator:

```bash
scripts/demo.sh start --sim
```

- Check health with `scripts/demo.sh status`. Read logs with `scripts/demo.sh logs <agents|orchestrator|glasses|watcher|sim|tunnel>`.
- Open the Amazon cart window (to watch adds live) with `scripts/demo.sh cart`.
- Stop everything with `scripts/demo.sh stop`.

The first time, macOS asks to allow the microphone (simulator), Automation of Messages and
Spotify, and Chrome. Allow them.

### 6. Use real glasses

```bash
scripts/demo.sh restart --pair
```

This starts a Cloudflare tunnel and prints a QR code (also saved as `.runtime/demo/pair-qr.png`).
Scan it in the Even app on your phone: the glasses open Focal and sign in for this run only.
Each start makes a new code, so scan again after a restart.

### 7. Try it

Say "Hey Focal", then for example:

- "open up the AirPods page on Apple"
- "compare prices for AirPods Pro 3"
- "add AirPods Pro 3 to my Amazon cart"
- "play Blinding Lights on Spotify"
- "play Blinding Lights on YouTube"
- "send a message saying I'm on my way"
- "what's the latest on SpaceX?"
- "hide the display"

On a product page the "Focal can help" strip offers Compare prices, Add to Amazon cart and
Review summary. Swipe to move, tap to choose, double-tap to go back.

### Tests

```bash
npm --prefix glasses-web-reader test
uv run --directory services/orchestrator pytest -q
uv run --directory services/agents pytest -q
```

## Structure

```
glasses-web-reader/  G2 glasses app (Even Hub SDK) + condense server; voice, action popups
apps/
  glasses/        G2 session API contract (INTEGRATION.md)
  dashboard/      Focal Studio web dashboard (prototype/)
  public-info/    Public homepage and privacy pages for Google OAuth branding
services/
  orchestrator/   FastAPI backend: chat, sessions and approvals, Supabase persistence
  agents/         Fetch.ai uAgents: price, reviews, news, search, YouTube, Spotify, iMessage, Amazon cart
packages/
  shared/         OpenAPI schema shared by clients
supabase/         Migrations and database tests
scripts/          demo.sh: start, check and stop the whole stack
demo/             Demo script, pitch, quick guide and sample pages
```

## Deploy

- `apps/dashboard`: frontend host (e.g. Vercel)
- `services/orchestrator`: backend host (e.g. Railway, Render, Fly.io)
- `services/agents`: Fetch.ai Agentverse

## Backend

The [orchestrator](services/orchestrator/README.md) runs on port 8001, predicts tasks, checks
permissions, calls the agents on port 8000, and saves settings and task history in **Supabase**.
See the [implementation plan](services/orchestrator/PLAN.md) and
[database migration](supabase/migrations/202610170001_orchestrator.sql) for setup.
