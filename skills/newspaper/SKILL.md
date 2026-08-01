---
name: newspaper-maker
description: >
  Create gorgeous, accurate, genuinely fun-to-read newspapers as single-file HTML pages.
  Use this skill whenever the user asks for a "newspaper", "front page", "gazette", "daily digest",
  "news roundup", "broadsheet", "tabloid", "newsletter in newspaper style", or wants any collection
  of news, updates, or facts presented as a printed-paper-style layout — even if they don't say the
  word "newspaper". Covers research & fact-checking, editorial writing style, layout/typography,
  and breaking-news treatment.
---

# Newspaper Maker

You are about to be the entire staff of a small, excellent newspaper: researcher,
fact-checker, reporter, editor, and layout designer. Do the jobs **in that order** —
truth first, prose second, beauty third. A gorgeous page full of wrong facts is a
failure; a correct page that's boring or cramped is also a failure.

The final deliverable is **one self-contained HTML file** (inline CSS, no external
dependencies except optionally Google Fonts). It should look great on screen and
survive printing.

---

## Phase 1 — Research like a real newsroom

Never write a single headline until the reporting is done.

### Gather
1. Identify 5–9 story candidates on the requested topic/date range. More candidates
   than you'll print — you will cut the weakest.
2. If you have web search or browsing tools, **use them**. Search each candidate
   story separately; don't rely on one query for the whole paper.
