import argparse
import fcntl
import getpass
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import snapshot

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = {
    "attobot": ["agent.py", "SOUL.md", "opt", "requirements.txt", "lab-constraints.txt"],
    "attosys": ["hire.py", "seed.py", "services.py", "mux", "templates", "local/chat.py", "local/bootstrap.py", "local/snapshot.py", "local/attosys-maintenance.target"],
    "attotrain": ["*.py", "README.md", "steps", "tools", "tests"],
    "attobrowser": ["atto", "lib", "package.json", "package-lock.json"],
    "llmproxy": ["server.js", "stats-handler.js", "index.html", "package.json", "package-lock.json"],
}
BOOTSTRAP = "/opt/attosys/local/bootstrap.py"


class Runtime:
    def __init__(self, kind='auto'):
        self.kind = ('container' if platform.system() == 'Darwin' and platform.machine() == 'arm64' and shutil.which('container') else 'docker') if kind == 'auto' else kind
        self.binary = shutil.which(self.kind)
        if not self.binary:
            raise ValueError(f'{self.kind} runtime is required')

    def run(self, *args, **kwargs):
        return subprocess.run([self.binary, *map(str, args)], check=True, **kwargs)

    def info(self, name):
        if self.kind == 'container':
            rows = json.loads(self.run('list', '--all', '--format', 'json', capture_output=True, text=True).stdout)
            return next((row for row in rows if row['id'] == name), None)
        names = self.run('ps', '--all', '--format', '{{.Names}}', capture_output=True, text=True).stdout.splitlines()
        if name not in names:
            return None
        row = json.loads(self.run('inspect', name, capture_output=True, text=True).stdout)[0]
        return {'id': name, 'status': {'state': 'running' if row['State']['Running'] else 'stopped'}, 'configuration': {
            'initProcess': {'executable': row['Path'], 'arguments': row['Args']},
            'publishedPorts': row['HostConfig'].get('PortBindings'), 'dns': {'nameservers': row['HostConfig'].get('Dns') or []},
            'mounts': [{'destination': mount['Destination'], 'source': mount.get('Source'), 'type': {mount['Type']: {}}} for mount in row['Mounts']]}}

    def architecture(self):
        machine = platform.machine() if self.kind == 'container' else self.run('info', '--format', '{{.Architecture}}', capture_output=True, text=True).stdout.strip()
        return {'aarch64': 'arm64', 'x86_64': 'amd64', 'x64': 'amd64'}.get(machine, machine)

    def supported(self, info):
        cfg = info['configuration']
        process = cfg['initProcess']
        if process['executable'] != snapshot.ENTRYPOINT[0] or '--unit=' + snapshot.MAINTENANCE_TARGET not in process['arguments']:
            raise ValueError('This container predates isolated maintenance startup and was left untouched. Use a new --name for the new launcher.')
        for mount in cfg['mounts']:
            temporary = 'tmpfs' in mount['type'] and mount['destination'] in ('/run', '/tmp')
            cgroup = self.kind == 'docker' and mount['destination'] == '/sys/fs/cgroup' and mount.get('source') == '/sys/fs/cgroup'
            if not temporary and not cgroup:
                raise ValueError('additional external mounts cannot be included in a company snapshot')

    def create(self, args, image):
        dns = ['--dns', args.dns] if args.dns else []
        ports = [] if args.no_publish else ['--publish', '127.0.0.1:8090:8090', '--publish', '127.0.0.1:8810:8810']
        cgroup = ['--cgroupns', 'host', '--volume', '/sys/fs/cgroup:/sys/fs/cgroup:rw'] if self.kind == 'docker' else []
        self.run('create', '--name', args.name, *dns, '--cpus', '4', '--memory', '4G', '--cap-add', 'SYS_ADMIN',
                 '--tmpfs', '/run', '--tmpfs', '/tmp', *cgroup, *ports, image)

    def wait(self, name):
        for _ in range(60):
            result = subprocess.run([self.binary, 'exec', name, 'systemctl', 'is-active', snapshot.MAINTENANCE_TARGET], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip() == 'active':
                return
            time.sleep(1)
        raise RuntimeError('systemd did not become ready')

    def present(self, name, path):
        result = subprocess.run([self.binary, 'exec', name, 'python3', '-c',
                                 'import pathlib,sys; print(int(pathlib.Path(sys.argv[1]).is_file()))', path],
                                capture_output=True, text=True, timeout=10)
        result.check_returncode()
        if result.stdout.strip() not in ('0', '1'):
            raise RuntimeError('container returned an invalid file-presence response')
        return result.stdout.strip() == '1'

    def bootstrap(self, name, command, options=None):
        if options is None:
            return self.run('exec', name, 'python3', BOOTSTRAP, command, timeout=240)
        return self.run('exec', '--interactive', name, 'python3', BOOTSTRAP, command,
                        input=json.dumps(options), text=True, timeout=240)


def build(runtime, args):
    with tempfile.TemporaryDirectory(prefix='.local-build-', dir=ROOT) as directory:
        context = pathlib.Path(directory).resolve()
        for repo, patterns in SOURCES.items():
            source_root = args.source_root / repo
            if not source_root.is_dir():
                raise ValueError(f'missing sibling checkout: {source_root}')
            for pattern in patterns:
                for source in source_root.glob(pattern):
                    target = context / repo / source.relative_to(source_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_dir():
                        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                    else:
                        shutil.copy2(source, target)
        shutil.copy2(ROOT / 'local/Containerfile', context / 'Containerfile')
        if not (context / 'attobot/lab-constraints.txt').is_file():
            raise ValueError('build context is missing attobot dependency constraints')
        print(f"Building {sum(path.is_file() for path in context.rglob('*'))} source files", flush=True)
        dns = ['--dns', args.dns] if args.dns and runtime.kind == 'container' else []
        runtime.run('build', *dns, '--file', 'Containerfile', '--tag', 'attosys-local', '--progress', 'plain', '.', cwd=context)


def require_complete(runtime, name):
    if runtime.present(name, snapshot.RESTORE_PENDING):
        raise ValueError('restore is incomplete; company left intact. Retry restore from the original snapshot with a new --name.')


def start(runtime, args):
    info = runtime.info(args.name)
    if info is None:
        if not args.no_build:
            build(runtime, args)
        runtime.create(args, 'attosys-local')
        info = runtime.info(args.name)
    runtime.supported(info)
    running = info['status']['state'] == 'running'
    try:
        if not running:
            runtime.run('start', args.name)
        runtime.wait(args.name)
        require_complete(runtime, args.name)
        if runtime.present(args.name, '/run/attosys/ready'):
            print('Company already running; nothing changed.')
            return
        key = ''
        if not args.idle:
            key = os.environ.get('ATTOBOT_API_KEY') or ''
            if not key and not runtime.present(args.name, '/run/attosys/llm.env'):
                key = getpass.getpass('OpenAI API key (kept only in VM tmpfs): ')
                if not key:
                    raise ValueError('an API key is required')
        runtime.bootstrap(args.name, 'start', {'api_key': key, 'model': args.model, 'duration': args.duration, 'workers': not args.idle})
    except BaseException:
        runtime.run('stop', args.name)
        raise
    print(f'Container: {args.name}')
    if info['configuration']['publishedPorts']:
        print('Chat: http://127.0.0.1:8090')
        print('Captured model requests: http://127.0.0.1:8810')
    else:
        print('No host ports published.')
    print('Employees remain stopped.' if args.idle else f'Employee run deadline: {args.duration} seconds.')


def stop(runtime, args):
    info = runtime.info(args.name)
    if info is None:
        print('No company exists under that name.')
        return
    runtime.supported(info)
    if info['status']['state'] == 'running':
        runtime.bootstrap(args.name, 'pause')
        runtime.run('stop', args.name)
    print(f'{args.name} stopped; company state retained.')


def save(runtime, args):
    destination = args.archive.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('archive already exists; choose a new filename')
    if not destination.parent.is_dir():
        raise ValueError('archive parent directory does not exist')
    info = runtime.info(args.name)
    if info is None:
        raise ValueError('no company to save')
    runtime.supported(info)
    stop(runtime, args)
    info = runtime.info(args.name)
    with tempfile.TemporaryDirectory(prefix='.local-build-', dir=destination.parent) as directory:
        bundle = pathlib.Path(directory) / 'snapshot'
        bundle.mkdir(mode=0o700)
        remote = '/var/lib/.attosys-transfer-' + uuid.uuid4().hex + '.tar'
        helper = '/opt/attosys/local/snapshot.py'
        packed = False
        try:
            if info['status']['state'] != 'running':
                runtime.run('start', args.name)
                runtime.wait(args.name)
            require_complete(runtime, args.name)
            runtime.bootstrap(args.name, 'pause')
            machine = runtime.run('exec', args.name, 'uname', '-m', capture_output=True, text=True).stdout.strip()
            architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(machine, machine)
            print('Saving company data with workers and database writers stopped...', flush=True)
            runtime.run('exec', args.name, 'python3', helper, 'pack', remote)
            packed = True
            with (bundle / 'data.tar').open('xb') as output:
                runtime.run('exec', args.name, 'cat', remote, stdout=output)
            print('Saving OS files, installed tools and system configuration...', flush=True)
            with (bundle / 'os.tar').open('xb') as output:
                runtime.run('exec', args.name, 'python3', helper, 'os', remote, stdout=output)
        finally:
            if runtime.info(args.name)['status']['state'] == 'running':
                try:
                    if packed:
                        runtime.run('exec', args.name, 'python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).unlink()', remote)
                finally:
                    runtime.run('stop', args.name)
        manifest = {'format': 'attosys-pair', 'version': 1, 'architecture': architecture,
                    'dns': info['configuration'].get('dns', {}).get('nameservers', []),
                    'files': {name: snapshot.checksum(bundle / name) for name in ('os.tar', 'data.tar')}}
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        snapshot.inspect_pair(bundle)
        for path in bundle.iterdir():
            path.chmod(0o600)
        destination.mkdir(mode=0o700)
        for name in ('os.tar', 'data.tar', 'manifest.json'):
            os.link(bundle / name, destination / name)
    print(f'Saved matched OS and data snapshots in {destination}. Original company remains stopped and intact.')


def restore(runtime, args):
    if runtime.info(args.name) is not None:
        raise ValueError('a company already exists under this name; restore will not overwrite it')
    directory = args.archive.expanduser().absolute()
    print('Verifying the matched OS and data snapshots...', flush=True)
    manifest = snapshot.inspect_pair(directory)
    if manifest['architecture'] != runtime.architecture():
        raise ValueError('snapshot CPU architecture does not match this runtime')
    reference = 'attosys-restored:' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='.local-build-', dir=directory.parent) as temporary:
        context = pathlib.Path(temporary)
        shutil.copyfile(directory / 'os.tar', context / 'os.tar')
        (context / 'Containerfile').write_text(snapshot.CONTAINERFILE)
        shutil.copyfile(ROOT / 'local' / snapshot.MAINTENANCE_TARGET, context / snapshot.MAINTENANCE_TARGET)
        runtime.run('build', '--file', 'Containerfile', '--tag', reference, '--progress', 'plain', '.', cwd=context)
    if not args.dns and manifest.get('dns'):
        args.dns = manifest['dns'][0]
    runtime.create(args, reference)
    runtime.supported(runtime.info(args.name))
    remote = '/var/lib/.attosys-transfer-' + uuid.uuid4().hex + '.tar'
    helper = '/run/attosys-restore-' + uuid.uuid4().hex + '.py'
    try:
        runtime.run('start', args.name)
        runtime.wait(args.name)
        for source, target in ((pathlib.Path(snapshot.__file__), helper), (directory / 'data.tar', remote)):
            with source.open('rb') as data:
                runtime.run('exec', '--interactive', args.name, 'python3', '-c',
                            "import os,shutil,sys; os.umask(0o077); shutil.copyfileobj(sys.stdin.buffer, open(sys.argv[1], 'xb'))", target, stdin=data)
        runtime.run('exec', args.name, 'python3', helper, 'unpack', remote)
        runtime.run('exec', args.name, 'python3', '-c', 'import pathlib,sys; [pathlib.Path(path).unlink() for path in sys.argv[1:]]', remote, helper)
        runtime.bootstrap(args.name, 'prepare')
        runtime.run('exec', args.name, 'python3', '-c',
                    'import os,pathlib,sys; os.sync(); pathlib.Path(sys.argv[1]).unlink(); os.sync()', snapshot.RESTORE_PENDING)
    except BaseException:
        print(f'Restore did not complete. {args.name} is retained; retry the original snapshot with a new --name.', file=sys.stderr)
        raise
    finally:
        runtime.run('stop', args.name)
    print(f'Restored {args.name}; employees have not started. Run start with your own API key when ready.')


def main():
    parser = argparse.ArgumentParser(description='Start, stop or transfer matched Linux OS and company-data snapshots.')
    parser.add_argument('command', nargs='?', default='start', choices=('start', 'stop', 'save', 'restore'))
    parser.add_argument('archive', nargs='?', type=pathlib.Path)
    parser.add_argument('--source-root', type=pathlib.Path, default=ROOT.parent)
    parser.add_argument('--name', default='atto-company')
    parser.add_argument('--runtime', choices=('auto', 'container', 'docker'), default='auto')
    parser.add_argument('--dns', default=None)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--duration', type=int, default=900)
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--no-build', action='store_true')
    parser.add_argument('--idle', action='store_true', help='start infrastructure without employees or an API key')
    parser.add_argument('--no-publish', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if platform.system() not in ('Darwin', 'Linux'):
        parser.error('this launcher supports macOS and Linux hosts')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', args.name) or args.duration < 1:
        parser.error('invalid company name or duration')
    if (args.command in ('save', 'restore')) != (args.archive is not None):
        parser.error('save and restore require an archive; start and stop do not take one')
    if args.build_only and args.command != 'start':
        parser.error('--build-only is only valid with start')
    lock_path = pathlib.Path(tempfile.gettempdir()) / f'attosys-{os.getuid()}-{args.name}.lock'
    try:
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime = Runtime(args.runtime)
            if args.build_only:
                build(runtime, args)
            else:
                {'start': start, 'stop': stop, 'save': save, 'restore': restore}[args.command](runtime, args)
    except (OSError, ValueError, KeyError, RuntimeError, tarfile.TarError, subprocess.SubprocessError) as error:
        sys.exit(str(error))


if __name__ == '__main__':
    main()
