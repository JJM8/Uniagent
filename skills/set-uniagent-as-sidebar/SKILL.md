---
name: set-uniagent-as-sidebar
description: Pin the Uniagent window to the left 20% of the leftmost (ultrawide) monitor as a sidebar. Use when the user asks to turn Uniagent into a sidebar, split the screen, or make a sidebar.
---

# Set Uniagent as Sidebar

Pins the Uniagent window to the left 20% of the leftmost monitor
(DP-0, 3440×1440 at position +0+0), so it acts as a dedicated
side panel.

## The command

```bash
export DISPLAY=:0 && WIN=$(xdotool search --name "Uniagent" | tail -1) && wmctrl -i -r "$WIN" -b remove,maximized_vert,maximized_horz && wmctrl -i -r "$WIN" -e 0,0,0,688,1440
```

## What it does

1. Finds the Uniagent window by name via `xdotool search`
2. Unmaximizes it
3. Moves and resizes it to **x=0, y=0, width=688, height=1440** — that's
   the left 20% of the 3440×1440 ultrawide (688 = 3440 × 0.2)

## Monitor layout (current)

- `DP-0` — 3440×1440 at +0+0 (leftmost, the ultrawide)
- `DVI-D-0` — 1920×1080 at +3440+171 (primary)
- HDMI-0 / DP-1 are disconnected (older notes referenced HDMI-0 at +1920+0 — stale)

## Notes

- The `export DISPLAY=:0` is needed because Uniagent's own process
  doesn't have a DISPLAY set.
- The window will sit below any top panel and off the screen edge (it
  lands at around x=10, y=72, height=1408 automatically).
- To revert, maximise it again: `wmctrl -i -r "$WIN" -b toggle,maximized_vert,maximized_horz`
