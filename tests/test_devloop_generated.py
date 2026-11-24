"""devloop: generated files and files changed by the tests in devloop's commits (#627).

devloop commits everything a writing turn leaves in its worktree. A tracked file that the local
tests rewrite, such as the bytecode of ``__pycache__/``, ends up in the commit, and the tree
committed is not the one tested: the passing test is not bound to the commit. devloop does not
leave such files out, since a tracked file may be wanted: it points them out in the ledger, in
the PR and in the fix record.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from devloop import markdown
from devloop.dispatcher import Dispatcher
from devloop.generated import (ENTRY_OVERHEAD, MAX_CHARS, MAX_LISTED, capped, cost,
                               generated_files, generated_record, looks_generated,
                               render_generated)
from devloop.ledger import Ledger
from devloop.workspace import Workspace
from test_devloop import (CFG, FakeRunner, FakeWS, finding, fixed, implemented, norm, pr,
                          raw_review)
from test_devloop_ledger import LedgerGH, TreeWS

PYC = 'pkg/__pycache__/m.cpython-314.pyc'
ENV = {'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 'T',
       'GIT_COMMITTER_EMAIL': 't@x'}


class GeneratedFilesTests(unittest.TestCase):
    def test_cache_and_bytecode_look_generated(self):
        for name in (PYC, '__pycache__/a.txt', 'a/b.pyc', 'c.pyo'):
            with self.subTest(name):
                self.assertTrue(looks_generated(name))
        for name in ('pkg/m.py', 'pycache.md', 'docs/__pycache__.md', 'a.pyc.txt'):
            with self.subTest(name):
                self.assertFalse(looks_generated(name))

    def test_the_files_of_a_commit_to_point_out_and_why(self):
        self.assertEqual(
            generated_files(['x.py', PYC, 'data.bin', PYC], by_tests=['data.bin', PYC, 'gone']),
            [{'path': 'data.bin', 'reasons': ['tests']},
             {'path': PYC, 'reasons': ['generated', 'tests']}])
        self.assertEqual(generated_files(['x.py', 'docs/a.md']), [])
        # A binary of the commit is pointed out even when no test of the dispatcher changed it.
        self.assertEqual(generated_files(['x.py', 'img.png', PYC], binary=['img.png', PYC]),
                         [{'path': 'img.png', 'reasons': ['binary']},
                          {'path': PYC, 'reasons': ['generated', 'binary']}])

    def test_the_section_lists_them_and_says_they_stay(self):
        self.assertEqual(render_generated([]), '')
        text = render_generated(generated_files([PYC, 'data.bin'], by_tests=['data.bin']))
        self.assertTrue(text.startswith('### File generati nel commit'))
        self.assertIn('devloop non li esclude', text)
        self.assertIn('- `data.bin` (modificato dai test locali)', text)
        self.assertIn(f'- `{PYC}` (cache o bytecode)', text)
        self.assertIn('- `img.png` (binario)', render_generated(
            generated_files(['img.png'], binary=['img.png'])))
        many = generated_files([f'a/{i:02}.pyc' for i in range(MAX_LISTED + 2)])
        text = render_generated(many)
        self.assertEqual(text.count('\n- `'), MAX_LISTED)
        self.assertIn('- … e altri 2', text)


# Many caches with long paths, as a test suite can create (#627, R1-2).
MANY = [f'pkg/subpackage/component_{i:04}/__pycache__/module_{i:04}.cpython-312.pyc'
        for i in range(600)]
# Valid paths (each component within 200 bytes) whose characters the marker escapes in 12
# characters each (R2-1): within MAX_CHARS by their length, far beyond it once serialized.
WIDE = [('😀' * 50 + '/') * 2 + '😀' * 28 + f'/{i:02}.pyc' for i in range(30)]


class LimitTests(unittest.TestCase):
    """The lists of files stay within a bound in the texts and in their markers alike: a commit
    or a test full of caches must not take a body beyond the limit of GitHub."""

    def test_capped_keeps_the_first_within_count_and_characters(self):
        self.assertEqual(capped(['a', 'b']), ['a', 'b'])
        self.assertEqual(len(capped(MANY)), MAX_LISTED)
        long = ['x' * 1500] * 5
        self.assertEqual(capped(long), long[:3])  # the fourth goes beyond MAX_CHARS
        self.assertLessEqual(sum(map(cost, capped(long))), MAX_CHARS)
        self.assertEqual(capped(['y' * (MAX_CHARS + 1)]), [])

    def test_the_cost_counts_the_marker_form_of_a_path(self):
        # R2-1: json.dumps escapes a character outside the basic plane in 12, Base64 adds 1/3.
        self.assertEqual(cost('a.pyc'), 5 + 12 + ENTRY_OVERHEAD)
        self.assertEqual(cost('😀'), 1 + 4 * 5 + ENTRY_OVERHEAD)  # '"\\ud83d\\ude00"': 14
        self.assertLess(len(capped(WIDE)), len(WIDE))
        self.assertLessEqual(sum(map(cost, capped(WIDE))), MAX_CHARS)

    def test_the_marker_keeps_the_listed_files_and_counts_the_others(self):
        self.assertEqual(generated_record([]), {})
        few = generated_files([PYC])
        self.assertEqual(generated_record(few), {'generated_files': few})
        files = generated_files(MANY)
        record = generated_record(files)
        self.assertEqual(record['generated_files'], files[:MAX_LISTED])
        self.assertEqual(record['generated_omitted'], len(MANY) - MAX_LISTED)
        text = render_generated(files)
        self.assertEqual(text.count('\n- `'), MAX_LISTED)
        self.assertIn(f'- … e altri {len(MANY) - MAX_LISTED}', text)

    def test_the_pr_body_fits_github(self):
        gh = LedgerGH()
        d = Dispatcher(CFG, gh, TreeWS(trees=('t1', 't2'), by_tests=MANY, files=['x.py'] + MANY),
                       FakeRunner(implemented()), log=lambda _: None, ledger=Ledger(gh))
        d._do_implement(7)
        (created,) = gh.created
        self.assertLess(len(created['body']), 20000)
        record = markdown.read_marker(created['body'], 'pr')
        self.assertEqual(record['generated_files'],
                         [{'path': name, 'reasons': ['generated', 'tests']}
                          for name in sorted(MANY)[:MAX_LISTED]])
        self.assertEqual(record['generated_omitted'], len(MANY) - MAX_LISTED)
        self.assertIn(f'- … e altri {len(MANY) - MAX_LISTED}', created['body'])
        # The verification of the implementation, with the files its test changed, fits too.
        (check,) = gh.records(7, 'verification')
        self.assertEqual(check['changed_by_tests_omitted'], len(MANY) - MAX_LISTED)

    def test_the_fix_record_fits_github(self):
        fix = {'summary': 's', 'responses': [], 'head_sha': 'a' * 40,
               'generated_files': generated_files(MANY)}
        body = markdown.render_fix(fix, 'h', 1)
        self.assertLess(len(body), 20000)
        record = markdown.read_marker(body, 'fix')
        self.assertEqual(len(record['generated_files']), MAX_LISTED)
        self.assertEqual(record['generated_omitted'], len(MANY) - MAX_LISTED)
        self.assertIn(f'- … e altri {len(MANY) - MAX_LISTED}', body)

    def test_the_verification_record_fits_github(self):
        gh = LedgerGH()
        entry = Ledger(gh).verification(7, kind='implement', attempt=1, command='t',
                                        passed=True, duration_ms=1, tree_before='a',
                                        tree_after='b', changed_by_tests=MANY)
        self.assertEqual(entry.record['changed_by_tests'], MANY[:MAX_LISTED])
        self.assertEqual(entry.record['changed_by_tests_omitted'], len(MANY) - MAX_LISTED)
        body = gh.thread[7][-1]['body']
        self.assertLess(len(body), 20000)
        self.assertIn(f'- … e altri {len(MANY) - MAX_LISTED}', body)
        few = Ledger(gh).verification(7, kind='implement', attempt=1, command='t', passed=True,
                                      duration_ms=1, tree_before='a', tree_after='b',
                                      changed_by_tests=[PYC])
        self.assertNotIn('changed_by_tests_omitted', few.record)
        self.assertNotIn('e altri', gh.thread[7][-1]['body'])

    def test_paths_outside_ascii_fit_github_too(self):
        # R2-1: 30 such paths took the PR body to 68161 characters and the fix to 66369.
        gh = LedgerGH()
        d = Dispatcher(CFG, gh, TreeWS(trees=('t1', 't2'), by_tests=WIDE, files=['x.py'] + WIDE),
                       FakeRunner(implemented()), log=lambda _: None, ledger=Ledger(gh))
        d._do_implement(7)
        (created,) = gh.created
        self.assertLess(len(created['body']), 20000)
        kept = len(capped(sorted(WIDE)))
        record = markdown.read_marker(created['body'], 'pr')
        self.assertEqual([f['path'] for f in record['generated_files']], sorted(WIDE)[:kept])
        self.assertEqual(record['generated_omitted'], len(WIDE) - kept)
        (check,) = gh.records(7, 'verification')
        self.assertEqual(check['changed_by_tests_omitted'], len(WIDE) - kept)
        (comment,) = [c['body'] for c in gh.thread[7]
                      if '**File modificati dal test**' in c['body']]
        self.assertLess(len(comment), 20000)
        self.assertIn(f'- … e altri {len(WIDE) - kept}', comment)
        fix = {'summary': 's', 'responses': [], 'head_sha': 'a' * 40,
               'generated_files': generated_files(WIDE)}
        body = markdown.render_fix(fix, 'h', 1)
        self.assertLess(len(body), 20000)
        self.assertEqual(markdown.read_marker(body, 'fix')['generated_omitted'],
                         len(WIDE) - kept)


class CommitNotBoundTests(unittest.TestCase):
    """The mechanism of ``commit not bound`` (#627), on a real repository: a tracked file that
    the test command rewrites makes the committed tree differ from the tree tested before."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        self.git('init', '-q', '-b', 'main')
        (root / 'pkg' / '__pycache__').mkdir(parents=True)
        (root / 'pkg' / 'm.py').write_text('X = 1\n')
        (root / PYC).write_bytes(b'old bytecode')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'init')
        self.gh = LedgerGH()
        self.logs = []
        self.ws = Workspace(root, '.state', 'main')
        self.d = Dispatcher(CFG, self.gh, self.ws, FakeRunner(), log=self.logs.append,
                            ledger=Ledger(self.gh))

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True,
                       env=dict(os.environ, **ENV))

    def run_and_commit(self, command):
        (self.root / 'pkg' / 'm.py').write_text('X = 2\n')  # the change of the turn
        passed, _, tested = self.d._run_tests((7, 'implement'), self.root, 1, command)
        self.assertTrue(passed)
        self.tested = tested
        commit = self.ws.commit_all(self.root, 'change',
                                    author={'name': 'Bot', 'email': 'bot@example.com'})
        self.d._bind_tested(tested, self.root, commit)
        (check,) = self.gh.records(7, 'verification')
        return check, commit

    def test_a_tracked_file_rewritten_by_the_tests_leaves_the_commit_unbound(self):
        check, commit = self.run_and_commit(f'printf "new bytecode" > {PYC}')
        self.assertNotEqual(check['tree_before'], check['tree_after'])
        self.assertEqual(check['changed_by_tests'], [PYC])
        self.assertEqual(self.ws.commit_tree(self.root, commit), check['tree_after'])
        self.assertIsNone(check['commit'])
        self.assertIn(f'  the tree of {commit[:12]} is not the one tested: commit not bound '
                      '(the tests changed 1 files)', self.logs)
        self.assertIn(f'  the tests changed {PYC}', self.logs)
        # The rewritten bytecode is in the commit, and the ledger comment names it.
        self.assertEqual(sorted(self.ws.changed_files(self.root, since='HEAD~1')),
                         sorted(['pkg/m.py', PYC]))
        body = self.gh.thread[7][-1]['body']
        self.assertIn('**File modificati dal test**', body)
        self.assertIn(f'- `{PYC}`', body)
        # What the PR or the fix record points out of that commit.
        self.assertEqual(self.d._generated(self.root, 'HEAD~1', self.tested),
                         [{'path': PYC, 'reasons': ['generated', 'tests']}])

    def test_a_new_untracked_cache_changes_the_tree_too(self):
        check, _ = self.run_and_commit('mkdir -p t/__pycache__ && printf b > t/__pycache__/a.pyc')
        self.assertEqual(check['changed_by_tests'], ['t/__pycache__/a.pyc'])
        self.assertIsNone(check['commit'])

    def test_a_binary_regenerated_during_the_turn_is_pointed_out(self):
        # A tracked binary the agent's own tests regenerated before the dispatcher's check
        # (R1-1): the check changes nothing and the commit is bound, the binary is in it.
        (self.root / 'data.bin').write_bytes(b'\0old')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'binary')
        (self.root / 'data.bin').write_bytes(b'\0new')
        check, commit = self.run_and_commit('true')
        self.assertNotIn('changed_by_tests', check)
        self.assertEqual(check['commit'], commit)
        self.assertEqual(self.d._generated(self.root, 'HEAD~1', self.tested),
                         [{'path': 'data.bin', 'reasons': ['binary']}])

    def test_the_binary_files_of_a_commit(self):
        (self.root / 'gone.bin').write_bytes(b'\0gone')
        (self.root / 'kept.bin').write_bytes(b'\0kept')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'binaries')
        (self.root / 'gone.bin').unlink()  # a deleted binary is not pointed out
        (self.root / 'kept.bin').write_bytes(b'\0changed')
        (self.root / 'new.bin').write_bytes(b'\0new')
        (self.root / 'pkg' / 'm.py').write_text('X = 3\n')  # text
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change')
        self.assertEqual(sorted(self.ws.binary_files(self.root, 'HEAD~1')),
                         ['kept.bin', 'new.bin'])

    def test_a_test_that_changes_nothing_binds_the_commit(self):
        check, commit = self.run_and_commit('true')
        self.assertEqual(check['tree_before'], check['tree_after'])
        self.assertNotIn('changed_by_tests', check)
        self.assertEqual(check['commit'], commit)


