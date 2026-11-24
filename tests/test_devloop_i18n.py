"""devloop: the language of the dispatcher's texts (#631, #632, #633). The project's
``language`` chooses Italian or English, English for a language without translation; the ledger
writes its records in it, and so do the review, the fix, the readiness check, the advisory
review, the conclave, the escalations and the answers to the commands on them, with the same
markers, and the records already published stay recognised after a change of ``language``."""

import dataclasses
import unittest

from devloop import (acceptance, conclave, dispatch_advisory, i18n, ledger, markdown, review,
                     supervision, watch)
from devloop.agents import AgentError
from devloop.context import _template, prompt_values
from devloop.dispatcher import Dispatcher
from devloop.i18n import ENGLISH, ITALIAN, Catalog, language_code
from devloop.ledger import Ledger
from helpers.devloop_fakes import SHA1, FakeGH
from test_devloop import CFG, FakeRunner, FakeWS, pr
from test_devloop_ledger import LedgerGH
from test_devloop_preflight import PCFG, IssueGH
from test_devloop_preflight import gap as recheck_gap
from test_devloop_preflight_recheck import CFG as RECHECK_CFG
from test_devloop_preflight_recheck import Case as RecheckCase
from test_devloop_authorize import CFG as AUTHORIZE_CFG
from test_devloop_authorize import TextGH
from test_devloop_conclave import SHA2, proposal, vote
from test_devloop_conclave_substitute import (CCFG, COORDINATED, NOW, TIMEOUT, UNAVAILABLE,
                                              SubstituteBase)
from test_devloop_coordinator import COORDINATOR
from test_devloop_preflight import checked
from test_devloop_supervision import AUTO as SUPERVISION_AUTO
from test_devloop_supervision import Case as SupervisionCase
from test_devloop_supervision import SUPERVISOR_LOGIN, Identities
from test_devloop_trust_decisions import (BOT, HEADER, LATER, APPROVAL, PreflightGH, comment,
                                          history)

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


# Part 2 (#632): review, fix, readiness check, advisory review and conclave. -----------------
FINDING = {'file': 'x.py', 'line': 3, 'title': 'bug', 'detail': 'd', 'evidence': 'e',
           'suggested_fix': 'f', 'security': True}


def review_record(**extra):
    record = review.normalize_review(
        {'verdict': 'request_changes', 'summary': 'ok', 'blocking_findings': [FINDING],
         'non_blocking_findings': [], 'previous_findings': [], 'human_reason': '',
         'operator_reason': 'none', 'checks_performed': ['read diff']},
        previous_open=[], round_no=1, max_rounds=3, head_sha=SHA1, ci='pass', reviewer='codex')
    return dict(record, **extra)


FIX = {'summary': 's', 'head_sha': SHA1, 'conclave': 12,
       'responses': [{'finding_id': 'R1-1', 'action': 'fixed', 'note': 'n'}],
       'base_merge': {'branch': 'main', 'base': SHA1, 'head': SHA1}}
GAP = {'kind': 'default_available', 'description': 'Which format?', 'code_refs': ['x.py:3'],
       'proposed_default': 'JSON', 'repository_dependent': True}
PREFLIGHT = {'ready': False, 'gaps': [GAP], 'split': 'Two parts.', 'hash': 'h', 'agent': 'codex',
             'blocked': True, 'labeled': 0, 'trust_policy': 'covered',
             'trust_policy_reason': 'Decided.'}
REFUSED = [('brushtyler', None, 'written after the takeover')]
RECHECK = {'request': 'recheck', 'by': 'brushtyler', 'reason': 'the block is another one'}
ADVISORY = {'summary': '', 'residual_risks': ['r'], 'operator_checks': [], 'overlooked': [],
            'unfounded': [], 'opinion': 'dubbi', 'opinion_reason': ''}


def conclave_record(outcome=conclave.UNANIMOUS):
    record = conclave.new_record(
        {'kind': 'blocked', 'agent': 'codex', 'step': 'implement', 'comment': 5}, 'implement',
        ['codex', 'claude', 'agy'], 7, 'h' * 64, SHA1, None, 'd')
    record['proposal'].update(state=conclave.DONE, resolution='Use JSON.')
    record['hash'] = conclave.text_hash('Use JSON.')
    for vote in record['votes']:
        vote.update(state=conclave.DONE, vote=conclave.APPROVE, reason='fine',
                    proposal_hash=record['hash'])
    record['outcome'] = outcome
    return record


def bodies(language):
    """Every text of part 2, rendered in ``language`` (None: the default), with its kind."""
    extra = {'language': language} if language else {}
    native = markdown.render_native_review(review_record(), 'h', **extra)['body']
    return [
        ('review', markdown.render_review(review_record(), 'h', **extra)),
        ('review', native),
        ('fix', markdown.render_fix(FIX, 'h', 1, **extra)),
        ('preflight', markdown.render_preflight(PREFLIGHT, 'h', 'n', REFUSED, **extra)),
        ('approve', markdown.render_approve_refused([3], REFUSED, **extra)),
        ('recheck', markdown.render_recheck(RECHECK, **extra)),
        ('advisory', dispatch_advisory.render_advisory_review(ADVISORY, 'codex', SHA1, 'h',
                                                              **extra)['body']),
        ('advisory', dispatch_advisory.render_advisory_summary(
            dispatch_advisory.advisory_record(SHA1, 'codex', 'dubbi'), **extra)),
        ('conclave', conclave.render(conclave_record(), **extra)),
    ]


