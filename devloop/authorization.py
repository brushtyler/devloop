"""The coordinator's authorization of an issue (#607, ADR 0002, second precision of 2026-11-23,
point 1).

The coordinator authorizes the work with a comment whose first line after the attribution header
is ``/devloop authorize [priority:<p>]``: the dispatcher records it (an ``authorize`` record, bound
to the SHA-256 of the title and the body, ``text_hash``) and removes ``agent:needs-coordinator``.
With ``[roles.coordinator]`` the supervisor's ``agent:ready`` admits an issue only while a recorded
authorization holds for its current text: a text changed since voids it, even if restored afterwards, and a new
``/devloop authorize`` is needed. A command of any other identity is refused, and recorded.

The functions here are pure: the dispatcher reads GitHub and writes the records
(``Dispatcher._do_authorize``, ``Dispatcher._ready_admitters``).
"""

import hashlib
import json

from .i18n import ENGLISH, Catalog
from .supervision import command_line

AUTHORIZE = 'authorize'
AUTHORIZE_COMMAND = f'/devloop {AUTHORIZE}'
# The priorities a command may carry, as the names of ``priority:<p>``.
PRIORITIES = ('high', 'normal', 'low')
OUTCOMES = ('authorized', 'refused')
# Why the arguments of a command are malformed, in the project's ``language`` (#635).
TEXTS = Catalog(
    en={'malformed': ('arguments not valid: the command is written `{command}` or '
                      '`{command} priority:<p>`, with `<p>` among {priorities}')},
    it={'malformed': ('argomenti non validi: il comando si scrive `{command}` o '
                      '`{command} priority:<p>`, con `<p>` fra {priorities}')})


def malformed(language=ENGLISH):
    """Why the arguments of a ``/devloop authorize`` are malformed, in ``language``."""
    return TEXTS.texts(language)('malformed', command=AUTHORIZE_COMMAND,
                                 priorities=', '.join(f'`{p}`' for p in PRIORITIES))


MALFORMED = malformed()


def authorize_arguments(body, language=ENGLISH):
    """``(priority, problem)`` of the ``/devloop authorize`` of a comment (#607), or None when
    the first line after the attribution header (``command_line``) is no such command.
    ``priority``: one of ``PRIORITIES``, or '' without one. ``problem``: '' or why the
    arguments are malformed, in ``language`` (#635). The rest of the comment is free text, such
    as the reason."""
    tokens = command_line(body).split()
    if tokens[:2] != ['/devloop', AUTHORIZE]:
        return None
    arguments = tokens[2:]
    if not arguments:
        return '', ''
    name, _, value = arguments[0].partition(':')
    if len(arguments) == 1 and name == 'priority' and value in PRIORITIES:
        return value, ''
    return '', malformed(language)


def text_hash(title, body):
    """The SHA-256 of an issue's title and body, what an authorization is bound to (#607)."""
    data = json.dumps([title or '', body or ''], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def changed_since(history, at):
    """Whether the title or the body of an issue (``history``, as ``Tracker.issue_history``)
    may have changed at or after ``at``, the time of a command: GitHub timestamps have
    one-second granularity, so a change in the same second cannot be proven to precede it. A
    command without a time is after no change only when the text was never changed."""
    times = [history.get('last_edited_at'), *(when for _, when in history.get('renamed') or ())]
    return any(t and (not isinstance(at, str) or t >= at) for t in times)


def valid_authorization(records, history):
    """The latest ``authorized`` record of ``records`` (the dispatcher's ``authorize`` records
    of the issue, oldest first) bound to the issue's current title and body (``history``), or
    None: a text changed since the authorization voids it, even when restored afterwards to
    the text the coordinator read. Both are checked: the hash of the current text, and the
    history of the changes against the time of the command (``changed_since``)."""
    digest = text_hash(history['title'], history['body'])
    return next((r for r in reversed(records)
                 if r['outcome'] == 'authorized' and r.get('hash') == digest
                 and not changed_since(history, r.get('at'))), None)
