---
name: django-testing
description: How to write backend tests in this project with pytest-django and DRF's APIClient - fixtures, permissions, query counts, dates. Use before writing, fixing or reviewing backend tests.
---

# Django testing (pytest-django, Django 6.1, DRF 3.18)

Run: `cd backend && pytest`. The pattern below was run and tested on Django 6.1.3.

## Pattern
```python
@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("ana", password="x-Strong-pass-1")

@pytest.fixture
def api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client

def test_cannot_see_other_users_batches(api, db):
    other = get_user_model().objects.create_user("ion", password="x-Strong-pass-1")
    batch = Batch.objects.create(product=Product.objects.create(name="Paine", owner=other),
                                 quantity=1, expiry_date=date.today())
    assert api.get(f"/api/v1/batches/{batch.id}/").status_code == 404

def test_list_has_no_n_plus_one(api, product, django_assert_num_queries):
    with django_assert_num_queries(2):   # count + page
        api.get("/api/v1/batches/?expiring_within=7")
```

## Every endpoint gets at least
1. The happy path (status code **and** the response body shape the frontend uses).
2. A validation error: 400, with the error on the right field.
3. Anonymous: 401/403.
4. Another user's or store's data: 404.
5. For list endpoints: the query count, so N+1 queries can't sneak back in.

## Rules
- Shared fixtures go in `conftest.py`. Keep factories simple (plain `objects.create` in
  fixtures, or `factory_boy` if DECISIONS.md adopts it).
- Tests are independent and order-free. Never depend on IDs or on data from another test.
- **Dates:** expiry logic must be tested at the boundaries: today, tomorrow, exactly
  N days, N+1, and already expired. Freeze "today" by patching `django.utils.timezone.localdate`
  (or with `time-machine` if adopted) so tests don't break at midnight.
- Imports: test good files, wrong headers, bad rows (mixed valid and invalid), empty
  files, huge files (limit), UTF-8 with BOM, `;` delimiter, Romanian diacritics (ă â î ș ț),
  and `.xlsx` with formulas (not executed).
- Test behaviour through the API, not private helpers. Model and queryset methods get
  their own small tests.
- Never weaken an assertion, mark a test skip/xfail, or delete it to get green. Fix the
  code or raise it with the PM.
