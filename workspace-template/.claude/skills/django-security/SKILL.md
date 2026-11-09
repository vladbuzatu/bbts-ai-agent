---
name: django-security
description: Security checklist for the Django 6.1 backend and its Angular client - auth, permissions, CSRF, uploads, secrets, headers. Use when building or reviewing auth, permissions, file uploads, or anything touching user data.
---

# Django security checklist (Django 6.1 + DRF)

Use this when you build or review anything listed below. A finding here is a
`[blocker]` in code review, not a nit.

## Authentication
- Same-site Angular app: **session auth with CSRF** (DRF `SessionAuthentication`). Don't
  add JWT unless SPEC.md requires it. If you do, use short-lived access tokens,
  rotate refresh tokens, and never keep tokens in `localStorage`.
- Angular sends the CSRF token: the cookie `csrftoken`, the header `X-CSRFToken`
  (functional interceptor, or `withXsrfConfiguration`).
- Passwords: Django's validators stay enabled (`AUTH_PASSWORD_VALIDATORS`). Never
  log or return passwords or tokens.
- Login and password reset: rate-limit (DRF throttles on those views) and use the same
  message for "wrong user" and "wrong password". Password reset doesn't reveal
  whether an email exists.

## Authorisation
- Every view has permissions. The global default is `IsAuthenticated`.
- Object scoping happens in `get_queryset()`, so another user's or store's data gives a 404.
- Every serializer that accepts a related ID checks the caller may use it (`validate_product`).
- Test it: every endpoint has an "other user's data" test and an "anonymous" test.

## Input and files
- Never build SQL with string formatting. Use the ORM, or `params=` with `raw()`/`cursor`.
- Uploads (CSV/XLSX): check extension **and** content, cap the size (`DATA_UPLOAD_MAX_MEMORY_SIZE`
  and the view itself) and the row count, use `openpyxl` read-only, and never execute
  or evaluate cell contents.
- **CSV injection:** when exporting, prefix cells that start with `=`, `+`, `-`, `@`
  with `'` so Excel doesn't run them as formulas.
- Angular: never `innerHTML` with user data, never `bypassSecurityTrust*` without a
  DECISIONS.md entry.

## Settings (production, from environment; see docs/DEPLOYMENT.md)
- `DEBUG` off, a real `SECRET_KEY`, explicit `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`, `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`, `SECURE_CONTENT_TYPE_NOSNIFF`.
- Content Security Policy: Django 6 has it built in (`django.middleware.csp.ContentSecurityPolicyMiddleware`
  and the `SECURE_CSP` setting, using `django.utils.csp.CSP` constants). Start with
  `default-src 'self'` and allow only what the app needs.
- Run `python manage.py check --deploy` with production settings. It must report no warnings
  you haven't consciously accepted in DECISIONS.md.

## Secrets and dependencies
- No secrets in git, in logs, or in error messages. `.env` is git-ignored.
- `pip-audit` (backend) and `npm audit --omit=dev` (frontend): fix high and critical
  issues before a production deploy.

## Personal data
- Store only what SPEC.md needs. User emails are personal data (GDPR), so don't log
  them, and support account deletion.
