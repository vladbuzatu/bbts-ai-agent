---
name: django-api
description: House rules for Django 6.1 models, migrations, querysets and Django REST Framework 3.18 APIs in this project. Use before writing or reviewing any backend code in backend/.
---

# Django 6.1 + DRF 3.18: models and APIs

Examples below were run and tested on Django 6.1.3 / DRF 3.18.3 / Python 3.12+.
Existing patterns in this repo win over this file for consistency.
`docs/API.md` is the contract. Update it first, then the code.

## Models
- `DEFAULT_AUTO_FIELD` is `BigAutoField` by default in Django 6. Don't override it.
- Put business rules in the database too: `UniqueConstraint`, and
  `CheckConstraint(condition=Q(...), name=...)`. In Django 6 the argument is
  `condition=`; the old `check=` was removed.
- Money and quantities: `DecimalField`, never `FloatField`. Dates without a time:
  `DateField`. Index the fields you filter or sort by often (`db_index=True`).
- Protect history: `on_delete=PROTECT` for things with business records attached.
  Prefer soft deletes (`discarded_at`) for stock so reports stay correct.
- Reusable filters live on a custom QuerySet (`Batch.objects.active().expiring_within(7)`),
  not copy-pasted into views.
- "Today" is `timezone.localdate()` (`TIME_ZONE = "Europe/Bucharest"`, `USE_TZ = True`).
  Never `date.today()` in app code.

```python
class BatchQuerySet(models.QuerySet):
    def active(self):
        return self.filter(quantity__gt=0, discarded_at__isnull=True)
    def expiring_within(self, days: int):
        return self.active().filter(expiry_date__lte=timezone.localdate() + timedelta(days=days))
```

## Migrations
- One migration per logical change, with a readable name (`makemigrations stock --name add_batch_location`).
- Never edit a migration that's already on `main`. Add a new one.
- Data migrations use `apps.get_model()`, never direct model imports, and have a reverse (or `noop`).
- `makemigrations --check` must pass (verify.sh enforces it).

## API (DRF)
- Everything under `/api/v1/`. ViewSets plus a router for resources, `@action` for verbs
  (`POST /batches/{id}/discard/`).
- **Permissions on every view**: the global default is `IsAuthenticated`, plus
  object-level scoping in `get_queryset()` so users only ever see their own store's
  data. Another store's object returns **404**, never 403, so you don't reveal it exists.
- **No N+1 queries**: `select_related` (FK) / `prefetch_related` (reverse/M2M) in
  `get_queryset()`, proven in tests with `django_assert_num_queries`.
- Pagination on every list (global `PageNumberPagination`). Clamp user-supplied numbers
  (`max(0, min(int(days), 365))`) and return 400 on non-numbers.
- Validation lives in serializers: `validate_<field>` for a single field, `validate()` for
  cross-field rules. Messages are human-readable and in the UI language. Validate
  ownership of related objects (`validate_product`) as well as types.
- Error format: DRF's default `{"field": ["message"]}` and `{"detail": "..."}`. The
  frontend relies on it, so don't invent another one.
- Write endpoints use explicit `fields = [...]`, never `"__all__"`. Read-only computed
  fields use `read_only=True`.
- Schema: drf-spectacular at `/api/schema/`. Keep `docs/API.md` in sync with it.

```python
class BatchViewSet(viewsets.ModelViewSet):
    serializer_class = BatchSerializer
    def get_queryset(self):
        return (Batch.objects.active()
                .filter(product__owner=self.request.user)   # scope to the user's data
                .select_related("product"))                  # no N+1
```

## Imports (CSV/Excel) and long work
- Parse uploads with `csv` (UTF-8 with BOM, `;` or `,` delimiters) and `openpyxl`
  (read-only mode). Validate every row, collect errors per row, and save nothing until
  the user confirms. Wrap the confirmed import in `transaction.atomic()`.
- Limit upload size (for example 5 MB) and row count. Treat file contents as untrusted.
- Slow jobs: Django 6's `django.tasks` framework (`@task`, `.enqueue()`) is available,
  but its built-in backends are `immediate` and `dummy`. Real background workers need
  a third-party backend, so don't add one without a DECISIONS.md entry.

## Done means
`ruff check .` clean, `pytest` green, `makemigrations --check` clean, API.md updated,
and every new endpoint tested for success, validation error, unauthenticated, and other user's data.
