"""devloop: the language of the dispatcher's texts (#631). The project's ``language`` chooses
Italian or English, English for a language without translation; the ledger writes its records in
it, with the same markers, and the records already published stay recognised after a change of
``language``."""

import dataclasses
import unittest

from devloop import i18n, ledger, markdown, watch
from devloop.dispatcher import Dispatcher
from devloop.i18n import ENGLISH, ITALIAN, Catalog, language_code
from devloop.ledger import Ledger
from test_devloop import CFG, FakeRunner, FakeWS
from test_devloop_ledger import LedgerGH

TURN = {'type': 'turn', 'state': 'open', 'kind': 'review', 'agent': 'codex', 'identity': None,
        'mode': 'read', 'model': 'm', 'effort': 'high', 'fallback_for': None,
        'model_real': None, 'label': 'l', 'started_at': '2026-11-24T10:00:00Z',
        'ended_at': None, 'duration_ms': None, 'outcome': None, 'error': None,
        'telemetry': None}
VERIFICATION = {'type': 'verification', 'kind': 'fix', 'attempt': 1, 'command': 'make test',
                'outcome': 'pass', 'duration_ms': 5, 'tree_before': 't', 'tree_after': 't',
                'commit': None, 'at': '2026-11-24T10:00:00Z', 'retried': ['a.b']}
EVENT = {'type': 'event', 'event': 'ci_round', 'at': '2026-11-24T10:00:00Z', 'head': 'abc',
         'checks': [{'name': 'tests', 'outcome': 'pass', 'run_id': None}]}
REQUEST = {'type': 'request', 'issue': 7, 'hash': 'h', 'labeled_by': 'brushtyler',
           'labeled_at': None, 'comments': [], 'taken_at': '2026-11-24T10:00:00Z'}
ROOT = {'story': 's1', 'repo': 'o/r', 'root': 7, 'issue': 7, 'hashes': ['h']}
RECORDS = [TURN, VERIFICATION, EVENT, REQUEST,
           {'type': 'round', 'round': 2, 'records': [dict(TURN, section=1),
                                                     dict(EVENT, section=2)]},
           {'type': 'fix', 'round': 0, 'records': [dict(TURN, section=1)]},
           {'type': 'fix', 'round': 3, 'records': [dict(VERIFICATION, section=1)]}]


def title(body):
    return watch.first_line(body)


class LanguageCodeTests(unittest.TestCase):
    def test_italian_and_english_by_name_or_tag_without_case(self):
        for value in ('Italian', 'italiano', 'IT', 'it-IT', 'it_IT', ' Italian (Italy) '):
            self.assertEqual(language_code(value), ITALIAN, value)
        for value in ('English', 'english', 'inglese', 'en', 'en-US'):
            self.assertEqual(language_code(value), ENGLISH, value)

    def test_a_language_without_translation_is_english(self):
        for value in ('French', 'Deutsch', 'es', '', None, 'italianissimo'):
            self.assertEqual(language_code(value), ENGLISH, value)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(en={'hello': 'Hello {who}', 'only': 'English only'},
                               it={'hello': 'Ciao {who}'})

    def test_texts_in_the_language_with_english_for_a_missing_key(self):
        self.assertEqual(self.catalog.texts('Italian')('hello', who='Ada'), 'Ciao Ada')
        self.assertEqual(self.catalog.texts('Italian')('only'), 'English only')
        self.assertEqual(self.catalog.texts('English')('hello', who='Ada'), 'Hello Ada')
        self.assertEqual(self.catalog.texts('French')('hello', who='Ada'), 'Hello Ada')

    def test_variants_list_every_translation_english_first(self):
        self.assertEqual(self.catalog.variants('hello'), ('Hello {who}', 'Ciao {who}'))
        self.assertEqual(self.catalog.variants('only'), ('English only',))

    def test_english_is_required_and_holds_every_key(self):
        with self.assertRaises(ValueError):
            Catalog(it={'a': 'b'})
        with self.assertRaises(ValueError):
            Catalog(en={'a': 'b'}, it={'c': 'd'})
        with self.assertRaises(ValueError):
            Catalog(en={'a': 'b'}, fr={'a': 'c'})


