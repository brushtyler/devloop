"""The step on the host a PR declares (#462).

Implementer and fixer return ``host_step``: empty, or a short text of what the operator must do
on the host, with the loop stopped, before the merged code is pinned (a new table of the project
file, a check to run), with a reference to the document that describes it. The PR marker keeps
the implementer's, a fix record the one that replaces it; the latest holds (``latest``). A PR
that declares one is merged as the others, but its merge holds the auto-pin until the operator
pins by hand (``pin.hold``).
"""

# The text of a declaration whose record is not a string: it holds the pin all the same.
UNREADABLE = '(dichiarazione non leggibile: vedere la PR)'


def latest(meta, fixes):
    """The step on the host that holds for a PR: that of the latest fix record that carries
    ``host_step``, else that of the PR marker ``meta``; '' when none declares one."""
    for record in [meta or {}, *(fixes or ())][::-1]:
        if 'host_step' in record:
            value = record['host_step']
            return value.strip() if isinstance(value, str) else UNREADABLE
    return ''


def format_step(step):
    """The declaration as prompt text."""
    if not step:
        return 'None: the pull request declares no step on the host (`host_step` is empty).'
    return '\n'.join('> ' + line for line in step.splitlines())


def render_step(step):
    """The declaration as Markdown for GitHub, under its own heading; '' without one."""
    if not step:
        return ''
    return ('### Passo sull\'host\n\nPrima del pin di questo codice l\'operatore deve fare, a '
            'loop fermo:\n\n' + '\n'.join('> ' + line for line in step.splitlines())
            + '\n\nIl merge di devloop sospende il pin automatico fino a un pin a mano '
              '(`devloop pin`) dopo il passo.')