def markers(body):
    return markdown.split_markers(body)[1]


class ReviewLanguageTests(unittest.TestCase):
    def test_every_catalogue_has_every_text_in_italian(self):
        for catalog in (markdown.TEXTS, dispatch_advisory.TEXTS, conclave.TEXTS):
            self.assertEqual(set(catalog.tables[ITALIAN]), set(catalog.tables[ENGLISH]))

    def test_the_texts_in_italian(self):
        texts = dict(bodies('Italian'))
        self.assertIn('## Review automatica, round 1/3', texts['review'])
        self.assertIn('**Verdetto:** Modifiche richieste', texts['review'])
        self.assertIn('### Bloccanti', texts['review'])
        self.assertIn('*(sicurezza)*', texts['review'])
        self.assertIn('## Correzioni per il round 1', texts['fix'])
        self.assertIn('| R1-1 | corretto | n |', texts['fix'])
        self.assertIn('registrata nel commento 12', texts['fix'])
        self.assertIn('## Controllo di prontezza: issue non pronta', texts['preflight'])
        self.assertIn('- **Default disponibile, da accettare:** Which format?',
                      texts['preflight'])
        self.assertIn('*Default proposto:* JSON', texts['preflight'])
        self.assertIn('### `/devloop approve` rifiutati', texts['preflight'])
        self.assertIn('del momento non noto', texts['preflight'])
        self.assertIn('## `/devloop approve` rifiutati', texts['approve'])
        self.assertIn('## Controllo di prontezza non ripetuto', texts['recheck'])
        self.assertIn('Il comando `/devloop recheck` di `brushtyler`', texts['recheck'])
        self.assertIn('## Review consultiva per l\'operatore', texts['advisory'])
        self.assertIn('## Conclave sul blocco', texts['conclave'])
        self.assertIn('`codex` si è fermato (`blocked`) nel passo `implement`',
                      texts['conclave'])
        self.assertIn('**UNANIMOUS**: la proposta e due `APPROVA`', texts['conclave'])
        advisory = dispatch_advisory.render_advisory_review(ADVISORY, 'codex', SHA1, 'h',
                                                            language='italiano')['body']
        self.assertIn('## Review consultiva: non conta per il quorum', advisory)
        self.assertIn('### Rischi residui', advisory)
        self.assertIn('_Non indicato._', advisory)

    def test_the_texts_in_english(self):
        texts = dict(bodies('English'))
        self.assertIn('## Automatic review, round 1/3', texts['review'])
        self.assertIn('**Verdict:** Changes requested', texts['review'])
        self.assertIn('### Blocking', texts['review'])
        self.assertIn('*(security)*', texts['review'])
        self.assertIn('## Fixes for round 1', texts['fix'])
        self.assertIn('| R1-1 | fixed | n |', texts['fix'])
        self.assertIn('recorded in comment 12', texts['fix'])
        self.assertIn('## Readiness check: issue not ready', texts['preflight'])
        self.assertIn('- **Default available, to be accepted:** Which format?',
                      texts['preflight'])
        self.assertIn('*Proposed default:* JSON', texts['preflight'])
        self.assertIn('### `/devloop approve` refused', texts['preflight'])
        self.assertIn('of an unknown time', texts['preflight'])
        self.assertIn('## `/devloop approve` refused', texts['approve'])
        self.assertIn('## Readiness check not run again', texts['recheck'])
        self.assertIn('The command `/devloop recheck` of `brushtyler`', texts['recheck'])
        self.assertIn('## Advisory review for the operator', texts['advisory'])
        self.assertIn('## Conclave on the block', texts['conclave'])
        self.assertIn('`codex` stopped (`blocked`) in step `implement`', texts['conclave'])
        self.assertIn('**UNANIMOUS**: the proposal and two `APPROVA`', texts['conclave'])
        advisory = dispatch_advisory.render_advisory_review(ADVISORY, 'codex', SHA1,
                                                            'h')['body']
        self.assertIn(f'## {dispatch_advisory.ADVISORY_TITLE}', advisory)
        self.assertIn('### Residual risks', advisory)
        self.assertIn('_Not indicated._', advisory)
        for _, body in bodies('English'):
            for word in ('Verdetto', 'Correzioni', 'Lacune', 'Controllo di prontezza',
                         'consultiva', 'Conclave sul', 'Esito', 'rifiutati'):
                self.assertNotIn(word, body)

    def test_english_is_the_default_and_a_language_without_translation_writes_english(self):
        english = bodies('English')
        self.assertEqual(bodies(None), english)
        self.assertEqual(bodies('French'), english)
        self.assertEqual(bodies('de-DE'), english)

    def test_the_markers_do_not_depend_on_the_language(self):
        english = bodies('English')
        for language in ('Italian', 'French'):
            for (kind, body), (_, other) in zip(bodies(language), english):
                with self.subTest(language=language, kind=kind):
                    self.assertEqual(markers(body), markers(other))
                    self.assertIsNotNone(markdown.read_marker(body, kind))
        # Commands, labels and the values of the records stay as they are.
        for language in ('Italian', 'English'):
            texts = dict(bodies(language))
            self.assertIn('`/devloop recheck`', texts['preflight'])
            self.assertIn('`agent:needs-operator`', texts['preflight'])
            self.assertIn('`/devloop veto`', texts['conclave'])
            self.assertIn('**dubbi**', texts['advisory'])

    def test_a_truncated_review_says_so_in_its_language(self):
        # The readable text and the marker each fit, not both together.
        long = review_record(summary='x' * 30000)
        for language, notice in (('Italian', '**Testo troncato:**'),
                                 ('English', '**Text truncated:**'),
                                 ('French', '**Text truncated:**')):
            body = markdown.render_review(long, 'h', language=language)
            self.assertLessEqual(len(body), markdown.MAX_BODY)
            self.assertIn(notice, body)
            self.assertEqual(markdown.read_marker(body, 'review'), long)

    def test_the_advisory_prompt_names_the_titles_in_the_projects_language(self):
        for language in ('Italian', 'English'):
            titles = dispatch_advisory.advisory_titles(language)
            text = ' '.join(_template('advisory', **prompt_values(CFG), **titles,
                                      language=language).split())
            t = dispatch_advisory.TEXTS.texts(language)
            self.assertIn(f'under the title "{t("title")}"', text)
            self.assertIn(f'shown in the section "{t("unfounded")}"', text)
            self.assertNotIn('$advisory_title', text)
            self.assertNotIn('$unfounded_title', text)


