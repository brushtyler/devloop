"""Follow, live, the agent working in a devloop worktree: its messages and tool calls.

devloop writes its own agent log only when a turn ends. This reads the newest session transcript
whose working directory is the worktree: Claude (``~/.claude/projects``) or Codex
(``~/.codex/sessions``), and keeps following it, switching to a newer transcript of the same
worktree when one appears (for example the next attempt of a turn). The reviewer of a PR works in
a disposable copy, ``pr-<N>-review``: following ``pr-<N>`` covers both, newest session first.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

NAME = re.compile(r'(issue|pr)-[0-9]+')
CODEX_SCAN = 40  # newest Codex transcripts inspected


def _clean(text):
    """One line, without control characters: transcripts hold model output, shown on a terminal."""
    return ''.join(' ' if ord(c) < 32 or 127 <= ord(c) < 160 else c for c in text)


def _cut(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                            separators=(',', ':'))
    return _clean(text)[:limit]


def _first(*values):
    for value in values:
        if value is not None and value is not False:
            return value
    return ''


def format_record(record):
    """The lines to show for one transcript record, Claude or Codex."""
    if not isinstance(record, dict):
        return []
    stamp = record.get('timestamp')
    t = stamp[11:19] if isinstance(stamp, str) else ''
    lines = []
    if record.get('type') == 'assistant':
        message = record.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'tool_use':
                args = item.get('input') if isinstance(item.get('input'), dict) else {}
                arg = _first(args.get('command'), args.get('file_path'), args.get('pattern'))
                lines.append(f'{t} → {_cut(_first(item.get("name")), 60)} {_cut(arg, 110)}')
            elif item.get('type') == 'text':
                lines.append(f'{t} · {_cut(_first(item.get("text")), 200)}')
    elif record.get('type') == 'response_item':
        payload = record.get('payload')
        payload = payload if isinstance(payload, dict) else {}
        if payload.get('type') in ('function_call', 'custom_tool_call'):
            arg = _first(payload.get('arguments'), payload.get('input'))
            lines.append(f'{t} → {_cut(_first(payload.get("name")), 60)} {_cut(arg, 110)}')
        elif payload.get('type') == 'message' and payload.get('role') == 'assistant':
            content = payload.get('content')
            texts = [_cut(c['text'], 200) for c in content if isinstance(c, dict)
                     and c.get('text') is not None] if isinstance(content, list) else []
            lines.append(f'{t} · {_cut(" ".join(texts), 200)}')
    return lines


def pending_call(path, limit=2_000_000):
    """``'<tool> <argument>'`` of the last tool call in a Claude or Codex transcript that has no
    result yet, read from its last ``limit`` bytes; None if there is none or it is not known."""
    try:
        with open(path, 'rb') as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return None
    lines = data.decode('utf-8', 'replace').split('\n')
    if size > limit:
        lines = lines[1:]  # the first line is cut
    pending = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        message = record.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
        if record.get('type') in ('assistant', 'user') and isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'tool_use':
                    args = item.get('input') if isinstance(item.get('input'), dict) else {}
                    arg = _first(args.get('command'), args.get('file_path'), args.get('pattern'))
                    pending[item.get('id')] = f'{_cut(_first(item.get("name")), 60)} {_cut(arg, 200)}'
                elif item.get('type') == 'tool_result':
                    pending.pop(item.get('tool_use_id'), None)
        elif record.get('type') == 'response_item':
            if payload.get('type') in ('function_call', 'custom_tool_call'):
                arg = _first(payload.get('arguments'), payload.get('input'))
                pending[payload.get('call_id')] = (f'{_cut(_first(payload.get("name")), 60)} '
                                                   f'{_cut(arg, 200)}')
            elif payload.get('type') in ('function_call_output', 'custom_tool_call_output'):
                pending.pop(payload.get('call_id'), None)
    return list(pending.values())[-1].strip() if pending else None


def worktrees(root, target):
    """The worktrees whose agents ``devloop follow <target>`` shows: ``issue-<N>`` for the
    implementer; ``pr-<N>`` for the fixer and its copy ``pr-<N>-review`` for the reviewer
    (see :meth:`devloop.workspace.Workspace.review_copy`), or the copies
    ``pr-<N>-review-<reviewer>`` of the reviewers running at once (#176)."""
    paths = [Path(root) / target]
    if target.startswith('pr-'):
        paths.append(Path(root) / f'{target}-review')
        paths += sorted(Path(root).glob(f'{target}-review-*'))
    return paths


def _paths(worktree):
    """One worktree or several, as a list."""
    return [worktree] if isinstance(worktree, (str, os.PathLike)) else list(worktree)


def claude_dirs(home, worktree):
    """Claude Code keeps a project's sessions in a directory named after its path."""
    names = set()
    for path in {str(worktree), os.path.realpath(worktree)}:
        names.add(re.sub(r'[^A-Za-z0-9]', '-', path))
        names.add(path.replace('/', '-').replace('.', '-'))
    return [Path(home) / '.claude' / 'projects' / name for name in sorted(names)]


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def codex_cwd(path):
    """The working directory recorded in the first line of a Codex transcript."""
    try:
        with open(path, encoding='utf-8') as fh:
            return json.loads(fh.readline())['payload']['cwd']
    except (OSError, ValueError, KeyError, TypeError):
        return None


def find_transcript(worktree, home):
    """The newest Claude or Codex transcript of ``worktree`` (one path or several), or None.
    Claude wins a tie."""
    wts = _paths(worktree)
    targets = {t for w in wts for t in (str(w), os.path.realpath(w))}
    found = []
    dirs = sorted({d for w in wts for d in claude_dirs(home, w)})
    claude = [(m, p) for d in dirs for p in d.glob('*.jsonl') if (m := _mtime(p)) is not None]
    if claude:
        found.append(max(claude, key=lambda c: c[0]))
    codex = [(m, p) for p in (Path(home) / '.codex' / 'sessions').glob('*/*/*/*.jsonl')
             if (m := _mtime(p)) is not None]
    for m, p in sorted(codex, key=lambda c: c[0], reverse=True)[:CODEX_SCAN]:
        if codex_cwd(p) in targets:
            found.append((m, p))
            break
    return max(found, key=lambda c: c[0])[1] if found else None


class Tail:
    """Complete lines appended to a file, from its start; reopened if replaced or truncated."""

    def __init__(self, path):
        self.path = Path(path)
        self._open()

    def _open(self):
        self._fh = open(self.path, 'rb')
        self._inode = os.fstat(self._fh.fileno()).st_ino
        self._partial = b''

    def close(self):
        self._fh.close()

    def lines(self):
        chunk = self._fh.read()
        if not chunk:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                return []
            if st.st_ino != self._inode or st.st_size < self._fh.tell():
                self.close()
                self._open()
                chunk = self._fh.read()
        *complete, self._partial = (self._partial + chunk).split(b'\n')
        return [line.decode('utf-8', 'replace') for line in complete]


def show(lines, out):
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for text in format_record(record):
            out.write(text + '\n')
    out.flush()


def follow(worktree, home=None, out=sys.stdout, err=sys.stderr, sleep=time.sleep,
           interval=1.0, rescan_every=10, polls=None):
    """Print the transcript of ``worktree`` (one path or several: the newest session among
    them) and keep following it; ``polls`` bounds the loop (for tests). Returns the exit
    status."""
    home = Path.home() if home is None else Path(home)
    path = find_transcript(worktree, home)
    if path is None:
        where = ' or '.join(str(w) for w in _paths(worktree))
        err.write(f'no Claude or Codex session found for {where}\n')
        return 1
    err.write(f'following {path}\n')
    tail = Tail(path)
    done = 0
    try:
        while polls is None or done < polls:
            show(tail.lines(), out)
            done += 1
            if done % rescan_every == 0:
                newer = find_transcript(worktree, home)
                if newer is not None and newer != tail.path:
                    tail.close()
                    err.write(f'following {newer}\n')
                    tail = Tail(newer)
                    continue
            sleep(interval)
    finally:
        tail.close()
    return 0