3. If you have NO live tools, you may only use knowledge you're confident in, and
   the paper must carry a small, honest dateline note (e.g. "Compiled from
   knowledge up to <cutoff>; details may have developed since."). Never fake
   freshness.

### Verify — the two-source rule
- Every factual claim that matters (numbers, quotes, dates, names, "first ever"
  claims) needs **two independent sources**, or it gets softened
  ("reportedly", "according to X") or cut.
- Independent means genuinely independent — ten outlets rewriting the same wire
  story count as one source.
- Prefer primary sources: the paper itself, the court filing, the company
  announcement, the agency data — not a blog summarizing them.
- Check dates hard. The #1 embarrassment in AI-made newspapers is presenting an
  old story as new. Confirm *when* the thing happened, not just *that* it happened.
- Numbers get sanity-checked: does the magnitude make sense? Units? Percent vs
  percentage points? If a figure looks incredible, it's probably wrong — chase it.
- Quotes are sacred. Never invent, trim-to-distort, or paraphrase-inside-quote-marks.
  If you can't verify the exact wording, describe it indirectly instead.

### Grade and cut
Rate each candidate story: **Solid / Probable / Shaky.**
- Solid → print confidently.
- Probable → print with attribution language.
- Shaky → cut it, or demote it to a one-line "Also making news" brief with hedging.
A shorter honest paper beats a fuller dubious one, every single time.

### Sources on the page
Include a discreet "Sources" line at the end of each article (tiny italic text:
*Sources: Reuters, ESA press release*) or a compact sources box in the footer.
Readers trusting you is the whole product.

---

## Phase 2 — Write like the paper you'd actually want to read

Tone target: smart friend explaining the news at breakfast. The Economist's clarity,
a local paper's warmth, zero corporate mush.

**Headlines**
- Active voice, present tense, strong verbs: "City Council Kills Parking Plan",
  not "Parking Plan Discontinued Following Council Deliberations".
- One headline per paper is allowed to be a pun. *One.* Spend it wisely
  (usually on the light/feature story, never on tragedy).
- Subheads (deks) carry the second-most-important fact, not a restatement.

**Body copy**
- Inverted pyramid: the reader should be able to stop after any paragraph and
  still have the story.
- Lede under 30 words. If your first sentence has a comma splice of three clauses,
  rewrite it.
- Short paragraphs — 1–3 sentences. Newsprint columns make long paragraphs
  look like walls.
- Explain jargon in-line the first time ("basis points — hundredths of a percent").
- Concrete beats abstract: "enough water to fill 400 Olympic pools", not
  "a substantial volume".
- Vary article length deliberately: one long anchor piece (~350–500 words),
  two-three mediums (~150–250), several briefs (30–80). Uniform length is the
  fastest way to make a page feel machine-generated.

**Fun, without clowning**
- Include at least one genuinely delightful item: a quirky science finding, an
  odd local story, "This Day in History", a well-made stat.
- Sprinkle classic furniture: a one-line weather box, a tiny "Corrections" box
  (can be playfully empty: "The editors are pleased to report no known errors.
  Yet."), a quote of the day, maybe a mini crossword clue or number puzzle.
- Wit lives in briefs, captions, and the light feature — never in the lead story
  of a serious paper, never anywhere near death or disaster.

---

## Phase 3 — Design a beautiful broadsheet

### The look
Classic newspaper DNA, executed cleanly:
- **Paper**: off-white/cream background (`#f7f4ec`–`#faf7f0`), near-black ink
  (`#1a1a1a`), and **one** accent color used sparingly (deep red `#8b1a1a` or
  navy work beautifully). Restraint = class.
- **Masthead**: big blackletter or high-contrast serif nameplate (UnifrakturCook,
  Playfair Display, or Chomsky-style), centered, with a thin–thick–thin rule
  below and a dateline bar: date • edition • price ("Priceless" is a fine gag) •
  weather glyph.
- **Type**: Serif everything for body and heads (Playfair Display / 'Old Standard TT'
  for heads, Georgia / 'PT Serif' / 'Source Serif 4' for body). A single condensed
  sans (Oswald / Archivo Narrow) is permitted for kickers, bylines, and the
  breaking-news panel only.
- **Details that sell the illusion**: drop cap on the lead article's first
  paragraph, small-caps kickers above headlines, hairline column rules, justified
  body text with `hyphens: auto`, "Continued on A4 →" jokes optional, tiny
  centered ornaments (❦) between sections.

### Layout — airy, never congested
Congestion is the enemy. Rules:
- CSS multi-column or grid: 3–4 columns on desktop, collapsing to 2 then 1 on
  mobile (`@media` breakpoints ~900px and ~600px).
- **Whitespace is content.** Generous line-height (1.55–1.7 for body),
  16–18px body size minimum, real gaps (`column-gap: 2rem+`), padding around
  every boxed element.
- Clear hierarchy in exactly three tiers: the lead story (biggest headline,
  spans 2+ columns), secondary stories, and briefs. If everything shouts,
  nothing does.
- Break up gray text with: pull quotes (big serif italics between rules),
  stat callout boxes, simple figures (inline SVG charts styled in ink +
  accent color only — no rainbow chart.js defaults), and image slots. If you
  can't generate real images, draw simple editorial-style inline SVGs
  (line-art illustrations, maps, diagrams) with proper italic captions —
  never leave "[image here]" placeholders and never use stock-photo look-alikes
  of real people.
- Section labels ("WORLD", "SCIENCE", "THE LIGHTER SIDE") as small-caps bars
  with rules — they let readers breathe and navigate.
- Footer: sources box, corrections, a tiny colophon ("Set in Playfair &
  Source Serif. Printed on pixels.").

### The BREAKING NEWS panel
Use **only** if the research surfaced something genuinely major and current
(disaster, historic first, market shock, huge verdict). Do not manufacture drama —
an unearned breaking banner destroys the paper's credibility instantly.

When earned:
- Full-width band directly under the masthead.
- Accent-red background (or ink-black with red border), white/paper text,
  condensed sans, letter-spaced "⚡ BREAKING" tag, one punchy sentence, timestamp.
- Optionally a subtle CSS pulse on the "BREAKING" tag (2s, gentle — no strobing).
- One per paper, maximum. If two things broke, the bigger one gets the banner
  and the other gets an "URGENT" kicker on its headline.

If nothing qualifies, a quiet "LATE EDITION" or "EXTRA" ribbon is a tasteful
substitute — or nothing at all.

### Print-friendliness
Add a small `@media print` block: white background, black text, hide any
animation, keep columns. Newspapers should be printable; it's the whole bit.

---

## Phase 4 — Edit like a grump, then ship

Before delivering, run the editor's pass:

1. **Fact pass** — re-scan every number, name, date, and quote against your notes.
   Anything you can't stand behind gets softened or cut *now*.
2. **Congestion pass** — squint at the layout. Any region that reads as a solid
   gray slab gets a pull quote, a rule, or a trim.
3. **Fun pass** — is there at least one thing that would make a reader smile or
   say "huh, neat"? If not, add one.
4. **Headline pass** — read all headlines in sequence aloud (mentally). They
   should sound like a confident front page, not a list of summaries.
5. **Honesty pass** — dateline accurate, sources listed, breaking banner earned
   (or absent), no invented quotes, no fake images of real people.

Then deliver the single HTML file, and briefly tell the user what the lead story
is and why it earned the top slot.

## Quick checklist

- [ ] 2 independent sources per major claim (or hedged/cut)
- [ ] Dates verified — nothing old dressed as new
- [ ] Lede < 30 words, inverted pyramid, varied article lengths
- [ ] Exactly one pun, placed responsibly
- [ ] Cream paper, ink text, ONE accent color
- [ ] Serif masthead + drop cap + column rules + small-caps kickers
- [ ] Line-height ≥ 1.55, real gaps, three-tier hierarchy
- [ ] Breaking panel only if genuinely earned; max one
- [ ] Sources line per article + footer sources box
- [ ] Responsive + `@media print` block
- [ ] Something delightful in every edition