class DispatcherLanguageTests(unittest.TestCase):
    """The dispatcher publishes in the project's ``language`` (#632)."""

    def test_a_review_in_the_projects_language(self):
        raw = {'verdict': 'request_changes', 'summary': 'ok', 'blocking_findings': [FINDING],
               'non_blocking_findings': [], 'previous_findings': [], 'human_reason': '',
               'operator_reason': 'none', 'checks_performed': []}
        found = {}
        for language in ('Italian', 'English', 'French'):
            gh = FakeGH()
            d = Dispatcher(dataclasses.replace(CFG, language=language), gh, FakeWS(),
                           FakeRunner(raw), log=lambda m: None)
            gh.labels[100] = {'agent:review'}
            d._do_review(pr())
            (body,) = [b for _, b in gh.comments if markdown.read_marker(b, 'review')]
            found[language] = body
        self.assertIn('**Verdetto:** Modifiche richieste', found['Italian'])
        self.assertIn('**Verdict:** Changes requested', found['English'])
        self.assertIn('**Verdict:** Changes requested', found['French'])
        self.assertEqual(markdown.read_marker(found['Italian'], 'review'),
                         markdown.read_marker(found['English'], 'review'))

    def test_the_notes_of_a_new_review_in_the_projects_language(self):
        # A blocking finding without evidence (#481): devloop demotes it and corrects the
        # verdict, and its notes are composed in the project's language when the record is made.
        raw = {'verdict': 'request_changes', 'summary': 'ok',
               'blocking_findings': [dict(FINDING, security=False, evidence='')],
               'non_blocking_findings': [], 'previous_findings': [], 'human_reason': '',
               'operator_reason': 'none', 'checks_performed': []}
        expected = {
            'Italian': ('devloop ha registrato come non bloccanti 1 finding',
                        'Verdetto corretto da request_changes a unproven'),
            'English': ('devloop recorded as non-blocking 1 findings',
                        'Verdict corrected from request_changes to unproven'),
            'French': ('devloop recorded as non-blocking 1 findings',
                       'Verdict corrected from request_changes to unproven'),
        }
        for language, says in expected.items():
            with self.subTest(language=language):
                gh = FakeGH()
                d = Dispatcher(dataclasses.replace(CFG, language=language), gh, FakeWS(),
                               FakeRunner(raw), log=lambda m: None)
                gh.labels[100] = {'agent:review'}
                d._do_review(pr())
                (body,) = [b for _, b in gh.comments if markdown.read_marker(b, 'review')]
                record = markdown.read_marker(body, 'review')
                self.assertEqual(record['verdict'], review.UNPROVEN)
                for text in says:
                    self.assertIn(text, body)
                    self.assertIn(text, record['note'])
                if language != 'Italian':
                    for word in ('registrato', 'Verdetto', 'bloccant', 'evidenza'):
                        self.assertNotIn(word, body)

    def test_the_texts_devloop_writes_in_a_review_record(self):
        def made(language, **raw):
            fields = dict({'verdict': 'request_changes', 'summary': 'ok',
                           'blocking_findings': [], 'non_blocking_findings': [],
                           'previous_findings': [], 'human_reason': '',
                           'operator_reason': 'none', 'checks_performed': []}, **raw)
            return review.normalize_review(
                fields, previous_open=[{'id': 'R1-1', 'key': '1/claude/R1-1', 'owner': 'claude'}],
                round_no=2, max_rounds=3, head_sha=SHA1, ci='pass', reviewer='codex',
                criteria=['a'], acceptance_map=None, language=language)

        for language, says in (
                ('Italian', ('Non valutato dal reviewer.', 'mappa di copertura',
                             'Mappa di copertura dei criteri di accettazione assente',
                             'non ha valutato tutti')),
                ('English', ('Not assessed by the reviewer.', 'coverage map',
                             'Coverage map of the acceptance criteria missing',
                             'did not assess every')),
                ('French', ('Not assessed by the reviewer.', 'coverage map',
                            'Coverage map of the acceptance criteria missing',
                            'did not assess every'))):
            with self.subTest(language=language):
                record = made(language)
                self.assertEqual(record['previous_findings'][0]['note'], says[0])
                self.assertIn(says[1], record['note'])
                self.assertEqual(record['blocking_findings'][0]['title'], says[2])
                self.assertIn(says[3], record['note'])
                # The default of a decision the reviewer did not name.
                decision = made(language, verdict='needs_operator', operator_reason='decision')
                self.assertEqual(decision['human_reason'],
                                 review.TEXTS.texts(language)('no_decision'))
        ci = {language: review.ci_failure_review(
            failed_checks=['tests'], log_tail='', round_no=2, max_rounds=3, head_sha=SHA1,
            previous_open=[{'id': 'R1-1', 'key': '1/claude/R1-1'}], language=language)
            for language in ('Italian', 'English', 'French')}
        self.assertEqual(ci['Italian']['blocking_findings'][0]['title'], 'CI fallita: tests')
        self.assertIn('La CI non è verde (tests)', ci['Italian']['summary'])
        self.assertEqual(ci['Italian']['previous_findings'][0]['note'],
                         'Non valutato: CI fallita.')
        for language in ('English', 'French'):
            self.assertEqual(ci[language]['blocking_findings'][0]['title'], 'CI failed: tests')
            self.assertIn('The CI is not green (tests)', ci[language]['summary'])
            self.assertEqual(ci[language]['previous_findings'][0]['note'],
                             'Not assessed: CI failed.')
        # Only the texts change: the record is otherwise the same.
        strip = ('summary', 'blocking_findings', 'previous_findings')
        self.assertEqual({k: v for k, v in ci['Italian'].items() if k not in strip},
                         {k: v for k, v in ci['English'].items() if k not in strip})

    def test_review_texts_catalogues_are_complete(self):
        for catalog in (review.TEXTS, acceptance.TEXTS):
            self.assertEqual(set(catalog.tables[ITALIAN]), set(catalog.tables[ENGLISH]))
        # Every class has its English description (#632), the default since the escalations
        # follow the project's language (#633), and its Italian one.
        for cls in (*supervision.CRITICAL, *supervision.NON_CRITICAL):
            with self.subTest(cls=cls):
                self.assertNotEqual(supervision.describe(cls, language='English'),
                                    'class not recognised, hence critical')
                self.assertEqual(supervision.describe(cls, language='English'),
                                 supervision.describe(cls))
                self.assertNotEqual(supervision.describe(cls, language='Italian'),
                                    supervision.describe(cls))
        self.assertEqual(supervision.describe('tests_failed', language='French'),
                         'failing tests, or no change')
        self.assertEqual(supervision.describe(None, language='English'),
                         'class not recognised, hence critical')

    def test_a_readiness_check_in_the_projects_language(self):
        output = {'ready': False, 'gaps': [dict(GAP, repository_dependent=False)], 'split': '',
                  'trust_policy': 'none', 'trust_policy_reason': ''}
        for language, says in (('Italian', ('issue non pronta', 'riusato')),
                               ('English', ('issue not ready', 'reused')),
                               ('French', ('issue not ready', 'reused'))):
            with self.subTest(language=language):
                gh = IssueGH()
                d = Dispatcher(dataclasses.replace(PCFG, language=language), gh, FakeWS(),
                               FakeRunner(output), log=lambda m: None)
                d.tick()
                self.assertIn(says[0], gh.thread[-1]['body'])
                # The outcome reused when the block is removed: its note in the same language.
                gh.labels[7].discard('agent:needs-operator')
                d.tick()
                self.assertIn(says[1], gh.thread[-1]['body'])
                self.assertEqual(markdown.read_marker(gh.thread[-1]['body'],
                                                      'preflight')['ready'], False)


