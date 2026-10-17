"""Readable operating resource names; no runtime or data mutation."""
import copy

ROLES = {
    'postgres': 'postgres', 'redis': 'redis', 'kafka': 'kafka',
    'connect': 'kafka-connect', 'api-gateway': 'gateway',
    'user-service': 'user', 'animal-service': 'animal',
    'community-service': 'community', 'store-service': 'store',
    'payment-service': 'payment', 'photo-service': 'photo',
    'python-ai-service': 'python-api', 'native-relay': 'native-ai-relay',
    'local-native-access': 'native-db-gallery-bridge',
    'backup': 'backup-scheduler', 'storage-init': 'storage-init',
}


def configure(document):
    doc = copy.deepcopy(document)
    for service, spec in doc['services'].items():
        spec['container_name'] = '${OPERATING_PROJECT}-' + ROLES[service]
    for name, spec in doc.get('networks', {}).items():
        spec['name'] = '${OPERATING_PROJECT}-' + name
    return doc
