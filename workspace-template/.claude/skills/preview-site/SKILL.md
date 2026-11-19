---
name: preview-site
description: Put a project on the owner's preview site (one public address, SITE_DOMAIN) and check it in a real browser. Use when the owner asks to deploy, publish, "put it online", show or preview a project, or when a project needs a preview/compose.yml.
---

# The preview site

The server has one public address (SITE_DOMAIN, e.g. `bbts.duckdns.org`) that shows **one
project at a time**: whatever was deployed last. It's for the owner to look at the work without
deploying anything themselves. Search engines and AI crawlers are told not to index it.

The server deploys only **committed** code, and only if the project has a `preview/compose.yml`.

## Deploying

1. Commit the work in the project's repo.
2. Call the team tool `deploy_preview_site` with the project's name (e.g. `tstauto`). By default it
   deploys the project's current branch; `branch` deploys another branch, tag or commit (looked up in
   the team's copy first, then on GitHub), without switching branches. It waits up to ~5 minutes; if
   it's still building, call `preview_site_status` with `wait_minutes: 5`.
3. When it's live, **check it in the browser** (below) before telling the owner it works.
4. If it failed, the tool returns the end of the deploy log: fix the cause, commit, deploy again.
   A failed deploy never takes the site down: the previous version keeps running.

The owner can do the same from Telegram: `/site <project> [branch]`, `/site`, `/site log`, `/site stop`.

## preview/compose.yml (one per project, committed)

A normal Docker Compose file with a few rules, enforced by the server (it refuses the deploy and says
why otherwise):

- **Paths are relative to the project root** (not to `preview/`): `build: {context: ., dockerfile: deploy/backend.Dockerfile}`.
- A service named **`web`** answers HTTP on port 80 (other port: label `bbts.site.port: "8000"` on `web`).
  It's the only thing reachable from outside; the address points at it.
- **No** `ports`, bind mounts (`./x:/y`), `privileged`, `cap_add`, `network_mode`, `devices`,
  `secrets`/`configs`, `env_file` outside the project, or custom volume drivers. Code goes into
  images with `COPY`; data goes into named volumes.
- Every service is limited to ~512 MB RAM and 1 CPU (the server also runs the agents). Keep it lean:
  `gunicorn --workers 2`, no dev servers, production builds.
- Available variables (interpolated by the server, `${NAME}`): `POSTGRES_PASSWORD`, `DJANGO_SECRET_KEY`
  (generated once per project), `SITE_URL` (`https://…`), `SITE_HOST` (bare host name), plus any
  `NAME=value` the owner put in the server's `/var/lib/bbts/site/<project>/secrets.env` (API keys).
  Never commit real secrets.
- Database migrations: run them when the backend starts (see the template). The database is kept
  between deploys of the same project.

### Template: Django + Angular

Reuse the project's production Dockerfiles if it has them (`deploy/*.Dockerfile`); otherwise write
them (multi-stage: Node builds Angular, Caddy or nginx serves it and forwards `/api/` and `/admin/`
to Django on `backend:8000`).

```yaml
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_DB: app
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: [db:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 5s
      retries: 20

  backend:
    build: {context: ., dockerfile: deploy/backend.Dockerfile}
    environment:
      DATABASE_URL: postgres://app:${POSTGRES_PASSWORD}@db:5432/app
      DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY}
      DJANGO_DEBUG: "0"
      DJANGO_ALLOWED_HOSTS: ${SITE_HOST},backend,localhost
      DJANGO_CSRF_TRUSTED_ORIGINS: ${SITE_URL}
    command: sh -c "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 60"
    depends_on:
      db: {condition: service_healthy}

  web:
    build: {context: ., dockerfile: deploy/frontend.Dockerfile}
    depends_on: [backend]

volumes:
  db:
```

Adjust names to the project (settings module, env var names its settings read, whether it uses
SQLite: then drop `db` and keep the SQLite file in a named volume). If the app needs demo data to
be worth looking at, add an idempotent `manage.py` command (e.g. `seed_demo`) to the backend's
start command.

## Checking it in the browser

You have a real browser (tools named `browser_*`, from the `browser` server):

- `browser_navigate` to the site's URL (the deploy result says it), then `browser_snapshot` to read
  the page (text and elements; cheaper than screenshots).
- Click through the main flows (`browser_click`, `browser_type`, `browser_fill_form`), and check
  `browser_console_messages` and `browser_network_requests` for errors (failed `/api/` calls, 404s,
  CORS/CSRF problems).
- `browser_take_screenshot` for the owner: screenshots land in the master folder's `.browser/`; send
  the useful ones with `send_screenshots_to_owner` (paths like `.browser/home.png`).
- Mobile check: `browser_resize` to 390×844.

The same browser works on the app while you build it (`http://localhost:4200` when the dev servers
run). Don't use it for general web browsing; it only reaches allowed sites.

## Common failures

- **Build fails**: read the log; usually a missing file in the Docker build context or a failing
  `npm ci`/`pip install`. The build runs with the project root as context.
- **"didn't start"**: `web` didn't answer on its port within ~3 minutes. Look at the service logs in
  the deploy log (crash on start, wrong port, migrations failing, missing env var).
- **400 Bad Request from Django**: `DJANGO_ALLOWED_HOSTS` doesn't include `${SITE_HOST}`.
- **CSRF errors on forms**: `DJANGO_CSRF_TRUSTED_ORIGINS` must be `${SITE_URL}`.
- **"isn't allowed on this server"**: the compose file uses something from the "No" list above.
