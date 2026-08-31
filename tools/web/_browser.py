"""Finding a real browser engine to load a page with.

Both tools in this folder need one and neither should have its own idea of
where it lives: web_fetch falls back to a headless browser when a page turns
out to be JavaScript-gated, and firefox uses one for the whole job on Windows.

It used to be the bare string "google-chrome". That is the Debian package's
name for it and nothing else's - not Chromium, not a Chrome installed from
Google's own .deb on some distributions, and certainly not Windows, where
nothing is on PATH by name and browsers live under Program Files. So the
fallback simply never fired anywhere but one kind of Linux desktop, and the
page came back as whatever the JavaScript gate had left behind.

The leading underscore keeps this out of the tool loader: it is a helper the
two tools import, not a tool.
"""

import os
import shutil
from pathlib import Path

WINDOWS = os.name == "nt"

# Any Chromium-family browser will do - they all take the same switches. Named
# in rough order of "most likely to be the one that is there".
_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
          "chrome", "msedge", "microsoft-edge", "brave-browser")

# Windows puts none of them on PATH, so the usual homes are checked directly.
# Edge is first because it is on every Windows 10 and 11 machine by definition -
# it ships with the OS, so this is the one that does not depend on the user
# having installed anything.
_WINDOWS_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)


def find():
    """The path to a usable browser, or None if there isn't one."""
    for name in _NAMES:
        found = shutil.which(name)
        if found:
            return found
    if WINDOWS:
        for path in _WINDOWS_PATHS:
            if Path(path).exists():
                return path
        # A per-user install, which is where Chrome puts itself without admin
        # rights and is therefore common on a work machine.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for rel in (r"Google\Chrome\Application\chrome.exe",
                        r"Microsoft\Edge\Application\msedge.exe"):
                p = Path(local) / rel
                if p.exists():
                    return str(p)
    return None


def argv(browser, url, user_agent=None, budget_ms=8000):
    """The command line that loads `url`, runs its JavaScript and prints the
    finished DOM.

    --headless=new is the current spelling of headless mode. --virtual-time-
    budget gives the page's scripts a few seconds to finish before the DOM is
    taken; without it a page that renders itself asynchronously dumps as an
    empty shell, which is the exact failure this is meant to fix.
    """
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--virtual-time-budget=" + str(budget_ms)]
    if not WINDOWS:
        # Only useful where the process might be running as a confined user;
        # on Windows it is not a recognised switch.
        cmd.append("--no-sandbox")
    if user_agent:
        cmd.append("--user-agent=" + user_agent)
    cmd += ["--dump-dom", url]
    return cmd


NOT_FOUND = ("no browser that can render a page was found. Looked for Chrome, "
             "Chromium, Edge and Brave" + (" in the usual Program Files "
             "locations" if WINDOWS else " on PATH") + ".")
