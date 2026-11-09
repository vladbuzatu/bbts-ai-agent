---
name: security-review
description: Security review checklist for this Django + Angular app, based on OWASP ASVS 5.0 Level 2 and mapped to the OWASP Top 10:2025. Use when reviewing any change that touches auth, permissions, data, files, configuration, dependencies or error handling.
---

# Security review (ASVS 5.0 L2 · OWASP Top 10:2025)

How to use it: walk the sections that the change touches. Each "no" is a finding.
Anything that exposes another user's data, a secret, or code execution is a
`[blocker]`. Implementation details live in `django-security`. This file is the
**reviewer's checklist**.

## A01 Broken access control (includes SSRF since 2025)
- [ ] Every view has permissions, and the default is deny.
- [ ] Objects are scoped in `get_queryset()`, so another user's object gives 404 (and there's a test).
- [ ] Related IDs in requests are ownership-checked in serializers.
- [ ] Function-level checks: staff/admin-only actions are actually restricted.
- [ ] No server-side fetching of user-supplied URLs; if unavoidable, use an allow-list of hosts and block internal addresses.

## A02 Security misconfiguration
- [ ] Production settings: `DEBUG` off, explicit `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS`,
      secure cookies, HSTS via the proxy, CSP via Django 6's built-in middleware.
- [ ] `manage.py check --deploy` shows no unexplained warnings.
- [ ] No debug endpoints, default credentials, or verbose error pages in production.

## A03 Software supply chain failures
- [ ] New dependencies are justified in DECISIONS.md, come from the official registry,
      are pinned, and are actively maintained. Check for typos in package names.
- [ ] `pip-audit` / `npm audit --omit=dev` show no high or critical issues.
- [ ] CI and deploy files were not changed by an agent without owner approval (they're security-critical).

## A04 Cryptographic failures
- [ ] Passwords only via Django's hashers. No home-made crypto, no secrets in code.
- [ ] Sensitive data isn't stored if it isn't needed. Tokens are random (`secrets` module).

## A05 Injection
- [ ] ORM or parameterised queries only. No string-built SQL, shell commands or templates.
- [ ] Angular: no `innerHTML` with user data, no `bypassSecurityTrust*` without a DECISIONS.md entry.
- [ ] CSV exports neutralise formula injection (cells starting with `= + - @`).

## A06 Insecure design
- [ ] Abuse cases considered: mass assignment (explicit serializer fields), enumeration,
      replay, race conditions on stock quantities (use `select_for_update` or `F()` expressions).
- [ ] Limits exist: upload size, rows per import, page size, request rate on auth.

## A07 Authentication failures
- [ ] Session auth with CSRF for the same-site app. Login and reset are rate-limited.
- [ ] Generic errors ("wrong email or password"). Reset doesn't reveal accounts.
- [ ] Password validators on. Sessions rotate on login and expire. Logout invalidates.

## A08 Software or data integrity failures
- [ ] Uploaded files are parsed safely (`openpyxl` read-only, no macros or formulas executed).
- [ ] No deserialising untrusted data with `pickle` or `yaml.load`.
- [ ] Imports are atomic (all or nothing) and show exactly what will change before confirming.

## A09 Security logging & alerting failures
- [ ] Security-relevant events are logged (login failures, permission denials, imports,
      deletions) without passwords, tokens or personal data.

## A10 Mishandling of exceptional conditions
- [ ] Errors fail **closed** (deny), never open. No bare `except: pass`.
- [ ] Users see a helpful message, and no stack traces or internals leak.
- [ ] Partial failures roll back (`transaction.atomic`). Timeouts on outbound calls.

## Report format (in your review)
`[blocker|should-fix|nit] A0x · file:line · problem · fix`, then a one-line verdict.