class PointedOutTests(unittest.TestCase):
    def make(self, runner, ws, ledger=True):
        self.gh, self.ws, self.logs = LedgerGH(), ws, []
        return Dispatcher(CFG, self.gh, ws, runner, log=self.logs.append,
                          ledger=Ledger(self.gh) if ledger else None)

    def test_the_pr_points_out_the_generated_files_and_keeps_them(self):
        ws = TreeWS(trees=('t1', 't2'), by_tests=[PYC, 'data.bin'],
                    files=['x.py', PYC, 'data.bin', 'other.pyc'])
        d = self.make(FakeRunner(implemented()), ws)
        d._do_implement(7)
        (created,) = self.gh.created
        expected = [{'path': 'data.bin', 'reasons': ['tests']},
                    {'path': 'other.pyc', 'reasons': ['generated']},
                    {'path': PYC, 'reasons': ['generated', 'tests']}]
        self.assertEqual(markdown.read_marker(created['body'], 'pr')['generated_files'], expected)
        self.assertIn(render_generated(expected), created['body'])
        self.assertEqual(ws.pushes, [('agent/issue-7', True)])  # published all the same
        self.assertIn('  3 generated files or files changed by the tests in the commit, '
                      'pointed out', self.logs)

    def test_a_pr_without_them_has_no_section(self):
        d = self.make(FakeRunner(implemented()), TreeWS(files=['x.py']))
        d._do_implement(7)
        (created,) = self.gh.created
        self.assertNotIn('generated_files', markdown.read_marker(created['body'], 'pr'))
        self.assertNotIn('File generati nel commit', created['body'])

    def test_the_fix_record_points_them_out(self):
        first = norm(raw_review('request_changes', [finding()]))
        d = self.make(FakeRunner(fixed()), FakeWS(files=['x.py', PYC]), ledger=False)
        d._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        (body,) = [b for _, b in self.gh.comments if markdown.read_marker(b, 'fix')]
        expected = [{'path': PYC, 'reasons': ['generated']}]
        self.assertEqual(markdown.read_marker(body, 'fix')['generated_files'], expected)
        self.assertIn(render_generated(expected), body)
        self.assertEqual(self.ws.pushes, [('agent/issue-7', False)])


if __name__ == '__main__':
    unittest.main()