class RecheckLanguageTests(RecheckCase):
    """The answer to a request that runs no check, with its reason, composed in the project's
    ``language`` (#632) on the real paths of the refusal."""

    LANGUAGES = {
        'Italian': ('## Controllo di prontezza non ripetuto',
                    'dopo il controllo di prontezza c\'è un\'escalation di classe '
                    '`tests_failed` (test che falliscono, o nessuna modifica)',
                    'attende il supervisore, per un\'escalation di classe `tool_failure` '
                    '(l\'agent o il modello non ha prodotto un risultato)'),
        'English': ('## Readiness check not run again',
                    'after the readiness check there is an escalation of class `tests_failed` '
                    '(failing tests, or no change)',
                    'waits for the supervisor, for an escalation of class `tool_failure` (the '
                    'agent or the model produced no result)'),
    }
    LANGUAGES['French'] = LANGUAGES['English']

    def answer(self, cfg, d):
        self.gh.say(7, 'brushtyler', '/devloop recheck')
        self.assertEqual(self.planned(d, cfg), [('recheck_refused', 7)])
        self.assertEqual([a.kind for a in d.tick()], ['recheck_refused'])
        body = self.gh.threads[7][-1]['body']
        self.assertEqual(markdown.read_marker(body, 'recheck')['request'], 'recheck')
        return body

    def test_an_escalation_of_another_class(self):
        for language, (title, reason, _) in self.LANGUAGES.items():
            with self.subTest(language=language):
                cfg = dataclasses.replace(RECHECK_CFG, language=language)
                d = self.make(cfg=cfg)
                self.blocked(d, recheck_gap())
                self.gh.comment(7, markdown.render_escalation('I test falliscono.',
                                                              cls='tests_failed'))
                body = self.answer(cfg, d)
                self.assertIn(title, body)
                self.assertIn(reason, body)
                self.assertTrue(markdown.read_marker(body, 'recheck')['reason']
                                .startswith(reason))
                if language != 'Italian':
                    for word in ('dopo', 'blocco', 'controllo di prontezza'):
                        self.assertNotIn(word, body)

    def test_an_issue_waiting_for_the_supervisor(self):
        for language, (title, _, reason) in self.LANGUAGES.items():
            with self.subTest(language=language):
                cfg = dataclasses.replace(RECHECK_CFG, language=language)
                d = self.make(cfg=cfg)
                self.blocked(d, recheck_gap())
                self.gh.labels[7] = {'agent:ready', 'agent:needs-supervisor'}
                self.gh.comment(7, markdown.render_escalation('Il passo è fallito.',
                                                              cls='tool_failure'))
                body = self.answer(cfg, d)
                self.assertIn(title, body)
                self.assertIn(reason, body)
                if language != 'Italian':
                    self.assertNotIn('attende', body)


