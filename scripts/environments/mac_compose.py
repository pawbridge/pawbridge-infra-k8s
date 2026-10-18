#!/usr/bin/env python3
"""Prepare and inspect the isolated Mac rehearsal. Never starts Docker or a database."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import stat
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / 'environments/mac/compose'
PROJECT = 'pawbridge-mac-rehearsal'
ENVIRONMENT = 'mac-rehearsal'
SERVICES = ('user', 'animal', 'community', 'store', 'payment')
PROFILES = ('API_GATEWAY', 'USER_SERVICE', 'ANIMAL_SERVICE', 'COMMUNITY_SERVICE', 'STORE_SERVICE', 'PAYMENT_SERVICE')
STAGES = ('data', 'migrations', 'events', 'core', 'extended', 'optional-ai')
STAGE_SERVICES = {
    'data': ('postgres', 'redis', 'kafka-volume-init', 'kafka', 'mail', 'local-access'),
    'migrations': tuple('migration-' + name for name in SERVICES),
    'events': ('connect',),
    'core': ('api-gateway', 'user-service', 'animal-service'),
    'extended': ('community-service', 'store-service', 'payment-service'),
    'optional-ai': ('photo-service', 'python-ai-service'),
}
IMAGE = re.compile(r'[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}')
LOCAL_IMAGE = re.compile(r'sha256:[a-f0-9]{64}')
APPROVED_REHEARSAL_REVISION = '501d513b76a7340c6a03fcb34ebb55f0d1984650'
APPROVED_REHEARSAL_PURPOSE = 'synthetic-gateway-user-animal'
APPROVED_EXTENDED_PURPOSE = 'synthetic-general-java-start'
APPROVED_PYTHON_REVISION = 'cf67d541833e8bbe9e31e0aa5325590e1abf21b4'
APPROVED_AI_PURPOSE = 'synthetic-python-photo-native-integration'
DEVELOPMENT_REHEARSAL_BASIS = 'approved-development-rehearsal'
REVISION = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})')
VARIABLE = re.compile(r'\$\{([A-Z][A-Z0-9_]*)(?:(:-|:\?|\?|\-)([^}]*))?\}')
LABELS = ('com.pawbridge.project', 'com.pawbridge.environment', 'com.pawbridge.owner')
PUBLIC_MOUNTS = ('init.sql', 'redis.conf', 'cdc.properties')
DISABLED_FLAGS = ('APMS_PHOTO_ARCHIVE_ENABLED', 'TOURAPI_ENABLED', 'TOURAPI_SCHEDULE_ENABLED', 'SHELTER_DIRECTORY_SCHEDULE_ENABLED', 'LOST_GALLERY_FEED_ENABLED', 'SPRING_BATCH_JOB_ENABLED')


class GuardError(ValueError):
    """An unsafe input; messages must not contain credentials."""


def desktop_endpoint():
    return 'unix://' + str(Path.home() / '.docker/run/docker.sock')


def reject_overrides(environ=None):
    env = os.environ if environ is None else environ
    forbidden = [key for key in env if key.startswith(('MAC_', 'DOCKER_', 'COMPOSE_'))]
    if forbidden:
        raise GuardError('Unset shell overrides before using rehearsal files: ' + ', '.join(sorted(forbidden)))


def state_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise GuardError('State must be an absolute path')
    if any(c in str(path) for c in ('\n', '\r', '$', '`', '=')):
        raise GuardError('Unsupported state path characters')
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise GuardError('State paths must not contain symlinks')
    path = path.resolve()
    if path in (Path('/'), Path.home()) or path == ROOT or ROOT in path.parents:
        raise GuardError('State must be outside the repository and outside the home root')
    if path.name in ('dev', 'prod', 'production', 'runtime-validation'):
        raise GuardError('Refusing another environment state path')
    return path


def private_path(path, mode, directory=False):
    if path.is_symlink():
        raise GuardError('Refusing symlink state entry: ' + path.name)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise GuardError('Required state entry missing: ' + path.name) from None
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        raise GuardError('Wrong owner, type or permissions: ' + path.name)
    if not directory and info.st_nlink != 1:
        raise GuardError('Refusing a hard-linked state file: ' + path.name)


def read_json(path, private=True):
    if private:
        private_path(path, 0o600)
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        raise GuardError('Invalid JSON file: ' + path.name) from None
    if not isinstance(document, dict):
        raise GuardError('Expected a JSON object: ' + path.name)
    return document


def read_env(path, private=True):
    if private:
        private_path(path, 0o600)
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'MAC_[A-Z0-9_]+', key) or key in values or not value or any(c in value for c in ('$','`','\n','\r')):
            raise GuardError('Invalid or duplicate env assignment in ' + path.name)
        values[key] = value
    return values


def identity_for(state):
    return {'version': 1, 'project': PROJECT, 'environment': ENVIRONMENT, 'runtime': 'docker-desktop-compose',
            'state': str(state), 'owner': str(uuid.uuid4()), 'uid': os.getuid(),
            'context': 'desktop-linux', 'endpoint': desktop_endpoint(),
            'volumes': [PROJECT + '_' + name for name in ('postgres', 'redis', 'kafka')]}


def validate_identity(state, identity):
    expected = identity_for(state)
    for key in expected:
        if key != 'owner' and identity.get(key) != expected[key]:
            raise GuardError('State identity mismatch: ' + key)
    if set(identity) != set(expected) or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', identity.get('owner', '')):
        raise GuardError('Invalid rehearsal owner identity')


def secret_names():
    return ('MAC_POSTGRES_PASSWORD', 'MAC_REDIS_PASSWORD', 'MAC_INTERNAL_API_KEY', 'MAC_VECTOR_PASSWORD') + tuple(
        'MAC_' + service.upper() + '_' + role + '_PASSWORD' for service in SERVICES for role in ('OWNER', 'APP', 'CDC'))


def runtime_values(state, identity):
    values = {'MAC_STATE': str(state), 'MAC_PROJECT': PROJECT, 'MAC_ENVIRONMENT': ENVIRONMENT, 'MAC_OWNER': identity['owner']}
    values['MAC_KAFKA_CLUSTER_ID'] = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
    values.update({key: secrets.token_hex(32) for key in secret_names()})
    values['MAC_JWT_SECRET'] = base64.b64encode(secrets.token_bytes(64)).decode()
    values.update({'MAC_PROFILE_' + profile: 'UNCONFIRMED' for profile in PROFILES})
    return values


def validate_runtime(state, identity, values):
    required = set(secret_names()) | {'MAC_STATE', 'MAC_PROJECT', 'MAC_ENVIRONMENT', 'MAC_OWNER', 'MAC_JWT_SECRET', 'MAC_KAFKA_CLUSTER_ID'} | {'MAC_PROFILE_' + p for p in PROFILES}
    if set(values) != required:
        raise GuardError('Unexpected runtime env key set; operational secrets are not accepted')
    for key, value in {'MAC_STATE': str(state), 'MAC_PROJECT': PROJECT, 'MAC_ENVIRONMENT': ENVIRONMENT, 'MAC_OWNER': identity['owner']}.items():
        if values[key] != value:
            raise GuardError('Runtime identity mismatch: ' + key)
    if any(not re.fullmatch(r'[a-f0-9]{64}', values[key]) for key in secret_names()):
        raise GuardError('Rehearsal passwords must be locally generated 256-bit values')
    if not re.fullmatch(r'[A-Za-z0-9_-]{22}', values['MAC_KAFKA_CLUSTER_ID']):
        raise GuardError('Invalid new Kafka cluster identity')
    try:
        if len(base64.b64decode(values['MAC_JWT_SECRET'], validate=True)) != 64:
            raise ValueError()
    except ValueError:
        raise GuardError('Invalid rehearsal JWT key') from None
    for profile in PROFILES:
        if not re.fullmatch(r'[A-Za-z0-9_,.-]+', values['MAC_PROFILE_' + profile]):
            raise GuardError('Invalid Spring profile assignment')


def sql_init(values, identity):
    # One-time initialization of a new volume: never run against an existing database.
    owner = identity['owner']
    lines = ["REVOKE CONNECT ON DATABASE pawbridge FROM PUBLIC;", 'REVOKE CREATE ON SCHEMA public FROM PUBLIC;',
             'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;', 'CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;',
             "CREATE TABLE public.pawbridge_local_environment (name text PRIMARY KEY CHECK(name = 'mac-rehearsal'), owner text NOT NULL);",
             f"INSERT INTO public.pawbridge_local_environment VALUES ('mac-rehearsal', '{owner}');"]
    for name in SERVICES:
        schema = 'pawbridge_' + name
        roles = {kind: 'pawbridge_mac_' + name + '_' + kind.lower() for kind in ('OWNER', 'APP', 'CDC')}
        for kind, role in roles.items():
            password = values['MAC_' + name.upper() + '_' + kind + '_PASSWORD']
            lines += [f"CREATE ROLE {role} LOGIN {'REPLICATION' if kind == 'CDC' else 'NOREPLICATION'} PASSWORD '{password}';", f'GRANT CONNECT ON DATABASE pawbridge TO {role};']
        lines += [f"CREATE SCHEMA {schema} AUTHORIZATION {roles['OWNER']};", f"GRANT USAGE ON SCHEMA {schema} TO {roles['APP']}, {roles['CDC']};",
                  f"ALTER DEFAULT PRIVILEGES FOR ROLE {roles['OWNER']} IN SCHEMA {schema} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {roles['APP']};",
                  f"ALTER DEFAULT PRIVILEGES FOR ROLE {roles['OWNER']} IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {roles['APP']};",
                  f"ALTER DEFAULT PRIVILEGES FOR ROLE {roles['OWNER']} IN SCHEMA {schema} GRANT SELECT ON TABLES TO {roles['CDC']};"]
    lines += [f"CREATE ROLE pawbridge_mac_vector LOGIN PASSWORD '{values['MAC_VECTOR_PASSWORD']}';", 'GRANT CONNECT ON DATABASE pawbridge TO pawbridge_mac_vector;',
              'GRANT USAGE ON SCHEMA pawbridge_animal TO pawbridge_mac_vector;',
              'ALTER DEFAULT PRIVILEGES FOR ROLE pawbridge_mac_animal_owner IN SCHEMA pawbridge_animal GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pawbridge_mac_vector;',
              'ALTER DEFAULT PRIVILEGES FOR ROLE pawbridge_mac_animal_owner IN SCHEMA pawbridge_animal GRANT USAGE, SELECT ON SEQUENCES TO pawbridge_mac_vector;']
    return '\n'.join(lines) + '\n'


def derived_files(values, identity):
    return {'init.sql': sql_init(values, identity),
            'redis.conf': 'appendonly yes\nmaxmemory 128mb\nmaxmemory-policy noeviction\nrequirepass ' + values['MAC_REDIS_PASSWORD'] + '\n',
            'cdc.properties': ''.join(service + '.password=' + values['MAC_' + service.upper() + '_CDC_PASSWORD'] + '\n' for service in SERVICES)}


def compose_document(assets=ASSETS):
    try:
        document = json.loads((assets / 'compose.yaml').read_text())
    except (OSError, ValueError):
        raise GuardError('Compose asset missing or invalid (JSON-compatible YAML required)') from None
    if not isinstance(document, dict) or not isinstance(document.get('services'), dict):
        raise GuardError('Invalid Compose service contract')
    return document


def initial_inventory(identity, compose, images):
    entries = {}
    for service, config in compose['services'].items():
        variable = VARIABLE.fullmatch(config.get('image', ''))
        entries[service] = {'image': images.get(variable[1], 'UNCONFIRMED') if variable else 'UNCONFIRMED',
                            'image_verified': False, 'arm64_verified': False, 'source_basis': 'unconfirmed',
                            'source_revision': 'UNCONFIRMED', 'windows_image': 'UNCONFIRMED', 'source_verified': False, 'evidence': '',
                            'architecture': 'UNCONFIRMED', 'image_source_revision': 'UNCONFIRMED', 'source_evidence': '',
                            'rehearsal_source_verified': False, 'approval_evidence': '', 'rehearsal_purpose': ''}
    return {'version': 1, 'project': PROJECT, 'environment': ENVIRONMENT,
            'target': {'context': identity['context'], 'endpoint': identity['endpoint'], 'engine': 'docker-desktop'},
            'services': entries, 'external': {service: {'verified': False, 'network_probe': '', 'scheduler_review': ''} for service in compose['services'] if service.endswith('-service') or service == 'api-gateway'},
            'migration_sources': {service: {'source_revision': 'UNCONFIRMED', 'sql_sha256': 'UNCONFIRMED', 'verified': False, 'evidence': ''} for service in SERVICES},
            'runtime_mount_readability': {'verified': False, 'evidence': ''}}


def validate_inventory_target(identity, inventory):
    if inventory.get('version') != 1 or inventory.get('project') != PROJECT or inventory.get('environment') != ENVIRONMENT:
        raise GuardError('Inventory project/environment mismatch')
    expected = {'context': identity['context'], 'endpoint': identity['endpoint'], 'engine': 'docker-desktop'}
    if inventory.get('target') != expected:
        raise GuardError('Inventory must target the recorded local Docker Desktop context and endpoint')


def write_new(path, content, mode=0o600):
    # Exclusive creation never repairs, replaces or truncates an existing file.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
        path.chmod(mode)
    except FileExistsError:
        raise GuardError('Refusing to overwrite existing state entry: ' + path.name) from None


def load_state(state, assets=ASSETS):
    private_path(state, 0o700, directory=True)
    identity = read_json(state / 'identity.json')
    validate_identity(state, identity)
    values = read_env(state / 'runtime.env')
    validate_runtime(state, identity, values)
    for name, expected in derived_files(values, identity).items():
        private_path(state / name, 0o444)
        if (state / name).read_text() != expected:
            raise GuardError('Prepared mount changed; refusing to replace: ' + name)
    inventory = read_json(state / 'inventory.json')
    validate_inventory_target(identity, inventory)
    images = read_env(state / 'images.env')
    compose = compose_document(assets)
    expected_images = {match[1] for config in compose['services'].values() if (match := VARIABLE.fullmatch(config.get('image', '')))}
    if set(images) != expected_images:
        raise GuardError('Image env must contain only the declared Compose image variables')
    return identity, values, images, inventory


def prepare(value, assets=ASSETS, environ=None):
    reject_overrides(environ)
    state = state_path(value)
    compose = compose_document(assets)
    images_template = assets / 'images.env.example'
    images = read_env(images_template, private=False)
    image_keys = {match[1] for config in compose['services'].values() if (match := VARIABLE.fullmatch(config.get('image', '')))}
    if set(images) != image_keys:
        raise GuardError('Image example does not match Compose image variables')
    if state.exists() and any(state.iterdir()):
        load_state(state, assets)
        return {'prepared': True, 'created': False, 'state': str(state), 'staticOnly': True, 'runtimeVerified': False}
    if state.exists():
        private_path(state, 0o700, directory=True)
    else:
        state.mkdir(parents=True, mode=0o700)
        private_path(state, 0o700, directory=True)
    identity = identity_for(state)
    values = runtime_values(state, identity)
    artifacts = {'runtime.env': ''.join(key + '=' + value + '\n' for key, value in values.items()),
                 'images.env': images_template.read_text(),
                 'inventory.json': json.dumps(initial_inventory(identity, compose, images), indent=2) + '\n'}
    artifacts.update(derived_files(values, identity))
    for name, content in artifacts.items():
        write_new(state / name, content, 0o444 if name in PUBLIC_MOUNTS else 0o600)
    distributions = state / 'migration-distributions'
    distributions.mkdir(mode=0o700)
    for service in SERVICES:
        (distributions / service).mkdir(mode=0o700)
        (distributions / service / 'lib').mkdir(mode=0o700)
    write_new(state / 'identity.json', json.dumps(identity, indent=2) + '\n')
    return {'prepared': True, 'created': True, 'state': str(state), 'staticOnly': True, 'runtimeVerified': False}


def expand(value, values):
    def substitute(match):
        key, operator, default = match.groups()
        if key in values:
            return values[key]
        if operator in (':-', '-'):
            return default
        raise GuardError('Missing Compose variable: ' + key)
    result = VARIABLE.sub(substitute, str(value))
    if '${' in result:
        raise GuardError('Unsupported Compose interpolation')
    return result


def address_guard(key, value, service_names):
    text = str(value)
    if any(word in text.lower() for word in ('api.pawbridge.kr', '.svc.cluster.local', 'host.docker.internal', 'gateway.docker.internal')):
        raise GuardError('Operational/host escape address in ' + key)
    if '://' in text:
        hosts = re.findall(r"://(?:[^/@\s]*@)?(\[[^]]+\]|[^/:?#\s\"'{}]+)", text)
        if not hosts:
            raise GuardError('Invalid endpoint in ' + key)
        for host in hosts:
            if host not in service_names | {'127.0.0.1', 'localhost'} and not host.endswith('.invalid'):
                raise GuardError('External endpoint in ' + key)
    elif re.search(r'(HOST|BOOTSTRAP_SERVERS|ENDPOINT|ADDRESS)$', key):
        if text not in service_names | {'127.0.0.1', 'localhost', 'UNCONFIRMED'} and not text.endswith('.invalid') and not re.fullmatch(r'(?:kafka|127\.0\.0\.1|localhost):[0-9]+', text):
            raise GuardError('External host in ' + key)
    placeholder_keys = ('R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'GOOGLE_CLIENT_ID', 'GOOGLE_SECRET_KEY', 'GOOGLE_EMAIL_SECRET_KEY', 'APMS_API_SERVICE_KEY')
    if key in placeholder_keys and text not in ('mac-unconfigured', ''):
        raise GuardError('Operational external credentials are not accepted: ' + key)
    if (key == 'TOSS_SECRET_KEY' and text != 'test_unconfigured') or (key == 'YOUTUBE_DATA_API_KEY' and text):
        raise GuardError('External credential must remain disabled: ' + key)
    if key.endswith(('POSTGRESQL_JDBC_URL', 'PG_MIGRATION_JDBC_URL', 'PG_MIGRATION_CONFIRM_TARGET')) and text not in ('jdbc:postgresql://postgres:5432/pawbridge', 'jdbc:postgresql://127.0.0.1:5432/pawbridge'):
        raise GuardError('Rehearsal PostgreSQL database address required: ' + key)
    if key in DISABLED_FLAGS and text.lower() != 'false':
        raise GuardError('Collection/batch flag enabled: ' + key)


def validate_compose(compose, identity, values, assets=ASSETS):
    if compose.get('name') != PROJECT:
        raise GuardError('Compose project mismatch')
    expected_labels = dict(zip(LABELS, (PROJECT, ENVIRONMENT, identity['owner'])))
    networks = compose.get('networks', {})
    if set(networks) != {'rehearsal', 'access'} or not networks['rehearsal'].get('internal'):
        raise GuardError('Rehearsal network must be internal; only the loopback access network is permitted')
    for name, config in networks.items():
        labels = {key: expand(value, values) for key, value in config.get('labels', {}).items()}
        if config.get('external') or config.get('driver') not in (None, 'bridge') or any(labels.get(key) != value for key, value in expected_labels.items()):
            raise GuardError('Unowned or externally backed network: ' + name)
    volume_configs = compose.get('volumes', {})
    if set(volume_configs) != {'postgres', 'redis', 'kafka'}:
        raise GuardError('Unexpected state volumes')
    for name, config in volume_configs.items():
        if config.get('external') or config.get('driver_opts') or config.get('driver') not in (None, 'local') or expand(config.get('name', ''), values) != PROJECT + '_' + name:
            raise GuardError('Reused or external volume: ' + name)
        labels = {key: expand(value, values) for key, value in config.get('labels', {}).items()}
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            raise GuardError('Volume ownership labels missing: ' + name)
    names = set(compose['services'])
    for name, config in compose['services'].items():
        if config.get('platform') != 'linux/arm64' or config.get('pull_policy') != 'never' or config.get('restart') not in (None, 'no'):
            raise GuardError('Unapproved image/start policy: ' + name)
        if any(key in config for key in ('build', 'privileged', 'devices', 'cap_add', 'pid', 'ipc', 'extra_hosts', 'env_file', 'secrets', 'configs', 'volumes_from', 'links', 'external_links', 'credential_spec', 'userns_mode', 'uts')):
            raise GuardError('Host privilege or uncontrolled env source: ' + name)
        if name in STAGE_SERVICES['core'] + STAGE_SERVICES['extended'] and (config.get('user') != '10001:10001' or config.get('read_only') is not True or config.get('cap_drop') != ['ALL'] or config.get('security_opt') != ['no-new-privileges:true']):
            raise GuardError('Java application privilege baseline missing: ' + name)
        if name in STAGE_SERVICES['optional-ai']:
            expected_user = '65532:65532' if name == 'photo-service' else '10001:10001'
            if (config.get('user') != expected_user or config.get('read_only') is not True
                    or config.get('cap_drop') != ['ALL'] or config.get('security_opt') != ['no-new-privileges:true']):
                raise GuardError('Python application privilege baseline missing: ' + name)
            env = config.get('environment', {})
            expected_keys = {'INTERNAL_API_KEY'} if name == 'photo-service' else {'INTERNAL_API_KEY', 'LOST_STORAGE_BACKEND', 'LOST_GALLERY_SYNC_ENABLED', 'LLM_PROVIDER'}
            if set(env) != expected_keys:
                raise GuardError('Python rehearsal must not receive DB/provider credentials: ' + name)
            if name == 'python-ai-service' and (env.get('LLM_PROVIDER') != 'stub' or env.get('LOST_STORAGE_BACKEND') != 'postgresql' or env.get('LOST_GALLERY_SYNC_ENABLED') != 'false'):
                raise GuardError('Python rehearsal requires PostgreSQL and the local stub provider')
        mode = config.get('network_mode')
        if mode == 'none' and name != 'kafka-volume-init':
            raise GuardError('Only volume initialization may use no network')
        if mode and mode not in ('none', 'service:postgres'):
            raise GuardError('Unapproved network mode: ' + name)
        if mode == 'service:postgres' and not name.startswith('migration-'):
            raise GuardError('Only PG migration runners may share the PG network')
        used_networks = config.get('networks', [])
        expected_networks = [] if mode else ['rehearsal', 'access'] if name == 'local-access' else ['rehearsal']
        if used_networks != expected_networks:
            raise GuardError('Uncontrolled/default egress network: ' + name)
        for port in config.get('ports', []):
            if not isinstance(port, str) or not port.startswith('127.0.0.1:'):
                raise GuardError('Non-loopback published port: ' + name)
        labels = {key: expand(value, values) for key, value in config.get('labels', {}).items()}
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            raise GuardError('Container ownership labels missing: ' + name)
        for mount in config.get('volumes', []):
            if isinstance(mount, str):
                parts = mount.split(':')
                if len(parts) != 2 or parts[0] not in volume_configs or not parts[1].startswith('/'):
                    raise GuardError('Unowned or implicit host mount: ' + name)
                continue
            if not isinstance(mount, dict):
                raise GuardError('Explicit bind mounts required: ' + name)
            source = expand(mount.get('source', ''), values)
            if mount.get('type') == 'volume':
                if source not in volume_configs:
                    raise GuardError('Unowned named volume: ' + name)
            elif mount.get('type') == 'bind':
                source_path = (assets / source).resolve() if not Path(source).is_absolute() else Path(source).resolve()
                if not mount.get('read_only') or not (source_path.is_relative_to(Path(identity['state'])) or source_path.is_relative_to(assets.resolve())):
                    raise GuardError('Unapproved writable/host bind mount: ' + name)
                allowed_state = {Path(identity['state']) / file for file in PUBLIC_MOUNTS}
                if name.startswith('migration-'):
                    allowed_state.add(Path(identity['state']) / 'migration-distributions' / name.removeprefix('migration-') / 'lib')
                allowed_assets = {assets.resolve() / file for file in ('kafka.properties', 'connect.properties', 'access.conf', 'mail_sink.py')}
                if source_path not in allowed_state | allowed_assets:
                    raise GuardError('Unapproved state/asset mount: ' + name)
                if any(p in source.lower() for p in ('docker.sock', '/.ssh', '/.obsidian')):
                    raise GuardError('Sensitive host mount: ' + name)
            else:
                raise GuardError('Unknown mount type: ' + name)
        for key, value in config.get('environment', {}).items():
            address_guard(key, expand(value, values), names)


def target_evidence(identity, document):
    for key in ('project', 'environment', 'owner', 'context', 'endpoint'):
        if document.get(key) != identity[key]:
            raise GuardError('Target evidence mismatch: ' + key)
    if document.get('engine') != 'docker-desktop':
        raise GuardError('Target evidence is not Docker Desktop')
    try:
        timestamp = datetime.fromisoformat(document['observed_at'].replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if not 0 <= age <= 1800:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise GuardError('Target evidence must have a UTC observation within 30 minutes') from None
    resources = document.get('resources')
    if not isinstance(resources, dict) or set(resources) != {'volumes', 'containers'}:
        raise GuardError('Target resource inventory missing')
    expected_labels = dict(zip(LABELS, (PROJECT, ENVIRONMENT, identity['owner'])))
    for kind in ('volumes', 'containers'):
        if not isinstance(resources[kind], list):
            raise GuardError('Invalid resource inventory')
        for item in resources[kind]:
            if not isinstance(item, dict) or any(item.get('labels', {}).get(key) != value for key, value in expected_labels.items()):
                raise GuardError('Existing resource owner mismatch')
            if kind == 'volumes' and item.get('name') not in identity['volumes']:
                raise GuardError('Existing volume belongs to another environment')
    marker = document.get('db_marker')
    if marker is not None and marker != {'name': ENVIRONMENT, 'owner': identity['owner']}:
        raise GuardError('Database environment marker mismatch')
    if resources['volumes'] and marker is None:
        raise GuardError('Existing volumes require the recorded DB environment marker')


def packaged_sql_digest(libs):
    """Compare packaged SQL with the independently reviewed source SQL inventory."""
    rows = {}
    total = 0
    for jar in sorted(libs.glob('*.jar')):
        if jar.is_symlink() or not jar.is_file():
            raise GuardError('Migration JAR must be a regular non-symlink file')
        try:
            with zipfile.ZipFile(jar) as archive:
                for entry in archive.infolist():
                    if not entry.filename.startswith('db/postgresql/') or not entry.filename.endswith('.sql'):
                        continue
                    normalized = str(PurePosixPath(entry.filename))
                    if '..' in PurePosixPath(entry.filename).parts or not normalized.startswith('db/postgresql/'):
                        raise GuardError('Invalid PostgreSQL SQL resource path')
                    total += entry.file_size
                    if entry.file_size > 8 * 1024 * 1024 or total > 32 * 1024 * 1024:
                        raise GuardError('Migration SQL package exceeds review limits')
                    digest = hashlib.sha256(archive.read(entry)).hexdigest()
                    if normalized in rows and rows[normalized] != digest:
                        raise GuardError('Conflicting PostgreSQL SQL entries in migration distribution')
                    rows[normalized] = digest
        except (zipfile.BadZipFile, RuntimeError):
            raise GuardError('Invalid migration distribution JAR') from None
    if not rows:
        raise GuardError('No PostgreSQL SQL resources in migration distribution')
    return hashlib.sha256(json.dumps(sorted(rows.items()), separators=(',', ':')).encode()).hexdigest()


def image_evidence_valid(image, entry):
    """Registry digests and local immutable image IDs have distinct provenance."""
    if entry.get('image') != image or entry.get('image_verified') is not True or entry.get('arm64_verified') is not True or not entry.get('evidence'):
        return False
    if entry.get('source_basis') == DEVELOPMENT_REHEARSAL_BASIS:
        expected = APPROVED_PYTHON_REVISION if entry.get('rehearsal_purpose') == APPROVED_AI_PURPOSE else APPROVED_REHEARSAL_REVISION
        if entry.get('image_source_revision') != expected or not entry.get('source_evidence'):
            return False
    if IMAGE.fullmatch(image):
        return True
    if LOCAL_IMAGE.fullmatch(image):
        return (entry.get('architecture') == 'arm64'
                and REVISION.fullmatch(entry.get('source_revision', '')) is not None
                and entry.get('image_source_revision') == entry.get('source_revision')
                and bool(entry.get('source_evidence')))
    return False


def source_baseline_blockers(service, entry, migration=False):
    """Development rehearsal receipts never establish a Windows operating baseline."""
    issues = []
    basis = entry.get('source_basis')
    if basis == DEVELOPMENT_REHEARSAL_BASIS:
        extended = service in STAGE_SERVICES['extended'] or service + '-service' in STAGE_SERVICES['extended']
        purpose = entry.get('rehearsal_purpose')
        allowed_purposes = {APPROVED_REHEARSAL_PURPOSE}
        if extended and migration:
            allowed_purposes.add(APPROVED_EXTENDED_PURPOSE)
        elif extended:
            allowed_purposes = {APPROVED_EXTENDED_PURPOSE}
        in_scope = service.removesuffix('-service') in SERVICES if migration else (service in STAGE_SERVICES['core'] or extended and purpose == APPROVED_EXTENDED_PURPOSE)
        expected_revision = APPROVED_REHEARSAL_REVISION
        if service in STAGE_SERVICES['optional-ai'] and not migration:
            allowed_purposes = {APPROVED_AI_PURPOSE}
            in_scope = purpose == APPROVED_AI_PURPOSE
            expected_revision = APPROVED_PYTHON_REVISION
        approved = (entry.get('source_revision') == expected_revision
                    and entry.get('rehearsal_source_verified') is True
                    and bool(entry.get('approval_evidence'))
                    and purpose in allowed_purposes
                    and bool(entry.get('source_evidence')))
        if not in_scope:
            issues.append(service + ': application startup is outside the approved development rehearsal scope')
        if not approved:
            issues.append(service + ': approved development rehearsal source/approval unconfirmed')
        if entry.get('source_verified') is not False:
            issues.append(service + ': development rehearsal cannot claim Windows production source verification')
        return issues
    valid = (entry.get('source_verified') is True
             and basis in ('windows-production', 'approved-production-difference')
             and REVISION.fullmatch(entry.get('source_revision', '')) is not None
             and IMAGE.fullmatch(entry.get('windows_image', '')) is not None)
    if migration:
        valid = valid and bool(entry.get('evidence'))
    if not valid:
        issues.append(service + (': migration application operating source baseline unconfirmed' if migration else ': Windows production image/source baseline unconfirmed'))
    if basis == 'approved-production-difference' and not entry.get('difference_approval'):
        issues.append(service + (': migration source difference approval missing' if migration else ': production source difference approval missing'))
    return issues


def selected_services(stage):
    return tuple(service for entry in STAGES[:STAGES.index(stage) + 1] for service in STAGE_SERVICES[entry])


def inspect(value, stage='core', assets=ASSETS, evidence=None, environ=None):
    reject_overrides(environ)
    state = state_path(value)
    identity, runtime, images, inventory = load_state(state, assets)
    compose = compose_document(assets)
    values = {**runtime, **images}
    validate_compose(compose, identity, values, assets)
    selected = selected_services(stage)
    blockers = []
    for service in selected:
        if service not in compose['services']:
            raise GuardError('Missing stage service: ' + service)
        config = compose['services'][service]
        entry = inventory.get('services', {}).get(service, {})
        image = expand(config['image'], values)
        if not image_evidence_valid(image, entry):
            blockers.append(service + ': immutable local ARM64 image evidence missing')
        if service.endswith('-service') or service == 'api-gateway':
            blockers.extend(source_baseline_blockers(service, entry))
            external = inventory.get('external', {}).get(service, {})
            if external.get('verified') is not True or not external.get('network_probe') or not external.get('scheduler_review'):
                blockers.append(service + ': external write isolation and scheduler review evidence missing')
        if service == 'api-gateway' or service in tuple(s + '-service' for s in SERVICES):
            profile = runtime['MAC_PROFILE_' + service.upper().replace('-', '_')]
            if profile == 'UNCONFIRMED' or (service != 'api-gateway' and (service != 'community-service' and 'postgresql' not in profile.split(','))):
                blockers.append(service + ': effective PG/Spring profile unconfirmed')
    if stage != 'data':
        for service in SERVICES:
            source = inventory.get('migration_sources', {}).get(service, {})
            app = inventory.get('services', {}).get(service + '-service', {})
            blockers.extend(source_baseline_blockers(service, app, migration=True))
            if source.get('verified') is not True or not source.get('evidence') or not REVISION.fullmatch(source.get('source_revision', '')) or source.get('source_revision') != app.get('source_revision') or not re.fullmatch(r'[a-f0-9]{64}', source.get('sql_sha256', '')):
                blockers.append(service + ': PG distribution/source SQL agreement missing')
            libs = state / 'migration-distributions' / service / 'lib'
            if libs.is_symlink() or not libs.is_dir() or not any(libs.glob('*.jar')):
                blockers.append(service + ': separately built PG migration distribution missing')
            elif packaged_sql_digest(libs) != source.get('sql_sha256'):
                blockers.append(service + ': packaged PG SQL differs from reviewed source SQL hash')
    readability = inventory.get('runtime_mount_readability', {})
    if readability.get('verified') is not True or not readability.get('evidence'):
        blockers.append('Docker Desktop read-only mount/UID readability is unverified')
    if evidence is None:
        blockers.append('Live Desktop endpoint/resource/DB inspection is required under separate startup approval')
    else:
        target_evidence(identity, read_json(Path(evidence)))
        blockers.append('Imported target snapshot is not a live verification; recheck immediately before any mutation')
    baseline_services = set(selected) | ({name + '-service' for name in SERVICES} if stage != 'data' else set())
    source_modes = {name: inventory.get('services', {}).get(name, {}).get('source_basis', 'unconfirmed') for name in sorted(baseline_services) if name.endswith('-service') or name == 'api-gateway'}
    return {'project': PROJECT, 'state': str(state), 'stage': stage, 'services': list(selected),
            'sourceModes': source_modes, 'developmentRehearsal': DEVELOPMENT_REHEARSAL_BASIS in source_modes.values(),
            'windowsLiveVerified': False,
            'staticOnly': True, 'runtimeVerified': False, 'readyToExecute': False, 'blockers': blockers,
            'note': 'Declarations and local files only. This tool never contacts Docker, a database or the network.'}


def plan(value, stage='core', assets=ASSETS, evidence=None, environ=None):
    report = inspect(value, stage, assets, evidence, environ)
    state = state_path(value)
    identity = read_json(state / 'identity.json')
    prefix = ['docker', '--context', identity['context'], 'compose', '--project-name', PROJECT,
              '--env-file', str(state / 'runtime.env'), '--env-file', str(state / 'images.env'), '-f', str(assets / 'compose.yaml')]
    def command(*parts):
        return shlex.join(prefix + list(parts))
    compose = compose_document(assets)
    migration_commands = [command('--profile', 'migrations', 'run', '--rm', 'migration-' + service, *compose['services']['migration-' + service]['command'][:-1], operation) for service in SERVICES for operation in ('migrate', 'validate')]
    steps = [
        {'stage': 'data', 'after': 'Separate D startup approval, live Desktop identity and absent/new owned volumes; C image preparation is separate',
         'candidate_commands': [command('up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '180', *STAGE_SERVICES['data'])],
         'verify': 'New DB marker, per-service schema/role/privilege, public.vector/pg_trgm, logical WAL and replication role; init.sql runs only on a new PG volume'},
        {'stage': 'migrations', 'after': 'DB marker/owner and schema initialization; all five PG migration distributions match the approved application source/SQL',
         'candidate_commands': migration_commands,
         'verify': 'Service-specific PostgresqlMigration, loopback PG network namespace, migrate/validate and Flyway history; never use API JAR or MySQL migration main'},
        {'stage': 'events', 'after': 'Five PG validate results; review topics.json and cdc-prerequisites.sql, create exact topics/heartbeat tables/publications before connector registration',
         'candidate_commands': [command('--profile', 'events', 'up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '180', 'connect')],
         'verify': 'Register reviewed connectors.json only after DB/plugin/publication/slot identity; tasks RUNNING and offset ready before generating a new no_data business event'},
        {'stage': 'core', 'after': 'Per-service approved source basis/profile (development rehearsal or production baseline), external/scheduler isolation gates; CDC ready',
         'candidate_commands': [command('--profile', 'core', 'up', '-d', '--pull', 'never', *STAGE_SERVICES['core'])],
         'verify': 'Readiness and actual isolated Gateway API→User/Animal→PG/Redis, not container running alone'},
        {'stage': 'extended', 'after': 'Core business request and DB checks; additional service external gates',
         'candidate_commands': [command('--profile', 'core', '--profile', 'extended', 'up', '-d', '--pull', 'never', *STAGE_SERVICES['extended'])],
         'verify': 'Per-service actual requests, state and failure boundaries'},
        {'stage': 'optional-ai', 'after': 'Separately approved ARM64/native dependencies and AI integration; no model acquisition in B',
         'candidate_commands': [command('--profile', 'optional-ai', 'up', '-d', '--pull', 'never', *STAGE_SERVICES['optional-ai'])],
         'verify': 'Python/photo startup, authenticated internal calls; model quality and GPU/MPS are separate'},
    ]
    report['executionSupported'] = False
    report['steps'] = steps[:STAGES.index(stage) + 1]
    report['final_verification'] = 'After Connect readiness, generate new synthetic committed/rolled-back Outbox events and verify exact topic/key/header/payload; then real mail/signup/login and User favourite→Animal→User compensation with final DB/processed-event state. Legacy dev tools target dev and must not be invoked unchanged.'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'inspect', 'check', 'plan'))
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/pawbridge/mac-rehearsal')
    parser.add_argument('--stage', choices=STAGES, default='core')
    parser.add_argument('--target-evidence', type=Path, help='Private imported observation, never treated as live execution approval')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare(args.state)
        elif args.command == 'plan':
            result = plan(args.state, args.stage, evidence=args.target_evidence)
        else:
            result = inspect(args.state, args.stage, evidence=args.target_evidence)
        print(json.dumps(result, indent=2))
        return 2 if args.command == 'check' and result['blockers'] else 0
    except (GuardError, OSError) as error:
        print('Mac rehearsal blocked: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
