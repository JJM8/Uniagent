# Infinite chat - ideas not yet built

Running list of things to consider adding to infinite chat now that the first
pass is in (`scripts/infini.py`). Nothing here is implemented. The design
discussion behind the feature is in `one-agent-mode.md`; the spec the first
pass was built from is in `infinite-chat-prompt.md`.

---

## Compacting on fork - FAVOURED, not yet implemented

**Josh is leaning towards adding a compaction element to the fork.** Recorded
here rather than built, so the judge can be watched on its own first.

### What it would be

Today a fork copies and leaves both ends whole:

- the child gets the turns from the cutoff onward, verbatim, under a
  `[carried from ...]` divider
- the parent keeps its entire history and gets a `[continued in chat-xxxx]`
  stub appended

So after a fork the same turns exist twice, and the parent is carrying a tail
that - if the judge was right - is now dead weight it will pay for on every
single request it makes from here on.

The compaction element would put `compaction.py` to work at one or both ends of
the seam:

- **the parent's tail.** Everything from the cutoff down summarised into a
  short block, so the parent stops re-sending a conversation that has moved out
  of it. `compaction.compact()` already does exactly this shape of thing (with
  its own archive copy first), just to a whole chat rather than to a range.
- **the child's head.** The carried turns are the START of the new chat and
  will only grow; there may be a case for summarising the older half of what
  was carried instead of copying it all, so a child born from a long tail
  doesn't start life already large.

### Why it was left out of the first pass

Two reasons, both still live:

1. **It blows the parent's prompt cache.** The parent's cached prefix is worth
   real money and it is not obvious the drift is permanent - you can be back in
   that chat in five minutes. Compacting the moment the fork happens spends
   that on a guess.
2. **It makes a wrong verdict expensive.** The whole reason the fork copies
   rather than moves is that a bad split then costs nothing: the parent still
   has everything. Compacting the parent takes that back - the archive is still
   there, but "go and dig it out of `chats/chat_archive/`" is not the same
   safety net as "it never left".

### Shapes worth considering

- **Deferred.** Fork now, compact the parent only if it goes untouched for a
  while (a day, say). Keeps the cache and the undo for exactly as long as
  either might still be wanted, and by the time it fires the judge has been
  proved right by the user simply not going back. This is the one that looks
  best on paper.
- **On the parent's next turn.** Cheaper to reason about than a timer, and the
  cache is being rebuilt at that moment anyway - but it fires precisely when
  somebody HAS come back, which is the case where the tail may still matter.
- **Immediate, behind a setting.** Simplest, and honest about the trade: an
  `infini_compact` toggle on the infiniagent tab, off by default.
- **Child-side only.** Leave the parent completely alone (keeps both objections
  above answered) and only summarise the older part of what the child carried.
  Much smaller change, and it addresses the duplication without touching the
  chat the user might still be working in.

### Open questions

- Which prompt? The compaction prompt summarises a conversation *for its own
  future*, which is right for the parent's tail but arguably wrong for the
  child's head - the child wants "here is what led up to this", not "here is
  where we got to". Possibly a fourth prompt setting, possibly the same one.
- Does the parent's summary keep the `[continued in ...]` stub visible under
  it? It has to - the stub is the only pointer from the old chat to the new
  one, and burying it inside a summary the model wrote is how that gets lost.
- If the parent is compacted, does the divider on the page still read
  correctly at both ends of the seam? See `render()` in `web/index.html`.
- Memory extraction (`memories/`) was parked for the same pass and is a
  *different* step from compaction - see the "two steps, not one" section of
  `one-agent-mode.md`. Whichever of these gets built, it should not quietly
  turn into the other.

---

## Also parked

Carried over from `infinite-chat-prompt.md`'s "explicitly NOT in this piece of
work", all still unbuilt:

- `agents.json` and an agents tab (see `agents-json.md`)
- memory extraction into `memories/`
- the heuristic prefilter - workspace change, path change, vocabulary overlap -
  so the judge isn't run after every single turn
- hysteresis / a confidence threshold. The `aside` verdict, the retroactive
  cutoff and the majority rule in the prompt cover most of it for now
- auto-resume: noticing the user has gone back to an old subject and returning
  to that chat rather than forking forward
- an **Undo** on the fork divider, for a few minutes after the split
- nesting the child under the parent in the sidebar