class TrustLanguageTests(unittest.TestCase):
    """The texts devloop composes about the trust policy in a readiness check (#632): the
    question of an uncovered change, the reasons of a refused ``/devloop approve`` and the gaps
    of the coordinator's gate, in the project's ``language``, on the real paths."""

    LANGUAGES = {
        'Italian': ('Approvi la modifica della policy di fiducia descritta qui?',
                    'l\'autore è il supervisore o un\'app',
                    'il commento porta l\'intestazione degli agent'),
        'English': ('Do you approve the change to the trust policy described here?',
                    'the author is the supervisor or an app',
                    'the comment carries the agents\' header'),
    }
    LANGUAGES['French'] = LANGUAGES['English']
    ITALIAN_WORDS = ('Approvi', 'l\'autore', 'il commento', 'intestazione')

    def make(self, language, thread=()):
        self.gh = PreflightGH(list(thread), history(taken=None))
        self.runner = FakeRunner(checked(True, trust='uncovered', reason='Point 4.'))
        return Dispatcher(dataclasses.replace(PCFG, language=language), self.gh, FakeWS(),
                          self.runner, log=lambda m: None)

    def test_a_readiness_check_writes_the_question_and_the_refusals(self):
        for language, (question, app, header) in self.LANGUAGES.items():
            with self.subTest(language=language):
                d = self.make(language, [comment(login=BOT),
                                         comment(at='2026-11-19T11:10:00Z',
                                                 body=HEADER + APPROVAL)])
                self.assertEqual([a.kind for a in d.tick()], ['preflight'])
                body = self.gh.thread[-1]['body']
                record = markdown.read_marker(body, 'preflight')
                self.assertEqual(record['gaps'][0]['description'], f'{question} Point 4.')
                self.assertIn(question, body)
                self.assertIn(app, body)
                self.assertIn(header, body)
                self.assertEqual(record['refused'], [1000, 1001])
                if language != 'Italian':
                    for word in self.ITALIAN_WORDS:
                        self.assertNotIn(word, body)

    def test_the_answer_to_a_refused_approve(self):
        for language, (_, app, _) in self.LANGUAGES.items():
            with self.subTest(language=language):
                d = self.make(language)
                self.assertEqual([a.kind for a in d.tick()], ['preflight'])
                self.gh.thread.append(comment(at=LATER, login=BOT))
                self.assertEqual([a.kind for a in d.tick()], ['approve_refused'])
                body = self.gh.thread[-1]['body']
                self.assertIsNotNone(markdown.read_marker(body, 'approve'))
                self.assertIn(f'`{BOT}`', body)
                self.assertIn(app, body)
                if language != 'Italian':
                    for word in self.ITALIAN_WORDS:
                        self.assertNotIn(word, body)

    def test_the_gaps_of_the_coordinators_gate(self):
        expected = {
            'Italian': ('L\'issue è stata aperta dal coordinatore',
                        'L\'issue non è stata aperta da un utente fidato'),
            'English': ('The issue was opened by the coordinator',
                        'The issue was not opened by a trusted user'),
        }
        expected['French'] = expected['English']
        outcome = {'ready': True, 'gaps': [], 'split': '', 'trust_policy': 'covered',
                   'trust_policy_reason': 'Decided.'}
        for language, (opened, authorized) in expected.items():
            with self.subTest(language=language):
                gh = TextGH()
                d = Dispatcher(dataclasses.replace(AUTHORIZE_CFG, language=language), gh,
                               FakeWS(), FakeRunner(), log=lambda m: None,
                               identities=Identities(gh))
                for author, says in ((COORDINATOR, opened), (SUPERVISOR_LOGIN, authorized)):
                    gated = d._coordinator_gate({'author': author, 'labeled': []}, outcome)
                    self.assertFalse(gated['ready'])
                    self.assertEqual(gated['gaps'][0]['kind'], 'operator_decision')
                    self.assertTrue(gated['gaps'][0]['description'].startswith(says))


