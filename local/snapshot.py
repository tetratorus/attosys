import hashlib
import io
import json
import os
import pathlib
import re
import tarfile

MAINTENANCE_TARGET = 'attosys-maintenance.target'
ENTRYPOINT = ["/lib/systemd/systemd", "--system", "--log-target=console", "--unit=" + MAINTENANCE_TARGET]
RESTORE_PENDING = '/var/lib/attosys-restore-pending'
CONTAINERFILE = '''FROM scratch
ADD os.tar /
COPY attosys-maintenance.target /etc/systemd/system/attosys-maintenance.target
RUN mkdir -p /run /tmp /proc /sys /dev /var/lib && chmod 1777 /tmp && touch /var/lib/attosys-restore-pending
ENV container=oci DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LABEL io.attosys.local.version="1"
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/lib/systemd/systemd", "--system", "--log-target=console", "--unit=attosys-maintenance.target"]
'''
MANIFEST = 'attosys-manifest.json'
COMPANY_FILES = ('opt/attosys/company.yaml', 'opt/attosys/handbook.md', 'opt/attosys/local-state.json', 'opt/attosys/shared')
STATE_DIRS = ('var/lib/atto-chat', 'var/lib/atto-mux', 'var/lib/atto-proxy')


def normalized(name):
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'unsafe archive path: {name}')
    return str(path)


def roots(manifest):
    if manifest.get('format') != 'attosys-company-data' or manifest.get('version') != 1:
        raise ValueError('unsupported company archive format')
    org = manifest.get('org', '')
    users = manifest.get('employees', [])
    if not re.fullmatch(r'[a-z][a-z0-9]{0,11}', org) or not users or len(users) != len(set(users)):
        raise ValueError('invalid company identity')
    if any(not re.fullmatch(re.escape(org) + r'-[a-zA-Z0-9_-]+', user) or len(user) > 32 for user in users):
        raise ValueError('invalid employee name')
    return (*COMPANY_FILES, *STATE_DIRS, *(f'home/{user}' for user in users))


def permitted(name, allowed):
    return any(name == root or name.startswith(root + '/') for root in allowed)


def signature(archive, member):
    result = {'type': member.type.decode(), 'mode': member.mode, 'user': member.uname,
              'group': member.gname, 'size': member.size, 'link': member.linkname}
    if member.isfile():
        digest = hashlib.sha256()
        with archive.extractfile(member) as data:
            for block in iter(lambda: data.read(1024 * 1024), b''):
                digest.update(block)
        result['sha256'] = digest.hexdigest()
    return result


def pack(destination, org, employees, root=pathlib.Path('/')):
    manifest = {'format': 'attosys-company-data', 'version': 1, 'org': org, 'employees': employees}
    allowed = roots(manifest)
    def include(member):
        parts = pathlib.PurePosixPath(member.name).parts
        if len(parts) >= 3 and parts[0] == 'home' and parts[2] == 'browser':
            return None
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            return None
        return member
    with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        with tarfile.open(fileobj=output, mode='w', dereference=False) as archive:
            for name in allowed:
                path = root / name
                if path.exists() or path.is_symlink():
                    archive.add(path, arcname=name, filter=include)
    with tarfile.open(destination) as archive:
        manifest['files'] = {member.name: signature(archive, member) for member in archive}
    content = json.dumps(manifest, sort_keys=True).encode()
    with tarfile.open(destination, 'a') as archive:
        member = tarfile.TarInfo(MANIFEST)
        member.size, member.mode = len(content), 0o600
        archive.addfile(member, io.BytesIO(content))
    inspect_archive(destination)


def inspect_archive(path):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        indexed = {normalized(member.name): member for member in members}
        if len(indexed) != len(members):
            raise ValueError('duplicate archive paths')
        header = indexed[MANIFEST]
        if not header.isfile() or header.size > 32 * 1024 * 1024:
            raise ValueError('invalid archive manifest')
        manifest = json.load(archive.extractfile(header))
        allowed = roots(manifest)
        if set(indexed) - {MANIFEST} != set(manifest['files']):
            raise ValueError('archive contents do not match the manifest')
        for name, member in indexed.items():
            if name == MANIFEST:
                continue
            if not permitted(name, allowed) or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f'unsupported archive entry: {name}')
            if not all(0 <= identity < 2**32 - 1 for identity in (member.uid, member.gid)):
                raise ValueError(f'invalid archive ownership: {name}')
            for parent in pathlib.PurePosixPath(name).parents:
                ancestor = indexed.get(str(parent))
                if ancestor and not ancestor.isdir():
                    raise ValueError(f'archive entry has a non-directory parent: {name}')
            if member.islnk():
                target = indexed.get(normalized(member.linkname))
                if target is None or not target.isfile():
                    raise ValueError(f'invalid hard link: {name}')
            if signature(archive, member) != manifest['files'][name]:
                raise ValueError(f'archive checksum or metadata mismatch: {name}')
        for required in ('opt/attosys/company.yaml', 'opt/attosys/local-state.json'):
            if required not in indexed or not indexed[required].isfile():
                raise ValueError(f'missing company state: {required}')
        return manifest