class LedgerLanguageTests(unittest.TestCase):
    def test_the_ledger_has_every_text_in_italian(self):
        self.assertEqual(set(ledger.TEXTS.tables[ITALIAN]), set(ledger.TEXTS.tables[ENGLISH]))

    def test_records_in_italian(self):
        body = ledger.render(TURN, language='Italian')
        self.assertEqual(title(body), '## Registro devloop: turno `review` di `codex`')
        self.assertIn('Turno in corso', body)
        self.assertIn('- **Identità GitHub:** login `gh` dell\'operatore', body)
        self.assertIn('- **Modello reale (attestato dalla CLI):** non riportato', body)
        body = ledger.render(RECORDS[4], language='italiano')
        self.assertEqual(title(body), '## Registro devloop: round 2')
        self.assertIn('### 1. Turno `review` di `codex`', body)
        self.assertIn('### 2. Evento `ci_round`', body)
        self.assertIn('| Check | Esito | Run di Actions |', body)
        self.assertEqual(title(ledger.render(RECORDS[5], language='it')),
                         '## Registro devloop: correzione prima di ogni review')
        body = ledger.render(RECORDS[6], language='it')
        self.assertEqual(title(body), '## Registro devloop: correzione del round 3')
        self.assertIn('### 1. Verifica locale', body)
        self.assertIn('**Test instabili**', body)
        body = ledger.render(REQUEST, story=ROOT, language='Italian')
        self.assertIn('### Storia', body)
        self.assertIn('Questa issue è la radice della storia `s1`', body)

    def test_records_in_english(self):
        body = ledger.render(TURN, language='English')
        self.assertEqual(title(body), '## devloop ledger: turn `review` of `codex`')
        self.assertIn('Turn in progress', body)
        self.assertIn('- **GitHub identity:** the operator\'s `gh` login', body)
        self.assertIn('- **Real model (attested by the CLI):** not reported', body)
        body = ledger.render(RECORDS[4], language='English')
        self.assertEqual(title(body), '## devloop ledger: round 2')
        self.assertIn('### 1. Turn `review` of `codex`', body)
        self.assertIn('### 2. Event `ci_round`', body)
        self.assertIn('| Check | Outcome | Actions run |', body)
        self.assertEqual(title(ledger.render(RECORDS[5])),
                         '## devloop ledger: fix before any review')
        body = ledger.render(RECORDS[6])
        self.assertEqual(title(body), '## devloop ledger: fix of round 3')
        self.assertIn('### 1. Local verification', body)
        self.assertIn('**Unstable tests**', body)
        body = ledger.render(REQUEST, story=ROOT)
        self.assertIn('### Story', body)
        self.assertIn('This issue is the root of story `s1`', body)
        for word in ('Registro', 'Turno', 'non riportato', 'Esito'):
            self.assertNotIn(word, body)

    def test_a_language_without_translation_writes_english(self):
        for record in RECORDS:
            self.assertEqual(ledger.render(record, language='French'),
                             ledger.render(record, language='English'))

    def test_the_markers_do_not_depend_on_the_language(self):
        for record in RECORDS:
            for language in ('Italian', 'English', 'French'):
                body = ledger.render(record, language=language)
                self.assertEqual(markdown.read_marker(body, 'ledger'), record)
                self.assertEqual(body.split('\n<!-- ')[-1],
                                 ledger.render(record).split('\n<!-- ')[-1])

    def test_every_title_starts_with_a_title_watch_leaves_out(self):
        self.assertEqual(ledger.TITLES, ('## devloop ledger', '## Registro devloop'))
        for language in ('Italian', 'English'):
            for record in RECORDS:
                self.assertTrue(title(ledger.render(record, language=language))
                                .startswith(ledger.TITLES))

    def test_the_ledger_writes_in_its_language_and_the_dispatcher_sets_the_projects(self):
        gh = LedgerGH()
        lg = Ledger(gh, language='Italian')
        lg.event(7, 'sync', base='b')
        self.assertIn('## Registro devloop: evento `sync`', gh.thread[7][0]['body'])
        lg.language = 'English'
        lg.event(7, 'sync', base='b')
        self.assertIn('## devloop ledger: event `sync`', gh.thread[7][1]['body'])
        # A record written in another language is still read, and edited in the current one.
        self.assertEqual([r['event'] for _, r in ledger.entries(gh.thread[7], lambda _: True)],
                         ['sync', 'sync'])
        for language in ('English', 'Italian', 'French'):
            cfg = dataclasses.replace(CFG, language=language)
            d = Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                           ledger=Ledger(gh))
            self.assertEqual(d.ledger.language, language)
            self.assertEqual(i18n.language_code(d.ledger.language),
                             ITALIAN if language == 'Italian' else ENGLISH)


if __name__ == '__main__':
    unittest.main()
