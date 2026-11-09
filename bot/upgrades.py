"""One-time, idempotent upgrades applied to an existing project when the kit is updated."""
from __future__ import annotations

import subprocess
from pathlib import Path

PLAYBOOKS = {
    "frontend-dev": ["angular-components", "angular-forms", "angular-testing"],
    "backend-dev": ["django-api", "django-security", "django-testing"],
    "qa-engineer": ["angular-testing", "django-testing"],
    "code-reviewer": ["angular-components", "angular-forms", "angular-testing",
                      "django-api", "django-security", "django-testing"],
    "devops-engineer": ["django-security"],
}

CLAUDE_MD_ADDITION = """
## Playbooks and lessons
- `.claude/skills/*/SKILL.md`: the team's Angular 22 and Django 6.1 playbooks. Follow them;
  they override general knowledge, which may be out of date.
- `docs/LESSONS.md`: mistakes made in this project and the rules they produced. Read it
  before starting a task.
"""


def playbook_section(name: str) -> str:
    lines = ["", "## Playbooks (read before you start)",
             "Read these and follow them. They override your general knowledge, which may be out of date:"]
    lines += [f"- `.claude/skills/{p}/SKILL.md`" for p in PLAYBOOKS[name]]
    lines.append("- `docs/LESSONS.md`: past mistakes in this project and the rules they produced.")
    if any(p.startswith("angular") for p in PLAYBOOKS[name]):
        lines.append("- For Angular questions the playbooks don't answer, use the `angular-cli` tools "
                     "(`get_best_practices`, `search_documentation`) instead of relying on memory.")
    if name == "code-reviewer":
        lines.append("- A violation of a playbook rule is a finding. Quote the rule.")
    return "\n".join(lines) + "\n"


def link_playbooks(ws: Path) -> list[str]:
    """Point each agent at its playbooks; fix advice that is outdated for Angular 22."""
    changed = []
    for name in PLAYBOOKS:
        f = ws / ".claude" / "agents" / f"{name}.md"
        if not f.exists():
            continue
        s = f.read_text()
        new = s.replace("`OnPush` change detection, strict", "strict")
        new = new.replace("@angular/cli@latest new frontend", "@angular/cli@22 new frontend")
        if "## Playbooks" not in new:
            new = new.rstrip("\n") + "\n" + playbook_section(name)
        if new != s:
            f.write_text(new)
            changed.append(str(f.relative_to(ws)))
    cm = ws / "CLAUDE.md"
    if cm.exists() and "LESSONS.md" not in cm.read_text():
        cm.write_text(cm.read_text().rstrip("\n") + "\n" + CLAUDE_MD_ADDITION)
        changed.append("CLAUDE.md")
    return changed


def commit(ws: Path, paths: list[str], message: str) -> None:
    if paths:
        import sandbox
        sandbox.give_to_agent(ws)
        sandbox.git("add", "--", *paths, cwd=ws)
        sandbox.git("commit", "-q", "-m", message, "--", *paths, cwd=ws)


QA_PLAYWRIGHT_NOTE = """
## Playwright version
The server has Playwright 1.65.0 (browsers in /ms-playwright). Pin `@playwright/test`
in `e2e/package.json` to the same version (`"@playwright/test": "1.65.0"`). If you must use
another version, run `npx playwright install chromium` for it.
"""


def fix_tools(ws: Path) -> list[str]:
    """Use the pinned `playwright` command and keep e2e on the same Playwright version."""
    changed = []
    f = ws / ".claude" / "agents" / "ui-designer.md"
    if f.exists():
        s = f.read_text()
        new = s.replace("npx -y playwright screenshot", "playwright screenshot")
        if new != s:
            f.write_text(new)
            changed.append(str(f.relative_to(ws)))
    q = ws / ".claude" / "agents" / "qa-engineer.md"
    if q.exists() and "## Playwright version" not in q.read_text():
        q.write_text(q.read_text().rstrip("\n") + "\n" + QA_PLAYWRIGHT_NOTE)
        changed.append(str(q.relative_to(ws)))
    return changed


OLD_DEVOPS = '- **Fix a failed pipeline:** find the run with `gh run list --limit 5`, read the\n  failure with `gh run view <id> --log-failed`, fix the cause, and make sure'
NEW_DEVOPS = '- **Fix a failed pipeline:** you have no GitHub access (by design). When a run fails,\n  the bot saves its failure log to `.ci-logs/run-<id>.log` and the PM gives you the path.\n  Read it, fix the cause, and make sure'
OLD_DEPLOY = 'Docker is not available inside the agent container. Images are built in CI, so\ncheck CI with `gh run list` and `gh run view <id> --log-failed`.'
NEW_DEPLOY = 'Docker is not available inside the agent container, and agents have no GitHub\naccess. Images are built in CI. When CI fails, the bot saves the failure log to\n`.ci-logs/run-<id>.log` (git-ignored) for the devops-engineer.'


