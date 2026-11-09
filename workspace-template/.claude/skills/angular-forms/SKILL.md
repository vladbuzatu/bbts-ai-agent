---
name: angular-forms
description: How to build forms in this Angular 22 project - Signal Forms for new forms, validation and server errors, accessibility. Use before writing or reviewing any form.
---

# Angular forms (Angular 22)

- **New forms: Signal Forms** (`@angular/forms/signals`). They're stable in v22.
- **Existing forms built with Reactive Forms stay Reactive.** Don't rewrite working
  forms, and never mix both styles inside one form.
- Never use template-driven forms (`ngModel`).

## Pattern (compiled and tested on Angular 22.2)

```ts
import { Component, signal, output } from '@angular/core';
import { form, FormField, required, min, submit } from '@angular/forms/signals';

interface BatchDraft { product: string; quantity: number; expiryDate: string; }

@Component({
  selector: 'app-batch-form',
  imports: [FormField],
  templateUrl: './batch-form.html',
})
export class BatchForm {
  readonly saved = output<BatchDraft>();
  readonly model = signal<BatchDraft>({ product: '', quantity: 1, expiryDate: '' });
  readonly f = form(this.model, (p) => {
    required(p.product, { message: 'Product is required' });
    min(p.quantity, 1, { message: 'Quantity must be at least 1' });
    required(p.expiryDate, { message: 'Expiry date is required' });
  });

  async save(event: Event) {
    event.preventDefault();
    await submit(this.f, async () => { this.saved.emit(this.model()); return undefined; });
  }
}
```

```html
<form (submit)="save($event)" novalidate>
  <label for="product">Product</label>
  <input id="product" [formField]="f.product"
         [attr.aria-invalid]="f.product().touched() && f.product().invalid()"
         aria-describedby="product-errors" />
  <div id="product-errors">
    @if (f.product().touched()) {
      @for (e of f.product().errors(); track e.kind) { <p role="alert">{{ e.message }}</p> }
    }
  </div>
  …
  <button type="submit" [disabled]="f().invalid() || saving()">Save</button>
</form>
```

## Rules
- The model is one `signal()` of a typed interface. Read values from the model, not the DOM.
- Validation lives in the schema function, with human messages in the UI language.
  Built-ins: `required`, `min`, `max`, `minLength`, `maxLength`, `pattern`, `email`,
  `minDate`, `maxDate`. Cross-field rules use `validate()`. Server checks use
  `validateHttp()` / `validateAsync()` and must be debounced.
- Show errors only after the field is touched or after a submit attempt, never on page load.
- Server-side errors (DRF `{"field": ["msg"]}`) are shown on the matching field.
  Non-field errors go in one alert at the top of the form.
- Disable the submit button while saving and prevent double submits.
- Every input has a visible `<label for>`. Errors are linked with `aria-describedby`
  and announced with `role="alert"`. Focus moves to the first invalid field on a failed submit.
- Dates: native `<input type="date">` (ISO `YYYY-MM-DD`), shown to users in the
  Romanian format. Numbers: `type="number"` with `min`/`step`, and remember the model
  may still need a `Number()` conversion when the field is empty.
- Unsaved changes: if the form is dirty, warn before navigating away (a `canDeactivate` guard).

## Testing a form
Set the model and assert validity. No DOM typing is needed for validation tests:
```ts
const fixture = TestBed.createComponent(BatchForm);
await fixture.whenStable();
const cmp = fixture.componentInstance;
expect(cmp.f().invalid()).toBe(true);
cmp.model.set({ product: 'Lapte', quantity: 3, expiryDate: '2026-11-20' });
expect(cmp.f().valid()).toBe(true);
```
Then add at least one end-to-end test (Playwright) that fills the real form and submits it.
