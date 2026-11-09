---
name: angular-testing
description: How to write Angular 22 unit tests in this project (Vitest via ng test, zoneless) and Playwright end-to-end tests. Use before writing, fixing or reviewing frontend tests.
---

# Angular testing (Angular 22, Vitest, zoneless)

## Facts about this setup
- `npx ng test --watch=false` runs **Vitest** in **jsdom** (`@angular/build:unit-test`).
  Use Vitest APIs: `describe/it/expect`, `vi.fn()`, `vi.spyOn()`, `vi.useFakeTimers()`.
  **Not** Jasmine (`jasmine.createSpy`, `spyOn` without `vi.`) and **not** Karma.
- The app is **zoneless**, so `fakeAsync`, `tick()` and `flush()` from zone.js are not
  available. Wait with `await fixture.whenStable()` or
  `await TestBed.inject(ApplicationRef).whenStable()`. For timers, use `vi.useFakeTimers()`.

## Component test
```ts
const fixture = TestBed.createComponent(StockList);
fixture.componentRef.setInput('filter', 'critical');   // signal inputs: use setInput
await fixture.whenStable();
const el = fixture.nativeElement as HTMLElement;
expect(el.querySelector('[data-testid="critical-count"]')?.textContent).toContain('3');
```
- Standalone components need no `configureTestingModule` imports. Add only `providers`.
- Query by role, label or `data-testid`, never by CSS class names used for styling.

## HTTP / resources (tested on 22.2)
```ts
TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
const store = TestBed.inject(StockStore);
const http = TestBed.inject(HttpTestingController);
TestBed.tick();                                   // lets httpResource send its request
http.expectOne('/api/v1/batches/').flush([{ id: 1, product: 'Lapte', quantity: 2, expiry_date: '2026-11-10' }]);
await TestBed.inject(ApplicationRef).whenStable(); // lets the resource value update
expect(store.count()).toBe(1);
http.verify();                                    // no unexpected requests
```
Test the unhappy paths too: `flush(body, { status: 400, statusText: 'Bad Request' })`
for validation errors, plus 401/403 and network errors.

## What to test
- Every component with logic: rendering of loading, empty, error and data states.
- Every service: correct URLs, params, payloads and error mapping.
- Every form: required fields, boundaries, cross-field rules, server errors.
- Dates near the edges for expiry logic (today, tomorrow, 7 days, already expired,
  timezone Europe/Bucharest). Freeze time with `vi.setSystemTime()`.
- Don't test Angular itself (that a button renders) or private methods.

## End-to-end (Playwright, `e2e/`)
- Web-first assertions only: `await expect(page.getByRole('heading', { name: 'Stocuri' })).toBeVisible()`.
  Never `waitForTimeout`.
- Locators: `getByRole`, `getByLabel`, `getByTestId`. Never XPath or CSS chains.
- Each test creates its own data through the API or fixtures, and tests don't depend on each other.
- Cover the core journeys from SPEC.md at 1440px and 390px.
- Accessibility: run `@axe-core/playwright` on each main screen and fail on violations.

## Never
Never weaken an assertion, add `.skip`, or delete a test to make the suite pass. Fix
the code, or explain to the PM why the test is wrong.
