# Agent Team Kit

An autonomous software team you manage from Telegram. You talk to a **Project
Manager** agent; it plans the work with you, then delegates to specialist agents
that design, build and test an **Angular + Django** application. It runs on the
Claude Agent SDK, using your Claude Pro/Max subscription.

```
You (Telegram) ──► Project Manager ──┬─► ui-designer    design system, HTML mockups, design reviews
                                     ├─► backend-dev    Django + DRF, owns the API contract
                                     ├─► frontend-dev   Angular, builds from the mockups
                                     ├─► qa-engineer    Playwright e2e tests, screenshots, bug reports
                                     ├─► code-reviewer  independent review before "done" (read-only)
                                     └─► devops-engineer  deployability, CI pipeline, failed deploys

After every build round the bot runs scripts/verify.sh itself and reports ✅/❌.
```

## How a project goes

1. **Discovery.** You describe the app. The PM asks a few questions at a time,
   writes `docs/SPEC.md` and a task board in `docs/PROGRESS.md`, and asks you to
   approve the plan.
2. **Build.** Send `/build`. The PM works in rounds: it delegates tasks, has the
   work reviewed, commits each finished task, and messages you after every round
   with the test status.
3. **Questions.** If the PM needs a decision, the build pauses and you get the
   question on Telegram. Reply normally and the build resumes by itself.
4. **Done.** The PM can only finish when the independent test run passes.

Messages you send while it's building get passed to the PM at the start of the
next round.

## Telegram commands

Type `/` in the chat for the menu. Many messages also come with **tap-to-answer buttons**
(approve, deploy, use the PM's suggestions, undo…).

| Command | |
|---|---|
| (any text) | Talk to the Project Manager |
| `/status` (`/status all`) | Done / in progress / to do, and the plan's features |
| `/build` · `/stop` | Start/resume autonomous building · stop everything |
| `/live` | A pinned status message that updates itself during builds |
| `/today` | Today's progress; also sent every evening (`/today off`) |
| `/changes` | What changed since you last looked |
| `/screens` (`app`/`mockups`) | Latest screenshots as an album |
| `/ask <agent> <question>` | Ask one specialist, read-only |
| `/undo` | Undo the last finished task (with confirmation) |
| `/preview` | Run the app on the server to try it via SSH tunnel |
| `/verify` · `/push` | Full test run · test and push to GitHub |
| `/deploy` · `history` · `drill` | Promote staging to production · past deploys · rollback fire drill |
| `/server` | CPU/RAM/disk, Claude usage window, versions running |
| `/agents` · `/run` · `/jobs` · `/job` · `/pm` | Studio (see below) |
| `/learn` | Knowledge curator proposals |
| `/log` · `/reset` | Agents' recent steps · fresh PM conversation |

## Security model

- **Two identities in the container.** The bot runs as root and alone holds the
  Telegram and GitHub tokens (read once at start, then removed from the environment).
  Agents, tests, previews and studio jobs run as the unprivileged `agent` user with
  no tokens, and can't read the bot's state.
- **Agents commit, the bot pushes**, from its own private mirror, never by running git
  as root inside the agents' folder (planted hooks or config can't escalate).
- **Production is reached only by you** (`/deploy` then tap *Deploy now*). The agents
  have no GitHub access at all; failed CI logs are handed to them by the bot.
- **The deploy key can only run the deploy gate** on the server (SSH forced command
  plus a sudo rule for one file). The gate validates every request, only promotes
  the exact version running on staging, backs up the production database, checks
  the new version (API, web page, web→API, protected API refuses anonymous users),
  and **rolls back automatically** if any check fails. The server's SSH identity is
  pinned (`DEPLOY_KNOWN_HOSTS`). `/deploy drill` proves the rollback works.
- **One operation at a time** for anything that touches the project; conflicting
  commands get a clear "busy" reply.
- What's not covered: agents can read the Claude login token (the Claude tool needs
  it; the worst case is usage, and you can revoke it), and the code agents write is
  only as safe as the tests and reviews that check it.

**The bot's own tests:** `docker compose run --rm agents pytest -q /app/tests`

## Setup (about 20 minutes)

### 1. A server
A Linux VPS with **at least 4 GB RAM (8 GB recommended)**, because Angular builds and
headless Chromium are memory-hungry. Any provider works (Hetzner, DigitalOcean…).
Install Docker: `curl -fsSL https://get.docker.com | sh`.

You can also run it on your own computer with Docker Desktop. It only works
while the computer is on.

### 2. A Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric user id. The bot ignores
   everyone else, which matters because these agents can run commands.