class ConclaveReasonLanguageTests(SubstituteBase):
    """The reasons devloop writes in a new conclave record (#632), a contribution missing, a
    context changed, a substitute vote refused or not given, in the project's ``language``,
    on the real paths of the dispatcher: the comment carries no text of another language."""

    LANGUAGES = ('Italian', 'English', 'French')
    ITALIAN_WORDS = ('fallito', 'nessuna proposta', 'non richiesto', 'contesto cambiato',
                     'non è quello', 'nomina il conclave', 'non ben formato', 'scadenza',
                     'senza un risultato', 'è passato')

    def run_in(self, language, cfg=None):
        self.cfg = dataclasses.replace(cfg or COORDINATED, language=language)

    def texts(self, language):
        return conclave.TEXTS.texts(language)

    def body(self):
        (body,) = [c['body'] for c in self.gh.thread[100]
                   if markdown.read_marker(c['body'], 'conclave')]
        return body

    def check_language(self, language, *phrases):
        body = self.body()
        for phrase in phrases:
            self.assertIn(phrase, body)
        if language != 'Italian':
            for word in self.ITALIAN_WORDS:
                self.assertNotIn(word, body)

    def test_a_failed_proposer(self):
        for language, (failed, none) in zip(self.LANGUAGES, (
                ('agent fallito o in timeout: claude crashed', 'nessuna proposta da votare'),
                ('agent failed or timed out: claude crashed', 'no proposal to vote on'),
                ('agent failed or timed out: claude crashed', 'no proposal to vote on'))):
            with self.subTest(language=language):
                self.run_in(language)
                self.make(AgentError('claude crashed'))
                self.block_pr()
                self.run_conclave(times=1)
                record = self.record()
                self.assertEqual(record['outcome'], conclave.BLOCKED)
                self.assertEqual(record['proposal']['missing'], failed)
                self.assertEqual([v['missing'] for v in record['votes']], [none] * 2)
                self.check_language(language, failed, none)

    def test_a_proposer_that_flags_the_reserved_perimeter(self):
        for language in self.LANGUAGES:
            with self.subTest(language=language):
                self.run_in(language)
                self.make(proposal(reserved=True, question='Grant `contents: write`?'))
                self.block_pr()
                self.run_conclave(times=1)
                record = self.record()
                self.assertEqual(record['outcome'], conclave.RESERVED)
                why = self.texts(language)('why_not_asked')
                self.assertEqual([v['missing'] for v in record['votes']], [why] * 2)
                self.check_language(language, why)
        self.assertTrue(self.texts('English')('why_not_asked').startswith('not asked'))

    def test_a_changed_context(self):
        for language, moved in zip(self.LANGUAGES, (
                f'l\'head della PR è passato a `{SHA2[:12]}`',
                f'the head of the PR moved to `{SHA2[:12]}`',
                f'the head of the PR moved to `{SHA2[:12]}`')):
            with self.subTest(language=language):
                self.run_in(language, CCFG)
                self.make(proposal(), vote(), vote())
                self.block_pr()
                self.run_conclave(times=1)
                self.gh.head = SHA2
                self.run_conclave(times=1)
                record = self.record()
                self.assertEqual(record['outcome'], conclave.BLOCKED)
                self.assertEqual(record['stale'], moved)
                changed = self.texts(language)('why_stale', reason=moved)
                self.assertEqual([v['missing'] for v in record['votes']], [changed] * 2)
                self.check_language(language, moved)

    def test_a_substitute_vote_refused_then_not_given(self):
        for language, (other, malformed, expired) in zip(self.LANGUAGES, (
                ('il comando nomina il conclave', 'comando non ben formato',
                 'nessun voto sostitutivo del coordinatore entro la scadenza'),
                ('the command names conclave', 'malformed command',
                 'no substitute vote of the coordinator by the deadline'),
                ('the command names conclave', 'malformed command',
                 'no substitute vote of the coordinator by the deadline'))):
            with self.subTest(language=language):
                self.run_in(language)
                record = self.start(proposal(), UNAVAILABLE, vote())
                failed = self.texts(language)('why_failed', error='codex: usage limit reached')
                self.assertEqual(record['substitute']['why'], failed)
                self.vote_cmd(conclave_id=self.conclave_id() + 1)
                self.vote_cmd(verdict='FORSE')
                self.run_conclave(times=1)
                whys = [r['why'] for r in self.record()['substitute']['refused']]
                self.assertTrue(whys[0].startswith(other), whys[0])
                self.assertTrue(whys[1].startswith(malformed), whys[1])
                self.now = NOW + TIMEOUT
                self.run_conclave(times=1)
                record = self.record()
                self.assertEqual(record['substitute']['state'], 'expired')
                self.assertEqual(record['votes'][0]['missing'], f'{failed}; {expired}')
                self.check_language(language, other, malformed, expired)

    def test_a_call_started_without_a_result(self):
        for language in self.LANGUAGES:
            with self.subTest(language=language):
                self.run_in(language, CCFG)
                self.make(proposal(), RuntimeError('dispatcher crash'), vote())
                self.block_pr()
                self.run_conclave(times=1)
                with self.assertRaises(RuntimeError):
                    self.run_conclave(times=1)
                self.run_conclave(times=2)
                record = self.record()
                self.assertEqual(record['outcome'], conclave.BLOCKED)
                why = self.texts(language)('why_started')
                self.assertEqual(record['votes'][0]['missing'], why)
                self.check_language(language, why)
        self.assertTrue(self.texts('French')('why_started').startswith('call started'))


