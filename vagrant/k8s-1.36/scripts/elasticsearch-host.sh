#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 027

readonly MINIMUM_MAX_MAP_COUNT=262144
readonly SYSCTL_FILE=/etc/sysctl.d/99-pawbridge-elasticsearch.conf

trap 'echo "Elasticsearch host provisioning failed at line ${LINENO}" >&2' ERR

if [[ "${EUID}" -ne 0 ]]; then
  echo "Elasticsearch host provisioning must run as root" >&2
  exit 1
fi

install -d -m 0755 /etc/sysctl.d
current_max_map_count="$(sysctl -n vm.max_map_count)"

if [[ ! "${current_max_map_count}" =~ ^[0-9]+$ ]]; then
  echo "vm.max_map_count must be numeric" >&2
  exit 1
fi

target_max_map_count="${current_max_map_count}"
if (( target_max_map_count < MINIMUM_MAX_MAP_COUNT )); then
  target_max_map_count="${MINIMUM_MAX_MAP_COUNT}"
fi

cat > "${SYSCTL_FILE}" <<SYSCTL
vm.max_map_count = ${target_max_map_count}
SYSCTL

sysctl --load "${SYSCTL_FILE}" >/dev/null
current_max_map_count="$(sysctl -n vm.max_map_count)"

if [[ ! "${current_max_map_count}" =~ ^[0-9]+$ ]] || (( current_max_map_count < MINIMUM_MAX_MAP_COUNT )); then
  echo "vm.max_map_count must be at least ${MINIMUM_MAX_MAP_COUNT}" >&2
  exit 1
fi

echo "Elasticsearch host provisioning completed"
