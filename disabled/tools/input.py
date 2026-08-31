"""Real keyboard and mouse input into the user's X session, in pure Python.

Sends real X11 input events straight from python-xlib's XTEST extension - no
xdotool, no shell, no subprocess. Keys, key combos (including the Super /
Windows key), typed text (unicode included), mouse movement, clicks and wheel
scrolling, with waits, as one ordered list of actions. The events land in
whatever window has focus, exactly as if the user pressed the keys themselves.

All the fiddly work is done from the display's own keymap rather than assumed:
a key's keysym is looked up with keysym_to_keycodes(), which returns the
keycode AND the level index, so shifted characters ('A', '!') press Shift
themselves and AltGr characters ('@' on some layouts) press Mode_switch.
Characters not on the keymap at all are typed by temporarily remapping a spare
keycode to their unicode keysym and restoring it immediately after - the same
trick xdotool uses.

Stuck modifier keys are handled too: any modifier physically held down is
released for the duration of a key/type action and re-pressed afterwards
(xdotool's --clearmodifiers behaviour), and Caps Lock is toggled off around
typing letters and back on after, so 'Hello' never comes out as 'hELLO'.
"""

NAME = "input"
DESCRIPTION = ("Press real keyboard keys (including the Windows/Super key at the bottom-left of "
               "the keyboard), type text, move the mouse, click, scroll and wait - one scripted "
               "list of real input events delivered to whatever window currently has focus on the "
               "user's screen, exactly as if the user pressed them. Pure X11 (XTEST) - no xdotool, "
               "no shell. Use it to drive a GUI program, fill a form, answer a dialog or demo "
               "something. The whole sequence goes in the single `actions` argument.")
INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "input".

The only argument is `actions`: a list of steps run in order. Each step is an
object with a "do" field:
- {"do": "key", "keys": "ctrl+c"} or {"do": "key", "keys": "ctrl+c Tab Tab", "repeat": 2}
- {"do": "type", "text": "hello world", "delay": 20}
- {"do": "wait", "seconds": 0.5}
- {"do": "mouse", "x": 100, "y": 200}
- {"do": "click", "button": "left", "x": 100, "y": 200, "repeat": 1}
- {"do": "scroll", "direction": "down", "amount": 3}

The events go to whichever window has focus. Make sure the right window is
active first - use wmctrl or a click action to focus it. The tool reports the
pointer position and what each step did.

