# Project memory (read by every agent)

## Layout
- `backend/`   Django + Django REST Framework (owner: backend-dev)
- `frontend/`  Angular (owner: frontend-dev)
- `e2e/`       Playwright end-to-end tests (owner: qa-engineer)
- `design/`    Mockups (`mockups/`) and screenshots (`screens/`) (owner: ui-designer)
- `docs/`      SPEC, PROGRESS (task board), DESIGN, API, DECISIONS
- `scripts/verify.sh`  The one command that decides whether the build is green
- `deploy/`, `.github/workflows/`  Production images, CI/CD (owner: devops-engineer, see docs/DEPLOYMENT.md)

## Commands
- Backend dev server:  `cd backend && python manage.py runserver 0.0.0.0:8000`
- Backend tests:       `cd backend && pytest`
- Frontend dev server: `cd frontend && npx ng serve --host 0.0.0.0 --proxy-config proxy.conf.json`
- Frontend tests:      `cd frontend && npx ng test --watch=false`
- E2E tests:           `cd e2e && npx playwright test`
- Everything:          `./scripts/verify.sh`

## Team rules
- Stay in your own folder unless the task says otherwise.
- `docs/API.md` is the contract between backend and frontend. Change it first,
  then the code.
- Update your task's status in `docs/PROGRESS.md` when you finish.
- Never weaken or delete tests to make them pass.
- Commit, but never `git push` and never trigger deploys: the bot pushes verified
  work and the owner deploys production.
- Never commit secrets. `.env` files are git-ignored; commit `.env.example`.
- Never run anything in watch mode or in the foreground forever: tests always with
  `--watch=false` (or `vitest run`), dev servers only in the background with a timeout,
  and stop them when done. A command that never exits freezes the whole team.

## Playbooks and lessons
- `.claude/skills/*/SKILL.md`: the team's Angular 22 and Django 6.1 playbooks. Follow them;
  they override general knowledge, which may be out of date.
- `docs/LESSONS.md`: mistakes made in this project and the rules they produced. Read it
  before starting a task.
