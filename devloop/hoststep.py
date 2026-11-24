"""The step on the host a PR declares (#462).

Implementer and fixer return ``host_step``: empty, or a short text of what the operator must do
on the host, with the loop stopped, before the merged code is pinned (a new table of the project
file, a check to run), with a reference to the document that describes it. The PR marker keeps
the implementer's, a fix record the one that replaces it; the latest holds (``latest``). A PR
that declares one is merged as the others, but its merge holds the auto-pin until the operator
pins by hand (``pin.hold``).
"""

from .i18n import ENGLISH, Catalog

# The texts of the declaration on GitHub, in the project's ``language`` (#635); English for a
# language without translation. ``unreadable``: a declaration whose record is not a string,
# which holds the pin all the same.
TEXTS = Catalog(
    en={
        'unreadable': '(declaration not readable: see the PR)',
        'title': '### Step on the host',
        'intro': 'Before the pin of this code the operator must do, with the loop stopped:',
        'hold': ('The merge of devloop suspends the auto-pin until a pin by hand (`devloop pin`) '
                 'after the step.'),
    },
    it={
        'unreadable': '(dichiarazione non leggibile: vedere la PR)',
        'title': '### Passo sull\'host',
        'intro': 'Prima del pin di questo codice l\'operatore deve fare, a loop fermo:',
        'hold': ('Il merge di devloop sospende il pin automatico fino a un pin a mano '
                 '(`devloop pin`) dopo il passo.'),
    })
UNREADABLE = TEXTS.texts(ENGLISH)('unreadable')


def latest(meta, fixes, language=ENGLISH):
    """The step on the host that holds for a PR: that of the latest fix record that carries
    ``host_step``, else that of the PR marker ``meta``; '' when none declares one. A
    declaration that is not a string reads ``unreadable`` in ``language`` (#635)."""
    for record in [meta or {}, *(fixes or ())][::-1]:
        if 'host_step' in record:
            value = record['host_step']
            return (value.strip() if isinstance(value, str)
                    else TEXTS.texts(language)('unreadable'))
    return ''


def format_step(step):
    """The declaration as prompt text."""
    if not step:
        return 'None: the pull request declares no step on the host (`host_step` is empty).'
    return '\n'.join('> ' + line for line in step.splitlines())


def render_step(step, language=ENGLISH):
    """The declaration as Markdown for GitHub, under its own heading, in ``language`` (#635);
    '' without one."""
    if not step:
        return ''
    t = TEXTS.texts(language)
    return (t('title') + '\n\n' + t('intro') + '\n\n'
            + '\n'.join('> ' + line for line in step.splitlines()) + '\n\n' + t('hold'))
