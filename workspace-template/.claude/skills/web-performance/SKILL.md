---
name: web-performance
description: Performance budgets and techniques for the Angular 22 + Django app - Core Web Vitals targets, bundle budgets, images, fonts, API and database speed, and how to measure. Use when building screens, adding dependencies, or reviewing.
---

# Web performance

Targets (Core Web Vitals, "good"), measured on a mid-range phone over 4G:
**LCP ≤ 2.5 s · INP ≤ 200 ms · CLS ≤ 0.1.** API responses for normal screens: under 300 ms on the server.

## Frontend (Angular)
- **Budgets:** keep the CLI's production budgets in `angular.json` (initial bundle: warn
  500 kB, error 1 MB; each component's styles: warn 4 kB, error 8 kB). Never raise a budget
  to make a build pass. Find what grew instead (`npx ng build --stats-json`).
- **Lazy-load** every feature route. Use `@defer (on viewport)` for heavy parts below the fold
  (charts, maps, editors).
- **Dependencies:** each new npm package needs a reason in DECISIONS.md. Prefer the
  platform and Angular CDK over big UI kits. Never import a whole library for one function.
- **Images:** `NgOptimizedImage` with explicit width and height (prevents layout shift),
  `priority` on the LCP image only, modern formats (AVIF/WebP), and responsive `sizes`.
- **Fonts:** at most 2 families and 4 files, self-hosted WOFF2, `font-display: swap`,
  preload only the font used above the fold, and a matched fallback (`size-adjust`) to avoid shift.
- **Layout stability:** reserve space for anything that loads late (skeletons with fixed
  heights, image dimensions, ads/embeds). Never insert content above what the user is reading.
- **Interaction (INP):** keep event handlers light, use `computed()` instead of recomputing
  in templates, `track` in every `@for`, and virtual scrolling (CDK) for lists over about 200 rows.
- **Motion:** animate only `transform` and `opacity`.

## Backend (Django)
- No N+1 queries: `select_related` / `prefetch_related`, proven with `django_assert_num_queries`.
- Paginate every list. Index the columns you filter and sort on. Use `only()` / `values()`
  for large exports.
- Compression and caching headers for static files (WhiteNoise handles hashed files).
- Long work (big imports, exports) goes to a background mechanism or chunked requests
  with progress, never a request that runs for 30 seconds.

## Measure
- Before a release, run Lighthouse (mobile) on the 3 most important pages of staging and
  record the scores in `docs/DECISIONS.md` or the release notes:
  `npx -y lighthouse <url> --only-categories=performance,accessibility --form-factor=mobile --quiet --chrome-flags="--headless" --output=json`
- Regressions over 10% in LCP or bundle size need a reason before merging.
