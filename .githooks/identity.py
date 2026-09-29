import os
import pathlib
import re
import subprocess
import sys
import tempfile

POLICY_ID = 'attosys-identity-v1'
NAME = 'Leonard Tan'
EMAIL = 'lentan1029@gmail.com'
IDENTITY = f'{NAME} <{EMAIL}>'
HOOKS = ('pre-commit', 'commit-msg', 'pre-push')


def git(*args, input=None, cwd=None):
    return subprocess.run(['git', *args], input=input, cwd=cwd, text=True, capture_output=True, check=True).stdout


def reject(message):
    raise ValueError(message)


def check_ident(value, role):
    if value.rsplit(' ', 2)[0] != IDENTITY:
        reject(f'unapproved {role}; expected {IDENTITY}')


def check_current():
    for role in ('AUTHOR', 'COMMITTER'):
        check_ident(git('var', f'GIT_{role}_IDENT').strip(), role.lower())


def check_message(message):
    for line in git('interpret-trailers', '--parse', input=message).splitlines():
        key, _, value = line.partition(':')
        if key.lower().endswith('-by') and value.strip() != IDENTITY:
            reject(f'unapproved {key} trailer; expected {IDENTITY}')


def check_refs(refs):
    if git('rev-parse', '--is-shallow-repository').strip() != 'false':
        reject('shallow history cannot be verified; fetch the complete history before pushing')
    commits = set()
    for ref in refs:
        if ref.startswith('-'):
            reject('expected a ref, not a Git option')
        oid = git('rev-parse', '--verify', ref).strip()
        while git('cat-file', '-t', oid).strip() == 'tag':
            headers, _, message = git('cat-file', '-p', oid).partition('\n\n')
            fields = dict(line.split(' ', 1) for line in headers.splitlines())
            check_ident(fields.get('tagger', ''), f'tagger on {oid[:12]}')
            check_message(message)
            oid = fields['object']
        if git('cat-file', '-t', oid).strip() != 'commit':
            reject('only commits and tags pointing to commits can be identity-checked')
        commits.add(oid)
    if not commits:
        reject('no commits to verify')
    fields = git('log', '--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00', *sorted(commits), '--').split('\0')
    if (len(fields) - 1) % 6 or fields[-1].strip():
        reject('unexpected commit metadata format')
    for index in range(0, len(fields) - 1, 6):
        sha, author, author_email, committer, committer_email, message = fields[index:index + 6]
        for role, name, email in (('author', author, author_email), ('committer', committer, committer_email)):
            if (name, email) != (NAME, EMAIL):
                reject(f'unapproved {role} on {sha.strip()[:12]}; expected {IDENTITY}')
        check_message(message)


def pre_push():
    refs = []
    for line in sys.stdin:
        fields = line.split()
        if len(fields) != 4 or any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', fields[index]) for index in (1, 3)):
            reject('invalid pre-push input')
        if fields[1].strip('0'):
            refs.append(fields[1])
    if refs:
        check_refs(refs)


def install(repo):
    custom = subprocess.run(['git', '-C', str(repo), 'config', '--get', 'core.hooksPath'], capture_output=True, text=True)
    if custom.returncode != 1:
        reject('custom core.hooksPath or unreadable configuration; existing hook configuration left untouched')
    hooks = pathlib.Path(git('rev-parse', '--path-format=absolute', '--git-path', 'hooks', cwd=repo).strip())
    source = pathlib.Path(__file__).read_text()
    payloads = {hook: f'#!/bin/sh\nexec python3 "$(dirname "$0")/attosys-identity.py" {hook} "$@"\n' for hook in HOOKS}
    payloads['attosys-identity.py'] = source
    for name, text in payloads.items():
        target = hooks / name
        if target.is_symlink():
            reject(f'{target} is a symlink; left untouched')
        if target.exists():
            existing = target.read_text()
            managed = f"POLICY_ID = '{POLICY_ID}'\n" in existing if name == 'attosys-identity.py' else existing == text
            if not managed:
                reject(f'{target} is not managed by attosys; all existing hooks left untouched')
    hooks.mkdir(parents=True, exist_ok=True)
    for name, text in payloads.items():
        with tempfile.NamedTemporaryFile(mode='w', dir=hooks, prefix='.attosys-identity-', delete=False) as output:
            temporary = pathlib.Path(output.name)
            output.write(text)
        temporary.chmod(0o755 if name in HOOKS else 0o644)
        temporary.replace(hooks / name)
    print(f'Installed identity hooks in {hooks}; required identity: {IDENTITY}')


def main():
    args = sys.argv[1:]
    if not args:
        reject('usage: identity.py install [repo] | commit [git commit args] | check [refs...]')
    command, *args = args
    if command == 'install':
        if len(args) > 1:
            reject('install takes at most one repository path')
        install(pathlib.Path(args[0] if args else '.').resolve())
    elif command == 'commit':
        if any(arg in ('-n', '--no-verify') for arg in args):
            reject('the commit helper does not allow hook bypass flags')
        environment = dict(os.environ)
        for role in ('AUTHOR', 'COMMITTER'):
            environment[f'GIT_{role}_NAME'] = NAME
            environment[f'GIT_{role}_EMAIL'] = EMAIL
        raise SystemExit(subprocess.run(['git', 'commit', *args], env=environment).returncode)
    elif command == 'pre-commit':
        check_current()
    elif command == 'commit-msg':
        if len(args) != 1:
            reject('commit-msg requires one message file')
        check_message(pathlib.Path(args[0]).read_text())
    elif command == 'pre-push':
        pre_push()
    elif command == 'check':
        refs = args or git('for-each-ref', '--format=%(objectname)', 'refs/heads', 'refs/remotes', 'refs/tags').splitlines()
        check_refs(refs)
        print(f'History identity check passed: {IDENTITY}')
    else:
        reject(f'unknown command: {command}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        detail = 'Git command failed; identity could not be verified' if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f'Identity check blocked: {detail}', file=sys.stderr)
        sys.exit(1)