WHAT THIS TOOL ACTUALLY DOES: These are REAL input events on the user's real
machine - the focused application sees the keys, text, mouse movements and
clicks exactly as if a person at the keyboard did them. This is not a
simulation. So the user's own keys and mouse still work normally; your events
are merged with theirs. Never type anything the user did not ask to be typed,
and never click things the user did not ask to be clicked - a click can hit
buttons with real consequences (delete, send, buy)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "description": ("REQUIRED. A list of input steps, run in order, each an object with a "
                            '"do" field naming the action. Supported actions:\n'
                            '1) {"do": "key", "keys": ...} - press real keyboard keys. "keys" is a '
                            'string; a space separates separate presses, "+" joins a simultaneous '
                            'combo. Examples: "Return", "Tab", "ctrl+c", "ctrl+alt+Delete", '
                            '"super", "super+Return", "ctrl+c Tab Tab". Key names are real X11 '
                            'names - Return, Escape, Tab, BackSpace, Delete, Insert, Home, End, '
                            'Page_Up, Page_Down, Up, Down, Left, Right, F1-F24, space, single '
                            'letters and digits - plus friendly aliases: enter=Return, esc=Escape, '
                            'del=Delete, ins=Insert, pgup/pgdn, win/windows/super/meta = the '
                            'Windows key (bottom-left of the keyboard, "Super" on Linux), and '
                            'ctrl/shift/alt as modifiers. Optional "repeat": int (default 1) '
                            'presses the whole thing that many times.\n'
                            '2) {"do": "type", "text": "..."} - type text into the focused window '
                            'as keystrokes; a newline in the text presses Return. Optional '
                            '"delay": milliseconds between characters, default 20. Unicode '
                            'characters are typed correctly. Max 5000 characters per step.\n'
                            '3) {"do": "wait", "seconds": 0.5} - pause, 0.01 to 30 seconds.\n'
                            '4) {"do": "mouse", "x": 100, "y": 200} - move the mouse pointer to '
                            'absolute screen coordinates (0,0 is the top-left of the ultrawide '
                            'monitor; the whole desktop spans 5360x1440 - left monitor 0..3440, '
                            'right monitor 3440..5360).\n'
                            '5) {"do": "click", "button": "left"|"middle"|"right"} - click. '
                            'Optional "x"/"y" move there first; optional "repeat" (default 1).\n'
                            '6) {"do": "scroll", "direction": "up"|"down", "amount": 3} - wheel '
                            'scroll; optional "x"/"y" move there first.\n'
                            'The events go to whichever window has focus - make sure the right '
                            'window is active first. The result tells you the pointer position and '
                            'what each step did.'),
            "items": {
                "type": "object",
                "properties": {
                    "do": {"type": "string", "description":
                        "key | type | wait | mouse | click | scroll"},
                    "keys": {"type": "string", "description":
                        "For key: the key(s) to press - space separates separate presses, \"+\" "
                        "joins a simultaneous combo, e.g. \"ctrl+c Tab\", \"super+Return\"."},
                    "repeat": {"type": "integer", "description":
                        "For key/click: how many times, default 1."},
                    "text": {"type": "string", "description":
                        "For type: the text to type as keystrokes; newlines press Return."},
                    "delay": {"type": "number", "description":
                        "For type: milliseconds between characters, default 20."},
                    "seconds": {"type": "number", "description":
                        "For wait: seconds to pause, 0.01 to 30."},
                    "x": {"type": "integer", "description":
                        "For mouse/click/scroll: absolute x screen coordinate (0 is the left edge "
                        "of the ultrawide monitor)."},
                    "y": {"type": "integer", "description":
                        "For mouse/click/scroll: absolute y screen coordinate (0 is the top)."},
                    "button": {"type": "string", "description":
                        "For click: left, middle or right. Default left."},
                    "direction": {"type": "string", "description":
                        "For scroll: up or down."},
                    "amount": {"type": "integer", "description":
                        "For scroll: number of wheel steps, default 3."},
                },
            },
        },
    },
    "required": ["actions"],
}

import re
import time as _time

# Friendly names -> canonical X11 keysym names. Lowercased before lookup.
_ALIASES = {
    "win": "Super_L", "windows": "Super_L", "super": "Super_L", "meta": "Super_L",
    "win_l": "Super_L", "super_l": "Super_L", "meta_l": "Meta_L",
    "win_r": "Super_R", "super_r": "Super_R", "meta_r": "Meta_R",
    "enter": "Return", "return": "Return", "esc": "Escape",
    "backspace": "BackSpace", "del": "Delete", "delete": "Delete",
    "ins": "Insert", "insert": "Insert",
    "pgup": "Page_Up", "pageup": "Page_Up", "pgdn": "Page_Down", "pagedown": "Page_Down",
    "home": "Home", "end": "End", "space": "space", "tab": "Tab",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "capslock": "Caps_Lock", "caps": "Caps_Lock", "numlock": "Num_Lock",
    "scrolllock": "Scroll_Lock", "printscreen": "Print", "prtsc": "Print", "print": "Print",
    "pause": "Pause", "break": "Pause", "menu": "Menu",
    "ctrl": "Control_L", "control": "Control_L", "shift": "Shift_L", "alt": "Alt_L",
    "altgr": "Mode_switch",
    "kp_enter": "KP_Enter", "kp_add": "KP_Add", "kp_subtract": "KP_Subtract",
    "kp_multiply": "KP_Multiply", "kp_divide": "KP_Divide", "kp_decimal": "KP_Decimal",
    "kp_up": "KP_Up", "kp_down": "KP_Down", "kp_left": "KP_Left", "kp_right": "KP_Right",
    "kp_home": "KP_Home", "kp_end": "KP_End", "kp_page_up": "KP_Page_Up",
    "kp_page_down": "KP_Page_Down", "kp_insert": "KP_Insert", "kp_delete": "KP_Delete",
}
for _n in range(10):
    _ALIASES["kp_%d" % _n] = "KP_%d" % _n