class EscalationLanguageTests(unittest.TestCase):
    """#633: the frame of an escalation, its title, its class and how to resume, in the
    project's language; the marker is the same."""

    FRAMES = {
        'Italian': ('## Serve un intervento dell\'operatore', '**Classe:** `tool_failure` '
                    '(l\'agent o il modello non ha prodotto un risultato), non critica.',
                    'Per riprendere: correggere la causa', 'In alternativa, un utente fidato '
                    'può ripetere il passo `review`', '## Escalation al supervisore',
                    'Escalation non critica: decide il supervisore'),
        'English': ('## The operator is needed', '**Class:** `tool_failure` (the agent or the '
                    'model produced no result), non-critical.', 'To resume: fix the cause',
                    'Otherwise, a trusted user may run the step `review` again',
                    '## Escalation to the supervisor',
                    'Non-critical escalation: the supervisor decides'),
    }

    def test_the_frame_of_an_escalation(self):
        retry = {'step': 'review', 'key': {'step': 'review', 'head': SHA1}, 'resume': 'r'}
        markers = []
        for language in ('Italian', 'English', 'French'):
            with self.subTest(language=language):
                frame = self.FRAMES.get(language, self.FRAMES['English'])
                operator = markdown.render_escalation('Motivo.', cls='tool_failure',
                                                      retry=retry, language=language)
                supervised = markdown.render_escalation('Motivo.', cls='tool_failure',
                                                        supervised=retry, language=language)
                for phrase in frame[:4]:
                    self.assertIn(phrase, operator)
                for phrase in frame[4:]:
                    self.assertIn(phrase, supervised)
                self.assertIn('Motivo.', operator)  # the reason is the caller's
                markers.append((markdown.read_marker(operator, 'notice'),
                                markdown.read_marker(supervised, 'notice')))
        self.assertEqual(markers[0], markers[1])
        self.assertEqual(markers[0], markers[2])
        self.assertIn('**Class:** `round_limit` (limit of the review rounds), critical.',
                      markdown.render_escalation('r', cls='round_limit'))  # English by default

    def test_the_texts_supervision_gives(self):
        self.assertIn('`/devloop grant-round <motivo>`',
                      supervision.operator_rounds_text('Italian'))
        for language in ('English', 'French'):
            self.assertIn('`/devloop grant-round <reason>`',
                          supervision.operator_rounds_text(language))
        self.assertEqual(supervision.coordinator_refusal('reconcile', 'Italian')[1],
                         '`/devloop reconcile` è riservato all\'operatore di persona, mai al '
                         'coordinatore')
        self.assertEqual(supervision.coordinator_refusal('veto', 'French')[1],
                         '`/devloop veto` is not among the commands delegated to the coordinator')
        self.assertIsNone(supervision.coordinator_refusal('retry', 'Italian'))

    def test_the_catalogues_have_every_text_in_italian(self):
        from devloop import budget, dispatch_supervision, journal
        for catalog in (dispatch_supervision.TEXTS, markdown.ESCALATION_TEXTS, budget.TEXTS,
                        journal.TEXTS):
            self.assertEqual(set(catalog.tables[ITALIAN]), set(catalog.tables[ENGLISH]))


