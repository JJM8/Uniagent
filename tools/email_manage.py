"""Mailbox housekeeping: mark read/unread, move between folders, move to Trash.

Everything here is reversible, which is the whole design: there is deliberately
no way to permanently delete a message. "trash" moves it to the provider's
Trash folder, where it sits for about 30 days - so a mistake is always undoable
from the webmail. Emptying the trash for real is something the user does themselves.
"""

import _email

NAME = "email_manage"
DESCRIPTION = ("Tidy the user's mailbox: mark messages read or unread, move them between "
               "folders, or move them to Trash. Never deletes anything permanently.")

# Read from .env at import - see the same block in email_tool.py.
_ACCOUNTS = _email.account_names()
_ACCOUNT_HELP = _email.account_help()
_DEFAULT = _email.default_name()

INSTRUCTIONS = """HOW TO CALL: use the tool-call syntax already given to you, with tool name "email_manage".

""" + _ACCOUNT_HELP + """

Arguments:
- action:      REQUIRED. One of "mark_read", "mark_unread", "move", "trash".
- uid:         one uid, or a list of them - up to 25 in a call. Take uids from
               the square brackets in an email list/search result.
- folder:      OPTIONAL, where those uids live. Defaults to "INBOX".
- account:     OPTIONAL, the name of the account to work on. Left out, it uses
               """ + ('"' + str(_DEFAULT) + '".' if _DEFAULT else "the default one.") + """
- dest_folder: REQUIRED for "move" only - where to move them to.

UIDS ARE PER FOLDER, so always pass the folder you got them from. A uid moves
to a NEW number when you move the message - do not reuse a uid after moving it,
list the destination folder again instead.

NOTHING HERE IS PERMANENT. "trash" means moved to Trash, where it stays for
about 30 days and can be recovered. There is no permanent-delete action and you
should not try to build one out of the terminal - if the user wants something gone
for good they will do it themselves.

Marking and moving are reversible so they run without asking the user first. You
get back exactly what changed - report that honestly rather than assuming."""

# For native provider tool-calling. `uid` is scalar-or-list in practice (see
# _uids() below), expressed here as oneOf so either shape validates.
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                    "enum": ["mark_read", "mark_unread", "move", "trash"],
                    "description": "Which operation to perform."},
        "uid": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                 "description":
            "One uid, or a list of them - up to 25 in a call. Take uids from "
            "the square brackets in an email list/search result."},
        "folder": {"type": "string", "description":
            "Optional, where those uids live. Defaults to \"INBOX\"."},
        "account": {"type": "string",
                     **({"enum": _ACCOUNTS} if _ACCOUNTS else {}),
                     "description": "Optional, which account to work on. " + _ACCOUNT_HELP},
        "dest_folder": {"type": "string", "description":
            "Required for \"move\" only - where to move them to."},
    },
    "required": ["action", "uid"],
}


def _uids(value):
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in values if str(v).strip()]


def run(action, uid=None, account=None, folder="INBOX", dest_folder=None):
    action = (action or "").strip().lower()
    # Check the action before logging in: a typo shouldn't cost an IMAP round
    # trip, and "no permanent delete" is worth saying even on an unset-up account.
    if action not in ("mark_read", "mark_unread", "move", "trash"):
        return ('ERROR: no such action "' + action + '". Use "mark_read", '
                '"mark_unread", "move" or "trash". There is deliberately no '
                "permanent delete - trash is recoverable, gone is not.")
    uids = _uids(uid)
    if not uids:
        return ('ERROR: no "uid" given. Take uids from the square brackets in an '
                "email list or search result.")
    if len(uids) > _email.LIST_CAP:
        return ("ERROR: that is " + str(len(uids)) + " messages and the limit is "
                + str(_email.LIST_CAP) + " per call. Do it in smaller batches so a "
                "mistake stays small.")

    try:
        imap, address = _email.connect(account)
    except _email.EmailError as e:
        return "ERROR: " + str(e)

    try:
        # readonly=False: this tool is the one that's allowed to change things.
        real = _email.select(imap, folder, readonly=False)

        if action in ("mark_read", "mark_unread"):
            sign = "+FLAGS" if action == "mark_read" else "-FLAGS"
            for u in uids:
                typ, _ = imap.uid("STORE", u, sign, "\\Seen")
                if typ != "OK":
                    return "ERROR: the server refused to change flags on uid " + u
            word = "read" if action == "mark_read" else "unread"
            return ("(marked " + str(len(uids)) + " message(s) " + word + " in "
                    + real + " on " + address + ": " + ", ".join(uids) + ")")

        if action in ("move", "trash"):
            if action == "trash":
                destination = _email.resolve_folder(imap, "trash")
            else:
                if not dest_folder:
                    return 'ERROR: "move" needs a "dest_folder" to move them to.'
                destination = _email.resolve_folder(imap, dest_folder)
            if destination == real:
                return ("ERROR: " + real + " is already where those messages are.")

            # COPY then flag+expunge, rather than MOVE: it behaves the same on
            # both providers, and on Gmail a bare \\Deleted+EXPUNGE in INBOX
            # only removes the label - the message would quietly survive.
            for u in uids:
                typ, data = imap.uid("COPY", u, '"' + destination + '"')
                if typ != "OK":
                    return ("ERROR: could not copy uid " + u + " to " + destination
                            + " (" + str(data) + "). Nothing was removed from "
                            + real + ".")
            for u in uids:
                imap.uid("STORE", u, "+FLAGS", "\\Deleted")
            imap.expunge()
            return ("(moved " + str(len(uids)) + " message(s) from " + real + " to "
                    + destination + " on " + address + ": " + ", ".join(uids)
                    + ". They are recoverable from " + destination + ".)")

        return ('ERROR: no such action "' + action + '". Use "mark_read", '
                '"mark_unread", "move" or "trash". There is deliberately no '
                "permanent delete.")

    except _email.EmailError as e:
        return "ERROR: " + str(e)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
