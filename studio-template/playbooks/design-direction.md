---
name: design-direction
description: How this team designs interfaces that are distinctive, brand-specific and current rather than generic or AI-looking - brand brief, three directions for the owner to choose from, banned defaults, signature element, materials, and self-critique. Use for any new product design, redesign, or new screen family.
---

# Design direction: distinctive, not default

**Why AI designs look alike:** a model drifts to the statistical average of everything
it has seen. A vague brief produces the average. The cure is **specific inputs, several
deliberately different options, and an explicit list of defaults to avoid**.

## 1. Brand brief (before drawing anything): `design/BRIEF.md`
- **Subject:** what the product is, in the user's words. Use its world for ideas: for a
  grocery-stock app that's shelves, labels, dates, freshness, crates, receipts. Distinctive
  choices come from the subject's materials and vocabulary, not from "modern SaaS".
- **Audience and context:** who, on what device, in what conditions (store staff, on a
  tablet in a cold storeroom, in a hurry, with gloves).
- **Primary job of each key screen** (one sentence each).
- **Personality:** 3 adjectives, plus 3 it must *not* be.
- **Competitors and references:** see `design/REFERENCES.md`. What everyone in the
  category does (to avoid) and what's worth learning.

## 2. Three directions, then the owner chooses
Make **three genuinely different directions**. Different means a different typeface
family, palette and layout idea, not the same thing in three colours. For each:
- **Name** (two words) and a one-sentence idea.
- **Palette:** 4–6 named hex values, with contrast checked.
- **Type:** one or two families (clearly distinct if two), with a scale.
- **Layout concept:** one sentence plus an ASCII wireframe of the key screen.
- **Signature element:** the one memorable thing, rooted in the subject (for StocFresh,
  for example, a freshness timeline that makes expiry visible at a glance).
- **One key screen** as an HTML mockup with a 1440px screenshot and a 390px screenshot.

**Before showing them, test each plan for genericness:** "Would I produce this same
plan for any similar app?" If yes, change that part and note what you changed and why.
Then the PM sends the screenshots to the owner, who picks one (or asks for a mix).
Only the chosen direction becomes the full design system.

## 3. Banned defaults (use only if the brief explicitly asks for them)
These read as "generated" because they appear regardless of subject:
- Inter, or the system font, as the whole personality; one font doing every job.
- Indigo or blue-to-purple gradients and gradient "blobs" as decoration.
- A centered hero, then three identical feature cards with icon, heading and two lines.
- Everything in identical rounded cards with one radius and the same soft grey shadow.
- A warm cream background with a big serif and a terracotta accent. A near-black page
  with one neon accent. Newspaper hairlines everywhere. (Fine when chosen, not as defaults.)
- Template chrome: ALL-CAPS tracked labels above every heading, "A · B · C" meta strings,
  "→" on every link and button, monospace for small labels, numbered markers (01/02/03)
  on content that isn't a sequence.
- Highlighting one word of a headline in a different colour or italic.
- Fade-and-slide-up entrances on every section and hover effects on every card.
- Stock photos of smiling people, generic 3D blobs, emoji used as icons, lorem ipsum.

## 4. Principles for the chosen direction
- **Spend boldness in one place:** the signature element. Keep everything around it calm
  and disciplined. Before finishing, remove one decoration.
- **Typography carries the personality.** Choose typefaces for this brand, set a real scale,
  keep body lines under about 75 characters, and give serif body text more line height.
- **Structure is information:** borders, numbers, labels and dividers must encode something.
- **Layout from the content:** dense data screens get dense, scannable layouts; asymmetry and
  whitespace where they help the eye. Data tables are a first-class design surface.
- **Motion answers actions** (expand, confirm, show what changed). At most one orchestrated
  moment (for example the first load of the dashboard). Always respect reduced motion.
- **Words are design:** plain, specific, active ("Save batch", not "Submit"). The same action
  keeps the same name through the flow. Errors say what happened and how to fix it. Empty
  screens invite the next action.
- **Quality floor, without announcing it:** WCAG 2.2 AA (`accessibility` playbook), visible
  focus, 44px touch targets, reduced motion, and a layout designed on purpose for 390px.

## 5. Materials
- **Fonts:** open-licence families (Google Fonts / Fontsource), self-hosted WOFF2, at most 2
  families and 4 files. Pick with intent (a humanist sans for warmth, a grotesk for precision,
  a slab for sturdiness…) and record why in `design/BRIEF.md`.
- **Icons:** one set, used consistently (Lucide, Phosphor or Tabler), or custom SVG for the
  signature element. Never mix sets. Never emoji.
- **Imagery:** custom SVG illustrations and patterns made from the subject (labels,
  shelves, date stamps) instead of stock images.
- **Trends:** read `design/TRENDS.md`. Use at most one or two signals that genuinely fit the brand.

## 6. Self-critique (every screen)
Screenshot at 390 and 1440. Then look like an art director:
1. Count the banned defaults present. The goal is zero.
2. Squint test: is the hierarchy obvious? Is the primary action the most visible thing?
3. Would a competitor's logo fit on this page without anyone noticing? If yes, push the brand.
4. Check contrast, focus, touch targets, and long Romanian words (ă â î ș ț) for overflow.
Fix, re-shoot, repeat. Then hand over to `art-director` for an independent review.
