# Deployment

Owned by devops-engineer. Everyone must keep the app compatible with this.

## How code gets to users

1. Agents commit finished tasks locally.
2. After every build round where `scripts/verify.sh` passes, the **bot** pushes
   `main` to GitHub. Agents never push themselves.
3. GitHub Actions (`.github/workflows/pipeline.yml`) runs verify again, builds two
   images (`deploy/backend.Dockerfile`, `deploy/frontend.Dockerfile`), pushes them
   to GitHub's registry, and deploys them to **staging** (`deploy/deploy.sh`).
4. **Production** is deployed only when the owner sends `/deploy` in Telegram
   (`deploy-production.yml`). It promotes the exact images running on staging.
   Agents must never trigger a production deploy.

Docker is not available inside the agent container, and agents have no GitHub
access. Images are built in CI. When CI fails, the bot saves the failure log to
`.ci-logs/run-<id>.log` (git-ignored) for the devops-engineer.

## Production architecture (one small app server)

```
Internet ─► Caddy proxy (HTTPS, password on staging)
              ├─► web-production ─┬─ Angular static files
              │                   └─ /api/*, /admin/*, /django-static/* ─► backend (gunicorn) ─► Postgres
              └─► web-staging    (same, separate database)
```

## Contract the app must meet

### Backend (Django)
- `backend/requirements.txt` includes `gunicorn`, `psycopg[binary]`,
  `dj-database-url`, `whitenoise`.
- WSGI module is `config.wsgi` (project package `backend/config/`).
- All settings come from environment variables, with safe local defaults:
  | Variable | Production value | Local default |
  |---|---|---|
  | `DJANGO_SECRET_KEY` | set by server | an insecure dev key |
  | `DJANGO_DEBUG` | `0` | `1` |
  | `DJANGO_ALLOWED_HOSTS` | comma-separated | `localhost,127.0.0.1` |
  | `DJANGO_CSRF_TRUSTED_ORIGINS` | comma-separated, e.g. `https://shop.ro` | empty |
  | `DATABASE_URL` | `postgres://…` | SQLite file (via `dj-database-url` default) |
- `STATIC_URL = "/django-static/"`, `STATIC_ROOT = BASE_DIR / "staticfiles"`,
  WhiteNoise middleware enabled. `python manage.py collectstatic` must work with only
  `DJANGO_SECRET_KEY` set (no database needed).
- Behind the HTTPS proxy: `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`,
  and when `DEBUG` is off: `SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True`.
- `GET /api/health/` returns HTTP 200 without authentication (used by the deploy
  health check). Keep it cheap: it may check the DB connection, nothing else.
- Migrations must be safe to run on every deploy (`migrate --noinput`).

### Frontend (Angular)
- The production build (`ng build --configuration production`) calls the API at
  the relative path `/api/v1/`, never a hard-coded host.
- Same origin as the API in production, so no CORS is needed there.

## Rules
- Never commit secrets. Server secrets live in `/opt/app/<env>/.env` on the app
  server; CI secrets live in GitHub repository secrets.
- A change that needs a new environment variable must be documented here and in
  `docs/DECISIONS.md`, and the owner must be told (it needs adding on the server).