def ownership(member):
    import grp
    import pwd
    try:
        uid = pwd.getpwnam(member.uname).pw_uid
    except KeyError:
        uid = member.uid
    try:
        gid = grp.getgrnam(member.gname).gr_gid
    except KeyError:
        gid = member.gid
    return uid, gid


def unpack(source, root=pathlib.Path('/')):
    import grp
    import os
    import pwd
    import subprocess
    import yaml
    manifest = inspect_archive(source)
    if (root / 'opt/attosys/company.yaml').exists():
        raise ValueError('restore requires an empty company; existing state will not be overwritten')
    with tarfile.open(source) as archive:
        company = yaml.safe_load(archive.extractfile('opt/attosys/company.yaml'))
        if company['org'] != manifest['org'] or {f"{company['org']}-{role}" for role in company['agents']} != set(manifest['employees']):
            raise ValueError('roster does not match the archive manifest')
        subprocess.run(['groupadd', '-f', company['org']], check=True)
        for name in ('_atto_mux', '_atto_proxy', '_atto_chat', *manifest['employees']):
            try:
                pwd.getpwnam(name)
            except KeyError:
                subprocess.run(['useradd', '--no-create-home', *(['--system'] if name.startswith('_') else ['--user-group', '--shell', '/bin/bash']), name], check=True)
        for name in manifest['employees']:
            subprocess.run(['usermod', '-aG', company['org'], name], check=True)
        members = [member for member in archive if member.name != MANIFEST]
        identities = {member.name: ownership(member) for member in members}
        for member in members:
            destination = root / member.name
            for parent in destination.parents:
                if parent.is_symlink():
                    raise ValueError(f'restore path traverses a symlink: {destination}')
            if destination.is_symlink():
                raise ValueError(f'restore destination is a symlink: {destination}')
            member.uid, member.gid = identities[member.name]
            member.mode &= 0o3777 if member.isdir() else 0o777
            options = {'filter': 'fully_trusted'} if hasattr(tarfile, 'fully_trusted_filter') else {}
            archive.extract(member, path=root, set_attrs=False, **options)
        for member in reversed(members):
            destination = root / member.name
            os.chown(destination, member.uid, member.gid, follow_symlinks=False)
            if not member.issym():
                destination.chmod(member.mode)
                os.utime(destination, (member.mtime, member.mtime))
    print('Company data restored; OS accounts recreated by name.', flush=True)


def checksum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def inspect_pair(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('format') != 'attosys-pair' or manifest.get('version') != 1:
        raise ValueError('unsupported snapshot pair')
    if set(manifest.get('files', {})) != {'os.tar', 'data.tar'}:
        raise ValueError('snapshot pair must contain both OS and company data')
    for name, expected in manifest['files'].items():
        if checksum(directory / name) != expected:
            raise ValueError(f'snapshot pair checksum mismatch: {name}')
    inspect_archive(directory / 'data.tar')
    return manifest


def export_os(data_archive):
    import os
    import subprocess
    manifest = inspect_archive(data_archive)
    result = subprocess.run(['systemctl', 'list-units', '--all', '--plain', '--no-legend', '--type=service,socket',
                             'systemd-journald*'], check=True, capture_output=True, text=True)
    units = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    if units:
        subprocess.run(['systemctl', 'stop', *units], check=True)
    exclude = [*roots(manifest), 'opt/attosys/secrets.yaml', 'var/lib/.attosys-transfer-*', 'run', 'tmp', 'proc', 'sys', 'dev']
    os.execv('/bin/tar', ['tar', '--one-file-system', '--acls', '--xattrs',
                         *(f'--exclude=./{name}' for name in exclude), '-C', '/', '-cf', '-', '.'])


if __name__ == '__main__':
    import sys
    import yaml
    command, filename = sys.argv[1:]
    if command == 'pack':
        company = yaml.safe_load(pathlib.Path('/opt/attosys/company.yaml').read_text())
        pack(filename, company['org'], [f"{company['org']}-{role}" for role in company['agents']])
    elif command == 'unpack':
        unpack(filename)
    elif command == 'os':
        export_os(filename)
    else:
        raise SystemExit('expected pack, unpack or os')
