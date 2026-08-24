---
name: claude-code-remote
description: How to open a new GNOME Terminal at the Uniagent project, launch Claude Code, and enable /remote-control mode. Use when the user wants a fresh Claude Code terminal ready for remote control.
---

# Open Claude Code with Remote Control

## Steps

1. **Launch a new GNOME Terminal** at the Uniagent project directory:
   ```bash
   gnome-terminal --working-directory=/media/joshy/WD_Blue/Projects/Uniagent -- bash -c "claude; exec bash"
   ```

2. **Wait for Claude Code to fully start** (about 5 seconds), then find the new window:
   ```bash
   sleep 3 && wmctrl -l | grep -i "claude\|gnome-terminal"
   ```
   Look for the newest window ID with "✳ Claude Code" in the title.

3. **Raise that window** so it has focus:
   ```bash
   DISPLAY=:0 wmctrl -ia <WINDOW_ID>
   ```

4. **Send the `/remote-control` command** via real keystrokes:
   ```bash
   # Using the input tool:
   # wait 5 seconds for Claude to be ready, then type /remote-control + Return
   ```

## Notes

- The `exec bash` at the end keeps the terminal open after you exit Claude Code.
- The 5-second wait before sending `/remote-control` is important — Claude Code needs time to initialize its TUI before accepting commands.
- If the command doesn't register, send `Ctrl+C` first to clear any pending input, then retry.
- Window IDs change every launch; always re-query with `wmctrl -l` after opening the terminal.

## One-liner for the terminal (if doing manually)

```bash
gnome-terminal --working-directory=/media/joshy/WD_Blue/Projects/Uniagent -- bash -c "claude; exec bash" && sleep 5 && DISPLAY=:0 wmctrl -ia $(wmctrl -l | grep "✳ Claude Code" | tail -1 | awk '{print $1}')
```
Then manually type `/remote-control` in the raised window.