"""Telegram front-end for the agent team. Only the owner's Telegram user id is served."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sandbox

# Read secrets once and remove them from the environment, so agents, tests and
# previews (which inherit the environment) never see them.
_SECRETS = sandbox.load_and_scrub()

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update  # noqa: E402
from telegram.constants import ChatAction  # noqa: E402
from telegram.error import BadRequest, TelegramError  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import extras  # noqa: E402
import lab as lab_mod  # noqa: E402
from orchestrator import Config, Orchestrator  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = _SECRETS.get("TELEGRAM_BOT_TOKEN") or ""
OWNER_ID = int(os.environ.get("TELEGRAM_OWNER_ID", "0") or 0)
TG_LIMIT = 4000

# (command, menu description). Shown in Telegram's "/" menu and in /help.
COMMANDS = [
    ("status", "Progress of a project: /status [name] [all]"),
    ("build", "Build every project with open tasks: /build [names]"),
    ("stop", "Stop whatever is running"),
    ("today", "Today's progress (also sent each evening)"),
    ("changes", "What changed since you last looked"),
    ("screens", "Latest screenshots of the app and mockups"),
    ("ask", "Ask one specialist a question (read-only)"),
    ("undo", "Undo the last finished task"),
    ("preview", "Run the app on the server to try it"),
    ("verify", "Run the full test suite now"),
    ("push", "Test and push to GitHub now"),
    ("deploy", "Put staging's version live · history · drill"),
    ("server", "Server health and Claude usage"),
    ("agents", "Studio agents you can run on demand"),
    ("run", "Start a studio job: /run <agent> <brief>"),
    ("jobs", "Recent studio jobs"),
    ("job", "The current studio job"),
    ("pm", "Talk to the Project Manager again"),
    ("project", "Which project /status shows: /project <name>"),
    ("lab", "CarStage lab: what's running · spend · sync code"),
    ("learn", "Knowledge curator proposals"),
    ("live", "Pin a live, self-updating status message"),
    ("log", "Last steps the agents took"),
    ("reset", "Fresh PM conversation (files kept)"),
    ("help", "All commands"),
]
HELP = ("I'm your Project Manager. Just write to me normally – describe what you want built, "
        "answer my questions, give feedback.\n\nCommands\n"
        + "\n".join(f"/{c} – {d}" for c, d in COMMANDS)
        + "\n\nTip: type / to get this list as a menu.")


def owner_only(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != OWNER_ID:
            log.warning("Ignoring update from non-owner %s", user.id if user else None)
            if update.callback_query:
                await update.callback_query.answer()
            return
        return await handler(update, context)
    return wrapped


def keyboard(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                 for row in buttons])


async def send(app: Application, text: str, html_mode: bool = False, buttons=None):
    """Message the owner. Never crashes the bot: a failed notification is only logged.
    Buttons go on the last chunk. Returns the last sent message (or None)."""
    chunks = [text[i:i + TG_LIMIT] for i in range(0, max(len(text), 1), TG_LIMIT)] or ["…"]
    msg = None
    for n, chunk in enumerate(chunks):
        try:
            msg = await app.bot.send_message(
                chat_id=OWNER_ID, text=chunk or "…", parse_mode="HTML" if html_mode else None,
                disable_web_page_preview=True,
                reply_markup=keyboard(buttons) if n == len(chunks) - 1 else None)
        except TelegramError as e:
            if "chat not found" in str(e).lower():
                log.warning("Can't message the owner yet: open your bot in Telegram and press Start "
                            "(and check TELEGRAM_OWNER_ID is YOUR user id from @userinfobot).")
            else:
                log.warning("Telegram send failed: %s", e)
            return None
    return msg


async def send_image(app: Application, path, caption: str) -> None:
    try:
        with open(path, "rb") as f:
            await app.bot.send_photo(chat_id=OWNER_ID, photo=f, caption=caption[:1000])
    except TelegramError:
        try:
            with open(path, "rb") as f:
                await app.bot.send_document(chat_id=OWNER_ID, document=f, caption=caption[:1000])
        except TelegramError as e:
            log.warning("Could not send image %s: %s", path, e)


async def send_any_file(app: Application, path, caption: str) -> None:
    if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        await send_image(app, path, caption)
        return
    try:
        with open(path, "rb") as f:
            await app.bot.send_document(chat_id=OWNER_ID, document=f, caption=(caption or "")[:1000])
    except TelegramError as e:
        log.warning("Could not send file %s: %s", path, e)


async def keep_typing(app: Application, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await app.bot.send_chat_action(OWNER_ID, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            pass


async def with_typing(app: Application, coro):
    stop = asyncio.Event()
    typing = asyncio.create_task(keep_typing(app, stop))
    try:
        return await coro
    finally:
        stop.set()
        await typing


def build_app() -> Application:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in .env")
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    orch = Orchestrator(Config.from_env(),
                        notify=lambda t, buttons=None: send(app, t, buttons=buttons),
                        send_image=lambda p, c: send_image(app, p, c),
                        send_file=lambda p, c: send_any_file(app, p, c))
    tz = ZoneInfo(orch.cfg.timezone)

    # ------------------------------------------------------------ shared actions
    async def show_status(full: bool = False, project: str | None = None):
        await send(app, orch.status(full=full, project=project), html_mode=True,
                   buttons=[[("📝 Changes", "changes"), ("🖼 Screens", "screens"), ("☀️ Today", "today")]])

    async def stop_all():
        stopped = orch.stop()
        stopped = orch.studio.stop() or stopped
        await asyncio.sleep(3)   # let cancelled turns close their Claude process cleanly
        orch._kill_previews()
        killed = sandbox.kill_agent_processes()
        extra = f" Cleaned up {killed} leftover process(es)." if killed else ""
        await send(app, ("⏹ Stopped." if stopped else "Nothing was running.") + extra)

    async def deploy_preview():
        text, can = await orch.deploy_preview()
        await send(app, text, buttons=[[("✅ Deploy now", "deploy:yes"), ("✖️ Cancel", "deploy:no")]] if can else None)

    async def send_screens(which: str = ""):
        files = extras.screenshots(orch, which)
        if not files:
            await send(app, "No screenshots yet. They appear when the designer or QA saves them "
                            "(design/screens, design/mockups).")
            return
        handles = [open(p, "rb") for p in files]
        try:
            if len(files) == 1:
                await app.bot.send_photo(OWNER_ID, handles[0], caption=files[0].name)
            else:
                await app.bot.send_media_group(OWNER_ID, [InputMediaPhoto(h, caption=p.name)
                                                          for h, p in zip(handles, files)])
        except TelegramError as e:
            await send(app, f"Couldn't send the screenshots: {e}")
        finally:
            for h in handles:
                h.close()

    async def undo_preview():
        c = extras.undo_candidate(orch)
        if not c:
            await send(app, "There's no finished task to undo.")
            return
        await send(app, f"↩️ Undo this task?\n\n{c['subject']}\n{c['when']} · {c['stat'] or 'no file changes'}\n\n"
                        "This reverts its changes (a new commit, so it can be redone later).",
                   buttons=[[("↩️ Yes, undo it", f"undo:{c['sha'][:40]}"), ("✖️ Keep it", "undo:no")]])

    async def pm_message(text: str):
        async def still_working():
            await asyncio.sleep(45)
            act = extras.current_activity(orch)
            await send(app, "⏳ The PM is still working on it" + (f" – now: {act}" if act else "") +
                       ". /log shows each step.")
        nudge = asyncio.create_task(still_working())
        try:
            reply = await with_typing(app, orch.chat(text))
        finally:
            nudge.cancel()
        await send(app, reply)

    # ----------------------------------------------------------------- commands
    @owner_only
    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        job = orch.studio.active_job()
        if job:
            r = await orch.studio.message(update.message.text)
            await send(app, r or f"⏳ [{job['agent']}] working on it… (/pm to talk to the PM instead)")
            return
        try:
            await pm_message(update.message.text)
        except asyncio.CancelledError:
            await send(app, "⏹ Stopped.")

    @owner_only
    async def on_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
        wanted = [a.lower() for a in (context.args or [])]
        unknown = [w for w in wanted if w not in orch.project_names() and w != "all"]
        if unknown:
            await send(app, f"Unknown project(s): {', '.join(unknown)}. Projects: {', '.join(orch.project_names())}")
            return
        if not orch.start_build([w for w in wanted if w != "all"] or None):
            busy = orch.ops.busy() or ("A studio job is working." if orch.studio.busy else None)
            await send(app, busy or "Already building. /status to check, /stop to stop.")
        elif orch.cfg.multi:
            await send(app, f"🔨 Building: {', '.join(orch.state.build_scope)}")

    @owner_only
    async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await stop_all()

    @owner_only
    async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = [a.lower() for a in (context.args or [])]
        full = any(a in ("all", "full") for a in args)
        project = next((a for a in args if a in orch.project_names()), None)
        await show_status(full=full, project=project)

    @owner_only
    async def on_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
        busy = orch.ops.busy()
        if busy:
            await send(app, busy)
            return
        name = (context.args[0].lower() if context.args and context.args[0].lower() in orch.project_names()
                else orch.state.active_project or orch.main_name)
        await send(app, f"🧪 Running {name}'s full test suite (including end-to-end)…")
        async with orch.ops.run(f"a full test run of {name} (/verify)"):
            ok, summary = await orch.verify(full=True, project=name)
        await send(app, ("✅ " if ok else "❌ ") + "\n" + summary)

    @owner_only
    async def on_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(app, orch.recent_log())

    @owner_only
    async def on_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        busy = orch.ops.busy()
        if busy or (orch.build_task and not orch.build_task.done()):
            await send(app, (busy or "A build is running.") + " Send /stop first, then /reset.")
            return
        orch.reset_session()
        await send(app, "🔄 Fresh PM conversation. Project files and the task board are kept.")

    @owner_only
    async def on_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if orch.build_task and not orch.build_task.done():
            await send(app, "A build is running – it pushes automatically after every green round.")
            return
        busy = orch.ops.busy()
        if busy:
            await send(app, busy)
            return
        await send(app, "🧪 Running the tests before pushing…")
        async with orch.ops.run("a push (/push)"):
            ok, summary = await orch.verify()
            result = await orch.push() if ok else None
        if not ok:
            await send(app, "❌ Tests are failing, so I didn't push. Send /build to let the agents fix them.\n\n" + summary)
            return
        await send(app, result)

    @owner_only
    async def on_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
        arg = context.args[0].lower() if context.args else ""
        if arg in ("yes", "confirm", "y"):
            await send(app, await orch.deploy_production())
        elif arg == "drill":
            await send(app, await orch.deploy_drill())
        elif arg == "history":
            await send(app, await orch.deploy_history())
        else:
            await deploy_preview()

    @owner_only
    async def on_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
        arg = (context.args[0].lower() if context.args else "")
        if arg == "stop":
            await send(app, orch.preview_stop())
        elif arg in ("log", "logs", "status"):
            await send(app, orch.preview_log())
        else:
            busy = orch.ops.busy()
            await send(app, busy if busy else await orch.preview_start())

    @owner_only
    async def on_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await learn_action(context.args[0].lower() if context.args else "")

    async def learn_action(arg: str):
        c = orch.curator
        if arg == "now":
            await send(app, "🧠 Starting the curator… (takes a few minutes; I'll send the proposal)")
            asyncio.create_task(c.run(scheduled=False))
        elif arg == "diff":
            await send(app, c.diff())
        elif arg in ("approve", "yes"):
            await send(app, c.approve())
        elif arg in ("reject", "no"):
            await send(app, c.reject())
        elif arg == "undo":
            await send(app, c.undo())
        elif arg == "stats":
            await send(app, c.stats())
        elif arg in ("off", "on"):
            await send(app, c.set_enabled(arg == "on"))
        else:
            await send(app, c.status())

    @owner_only
    async def on_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
        arg = context.args[0].lower() if context.args else ""
        if arg in ("off", "on"):
            orch.state.digest_enabled = arg == "on"
            orch._save_state()
            await send(app, f"Evening digest {'ON' if arg == 'on' else 'OFF'} (at {orch.cfg.digest_time}).")
            return
        text, _ = extras.today(orch)
        await send(app, text, html_mode=True)

    @owner_only
    async def on_changes(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(app, extras.changes(orch), html_mode=True)

    @owner_only
    async def on_screens(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send_screens(context.args[0].lower() if context.args else "")

    @owner_only
    async def on_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(app, await extras.server(orch), html_mode=True)

    @owner_only
    async def on_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await undo_preview()

    @owner_only
    async def on_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await send(app, "Usage: /ask <agent> <question>\nAgents: " + ", ".join(sorted(extras.team_agents(orch))))
            return
        name, question = context.args[0], " ".join(context.args[1:])
        await send(app, await with_typing(app, extras.ask(orch, name, question)))

    @owner_only
    async def on_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await refresh_live(force_new=True)

    @owner_only
    async def on_agents(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(app, orch.studio.list_agents_text())

    @owner_only
    async def on_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await send(app, orch.studio.list_agents_text())
            return
        await send(app, await orch.studio.start(context.args[0], " ".join(context.args[1:])))

    @owner_only
    async def on_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(app, orch.studio.jobs_text())

    @owner_only
    async def on_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
        sub = context.args[0].lower() if context.args else ""
        if sub == "mode":
            if len(context.args) < 3 or context.args[2].lower() not in ("main", "pr"):
                await send(app, "Usage: /project mode <name> pr|main\n"
                                "pr = feature branches + pull requests · main = push main after green tests "
                                "(the main project only: its CI deploys staging)")
                return
            name, mode = context.args[1].lower(), context.args[2].lower()
            if name not in orch.project_names():
                await send(app, f"Unknown project {name!r}.")
                return
            if mode == "main" and name != orch.main_name:
                await send(app, "Only the main project can push straight to main (its CI deploys staging).")
                return
            orch.state.projects.setdefault(name, {})["mode"] = mode
            orch._save_state()
            await send(app, f"OK: {name} now ships via " + ("pull requests from feature branches." if mode == "pr"
                                                           else "pushes to main (CI → staging)."))
            return
        if sub in ("link", "push", "pull"):
            if len(context.args) < 2 or (sub == "link" and len(context.args) < 3):
                await send(app, "Usage:\n/project link <name> <owner/repo>\n/project push <name>\n/project pull <name>")
                return
            name = context.args[1].lower()
            coro = (orch.repos.link(name, context.args[2]) if sub == "link" else
                    orch.repos.publish(name) if sub == "push" else orch.repos.pull(name))
            await send(app, await with_typing(app, coro))
            return
        if context.args:
            want = context.args[0].lower()
            if want in ("main", orch.main_name):
                orch.state.active_project = None
            elif want in orch.state.projects:
                orch.state.active_project = want
            else:
                await send(app, f"Unknown project {want!r}. Known: main, " + ", ".join(orch.state.projects))
                return
            orch._save_state()
            await show_status()
            return
        cur = orch.state.active_project
        rows = [f"{'👉' if cur in (None, orch.main_name) else '•'} {html.escape(orch.main_name)} – "
                f"{html.escape(orch.project_root(orch.main_name)[0])} (main project)"]
        for name, info in orch.state.projects.items():
            if name == orch.main_name:
                continue
            repo = f" · github.com/{html.escape(info['repo'])}" if info.get("repo") else ""
            rows.append(f"{'👉' if cur == name else '•'} {html.escape(name)} – {html.escape(info.get('title', name))} "
                        f"(<code>{html.escape(info['path'])}</code>){repo}")
        await send(app, "<b>Projects</b>\n" + "\n".join(rows) +
                   "\n\n/project &lt;name&gt; switches what /status shows (the PM switches it too).\n"
                   "/project link &lt;name&gt; &lt;owner/repo&gt; connects a project to its GitHub repo\n"
                   "/project push &lt;name&gt; publishes the agents' work as a pull request\n"
                   "/project pull &lt;name&gt; brings GitHub's main into the agents' copy\n"
                   "/project mode &lt;name&gt; pr|main sets how its work ships\n"
                   "/build [names] builds every project with open tasks, or only those named", html_mode=True)

    @owner_only
    async def on_lab(update: Update, context: ContextTypes.DEFAULT_TYPE):
        arg = context.args[0].lower() if context.args else ""
        job = orch.pm_lab_job()
        if arg == "budget" and len(context.args) > 1:
            try:
                value = float(context.args[1].replace("$", ""))
                assert 0 < value <= 1000
            except (ValueError, AssertionError):
                await send(app, "Usage: /lab budget 10   (US dollars)")
                return
            job["lab_budget"] = value
            orch._save_state()
            await send(app, f"💰 PM lab budget: ${value:.2f} per testing round "
                            f"(${float(job.get('lab_round_spent') or 0):.2f} used this round).")
        elif arg == "reset":
            job["lab_spent"] = 0.0
            job["lab_round_spent"] = 0.0
            orch._save_state()
            await send(app, "🔄 PM lab spend counter reset to $0.")
        elif arg == "sync":
            await lab_sync(apply=False)
        elif arg == "update":
            note = await orch.repos.refresh_lab(force=True)
            await send(app, note or "The lab already runs the latest GitHub main (or CarStage isn't linked: /project).")
        else:
            note = await orch.repos.refresh_lab()
            await send(app, (f"🔄 {note}\n\n" if note else "") + lab_view(), html_mode=True)

    def lab_view() -> str:
        e = html.escape
        lb = orch.studio.lab
        problem = lab_mod.status_text()
        lines = [f"🧪 <b>CarStage lab</b> · {'✅ ready' if not problem else '⚠️ ' + e(problem)}",
                 f"📦 Code: {e(lb.version_text())}"]
        cur = lb.current
        if cur:
            secs = int(time.time() - cur["started"])
            lines.append(f"⏳ <b>Now:</b> [{e(cur['who'])}] run {cur['run']} · {e(cur['pipeline'])}"
                         f"{' · ' + e(cur['label']) if cur['label'] else ''} · {e(cur['car'])} · {secs // 60}m {secs % 60:02d}s")
        else:
            lines.append("💤 No run in progress")

        def ledger(name, j):
            return (f"{name}: round {int(j.get('lab_round') or 1)} · ${float(j.get('lab_round_spent') or 0):.2f} of "
                    f"${float(j.get('lab_budget') or lb.budget):.2f} · total ${float(j.get('lab_spent') or 0):.2f} "
                    f"in {int(j.get('lab_runs') or 0)} runs")
        pm = orch.pm_lab_job()
        dirs = [Path(pm["dir"])]
        lines += ["", "<b>Budgets</b> (per testing round; your next message starts a new round)", e(ledger("PM", pm))]
        lab_jobs = sorted((j for j in orch.state.studio.get("jobs", {}).values() if j.get("agent") == "model-lab"),
                          key=lambda j: j.get("created", 0), reverse=True)[:2]
        for j in lab_jobs:
            active = orch.studio.active_job() is j
            lines.append(e(ledger(f"Job {j['id']}{' (active)' if active else ''}", j)))
            dirs.append(Path(j["dir"]))
        runs = lab_mod.recent_runs(dirs, 8)
        if runs:
            lines += ["", "<b>Latest runs</b>"]
            for r in runs:
                when = datetime.fromtimestamp(r["when"], tz).strftime("%H:%M")
                tail = f" · {e(r['error'][:70])}" if not r["ok"] and r["error"] else ""
                who = "PM" if r["where"] == "pm-lab" else "job"
                lines.append(f"{'✅' if r['ok'] else '❌'} {when} {e(r['name'])} · {r['seconds']} s"
                             f"{' · ' + e(r['car']) if r['car'] else ''} · {who}{tail}")
        lines += ["", "/lab update (pull GitHub main now) · /lab sync (test the PM's unmerged work) · "
                  "/lab budget &lt;usd&gt; · /lab reset"]
        return "\n".join(lines)

    async def lab_sync(apply: bool):
        src, dst = orch.project_root(os.environ.get("LAB_PROJECT", "carstage"))[1], lab_mod.CODE_DIR
        if not (src / "backend").is_dir():
            await send(app, f"Nothing to sync: the PM has no CarStage copy at {src}.")
            return
        plan = await asyncio.to_thread(lab_mod.sync_plan, src, dst)
        total = len(plan["added"]) + len(plan["changed"]) + len(plan["removed"])
        if not total:
            await send(app, "✅ The lab already runs the PM's current CarStage code.")
            return
        if apply:
            try:
                n = await asyncio.to_thread(lab_mod.sync_apply, src, dst)
            except OSError as e:
                await send(app, f"❌ Couldn't update the lab code: {e}\n"
                           "If it says read-only, update docker-compose.yml from the latest kit and run "
                           "docker compose up -d.")
                return
            orch._log(f"LAB SYNC: {n} file(s) from {src}")
            orch.state.lab_code.update({"source": "the PM's working copy (approved /lab sync, not merged yet)",
                                        "sha": "", "at": time.time()})
            orch._save_state()
            await send(app, f"✅ Lab code updated ({n} file(s)). New lab runs use it from now on.")
            return
        def lst(name, items):
            return f"<b>{name} ({len(items)})</b>\n" + "\n".join(f"• {html.escape(i)}" for i in items[:15]) + \
                   ("\n• …" if len(items) > 15 else "") + "\n" if items else ""
        text = ("🧪 <b>Install the PM's CarStage changes in the lab?</b>\nThe lab runs this code with your "
                "provider keys, so check the list.\n\n" + lst("Changed", plan["changed"]) +
                lst("Added", plan["added"]) + lst("Removed", plan["removed"]))
        if plan["flags"]:
            text += ("\n⚠️ <b>New lines worth a look</b> (network, processes, environment):\n" +
                     "\n".join(f"<code>{html.escape(f)}</code>" for f in plan["flags"][:12]))
        else:
            text += "\n✅ No new network, process or environment access in the changed Python code."
        await send(app, text, html_mode=True,
                   buttons=[[("✅ Install in lab", "labsync:yes"), ("✖️ Cancel", "labsync:no")]])

    @owner_only
    async def on_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await job_action(context.args[0].lower() if context.args else "",
                         " ".join(context.args[1:]) if context.args else "")

    async def job_action(arg: str, rest: str = ""):
        st = orch.studio
        if arg == "files":
            await send(app, st.files_text())
        elif arg == "send":
            await send(app, await st.send_output(rest))
        elif arg == "stop":
            await send(app, "Stopping the job…" if st.stop() else "The job isn't working on anything.")
        elif arg == "end":
            await send(app, st.end())
        elif arg == "open":
            await send(app, st.open(rest))
        elif arg == "budget":
            await send(app, st.set_budget(rest))
        else:
            await send(app, st.job_text())

    @owner_only
    async def on_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(app, orch.studio.deactivate())

    @owner_only
    async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if msg.document:
            tg_file, name = await msg.document.get_file(), msg.document.file_name or "file"
        elif msg.photo:
            tg_file, name = await msg.photo[-1].get_file(), f"photo-{msg.message_id}.jpg"
        else:
            return
        data = bytes(await tg_file.download_as_bytearray())
        dest, where = orch.studio.save_upload(data, name, msg.caption or "")
        if where == "brand":
            await send(app, f"🏷 Saved as your logo: {dest}. Studio agents will use it.")
        elif where == "job":
            job = orch.studio.active_job()
            note = (f"The owner sent a file: input/{dest.name}"
                    + (f" with the message: {msg.caption}" if msg.caption else ". Use it for this job."))
            r = await orch.studio.message(note)
            await send(app, r or f"📎 Saved to the job and passed to [{job['agent']}].")
        else:
            await send(app, f"📎 Saved to {dest}. Start a job with /run, or send it with the caption 'logo' "
                            "to use it as your logo.")

    @owner_only
    async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(app, HELP)

    # ------------------------------------------------------------------ buttons
    ONE_SHOT = ("labsync:", "deploy:", "undo:", "learn:approve", "learn:reject", "pm:defaults", "job:defaults", "job:end")

    @owner_only
    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = q.data or ""
        await q.answer()
        if data.startswith(ONE_SHOT):     # prevent double taps on decisions
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
        if data == "status":
            await show_status()
        elif data == "stop":
            await stop_all()
        elif data == "changes":
            await send(app, extras.changes(orch), html_mode=True)
        elif data == "today":
            await send(app, extras.today(orch)[0], html_mode=True)
        elif data.startswith("screens"):
            await send_screens(data.split(":", 1)[1] if ":" in data else "")
        elif data == "deploy:preview":
            await deploy_preview()
        elif data == "deploy:yes":
            await send(app, await orch.deploy_production())
        elif data == "deploy:no":
            await send(app, "OK, production stays as it is.")
        elif data.startswith("learn:"):
            await learn_action(data.split(":", 1)[1])
        elif data == "pm:defaults":
            await pm_message("Go with your suggested defaults.")
        elif data == "job:defaults":
            r = await orch.studio.message("Go with your suggested defaults.")
            await send(app, r or "👍 Passed to the job.")
        elif data.startswith("job:"):
            await job_action(data.split(":", 1)[1])
        elif data == "labsync:yes":
            await lab_sync(apply=True)
        elif data == "labsync:no":
            await send(app, "OK, the lab keeps its current code.")
        elif data == "undo:no":
            await send(app, "OK, kept.")
        elif data.startswith("undo:"):
            await send(app, extras.undo(orch, data.split(":", 1)[1]))

    # ------------------------------------------- live status + evening digest loop
    live = {"was_building": False, "last_edit": 0.0}

    def live_text() -> str:
        now = datetime.now(tz).strftime("%H:%M")
        act = extras.current_activity(orch) if orch.build_task and not orch.build_task.done() else None
        tail = f"\n\n⏱ <i>Now: {html.escape(act)}</i>" if act else ""
        return orch.status() + tail + f"\n<i>🔄 live · updated {now}</i>"

    async def refresh_live(force_new: bool = False):
        mid = orch.state.live_msg_id
        if mid and not force_new:
            try:
                await app.bot.edit_message_text(live_text(), chat_id=OWNER_ID, message_id=mid,
                                                parse_mode="HTML", disable_web_page_preview=True)
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                log.info("Live status message gone (%s) – creating a new one", e)
            except TelegramError as e:
                log.warning("Live status edit failed: %s", e)
                return
        msg = await send(app, live_text(), html_mode=True)
        if msg:
            orch.state.live_msg_id = msg.message_id
            orch._save_state()
            try:
                await app.bot.pin_chat_message(OWNER_ID, msg.message_id, disable_notification=True)
            except TelegramError as e:
                log.info("Couldn't pin the live status: %s", e)

    async def background_loop():
        while True:
            try:
                building = bool(orch.build_task and not orch.build_task.done())
                if building and not live["was_building"]:
                    await refresh_live(force_new=orch.state.live_msg_id is None)
                    live["last_edit"] = time.time()
                elif building and time.time() - live["last_edit"] >= 60:
                    await refresh_live()
                    live["last_edit"] = time.time()
                elif live["was_building"] and not building and orch.state.live_msg_id:
                    await refresh_live()        # final state after the build stops
                live["was_building"] = building

                if orch.digest_due():
                    text, happened = extras.today(orch)
                    if happened:
                        await send(app, "🌆 Evening digest\n\n" + text, html_mode=True,
                                   buttons=[[("📋 Status", "status"), ("📝 Changes", "changes")]])
                    orch.mark_digest_sent()
            except Exception:
                log.exception("background loop error")
            await asyncio.sleep(20)

    handlers = {"start": on_help, "help": on_help, "build": on_build, "stop": on_stop, "status": on_status,
                "verify": on_verify, "log": on_log, "reset": on_reset, "push": on_push, "deploy": on_deploy,
                "preview": on_preview, "learn": on_learn, "agents": on_agents, "run": on_run, "jobs": on_jobs,
                "job": on_job, "pm": on_pm, "today": on_today, "changes": on_changes, "screens": on_screens,
                "server": on_server, "undo": on_undo, "ask": on_ask, "live": on_live, "lab": on_lab, "project": on_project}
    for name, fn in handlers.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))

    async def on_startup(app: Application):
        orch.curator.start_scheduler()
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except TelegramError as e:
            log.warning("Couldn't set the command menu: %s", e)
        asyncio.create_task(background_loop())
        await send(app, "🟢 Agent team online. Type / for the command menu.")
    app.post_init = on_startup
    app.bot_data["orch"] = orch
    app.bot_data["handlers"] = handlers
    return app


if __name__ == "__main__":
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
