"""Linux data-volume contract and empty-volume bootstrap for Mac operating bundles.

No volume is created, removed, migrated or recursively chowned by this module.
The bootstrap entry point runs only inside a one-shot network-none container.
"""
import copy
import json
import os
import re
import sys
from pathlib import Path

DATA = {'postgres': '/var/lib/postgresql/data', 'redis': '/data', 'kafka': '/var/lib/kafka'}
LINUX_FILESYSTEMS = {'ext4', 'xfs', 'btrfs'}


def configure(document, bootstrap_image):
    """Return a full candidate; unrelated application/network settings are preserved."""
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', bootstrap_image):
        raise ValueError('Bootstrap image must be pinned')
    doc = copy.deepcopy(document)
    for service, target in DATA.items():
        mounts = doc['services'][service]['volumes']
        found = [i for i, mount in enumerate(mounts) if mount['target'] == target]
        if len(found) != 1:
            raise ValueError('Exactly one data mount required: ' + service)
        key = service + '-data'
        mounts[found[0]] = {'type': 'volume', 'source': key, 'target': target,
                            'read_only': False, 'volume': {'nocopy': True}}
        doc.setdefault('volumes', {})[key] = {
            'external': True, 'name': '${OPERATING_PROJECT}-' + key}
    doc['services']['storage-init'] = {
        'image': bootstrap_image, 'platform': 'linux/arm64', 'pull_policy': 'never',
        'container_name': '${OPERATING_PROJECT}-storage-init',
        'profiles': ['bootstrap'], 'restart': 'no', 'network_mode': 'none',
        'user': '0:0', 'read_only': True, 'cap_drop': ['ALL'],
        'cap_add': ['CHOWN', 'FOWNER', 'DAC_READ_SEARCH'],
        'security_opt': ['no-new-privileges:true'],
        'mem_limit': 134217728, 'memswap_limit': 134217728,
        'cpus': 0.5, 'pids_limit': 32,
        # The bootstrap image may declare this VOLUME; do not create an anonymous volume.
        'tmpfs': ['/var/lib/postgresql/data:rw,noexec,nosuid,size=1m'],
        'labels': {'com.pawbridge.operating-owner': '${OPERATING_OWNER}'},
        'entrypoint': ['python3', '/bootstrap/mac_operating_storage.py'],
        'command': ['${OPERATING_UID}', '${OPERATING_GID}'],
        'volumes': [
            {'type': 'volume', 'source': key + '-data', 'target': '/storage/' + key,
             'read_only': False, 'volume': {'nocopy': True}} for key in DATA
        ] + [{'type': 'bind', 'source': '${BACKGROUND_SCRIPTS}/mac_operating_storage.py',
              'target': '/bootstrap/mac_operating_storage.py', 'read_only': True,
              'bind': {'create_host_path': False}}],
    }
    validate(doc)
    return doc


def validate(doc):
    for service, target in DATA.items():
        mounts = [m for m in doc['services'][service]['volumes'] if m['target'] == target]
        expected = {'type': 'volume', 'source': service + '-data', 'target': target,
                    'read_only': False, 'volume': {'nocopy': True}}
        if mounts != [expected]:
            raise ValueError('Data must use a Linux volume: ' + service)
        if doc['volumes'][service + '-data'] != {
            'external': True, 'name': '${OPERATING_PROJECT}-' + service + '-data'}:
            raise ValueError('Unexpected external volume identity')


def mount_filesystems(text):
    result = {}
    for line in text.splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        result[fields[4]] = (right.split()[0], set(fields[5].split(',')))
    return result


def initialize_empty(paths, uid, gid, mounts):
    if not (0 < uid < 2**31 and 0 < gid < 2**31):
        raise ValueError('Non-root numeric runtime identity required')
    # Validate every mount before any mutation; populated data always fails closed.
    for path in paths:
        fs, options = mounts.get(str(path), ('', set()))
        if path.is_symlink() or not path.is_dir() or fs not in LINUX_FILESYSTEMS or 'rw' not in options:
            raise ValueError('Expected an explicit writable Linux filesystem mount')
        if any(path.iterdir()):
            raise ValueError('Refusing populated data volume')
    for path in paths:
        os.chown(path, uid, gid)
        os.chmod(path, 0o700)
        stat = path.stat()
        if stat.st_uid != uid or stat.st_gid != gid or stat.st_mode & 0o777 != 0o700:
            raise RuntimeError('Data volume ownership verification failed')


if __name__ == '__main__':
    if len(sys.argv) != 3 or os.geteuid() != 0:
        raise SystemExit('Run only in the dedicated root bootstrap container')
    try:
        initialize_empty([Path('/storage') / k for k in DATA], int(sys.argv[1]), int(sys.argv[2]),
                         mount_filesystems(Path('/proc/self/mountinfo').read_text()))
    except (ValueError, OSError, RuntimeError):
        raise SystemExit('Storage initialization refused; no data migration or deletion performed')
    print(json.dumps({'emptyLinuxVolumesInitialized': 3, 'dataCreated': False}))