def no_github_for_agents(ws: Path) -> list[str]:
    """Agents lost GitHub access: point the devops agent and DEPLOYMENT.md at .ci-logs/."""
    changed = []
    for rel, a, b in ((".claude/agents/devops-engineer.md", OLD_DEVOPS, NEW_DEVOPS),
                      ("docs/DEPLOYMENT.md", OLD_DEPLOY, NEW_DEPLOY)):
        f = ws / rel
        if f.exists() and a in f.read_text():
            f.write_text(f.read_text().replace(a, b))
            changed.append(rel)
    gi = ws / ".gitignore"
    if gi.exists() and ".ci-logs/" not in gi.read_text():
        gi.write_text(gi.read_text().rstrip("\n") + "\n\n# CI failure logs fetched by the bot\n.ci-logs/\n")
        changed.append(".gitignore")
    return changed


SECURE_DEPLOY_FILES = ("deploy/deploy.sh", "deploy/deploy-gate", "deploy/server-setup.sh", "deploy/compose.yml",
                       ".github/workflows/deploy-production.yml", ".github/workflows/deploy-drill.yml")


def secure_deploy_v1(ws: Path, template: Path) -> list[str]:
    """Replace deploy files with the gate-based versions (security-critical, so overwritten)
    and patch the pipeline/frontend image for pinned host keys and /version.json."""
    import shutil
    changed = []
    for rel in SECURE_DEPLOY_FILES:
        src, dst = template / rel, ws / rel
        if src.exists() and (not dst.exists() or dst.read_bytes() != src.read_bytes()):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed.append(rel)
    p = ws / ".github/workflows/pipeline.yml"
    if p.exists():
        s = p.read_text()
        new = s
        if "DEPLOY_KNOWN_HOSTS" not in new:
            new = new.replace("          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}\n          REGISTRY_TOKEN",
                              "          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}\n"
                              "          DEPLOY_KNOWN_HOSTS: ${{ secrets.DEPLOY_KNOWN_HOSTS }}\n          REGISTRY_TOKEN")
        if "APP_VERSION" not in new:
            new = new.replace("          file: deploy/frontend.Dockerfile\n          push: true",
                              "          file: deploy/frontend.Dockerfile\n"
                              "          build-args: APP_VERSION=${{ steps.meta.outputs.tag }}\n          push: true")
        if new != s:
            p.write_text(new)
            changed.append(str(p.relative_to(ws)))
    f = ws / "deploy/frontend.Dockerfile"
    if f.exists() and "version.json" not in f.read_text():
        s = f.read_text()
        new = s.replace("FROM caddy:2-alpine\nCOPY --from=build /out /srv",
                        "FROM caddy:2-alpine\nARG APP_VERSION=dev\nCOPY --from=build /out /srv\n"
                        "# Public, harmless build info: lets the bot show which version is really running.\n"
                        "RUN printf '{\"version\":\"%s\"}\\n' \"$APP_VERSION\" > /srv/version.json")
        if new != s:
            f.write_text(new)
            changed.append("deploy/frontend.Dockerfile")
    for sh in ("deploy/deploy.sh", "deploy/deploy-gate", "deploy/server-setup.sh"):
        if (ws / sh).exists():
            (ws / sh).chmod(0o755)
    return changed


def verify_fail_loudly(ws: Path) -> list[str]:
    f = ws / "scripts" / "verify.sh"
    old = "    pip install -q -r backend/requirements.txt >/dev/null 2>&1 || true"
    new = '    run "backend: install dependencies" bash -c "cd backend && pip install -q -r requirements.txt"'
    if f.exists() and old in f.read_text():
        f.write_text(f.read_text().replace(old, new))
        return ["scripts/verify.sh"]
    return []


# ---------------------------------------------------------------- agents v2
PLAYBOOKS_V2 = {
    "frontend-dev": ["angular-components", "angular-forms", "angular-testing", "accessibility", "web-performance"],
    "backend-dev": ["django-api", "django-security", "django-testing", "security-review"],
    "qa-engineer": ["angular-testing", "django-testing", "accessibility", "web-performance"],
    "code-reviewer": ["angular-components", "angular-forms", "angular-testing", "django-api", "django-security",
                      "django-testing", "security-review", "accessibility", "web-performance"],
    "devops-engineer": ["django-security", "security-review", "web-performance"],
}