### 3. A Claude token from your subscription
On your own computer, install Claude Code (see https://docs.claude.com/en/docs/claude-code/overview),
then run:

```bash
claude setup-token
```

Copy the token it prints (starts with `sk-ant-oat…`).
Agent usage counts against your plan's normal usage limits. If a limit is hit,
the bot pauses and continues automatically when it resets.

### 4. Configure and start

```bash
# on the server, in this folder
cp .env.example .env
nano .env                      # fill in the bot token, your user id, the Claude token

docker compose up -d --build
docker compose logs -f         # watch it start; Ctrl+C to stop watching
```

You should get "🟢 Agent team online" on Telegram. Say hi and describe what you
want to build.

## Previewing the app while it's built

Ports 4200 (Angular) and 8000 (Django) are exposed **only on the server's
localhost**. Ask the PM to "start the app so I can preview it", then from your
laptop:

```bash
ssh -L 4200:localhost:4200 -L 8000:localhost:8000 you@your-server
```

and open http://localhost:4200. The project itself is a normal git repo in
a Docker volume. Copy it out any time with
`docker compose cp agents:/workspace ./my-project`.

## GitHub and deployment

```
agents ──(tests pass)──► bot pushes main ──► GitHub Actions: test → build images → deploy STAGING
                                                                                  │
                                                     you: /deploy, /deploy yes ───┴─► PRODUCTION
```

- After every build round where `scripts/verify.sh` passes, the bot pushes to
  your private GitHub repo, so `main` only ever gets tested code. `/push` does the
  same on demand.
- GitHub Actions runs the tests again, builds a backend and a web image, and
  deploys them to **staging** (password-protected). You get a Telegram message
  when that passes or fails. Failures go to the agents to fix in the next round.
- **Production only changes when you send `/deploy` then `/deploy yes`.** It
  gets exactly the version you tried on staging.
- Both environments run on one small **app server** (2 GB droplet) with
  automatic HTTPS, separate databases, and daily production database backups.
  The heavy building happens on GitHub, not on the server.

Setup: create a private GitHub repo and a fine-grained token, prepare the app
server with `deploy/server-setup.sh` (it prints the GitHub secrets to add), then
fill in `GITHUB_REPO`, `GITHUB_TOKEN`, `STAGING_URL` and `PRODUCTION_URL` in
`.env`. The agents' `devops-engineer` makes the app itself deployable
(see `docs/DEPLOYMENT.md` in the project).

## Studio: on-demand agents (not in git)

Separate from the build team: agents you pick for one-off jobs, one at a time.
They never run while the build team is working.

| Command | |
|---|---|
| `/agents` | List studio agents |
| `/run <agent> <brief>` | Start a job, e.g. `/run project-proposal https://exemplu.ro mobile first` |
| (any text or file) | While a job is open, goes to that job; files land in its `input/` |
| `/job` · `/job files` · `/job send <file>` · `/job stop` · `/job end` | The current job |
| `/jobs` · `/job open <id>` | Recent jobs, reopen one |
| `/pm` | Talk to the Project Manager again |

Included agents: **project-proposal** (analysis, then mockups, then a PDF proposal),
**website-analyst**, **mockup-designer**, **commercial-offer** (an ofertă comercială
PDF), and **ai-visibility-auditor** (AI-search visibility, Romanian auto market).

Everything lives in the `/studio` volume: `agents/` (add your own `.md` agents),
`brand/company.md` (your details, rates, VAT, terms; fill it in once, or let the
agents ask), `brand/logo.*` (send your logo on Telegram with the caption "logo"),
and `jobs/<id>/{input,research,mockups,output}`. PDFs are made with WeasyPrint, and the
agents check every page visually before sending.

## Updates, self-healing and backups (host services)

The kit lives in its own GitHub repo. On the server it's a git clone; `.env` stays next to it (never in git).

**One-time move from a zip folder to the repo** (as root on the server):
```bash
TOKEN=$(grep ^GITHUB_TOKEN= ~/agent-team-kit/.env | cut -d= -f2-)   # needs Contents: read on the kit repo
git config --global credential.helper store && git config --global credential.useHttpPath true
echo "https://x-access-token:${TOKEN}@github.com/vladbuzatu/bbts-ai-agent.git" >> ~/.git-credentials
git clone https://github.com/vladbuzatu/bbts-ai-agent.git ~/bbts-ai-agent
cp ~/agent-team-kit/.env ~/bbts-ai-agent/.env
cd ~/bbts-ai-agent && docker compose up -d --build      # same volumes: the project name is fixed
sudo ops/install.sh                                      # updates, watchdog, backups
```

**Updating:** `/update` in Telegram (or `sudo ops/update.sh`). It keeps `.env`, builds the new version,
waits for the bot's health check and **rolls back by itself** if the new version doesn't come up.
`/server` shows the version and whether an update is available (checked hourly).

**Watchdog** (every 2 min): restarts the agent team or the HTTPS proxy if they stopped, puts staging/
production back on the proxy network (the "502" problem), warns about low memory, out-of-memory kills
and a full disk. It tells you on Telegram what it fixed.

**Backups** (nightly 03:40, `/var/backups/bbts/<date>`): projects and team, bot state, studio, agent
history, `/opt/app` + `/opt/carstage`, Postgres dumps of staging/production, `.env`. 7 daily + 4 weekly.
Off-site copy: install `rclone`, configure a remote, set `BACKUP_REMOTE=remote:bucket/bbts` in `.env`.
Restore: `sudo ops/restore.sh` lists backups; `sudo ops/restore.sh 2026-11-09 workspace` restores one
part (the current state is saved first). DigitalOcean's weekly droplet backups are a good extra layer.

**The kit's CI** (`.github/workflows/ci.yml`) builds the image and runs all tests *inside it*, plus a
start-up smoke test and lint, on every push. Update when it's green.

## Reviewing and following the work

- **Pull requests in Telegram:** every PR the bot opens arrives as a card (files, +/−, tests) with
  ✅ Merge · 💬 Request changes · ❌ Close. A change request goes to the PM and as a PR comment, and
  starts a build for that project. Comments you write on GitHub reach the PM within 5 minutes;
  merging on the website is noticed too.
- **Visual regression:** screenshots (`design/screens/*.png`) are compared with the approved look after
  every round; changed screens come with before / now / what-changed images. Merging a PR approves
  the new look; `/screens diff [project]`, `/screens accept [project]`.
- **Scorecard:** `/score [days]`, and every Monday: tasks committed, build rounds and how many were
  green, PRs, deploys, reviews, agent time, tokens and API-equivalent usage, lab spend, per project.
- **Voice:** send a voice note; it's transcribed (`VOICE_OPENAI_API_KEY`) and handled like text.
- **Dashboard:** `ssh -L 8090:localhost:8090 root@SERVER` → http://localhost:8090 (status of every
  project, scorecard, screenshots and diffs, lab runs, log). Read-only.