# Momentary modifiers: keys pressed and HELD while the rest of a combo fires.
# (Caps/Num/Scroll_Lock are toggles, not momentary, so they are left out.)
_MOD_KEYSYMS = frozenset([
    "Control_L", "Control_R", "Shift_L", "Shift_R",
    "Alt_L", "Alt_R", "Meta_L", "Meta_R",
    "Super_L", "Super_R", "Hyper_L", "Hyper_R", "Mode_switch",
])

_MAX_TEXT = 5000
_MAX_REPEAT = 100


class _StepError(Exception):
    """Raised from _execute with which step failed and why; turned into the
    tool's ERROR string in run()."""

    def __init__(self, step, what, message):
        super().__init__(message)
        self.step = step      # 1-based index
        self.what = what      # short description of the step
        self.message = message


def _execute(d, X, XK, xtest, actions):
    """Run the actions against display d, returning the result string.

    Everything below lives here rather than at module level because the
    module is reloaded every turn: the import of Xlib is done once per call
    in run(), and these helpers only exist while a call is in flight."""

    def sync():
        d.sync()

    def press(kc):
        xtest.fake_input(d, X.KeyPress, kc)
        sync()

    def release(kc):
        xtest.fake_input(d, X.KeyRelease, kc)
        sync()

    def key_down(kbd, kc):
        # XQueryKeymap: keycode N lives in byte N/8, bit N%8.
        return bool(kbd[kc // 8] & (1 << (kc % 8)))

    def kc_for(name):
        return d.keysym_to_keycode(XK.string_to_keysym(name))

    def resolve(token):
        """'ctrl', 'enter', 'F5', 'a' ... -> (keysym, is_modifier)."""
        lower = token.lower()
        canon = _ALIASES.get(lower)
        if canon is None:
            m = re.fullmatch(r"f([0-9]{1,2})", lower)
            if m and 1 <= int(m.group(1)) <= 24:
                canon = "F" + m.group(1)
        if canon is not None:
            keysym = XK.string_to_keysym(canon)
            if not keysym:
                raise ValueError("no such key '%s'" % token)
            return keysym, keysym in _MOD_KEYSYMS
        if len(token) == 1:
            o = ord(token)
            keysym = o if 32 <= o < 127 else 0x1000000 + o
            return keysym, False
        keysym = XK.string_to_keysym(token)
        if not keysym:
            raise ValueError(
                "unknown key '%s' - use a real X11 key name (Return, Tab, F5, "
                "Page_Up, ...) or an alias (enter, esc, del, win, super, ctrl, ...)"
                % token)
        return keysym, keysym in _MOD_KEYSYMS

    def mods_for(idx):
        """Which modifier keycodes produce keysym level `idx` of a key.
        0=plain, 1=Shift, 2=Mode_switch (AltGr), 3=Shift+Mode_switch;
        levels >=4 (a second keymap group) get Mode_switch (+ Shift for odd
        levels) as a close approximation."""
        if idx == 0:
            return []
        if idx == 1:
            return [kc_for("Shift_L")]
        if idx == 2:
            return [kc_for("Mode_switch")]
        if idx == 3:
            return [kc_for("Shift_L"), kc_for("Mode_switch")]
        group, level = divmod(idx, 4)
        mods = []
        if group:
            mods.append(kc_for("Mode_switch"))
        if level in (1, 3):
            mods.append(kc_for("Shift_L"))
        return [kc for kc in mods if kc]

    def press_regular(keysym, gap):
        """Press and release one non-modifier keysym, adding whatever
        modifiers its keymap level needs (Shift for 'A'/'!', AltGr, ...).
        Keysyms the keymap cannot produce are typed by temporarily remapping
        a spare keycode to their unicode keysym, then restoring it."""
        pairs = list(d.keysym_to_keycodes(keysym))
        if not pairs:
            kc = 8  # spare: unassigned on every real keyboard
            old = list(d.get_keyboard_mapping(kc, 1)[0])
            d.change_keyboard_mapping(kc, [(keysym,)])
            sync()
            try:
                press(kc)
                _time.sleep(gap)
                release(kc)
            finally:
                d.change_keyboard_mapping(kc, [old])
                sync()
            return
        kc, idx = pairs[0]
        mods = mods_for(idx)
        for mk in mods:
            press(mk)
        press(kc)
        _time.sleep(gap)
        release(kc)
        for mk in reversed(mods):
            release(mk)

    def press_combo(combo, gap=0.015):
        """One key string, e.g. 'ctrl+c', 'super+Return', 'F5', '!'."""
        tokens = [t for t in combo.split("+") if t]
        if not tokens:
            raise ValueError("empty key combo '%s'" % combo)
        resolved = [resolve(t) for t in tokens]
        mod_kcs = [d.keysym_to_keycode(ks) for ks, is_mod in resolved
                   if is_mod and d.keysym_to_keycode(ks)]
        regular = [ks for ks, is_mod in resolved if not is_mod]
        for kc in mod_kcs:
            press(kc)
        for ks in regular:
            press_regular(ks, gap)
        _time.sleep(gap)
        for kc in reversed(mod_kcs):
            release(kc)

    def clear_mods():
        """Keycodes of momentary modifiers physically held down right now."""
        try:
            kbd = d.query_keymap()
        except Exception:
            return []
        held = [kc for ks in _MOD_KEYSYMS
                if (kc := d.keysym_to_keycode(XK.string_to_keysym(ks))) and key_down(kbd, kc)]
        return held

    def with_cleared_mods(fn):
        """Release held modifiers for the duration of fn, re-press after
        (xdotool --clearmodifiers): a Ctrl the user is physically holding must
        not turn 'c' into Ctrl-C."""
        held = clear_mods()
        if not held:
            return fn()
        for kc in held:
            release(kc)
        try:
            return fn()
        finally:
            for kc in held:
                press(kc)

    def type_text(text, delay_ms, gap=0.005):
        for ch in text:
            o = ord(ch)
            if ch == "\n":
                press_combo("Return", gap)
            elif ch == "\t":
                press_combo("Tab", gap)
            elif o < 32:
                pass  # other control characters: nothing to type
            elif o < 127:
                press_regular(o, gap)          # ASCII: keysym == codepoint
            else:
                press_regular(0x1000000 + o, gap)  # unicode keysym
            if delay_ms:
                _time.sleep(delay_ms / 1000.0)

    def move(x, y):
        w, h = d.screen().width_in_pixels, d.screen().height_in_pixels
        x = max(0, min(int(x), w - 1))
        y = max(0, min(int(y), h - 1))
        xtest.fake_input(d, X.MotionNotify, x=x, y=y)
        sync()
        return x, y

    def click(button, repeat, x, y):
        if x is not None:
            move(x, y)
        for _ in range(repeat):
            xtest.fake_input(d, X.ButtonPress, button)
            sync()
            _time.sleep(0.02)
            xtest.fake_input(d, X.ButtonRelease, button)
            sync()
            if repeat > 1:
                _time.sleep(0.05)

    def capslock_on():
        try:
            return bool(d.get_keyboard_control().led_mask & 1)
        except Exception:
            return False

    # ------------------------------------------------------------- the run
    log = []
    for i, step in enumerate(actions, 1):
        if not isinstance(step, dict) or not step.get("do"):
            raise _StepError(i, str(step)[:60], "each action must be an object with a \"do\" field")
        do = step["do"]
        try:
            if do == "wait":
                secs = float(step.get("seconds", 1))
                if not 0.01 <= secs <= 30:
                    raise ValueError("seconds must be between 0.01 and 30")
                _time.sleep(secs)
                log.append("%d. wait %.2fs -> ok" % (i, secs))

            elif do == "key":
                keys = step.get("keys", "")
                if isinstance(keys, (list, tuple)):
                    keys = " ".join(str(k) for k in keys)
                keys = str(keys).strip()
                if not keys:
                    raise ValueError("key action needs a \"keys\" value")
                repeat = max(1, min(int(step.get("repeat", 1)), _MAX_REPEAT))
                combos = keys.split()

                def do_keys():
                    for _ in range(repeat):
                        for combo in combos:
                            press_combo(combo)
                            _time.sleep(0.02)
                        if repeat > 1:
                            _time.sleep(0.05)
                with_cleared_mods(do_keys)
                log.append("%d. key %s%s -> ok" % (i, keys, " x%d" % repeat if repeat > 1 else ""))

            elif do == "type":
                text = step.get("text", "")
                if not isinstance(text, str):
                    text = str(text)
                if not text:
                    raise ValueError("type action needs a non-empty \"text\" value")
                if len(text) > _MAX_TEXT:
                    raise ValueError("text is %d characters - too long (max %d); split it up"
                                     % (len(text), _MAX_TEXT))
                delay = max(0.0, min(float(step.get("delay", 20)), 500.0))
                caps = capslock_on()
                has_letters = any(ch.isalpha() for ch in text)

                def do_type():
                    type_text(text, delay)
                try:
                    if caps and has_letters:
                        press_combo("Caps_Lock", 0.01)   # off for typing
                    with_cleared_mods(do_type)
                finally:
                    if caps and has_letters:
                        press_combo("Caps_Lock", 0.01)   # back on after
                shown = text if len(text) <= 40 else text[:37] + "..."
                log.append('%d. type "%s" -> ok' % (i, shown))

            elif do == "mouse":
                if "x" not in step or "y" not in step:
                    raise ValueError("mouse action needs x and y")
                x, y = move(step["x"], step["y"])
                log.append("%d. mouse -> (%d, %d)" % (i, x, y))

            elif do == "click":
                button = {"left": 1, "middle": 2, "right": 3}.get(
                    str(step.get("button", "left")).lower())
                if not button:
                    raise ValueError("click button must be left, middle or right")
                repeat = max(1, min(int(step.get("repeat", 1)), _MAX_REPEAT))
                click(button, repeat,
                      step.get("x"), step.get("y"))
                where = " at (%s, %s)" % (step["x"], step["y"]) if "x" in step else ""
                log.append("%d. click %s%s%s -> ok"
                           % (i, step.get("button", "left"), where,
                              " x%d" % repeat if repeat > 1 else ""))

            elif do == "scroll":
                direction = str(step.get("direction", "down")).lower()
                if direction not in ("up", "down"):
                    raise ValueError("scroll direction must be up or down")
                amount = max(1, min(int(step.get("amount", 3)), _MAX_REPEAT))
                button = 4 if direction == "up" else 5
                click(button, amount, step.get("x"), step.get("y"))
                log.append("%d. scroll %s x%d -> ok" % (i, direction, amount))

            else:
                raise ValueError("unknown action \"%s\" - use key, type, wait, mouse, click or scroll"
                                 % do)
        except _StepError:
            raise
        except Exception as e:
            raise _StepError(i, "%s %s" % (do, str(step)[:60]), str(e))

    w, h = d.screen().width_in_pixels, d.screen().height_in_pixels
    try:
        p = d.screen().root.query_pointer()
        head = "screen %dx%d, pointer now at (%d, %d)" % (w, h, p.root_x, p.root_y)
    except Exception:
        head = "screen %dx%d" % (w, h)
    return head + "\n" + "\n".join(log)


def run(actions):
    """Perform the `actions` sequence as real input events and report back."""
    if not actions:
        return "ERROR: `actions` was empty - nothing was pressed, typed or clicked."
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        return "ERROR: `actions` must be a list of action objects."

    try:
        import os
        from Xlib import X, XK, display
        from Xlib.ext import xtest
    except ImportError as e:
        return ("ERROR: this tool needs python-xlib in Uniagent's Python "
                "(pip install python-xlib): %s" % e)

    try:
        d = display.Display(os.environ.get("DISPLAY") or ":0")
    except Exception as e:
        return ("ERROR: could not open the X display (%s) - is the user logged "
                "into a graphical session? Set DISPLAY if it is not :0." % e)

    try:
        try:
            return _execute(d, X, XK, xtest, actions)
        except _StepError as e:
            return ("ERROR at step %d (%s): %s. Steps 1..%d were performed; "
                    "nothing after step %d ran."
                    % (e.step, e.what, e.message, e.step - 1, e.step))
        except Exception as e:
            return "ERROR: %s: %s" % (type(e).__name__, e)
    finally:
        try:
            d.close()
        except Exception:
            pass
