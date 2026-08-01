---
name: professional_style
description: Create presentations, documents, or visual materials with a polished, classically professional look. Use when the user asks for "professional", "classy", "corporate", "clean", or requests a style overhaul away from flashy or themed designs.
---

# Professional Style

Load the `humanizer` skill alongside this one — professional style means no AI-ese, no filler, no emojis, no decorative fluff. Every element earns its place.

## Core principles

- **No emojis.** Not in titles, not in bullets, not anywhere. If you want visual punctuation, use a small shape, a rule, or typographic weight instead.
- **Concise and human.** Say what needs saying and stop. No warm-up sentences, no signposting ("Let's dive in", "Now let's look at"), no rhetorical questions. Read the `human_writing` skill for the full treatment.
- **Substance over decoration.** Every slide element — every shape, line, gradient, icon, or divider — should serve the content. If removing it loses nothing, remove it.

## Color palette

| Role | Hex | Notes |
|---|---|---|
| Background | `#FAF8F5` | Warm cream, easier on the eye than pure white. Slide background. |
| Text | `#1A1A1E` | Near-black with a hint of warmth. All body copy and headings. |
| Accent primary | `#1B2A4A` | Deep navy. The anchor. Use for key headings, borders, slide titles, and the one strong visual element on the slide. |
| Accent secondary | `#C4A35A` | Warm gold, subdued. Small accents: data highlights, separator lines, subtle fills. Use sparingly — one hit per slide at most. |
| Surface | `#EDEBE6` | Light warm neutral. Cards, table rows, panel backgrounds. |

Dark-mode variant (if needed):

| Role | Hex |
|---|---|
| Background | `#1A1A24` |
| Text | `#E8E6E1` |
| Accent primary | `#8BA4C7` |
| Accent secondary | `#C4A35A` |
| Surface | `#262630` |

## Typography

- **Headings:** Georgia (elegant serif, reads as classic and authoritative). Fallback: Times New Roman. Never less than 24pt on a slide.
- **Body:** Calibri (clean, safe, professional). Fallback: Arial. 14-18pt depending on density.
- **Code/monospace:** Courier New. 10-12pt. Use grey (`#555555`) to keep it from competing with headings.
- **No font weights below 400.** Light/ultralight weights read as trendy, not professional.
- **Kerning:** Normal or slightly tight (-0.2pt for headings if the tool allows it).

## Layout principles

- **White space is the primary layout tool.** Let content breathe. At least 0.5in padding on all sides of every slide. No element should feel crammed.
- **Alignment is strict.** Nothing placed by eye. Grid-align everything. If two elements share a horizontal or vertical axis, they share it exactly.
- **One visual anchor per slide.** A single navy element (a heading rule, a shape, a callout) that grounds the slide. Everything else recedes.
- **No gradients that fight the palette.** A subtle linear gradient from navy to a slightly lighter navy (`#1B2A4A` to `#2A4066`) is acceptable for a main shape. No rainbow, no neon, no glow.
- **No drop shadows that simulate 3D.** A flat, subtle shadow 2px below a card (opacity 0.08) is fine. No perspective, no depth.
- **Data before decoration.** If the slide has numbers, they are the hero: large, bold, in the accent primary or secondary colour. Don't bury them in a chart if a single number says more.

## Slide structure

- **Title slides:** Large Georgia heading (36-44pt), centred or left-aligned with a thin navy rule underneath. Subtitle in Calibri 18pt, `#555555`. No background image, no texture. Just typography and space.
- **Content slides:** A thin navy rule (1.5pt) at the top of the slide below the title, spanning the content width. This is your anchor. Bullet text in Calibri 16pt, `#1A1A1E`. Line spacing 1.3-1.5.
- **Section dividers:** Navy background (`#1B2A4A`), white text, one short line of copy, 44pt Georgia, no other elements. When you need the audience to reset.
- **Quote/testimonial slides:** Large Georgia italic 28pt, navy, centred, with a thin gold rule (`#C4A35A`) above and below the quote. Attribution in Calibri 14pt below, right-aligned.

## When to use this skill

- Creating a presentation, brochure, report, or one-pager that needs to look like it belongs in a boardroom.
- Reskinning an existing deck that leans too colourful, themed, informal, or "tech startup".
- The user says "make it professional", "clean it up", "classy", "elegant", or "sober colours".
- The audience is executives, investors, clients, or any room where the goal is credibility, not flash.

## What this is not for

- Casual internal decks, personal projects, creative pitches, or anything where the brief explicitly calls for a distinctive or themed look. Those need their own direction.
