---
name: angular-components
description: House rules for Angular 22 components, state, services, routing and HTTP in this project. Use before writing or reviewing any Angular code in frontend/.
---

# Angular components, state and services (Angular 22)

Verified against Angular 22.2 and the official best-practices guide. When unsure,
call the Angular MCP tools `get_best_practices` / `search_documentation` instead of
guessing from memory. Existing code in this repo wins over this file for
consistency. Never mix two patterns in one feature.

## Defaults you must NOT write by hand
- No `standalone: true` (default since v20).
- No `changeDetection: ChangeDetectionStrategy.OnPush` (OnPush is the default in v22).
- No `zone.js` and no `provideZoneChangeDetection()` (new apps are zoneless).
- No `CommonModule` import. Import only what the template uses (`DatePipe`, `RouterLink`, …).

## Components
- Small and single-purpose. Use the CLI's file naming: `stock-list.ts` /
  `.html` / `.scss`, class `StockList` (no `Component` suffix).
- Inputs/outputs: `input()`, `input.required()`, `output()`, and `model()` for two-way
  binding. No `@Input`/`@Output` decorators.
- Host bindings go in the `host: {}` object, not `@HostBinding`/`@HostListener`.
- Templates: `@if`, `@for (x of xs; track x.id)` (always track a stable id), `@switch`,
  `@defer` for heavy below-the-fold parts. Use `[class.x]` / `[style.x]`, not `ngClass`/`ngStyle`.
- Images: `NgOptimizedImage` (`ngSrc`) for static images.
- Keep logic out of templates: derive in `computed()`.

## State
- Local state: `signal()`. Derived: `computed()`. State that must reset when a source
  changes: `linkedSignal()`. Update with `.set()` / `.update()`, never mutate in place.
- Shared feature state: a service holding private writable signals and exposing
  read-only ones (`asReadonly()`) plus intent methods (`addBatch()`, `discard()`).
  No NgRx or other store library unless DECISIONS.md says so.
- `effect()` only for side effects (localStorage, logging), never to sync state into other signals.

## Services and DI
- `inject()`, never constructor injection.
- New singletons: `@Service()` (v22+). Older `@Injectable({ providedIn: 'root' })` in
  existing code is fine. Don't churn it.
- One service per API resource (`StockBatchesApi`), typed with interfaces that match
  `docs/API.md` exactly. Put API types in `src/app/core/api/` and don't redefine them per component.

## HTTP
- `provideHttpClient(withInterceptors([...]))` with functional interceptors (auth/CSRF,
  error mapping). No class-based interceptors.
- Base URL: the relative `/api/v1/` (see docs/DEPLOYMENT.md). Never hard-code a host.
- Reading data into a component: `httpResource()` or `toSignal(api.list())`. Show
  `isLoading()`, `error()` and empty states. Every data screen has all three.
- Errors: map backend validation errors (DRF field errors) onto the form fields; show a
  human message for everything else, never a raw error object.

## Routing
- Lazy-load every feature: `loadComponent` / `loadChildren`.
- Functional guards (`canActivate: [authGuard]`), not class guards.
- Route params via `input()` with `withComponentInputBinding()`.

## Done means
`npx ng build` with no new warnings, `npx ng test --watch=false` green, the screen
matches its mockup at 390px and 1440px, and no AXE accessibility violations.