- **Parallel lanes:** studio jobs run beside the PM and builds; `/pm <message>` asks the PM without
  leaving a studio job.
- **Deploys per project:** `/project urls <name> <staging> [production]`, then
  `/deploy <name>` (preview → approve), `/deploy <name> drill|history`. The project's repo needs the
  same workflows and a deploy gate on its server as the main project (devops-engineer sets them up).

## Agents' internet allowlist

`AGENT_EGRESS=restricted` (needs `cap_add: NET_ADMIN`, already in docker-compose.yml): the build team
(user `agent`) can only reach Claude's API, package registries, GitHub and documentation sites,
through a proxy in the bot; a firewall rule rejects anything else that user sends. Studio jobs run as
their own user (`studio`) with normal access, because website research needs it. Blocked hosts show
in `/server`; add hosts with `AGENT_EGRESS_ALLOW`.

## Several projects (master folder)

`/workspace` is the team's master folder: the agents, playbooks, `docs/LESSONS.md` and
`docs/STANDARDS.md` live there and are shared by every project. Each project has its own folder and
git repo under `/workspace/projects/<name>` (an old single-project install is moved there
automatically on the first start, history included).

- Every project follows the same lifecycle: its own `docs/SPEC.md`, `docs/PROGRESS.md` and
  `scripts/verify.sh` (the test gate).
- `/build` works through every project with open tasks (or `/build carstage stocfresh`); after each
  round the bot runs each changed project's `verify.sh`.
- Shipping: the main project can push `main` (its CI deploys staging). Other projects work on feature
  branches; after green tests the bot pushes the branch and opens/updates a pull request
  (`/project mode <name> pr|main`).
- `/project link <name> owner/repo` clones a repo into `projects/<name>`. Access per repo: one
  `GITHUB_TOKEN` with every repo added, or `GITHUB_TOKEN_<NAME>` (e.g. `GITHUB_TOKEN_CARSTAGE`) for a
  separate token per project. Agents never see the tokens.
- `/status [name]` shows a project (plus one line per other project); `/project <name>` switches the
  default. `MULTI_PROJECT=0` keeps the old single-project layout.

## CarStage Model Lab (studio agent `model-lab`)

Runs CarStage's own pipelines (production standard/advanced and the process-tester ones) with
different models and settings, scores the results and sends a PDF report.

1. Put the CarStage code on the server at `/opt/carstage` (root-owned, without `settings.py`).
   It is mounted read-only into the container at `/lab/carstage`.
2. Add `LAB_GEMINI_API_KEY` / `LAB_OPENAI_API_KEY` (separate keys with their own spending limits
   are best) and optionally `LAB_BUDGET_USD` to `.env`, then `docker compose up -d --build`.
3. `/run model-lab compare our standard and advanced output with newer models`. Send car photos
   with the caption `car` and showroom images with the caption `stage`, **as files** (not
   compressed photos). The agent proposes a plan with a cost estimate and waits for your OK.
