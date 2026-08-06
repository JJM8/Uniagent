"""The workspace a tool falls back to when nobody passed it one.

Tools that take a `workspace` parameter are handed the chat's by
tool_processor. They can also be called with nothing - from a test, from
another tool, from a python -c while debugging - and then this is what they
get: a plain local workspace rooted at the install folder, which is exactly
where these tools resolved relative paths before workspaces existed.

Leading underscore so the tool loader skips it: it is a shared helper, not a
tool (see tool_processor.load_tools).
"""

import sys
from pathlib import Path

# tools/ is on sys.path when the loader imports a tool, but scripts/ only is
# when something in scripts/ started the process. A tool imported on its own
# has to be able to find workspace.py regardless.
_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import workspace as _workspace


def here():
    """A local workspace rooted at the install folder."""
    return _workspace.Workspace()
