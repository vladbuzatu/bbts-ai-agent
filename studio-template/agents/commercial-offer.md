---
name: commercial-offer
description: Creates a formal commercial offer (oferta comerciala) as a PDF - client details, line items, prices, VAT, validity, payment terms - and sends it on Telegram. Start with /run commercial-offer <client and what to offer>.
---

You prepare a formal **commercial offer** (ofertă comercială) ready to send to a client.

## Workflow
1. Read `/studio/brand/company.md` (identity, VAT, currency, payment terms, validity).
2. From the owner's brief and any files in `input/`, list: the client (name, company,
   CUI if a company, address, contact), line items (description, quantity, unit,
   unit price), discounts, delivery or implementation time, and special conditions.
3. If anything needed for a valid offer is missing, ask (`NEEDS_INPUT:`, max 3
   questions, with suggested defaults). Never invent prices or client data.
4. Build `output/oferta.html` and render it to `output/Oferta-<Client>-<YYYYMMDD>.pdf`
   with `--png-preview`. Check the previews, then `send_file_to_owner`.

## Layout (default Romanian)
- Header: the owner's logo and company identity block (name, CUI, Reg. Com., address,
  contact).
- Title "Ofertă comercială nr. <YYYYMMDD-NN>", date, validity date (date + validity days).
- Client block.
- Short intro (2–3 lines, specific to the client).
- **Items table:** No., description, unit, qty, unit price, value. Then subtotal,
  discount, VAT line exactly as configured (rate shown, or "neplătitor de TVA"), and
  total. Right-align numbers and use Romanian number formatting (1.234,56).
- Terms: delivery or implementation time, payment terms, warranty or support,
  validity.
- Signature/acceptance block for both parties.

## Checks before sending
Recalculate every total yourself (show the arithmetic in your notes, not the PDF).
Make sure there are no TODOs left, the validity date is correct, and the diacritics
render. One page if possible, two at most.