4. The budget ($5 by default, `LAB_BUDGET_USD`) applies per testing round: each message you send
   starts a new round. `/lab` shows what the lab is doing and recent runs; `/job budget 8` and
   `/lab budget 8` change the per-round budget of the job or the PM.

How the keys stay safe: runs happen as a separate `lab` user with the keys in its environment
only; the agent chooses the pipeline, models and settings (checked against CarStage's
`compose_api/config.py`) but can't change the code or read the keys. Costs are estimates charged
before each run; check the providers' billing pages for real numbers.

## Standards and design quality

- **`docs/STANDARDS.md`:** the external standards the team follows, pinned and mapped to how
  each is checked: WCAG 2.2 AA (axe-core in e2e), OWASP ASVS 5.0 Level 2 and Top 10:2025
  (security-review checklist), OWASP API Top 10 (other user's object → 404 tests), Core Web
  Vitals (budgets + Lighthouse), GDPR, pinned framework versions. Reviews treat a violation as
  a blocker. The curator proposes updates when a source is older than 3 months.
- **Designs that don't look AI-made** (`design-direction` playbook): brand brief and reference
  board first, then **three genuinely different directions** sent to you as screenshots, and
  you choose. Banned generic defaults, one signature element per product, curated fonts and
  icons, custom SVG instead of stock images, a sourced trend radar (`design/TRENDS.md`).
- **`art-director` agent:** an independent, read-only critic that scores screenshots on a
  rubric and counts AI-design tells; designs are only shown to you after it approves.
- New playbooks: `accessibility`, `web-performance`, `security-review`. QA covers five test
  layers, and writes tests first for risky features.
- Studio's mockup designer and proposal writer follow the same method (`/studio/playbooks/`).

## Playbooks, lessons and the knowledge curator

- **Playbooks** (`.claude/skills/*/SKILL.md` in the project): concise house rules for
  Angular 22 and Django 6.1 (components, forms, testing, APIs, security). Their code
  examples were compiled and tested against those exact versions. Each agent's file
  says which playbooks it must follow.
- **Angular's official AI tooling** (`ng mcp`, read-only): agents look up the official
  best practices and docs instead of relying on memory.
- **Lessons** (`docs/LESSONS.md`): the PM records every real mistake with evidence and
  the rule that prevents it.
- **Knowledge curator** (propose-only): nightly (default 02:30, only when the team is
  idle) it promotes proven lessons into the playbooks, fixes rules that contradict the
  official docs, and prunes. It works on a separate copy, and the bot rejects any
  proposal that touches anything other than the playbooks and lessons, exceeds size
  limits, or deletes a playbook. **Nothing changes until you send `/learn approve`.**
  `/learn undo` reverts the last approval, and `/learn stats` shows whether failing
  rounds go down. If they don't, turn it off with `/learn off`.

## Customising the team

- **Agent behaviour:** edit `workspace-template/.claude/agents/*.md` before the
  first run. After that, the live copies are in the project (`.claude/agents/`),
  so you can ask the PM to change them or edit them with
  `docker compose exec agents nano /workspace/.claude/agents/<name>.md`. Plain Markdown with a
  short header. Add a new file to add a new agent (e.g. `devops.md`).
- **Model per agent:** in each agent file's header, `model: inherit` uses the
  PM's model. You can set `opus` or `sonnet` instead. For example, keep the
  designer and reviewer on `opus` and the developers on `sonnet` to stretch your
  usage limits.
- **PM behaviour:** `bot/prompts/pm.md`.
- **What counts as "green":** `scripts/verify.sh` in the project.
- **Limits:** `MAX_BUILD_ROUNDS` and `MAX_TURNS_PER_ROUND` in `.env`.

## Files

```
bot/bot.py                    Telegram bot (owner-only), commands
bot/orchestrator.py           PM session, build loop, independent verify, usage-limit pauses
bot/prompts/pm.md             Project Manager instructions
workspace-template/           Copied into ./workspace on first start:
  CLAUDE.md                     shared project memory for all agents
  .claude/agents/*.md           the five specialist agents
  docs/                         SPEC, PROGRESS (task board), DESIGN, API, DECISIONS
  scripts/verify.sh             lint + tests + build + e2e: the definition of green
Dockerfile, docker-compose.yml  sandboxed runtime (Python 3.12, Node 22, Chromium)
```

## Safety notes

- Agents run with full permissions **inside the container only**. Don't mount
  your home directory or Docker socket into it.
- Only your Telegram user id is served. Keep `.env` private. It contains a token
  that spends your Claude usage.
- Don't set `ANTHROPIC_API_KEY` unless you want to pay per token instead of
  using your subscription.
- Everything is committed to git per task, so you can roll back any step:
  `docker compose exec agents git -C /workspace log --oneline`.
