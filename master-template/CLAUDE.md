# Team memory (read by every agent)

This is the team's **master folder**. Every project lives in its own folder with its own git repo:

    projects/<name>/        one project = one git repo (its own docs/, scripts/verify.sh, CLAUDE.md)
    .claude/agents/         the team (shared by all projects)
    .claude/skills/         playbooks (Angular 22, Django 6.1, security, accessibility, design...)
    docs/LESSONS.md         lessons from all projects
    docs/STANDARDS.md       the external standards we follow

## Working on a project
- You are always briefed with a project folder (e.g. `projects/carstage`). Work inside it: paths in your
  instructions such as `docs/PROGRESS.md`, `docs/API.md`, `scripts/verify.sh`, `backend/`, `frontend/`
  are relative to THAT folder. Run commands from there (`cd projects/<name>`).
- Read the project's own `CLAUDE.md` (if it has one) before starting: it has that project's layout and
  commands, which override the defaults in the playbooks.
- Never change another project's folder unless the task says so.

## Git
- Each project is its own repo. Commit there with the task ID: `git commit -m "D-04: editor before/after"`.
- Work on a feature branch (the PM creates it): `git switch -c feature/<task-id>-<short-name>`.
- Never `git push`, never add remotes, never deploy: the bot publishes verified work (pull requests) and
  the owner merges and deploys.
- Never commit secrets. `.env` files are git-ignored; commit `.env.example`.

## Team rules
- `docs/API.md` (in the project) is the contract between backend and frontend. Change it first, then the code.
- Update your task's status in the project's `docs/PROGRESS.md` when you finish.
- Never weaken or delete tests to make them pass.
- Never run anything in watch mode or in the foreground forever: tests always with
  `--watch=false` (or `vitest run`), dev servers only in the background with a timeout,
  and stop them when done. A command that never exits freezes the whole team.

## Playbooks and lessons
- `.claude/skills/*/SKILL.md` (this master folder): follow them; they override general knowledge.
- `docs/LESSONS.md` (this master folder): read it before starting a task. Lessons apply to every project.