class BudgetJournalLanguageTests(unittest.TestCase):
    """#633: the reasons ``budget`` and ``journal`` give to the escalations and to the answers
    of the dispatcher, in the project's language, English for a language without
    translation."""

    def test_the_refusal_of_a_turn_of_no_story(self):
        from devloop import budget
        from devloop.budget import BudgetRefused
        from devloop.journal import TurnJournal
        expected = {'Italian': 'non è coperto dall\'envelope di budget: l\'issue #9 non '
                               'appartiene a nessuna storia del journal',
                    'English': 'is not covered by the budget envelope: issue #9 belongs to no '
                               'story of the journal'}
        for language in ('Italian', 'English', 'French'):
            with self.subTest(language=language):
                with self.assertRaises(BudgetRefused) as caught:
                    budget.reserve(CFG.budget, TurnJournal(), repo='o/r', issue=9,
                                   kind='review', agent=CFG.agent('codex'), language=language)
                self.assertEqual(caught.exception.category, 'SCOPE_REQUIRED')
                self.assertIn(expected.get(language, expected['English']),
                              str(caught.exception))
                _, extension, why = budget.grantable(CFG.budget, TurnJournal(), repo='o/r',
                                                     issue=9, kind='review',
                                                     agent=CFG.agent('codex'),
                                                     language=language)
                self.assertIsNone(extension)
                self.assertEqual(why, budget.no_story(9, language))

    def test_the_extension_and_the_state_of_a_story(self):
        from devloop import budget
        extension = {'turns': 1, 'role_turns': {'review': 1}, 'phase_turns': {'impl': 2},
                     'wall_seconds': 900, 'percent': 25}
        self.assertEqual(budget.extension_text(extension, 'Italian'),
                         'turni +1, turni `review` +1, fase `impl` +2, tempo +900 s')
        for language in ('English', 'French'):
            self.assertEqual(budget.extension_text(extension, language),
                             'turns +1, turns `review` +1, phase `impl` +2, time +900 s')
        count = {'consumed': 1, 'reserved': 1, 'cap': 3, 'available': 1}
        found = {'turns': count, 'roles': {}, 'phases': {},
                 'usd': {'metered_usd': {'consumed': None, 'unattested': 2, 'reserved': 0.0,
                                         'available': None, 'cap': 1}},
                 'time': {'consumed': 10, 'cap': 60, 'available': 50,
                          'deadline_at': '2027-01-15T08:01:00Z'},
                 'extension': extension}
        self.assertEqual(budget.describe(found, 'Italian'), (
            'turni 2 di 3 (consumati 1, prenotati 1, disponibili 1); `metered_usd` consumati non '
            'attestato (turni senza valore attestato: 2), prenotati 0.00 USD, disponibili non '
            'attestato di 1.00 USD; tempo trascorso 10 s di 60 s, disponibili 50 s, scadenza '
            '2027-01-15T08:01:00Z; tetti già estesi una volta dal supervisore (`grant-budget`): '
            'turni +1, turni `review` +1, fase `impl` +2, tempo +900 s'))
        self.assertEqual(budget.describe(found, 'French'), (
            'turns 2 of 3 (consumed 1, reserved 1, available 1); `metered_usd` consumed not '
            'attested (turns without an attested value: 2), reserved 0.00 USD, available not '
            'attested of 1.00 USD; time elapsed 10 s of 60 s, available 50 s, deadline '
            '2027-01-15T08:01:00Z; caps already extended once by the supervisor (`grant-budget`): '
            'turns +1, turns `review` +1, phase `impl` +2, time +900 s'))

    def test_the_turns_that_block_a_work_and_the_reconciliation(self):
        from devloop import journal
        from devloop.journal import TurnJournal
        turn = {'turn': 'turn-1-0a1b2c3d', 'kind': 'review', 'agent': 'codex', 'number': 100,
                'at': '2026-11-24T10:00:00Z', 'outcome': None}
        expected = {
            'Italian': ('il lavoro dell\'issue #7 ha turni senza esito registrato',
                        'turno `review` di `codex` su #100', 'Per ognuno, un commento con prima '
                        'riga `/devloop reconcile <turno> <returned|failed|not_dispatched>`',
                        'il turno `turn-9-0a1b2c3d` non è nel journal'),
            'English': ('the work of issue #7 has turns without a recorded outcome',
                        'turn `review` of `codex` on #100', 'For each one, a comment whose first '
                        'line is `/devloop reconcile <turn> <returned|failed|not_dispatched>`',
                        'the turn `turn-9-0a1b2c3d` is not in the journal'),
        }
        for language in ('Italian', 'English', 'French'):
            with self.subTest(language=language):
                block, described, how, unknown = expected.get(language, expected['English'])
                text = journal.blocked_text(7, [turn], language)
                for phrase in (block, described, how):
                    self.assertIn(phrase, text)
                with self.assertRaisesRegex(ValueError, unknown):
                    TurnJournal().reconcile('turn-9-0a1b2c3d', 'failed', by='u', comment=1,
                                            note='n', language=language)


class SupervisionLanguageTests(SupervisionCase):
    """#633: the escalations to the supervisor and the dispatcher's answers to the commands
    on them, in the project's language; the records are the same."""

    LANGUAGES = {
        'Italian': ('## Escalation al supervisore', '## Decisione del supervisore',
                    'rifiutato: vale solo per un\'escalation `preflight_default` del controllo '
                    'di prontezza. Il comando è speso; l\'escalation resta al supervisore.',
                    'oppure `/devloop escalate <motivo>`',
                    '## Serve un intervento dell\'operatore',
                    'Il supervisore `agy-agent[bot]` passa all\'operatore l\'escalation di '
                    'classe `tool_failure`: modello instabile'),
        'English': ('## Escalation to the supervisor', '## Decision of the supervisor',
                    'refused: it applies only to a `preflight_default` escalation of the '
                    'readiness check. The command is spent; the escalation stays with the '
                    'supervisor.', 'or `/devloop escalate <reason>`',
                    '## The operator is needed',
                    'The supervisor `agy-agent[bot]` hands the escalation of class '
                    '`tool_failure` to the operator: modello instabile'),
    }

    def test_an_escalation_and_the_answers_to_the_commands(self):
        records = {}
        for language in ('Italian', 'English', 'French'):
            with self.subTest(language=language):
                (escalation, trace, refused, resume, operator,
                 handed) = self.LANGUAGES.get(language, self.LANGUAGES['English'])
                d = self.make(dataclasses.replace(SUPERVISION_AUTO, language=language),
                              AgentError('codex crashed'))
                self.assertEqual(self.tick(d), [('review', 100)])
                self.assertIn(escalation, self.gh.threads[100][-1]['body'])
                # A command that does not apply is answered, in the same language.
                self.gh.command(100, '/devloop accept-defaults')
                self.assertEqual(self.tick(d), [('supervise', 100)])
                body = ' '.join(self.gh.threads[100][-1]['body'].split())
                for phrase in (trace, refused, resume):
                    self.assertIn(phrase, body)
                # The escalation goes to the operator, with the supervisor's reason.
                self.gh.command(100, '/devloop escalate modello instabile')
                self.assertEqual(self.tick(d), [('supervise', 100)])
                self.assertIn(operator, self.gh.threads[100][-1]['body'])
                self.assertIn(handed, self.gh.threads[100][-1]['body'])
                records[language] = ([dict(r, comment=None) for r in self.decisions(100)],
                                     self.escalations(100))
        self.assertEqual(records['Italian'], records['English'])
        self.assertEqual(records['English'], records['French'])


if __name__ == '__main__':
    unittest.main()
