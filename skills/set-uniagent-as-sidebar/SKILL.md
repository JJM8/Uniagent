---
name: set-uniagent-as-sidebar
description: Pin the Uniagent window to the right 20% of the main (ultrawide) monitor as a sidebar. Use when the user asks to turn Uniagent into a sidebar, split the screen, or make a sidebar.
---

# Set Uniagent as Sidebar

Pins the Uniagent window to the right 20% of the main ultrawide monitor
(HDMI-0, 3440×1440 at position +1920+0), so it acts as a dedicated
side panel.

## The command

```bash
export DISPLAY=:0 && WIN=$(xdotool search --name "Uniagent" | tail -1) && wmctrl -i -r "$WIN" -b remove,maximized_vert,maximized_horz && wmctrl -i -r "$WIN" -e 0,4672,0,688,1440
```

## What it does

1. Finds the Uniagent window by name via `xdotool search`
2. Unmaximizes it (it's normally fullscreen on the main monitor)
3. Moves and resizes it to **x=4672, y=0, width=688, height=1440** — that's
   the right 20% of the 3440×1440 ultrawide (688 = 3440 × 0.2),
   positioned at 1920 + (3440 × 0.8) = 4672

## Notes

- The `export DISPLAY=:0` is needed because Uniagent's own process
  doesn't have a DISPLAY set.
- The window will sit below any top panel (it'll get y=72 and
  height=1358 automatically if there's a panel).
- To revert, maximise it again: `wmctrl -i -r "$WIN" -b toggle,maximized_vert,maximized_horz`