QA_LAYERS = """
## Test layers (all of them, proportionate to risk)
1. **Unit:** components, services, models, serializers.
2. **API:** contract (matches `docs/API.md`), permissions (anonymous, other user → 404), validation errors.
3. **Integration:** frontend and backend together (forms submit, errors map to fields).
4. **End-to-end:** every journey in SPEC.md at 390px and 1440px, with an **axe-core** scan on each
   main screen (`accessibility` playbook).
5. **Regression:** a test for every bug fixed, so it can never come back.
Also, where relevant: migrations on a copy of realistic data, import files with bad rows,
Romanian diacritics, and timezone edges for expiry dates.
**Test-first for risky features** (auth, permissions, imports, money): write the failing tests from
the acceptance criteria *before* the developer starts, so tests check the spec, not the implementation.
"""

REVIEWER_STANDARDS = """
## Standards
Check changes against `docs/STANDARDS.md`. A violation of any row is a `[blocker]` unless
`docs/DECISIONS.md` records an owner-approved exception. For security-relevant changes, walk the
`security-review` checklist and tag findings with the OWASP Top 10:2025 category (A01–A10).
For UI changes, check WCAG 2.2 AA (`accessibility`) and the performance budgets (`web-performance`).
Report a short score per area: correctness, security, tests, maintainability, performance, consistency.
You may reject a task even when all automated tests pass.
"""

OLD_DESIGNER_MARKER = "designs state-of-the-art, modern web"


def _add_playbook_lines(text: str, names: list[str]) -> str:
    lines = [f"- `.claude/skills/{n}/SKILL.md`" for n in names] + ["- `docs/STANDARDS.md`: the standards we follow and how they're checked."]
    if "## Playbooks" not in text:
        return text.rstrip("\n") + "\n\n## Playbooks (read before you start)\n" + "\n".join(lines) + "\n"
    head, _, rest = text.partition("## Playbooks")
    first_line, _, body = rest.partition("\n")
    missing = [l for l in lines if l.split("`")[1] not in body]
    if not missing:
        return text
    # insert right after the last existing playbook bullet (or at the start of the list)
    blines = body.split("\n")
    last = max((i for i, l in enumerate(blines) if l.startswith("- `.claude/skills/")), default=None)
    if last is None:
        last = next((i - 1 for i, l in enumerate(blines) if l.startswith("- ")), len(blines) - 1)
    blines[last + 1:last + 1] = missing
    body = "\n".join(blines)
    return head + "## Playbooks" + first_line + "\n" + body


def agents_v2(ws: Path, template: Path) -> list[str]:
    """Design overhaul + standards: new designer (if still ours), art-director, new playbooks per agent."""
    import shutil
    changed = []
    agents = ws / ".claude" / "agents"
    ud = agents / "ui-designer.md"
    tpl_ud = template / ".claude" / "agents" / "ui-designer.md"
    if ud.exists() and tpl_ud.exists() and tpl_ud.read_text() != ud.read_text():
        if OLD_DESIGNER_MARKER in ud.read_text():
            shutil.copy2(tpl_ud, ud)          # untouched template version -> upgrade in place
        elif "design-direction" not in ud.read_text():
            ud.write_text(ud.read_text().rstrip("\n") + "\n\n## Design process v2 (overrides earlier steps)\n"
                          "Follow `.claude/skills/design-direction/SKILL.md`: brand brief, three directions for the "
                          "owner to choose from, zero banned defaults, one signature element, then `art-director` review.\n")
        changed.append(str(ud.relative_to(ws)))
    for name, books in PLAYBOOKS_V2.items():
        f = agents / f"{name}.md"
        if not f.exists():
            continue
        s = f.read_text()
        new = _add_playbook_lines(s, books)
        if name == "qa-engineer" and "## Test layers" not in new:
            new = new.rstrip("\n") + "\n" + QA_LAYERS
        if name == "code-reviewer" and "## Standards" not in new:
            new = new.rstrip("\n") + "\n" + REVIEWER_STANDARDS
        if new != s:
            f.write_text(new)
            changed.append(str(f.relative_to(ws)))
    return changed


NO_WATCH_RULE = """- Never run anything in watch mode or in the foreground forever: tests always with
  `--watch=false` (or `vitest run`), dev servers only in the background with a timeout,
  and stop them when done. A command that never exits freezes the whole team."""


def no_watch_rule(ws: Path) -> list[str]:
    f = ws / "CLAUDE.md"
    if not f.exists() or "watch mode" in f.read_text():
        return []
    text = f.read_text()
    anchor = "## Team rules\n"
    text = text.replace(anchor, anchor + NO_WATCH_RULE + "\n", 1) if anchor in text else text + "\n" + NO_WATCH_RULE + "\n"
    f.write_text(text)
    return ["CLAUDE.md"]
