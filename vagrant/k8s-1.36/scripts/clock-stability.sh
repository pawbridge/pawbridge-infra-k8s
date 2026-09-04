#!/usr/bin/env bash

set -Eeuo pipefail
umask 022

readonly CLOCKSOURCE_DIR="/sys/devices/system/clocksource/clocksource0"
readonly ACTIVE_CLOCKSOURCE="${CLOCKSOURCE_DIR}/current_clocksource"
readonly AVAILABLE_CLOCKSOURCES="${CLOCKSOURCE_DIR}/available_clocksource"
readonly CLOCK_BASELINE_HELPER="/usr/local/sbin/pawbridge-validate-clock-baseline"
readonly CLOCK_BASELINE_UNIT="/etc/systemd/system/pawbridge-clock-baseline.service"
readonly LEGACY_CLOCKSOURCE_HELPER="/usr/local/sbin/pawbridge-configure-clocksource"
readonly LEGACY_CLOCKSOURCE_UNIT="/etc/systemd/system/pawbridge-clocksource.service"
readonly TIME_SYNC_HELPER="/usr/local/sbin/pawbridge-wait-for-time-sync"
readonly TIME_SYNC_UNIT="/etc/systemd/system/pawbridge-time-sync.service"
readonly MAX_SAFE_FREQUENCY_PPM="500"
readonly MAX_SAFE_LIVE_CORRECTION_SECONDS="0.1"

trap 'echo "clock stability provisioning failed at line ${LINENO}" >&2' ERR

declare -a staged_files=()

cleanup_staged_files() {
  local staged_file
  for staged_file in "${staged_files[@]}"; do
    rm -f -- "${staged_file}"
  done
}
trap cleanup_staged_files EXIT

stage_file() {
  REPLY="$(mktemp "${1}.XXXXXX")"
  staged_files+=("${REPLY}")
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "clock stability provisioning must run as root" >&2
  exit 1
fi
if [[ ! -r "${ACTIVE_CLOCKSOURCE}" || ! -r "${AVAILABLE_CLOCKSOURCES}" ]]; then
  echo "Linux clocksource metadata is unavailable" >&2
  exit 1
fi

current_clocksource="$(< "${ACTIVE_CLOCKSOURCE}")"
if [[ -z "${current_clocksource}" ]] || ! grep -qw -- "${current_clocksource}" "${AVAILABLE_CLOCKSOURCES}"; then
  echo "active clocksource is not listed as available: ${current_clocksource:-unknown}" >&2
  exit 1
fi

chrony_is_installed=false
if systemctl list-unit-files chrony.service --no-legend 2>/dev/null | grep -q '^chrony.service'; then
  chrony_is_installed=true
fi

runtime_is_active=false
for runtime_unit in containerd.service kubelet.service; do
  if systemctl is-active --quiet "${runtime_unit}"; then
    runtime_is_active=true
  fi
done

install -d -m 0755 /etc/systemd/system/containerd.service.d /etc/systemd/system/kubelet.service.d

stage_file "${CLOCK_BASELINE_HELPER}"
clock_baseline_helper_stage="${REPLY}"
stage_file "${TIME_SYNC_HELPER}"
time_sync_helper_stage="${REPLY}"
stage_file "${CLOCK_BASELINE_UNIT}"
clock_baseline_unit_stage="${REPLY}"
stage_file "${TIME_SYNC_UNIT}"
time_sync_unit_stage="${REPLY}"
stage_file /etc/systemd/system/containerd.service.d/10-pawbridge-time-sync.conf
containerd_drop_in_stage="${REPLY}"
stage_file /etc/systemd/system/kubelet.service.d/10-pawbridge-time-sync.conf
kubelet_drop_in_stage="${REPLY}"

cat > "${clock_baseline_helper_stage}" <<'HELPER'
#!/usr/bin/env bash

set -Eeuo pipefail

readonly CLOCKSOURCE_DIR="/sys/devices/system/clocksource/clocksource0"
readonly ACTIVE_CLOCKSOURCE="${CLOCKSOURCE_DIR}/current_clocksource"
readonly AVAILABLE_CLOCKSOURCES="${CLOCKSOURCE_DIR}/available_clocksource"
readonly CHRONY_DRIFT_FILE="/var/lib/chrony/chrony.drift"
readonly MAX_SAFE_FREQUENCY_PPM="500"

if [[ ! -r "${ACTIVE_CLOCKSOURCE}" || ! -r "${AVAILABLE_CLOCKSOURCES}" ]]; then
  echo "Linux clocksource metadata is unavailable" >&2
  exit 1
fi

current_clocksource="$(< "${ACTIVE_CLOCKSOURCE}")"
if [[ -z "${current_clocksource}" ]] || ! grep -qw -- "${current_clocksource}" "${AVAILABLE_CLOCKSOURCES}"; then
  echo "active clocksource is not listed as available: ${current_clocksource:-unknown}" >&2
  exit 1
fi

if [[ -r "${CHRONY_DRIFT_FILE}" ]] && awk -v limit="${MAX_SAFE_FREQUENCY_PPM}" '
  NR == 1 {
    frequency = $1
    if (frequency < 0) frequency = -frequency
    exit !(frequency > limit)
  }
' "${CHRONY_DRIFT_FILE}"; then
  mv --backup=numbered --force "${CHRONY_DRIFT_FILE}" "${CHRONY_DRIFT_FILE}.rejected"
  echo "rejected Chrony drift above ${MAX_SAFE_FREQUENCY_PPM} ppm" >&2
fi

printf 'validated active clocksource: %s\n' "${current_clocksource}"
HELPER
chmod 0755 "${clock_baseline_helper_stage}"

cat > "${time_sync_helper_stage}" <<'HELPER'
#!/usr/bin/env bash

set -Eeuo pipefail

readonly MAX_SAFE_FREQUENCY_PPM="500"

for runtime_unit in containerd.service kubelet.service; do
  if systemctl is-active --quiet "${runtime_unit}"; then
    echo "skipping time correction while ${runtime_unit} is active"
    exit 0
  fi
done

chronyc makestep 0.1 1
chronyc burst 4/4
chronyc waitsync 60 0.1 "${MAX_SAFE_FREQUENCY_PPM}" 2

frequency_ppm="$(chronyc tracking | awk '/^Frequency/ { print $3 }')"
if [[ -z "${frequency_ppm}" ]] || ! awk -v frequency="${frequency_ppm}" -v limit="${MAX_SAFE_FREQUENCY_PPM}" 'BEGIN {
  if (frequency < 0) frequency = -frequency
  exit !(frequency <= limit)
}'; then
  echo "Chrony frequency is outside the safe range: ${frequency_ppm:-unknown} ppm" >&2
  exit 1
