---
name: accessibility
description: WCAG 2.2 AA in practice for this Angular app - what to build, how to test it with axe-core in Playwright, and the manual checks automation misses. Use when designing, building, testing or reviewing any screen.
---

# Accessibility: WCAG 2.2 AA

Accessibility is part of "done", not a polish step. Automated checks catch roughly
a third of problems; the manual checks below catch the rest.

## Build it in
- **Semantics first:** real `<button>`, `<a href>`, `<label for>`, headings in order
  (one `h1` per page), landmarks (`header`, `nav`, `main`, `footer`). Use ARIA only
  when no native element exists, and then follow the WAI-ARIA Authoring Practices pattern.
- **Keyboard:** everything works with Tab / Shift+Tab / Enter / Space / Esc. Logical
  focus order. No keyboard traps. Dialogs trap focus while open and return it on close
  (Angular CDK `cdkTrapFocus`, `FocusMonitor`).
- **Focus:** always visible (a `:focus-visible` style with ≥ 3:1 contrast) and **never
  hidden** behind sticky headers or footers (WCAG 2.4.11). Use `scroll-padding-top`.
- **Contrast:** text ≥ 4.5:1 (large text ≥ 3:1). UI parts and icons ≥ 3:1. Never use
  colour alone: expiry states also get an icon and a word ("Expired", "Today").
- **Targets:** at least 24×24 px (WCAG 2.5.8). We use 44×44 px for touch.
- **Dragging:** any drag action has a single-pointer alternative (WCAG 2.5.7).
- **Forms:** visible labels, errors as text linked with `aria-describedby`, focus moved
  to the first invalid field on submit, no information only in placeholders.
- **Status messages:** toasts and "saved" notices use `role="status"` (or the CDK
  `LiveAnnouncer`), and errors use `role="alert"`.
- **Motion:** respect `prefers-reduced-motion`. Nothing flashes more than 3 times a second.
  Auto-moving content can be paused.
- **Language and text:** set `<html lang="ro">` (or `en`), and text that zooms to 200%
  without breaking. Tables have `<th scope>` and a caption.
- **Images:** meaningful images get alt text describing their purpose; decorative ones get `alt=""`.

## Test it (automated): axe-core in every e2e journey
```ts
import AxeBuilder from '@axe-core/playwright';
const results = await new AxeBuilder({ page })
  .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
  .analyze();
expect(results.violations).toEqual([]);
```
Run it on every main screen, including empty, error and dialog-open states, at 390px
and 1440px.

## Test it (manual, per new screen)
1. Use the whole flow with the keyboard only.
2. Zoom to 200% and use a 320px width: no horizontal scrolling, nothing cut off.
3. Turn on reduced motion: nothing important depends on animation.
4. Read the page as a screen reader would: `await page.locator('body').ariaSnapshot()` in
   Playwright prints the accessibility tree. Check that headings, landmarks and labels make sense.

## Review
Any WCAG 2.2 AA failure is a `[blocker]`. Quote the success criterion number.
