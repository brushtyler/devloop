"""The language of the texts the dispatcher writes (#628, #631).

The project's ``language`` (``Config.language``, default ``English``) is free text, the same the
prompts tell the agents to write in. The dispatcher's own texts exist in a few languages only:
``language_code`` maps the value to one of ``LANGUAGES``, English for any language without a
translation.

A ``Catalog`` holds the texts of one module, by language: English has every key, another
language may lack some, which then read in English. ``Catalog.texts`` gives the texts of a
language, ``Catalog.variants`` every translation of a key: what recognises a published text by
its title or a phrase, whatever the language it was written in, so that a change of ``language``
leaves the texts already published recognised. Markers, commands and labels are never
translated.
"""

import re

ENGLISH = 'en'
ITALIAN = 'it'
LANGUAGES = (ENGLISH, ITALIAN)
# The names of a language the project may write, compared without case: English and its own.
_NAMES = {
    'en': ENGLISH, 'english': ENGLISH, 'inglese': ENGLISH,
    'it': ITALIAN, 'italian': ITALIAN, 'italiano': ITALIAN,
}
_SEPARATORS = re.compile(r'[-_\s(]')


def language_code(language):
    """The code in ``LANGUAGES`` of ``language``: a name (``English``, ``italiano``) or a tag
    (``it``, ``it-IT``, ``en_US``), without case; English for any other value."""
    name = str(language or '').strip().casefold()
    if name in _NAMES:
        return _NAMES[name]
    return _NAMES.get(_SEPARATORS.split(name, 1)[0], ENGLISH)


class Catalog:
    """The texts of a module by language code: ``Catalog(en={...}, it={...})``. English must
    hold every key, and no other language a key English lacks. A text with fields is a
    ``str.format`` template."""

    def __init__(self, **tables):
        unknown = set(tables) - set(LANGUAGES)
        if unknown or ENGLISH not in tables:
            raise ValueError(f'catalog: languages {sorted(tables)}, English required among '
                             f'{list(LANGUAGES)}')
        for code, table in tables.items():
            extra = set(table) - set(tables[ENGLISH])
            if extra:
                raise ValueError(f'catalog: keys {sorted(extra)} of {code} are not in English')
        self.tables = tables

    def texts(self, language):
        """The ``Texts`` of the project's ``language`` (``language_code``)."""
        return Texts(self, language_code(language))

    def variants(self, key):
        """Every translation of ``key``, English first, without repetitions."""
        found = []
        for code in LANGUAGES:
            text = self.tables.get(code, {}).get(key)
            if text is not None and text not in found:
                found.append(text)
        return tuple(found)


class Texts:
    """The texts of ``catalog`` in language ``code``: ``texts(key, **fields)`` is the text of
    ``key``, in English when that language lacks it, its fields filled."""

    def __init__(self, catalog, code):
        self.catalog = catalog
        self.code = code

    def __call__(self, key, **fields):
        text = self.catalog.tables.get(self.code, {}).get(key)
        if text is None:
            text = self.catalog.tables[ENGLISH][key]
        return text.format(**fields) if fields else text