fi
HELPER
chmod 0755 "${time_sync_helper_stage}"

cat > "${clock_baseline_unit_stage}" <<'UNIT'
[Unit]
Description=PawBridge clock baseline validation
DefaultDependencies=no
After=local-fs.target
Before=chrony.service chronyd.service containerd.service kubelet.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pawbridge-validate-clock-baseline
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
UNIT

cat > "${time_sync_unit_stage}" <<'UNIT'
[Unit]
Description=PawBridge time synchronization gate
Requires=chrony.service pawbridge-clock-baseline.service
Wants=network-online.target
After=chrony.service network-online.target pawbridge-clock-baseline.service
Before=containerd.service kubelet.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pawbridge-wait-for-time-sync
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

cat > "${containerd_drop_in_stage}" <<'UNIT'
[Unit]
Requires=pawbridge-time-sync.service
After=pawbridge-time-sync.service
UNIT
cat > "${kubelet_drop_in_stage}" <<'UNIT'
[Unit]
Requires=pawbridge-time-sync.service
After=pawbridge-time-sync.service
UNIT

chmod 0644 "${clock_baseline_unit_stage}" "${time_sync_unit_stage}" \
  "${containerd_drop_in_stage}" "${kubelet_drop_in_stage}"
mv -- "${clock_baseline_helper_stage}" "${CLOCK_BASELINE_HELPER}"
mv -- "${time_sync_helper_stage}" "${TIME_SYNC_HELPER}"
mv -- "${clock_baseline_unit_stage}" "${CLOCK_BASELINE_UNIT}"
mv -- "${time_sync_unit_stage}" "${TIME_SYNC_UNIT}"
mv -- "${containerd_drop_in_stage}" /etc/systemd/system/containerd.service.d/10-pawbridge-time-sync.conf
mv -- "${kubelet_drop_in_stage}" /etc/systemd/system/kubelet.service.d/10-pawbridge-time-sync.conf

systemctl daemon-reload
systemctl enable pawbridge-clock-baseline.service
systemctl enable pawbridge-time-sync.service

if [[ -e "${LEGACY_CLOCKSOURCE_UNIT}" ]]; then
  systemctl disable pawbridge-clocksource.service
  rm --force "${LEGACY_CLOCKSOURCE_UNIT}" "${LEGACY_CLOCKSOURCE_HELPER}"
  systemctl daemon-reload
fi

if [[ "${runtime_is_active}" == "true" ]]; then
  if [[ "${chrony_is_installed}" != "true" ]]; then
    echo "refusing to activate the time gate after workloads started without Chrony health evidence" >&2
    exit 1
  fi

  tracking_output="$(chronyc tracking)"
  frequency_ppm="$(awk '/^Frequency/ { print $3 }' <<< "${tracking_output}")"
  correction_seconds="$(awk '/^System time/ { print $4 }' <<< "${tracking_output}")"
  leap_status="$(awk -F ': ' '/^Leap status/ { print $2 }' <<< "${tracking_output}")"

  if [[ -z "${frequency_ppm}" || -z "${correction_seconds}" || "${leap_status}" != "Normal" ]] ||
    ! awk -v frequency="${frequency_ppm}" -v correction="${correction_seconds}" \
      -v frequency_limit="${MAX_SAFE_FREQUENCY_PPM}" -v correction_limit="${MAX_SAFE_LIVE_CORRECTION_SECONDS}" 'BEGIN {
        if (frequency < 0) frequency = -frequency
        if (correction < 0) correction = -correction
        exit !(frequency <= frequency_limit && correction <= correction_limit)
      }'; then
    echo "refusing unsafe time gate activation while workloads are active" >&2
    exit 1
  fi
fi

if [[ "${runtime_is_active}" == "true" ]]; then
  echo "clock stability gate installed; activation is deferred until the next runtime start"
  exit 0
fi

systemctl restart pawbridge-clock-baseline.service

if [[ "${chrony_is_installed}" == "true" ]]; then
  systemctl start chrony.service
  systemctl restart pawbridge-time-sync.service
fi

echo "clock stability provisioning completed with ${current_clocksource}"
