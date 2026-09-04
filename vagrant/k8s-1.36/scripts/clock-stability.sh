#!/usr/bin/env bash

set -Eeuo pipefail
umask 022

readonly CLOCKSOURCE_DIR="/sys/devices/system/clocksource/clocksource0"
readonly ACTIVE_CLOCKSOURCE="${CLOCKSOURCE_DIR}/current_clocksource"
readonly AVAILABLE_CLOCKSOURCES="${CLOCKSOURCE_DIR}/available_clocksource"
readonly CLOCKSOURCE_HELPER="/usr/local/sbin/pawbridge-configure-clocksource"
readonly CLOCKSOURCE_UNIT="/etc/systemd/system/pawbridge-clocksource.service"
readonly TIME_SYNC_HELPER="/usr/local/sbin/pawbridge-wait-for-time-sync"
readonly TIME_SYNC_UNIT="/etc/systemd/system/pawbridge-time-sync.service"
readonly MAX_SAFE_FREQUENCY_PPM="500"
readonly MAX_SAFE_LIVE_CORRECTION_SECONDS="0.1"

trap 'echo "clock stability provisioning failed at line ${LINENO}" >&2' ERR

if [[ "${EUID}" -ne 0 ]]; then
  echo "clock stability provisioning must run as root" >&2
  exit 1
fi
if [[ ! -r "${AVAILABLE_CLOCKSOURCES}" ]] || ! grep -qw "kvm-clock" "${AVAILABLE_CLOCKSOURCES}"; then
  echo "kvm-clock is unavailable; verify the VirtualBox KVM paravirtualization provider" >&2
  exit 1
fi
if ! command -v busybox >/dev/null 2>&1; then
  echo "busybox is required to reset the kernel clock adjustment" >&2
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

clock_baseline_needs_activation=false
if ! systemctl is-active --quiet pawbridge-clocksource.service ||
  [[ "$(< "${ACTIVE_CLOCKSOURCE}")" != "kvm-clock" ]] ||
  { [[ "${chrony_is_installed}" == "true" ]] && ! systemctl is-active --quiet pawbridge-time-sync.service; }; then
  clock_baseline_needs_activation=true
fi

if [[ "${clock_baseline_needs_activation}" == "true" && "${runtime_is_active}" == "true" ]]; then
  if [[ "${chrony_is_installed}" != "true" ]]; then
    echo "refusing to switch clocksource after workloads started without Chrony health evidence" >&2
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
    echo "refusing unsafe live clocksource transition while workloads are active" >&2
    exit 1
  fi
fi

cat > "${CLOCKSOURCE_HELPER}" <<'HELPER'
#!/usr/bin/env bash

set -Eeuo pipefail

readonly CLOCKSOURCE_DIR="/sys/devices/system/clocksource/clocksource0"
readonly ACTIVE_CLOCKSOURCE="${CLOCKSOURCE_DIR}/current_clocksource"
readonly AVAILABLE_CLOCKSOURCES="${CLOCKSOURCE_DIR}/available_clocksource"
readonly CHRONY_DRIFT_FILE="/var/lib/chrony/chrony.drift"
readonly MAX_SAFE_FREQUENCY_PPM="500"

if ! grep -qw "kvm-clock" "${AVAILABLE_CLOCKSOURCES}"; then
  echo "kvm-clock is unavailable" >&2
  exit 1
fi

if [[ -r "${CHRONY_DRIFT_FILE}" ]] && awk -v limit="${MAX_SAFE_FREQUENCY_PPM}" '
  NR == 1 {
    frequency = $1
    if (frequency < 0) frequency = -frequency
    exit !(frequency > limit)
  }
' "${CHRONY_DRIFT_FILE}"; then
  cp --preserve=mode,ownership,timestamps --backup=numbered --force \
    "${CHRONY_DRIFT_FILE}" "${CHRONY_DRIFT_FILE}.rejected"
  rm --force "${CHRONY_DRIFT_FILE}"
  echo "rejected Chrony drift above ${MAX_SAFE_FREQUENCY_PPM} ppm" >&2
fi

printf '%s\n' "kvm-clock" > "${ACTIVE_CLOCKSOURCE}"
/usr/bin/busybox adjtimex -t 10000 -f 0 >/dev/null
HELPER
chmod 0755 "${CLOCKSOURCE_HELPER}"

cat > "${TIME_SYNC_HELPER}" <<'HELPER'
#!/usr/bin/env bash

set -Eeuo pipefail

readonly MAX_SAFE_FREQUENCY_PPM="500"

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
chmod 0755 "${TIME_SYNC_HELPER}"

cat > "${CLOCKSOURCE_UNIT}" <<'UNIT'
[Unit]
Description=PawBridge KVM clocksource baseline
DefaultDependencies=no
After=local-fs.target
Before=chrony.service chronyd.service containerd.service kubelet.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pawbridge-configure-clocksource
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
UNIT

cat > "${TIME_SYNC_UNIT}" <<'UNIT'
[Unit]
Description=PawBridge time synchronization gate
Requires=chrony.service pawbridge-clocksource.service
Wants=network-online.target
After=chrony.service network-online.target pawbridge-clocksource.service
Before=containerd.service kubelet.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pawbridge-wait-for-time-sync
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

install -d -m 0755 /etc/systemd/system/containerd.service.d /etc/systemd/system/kubelet.service.d
cat > /etc/systemd/system/containerd.service.d/10-pawbridge-time-sync.conf <<'UNIT'
[Unit]
Requires=pawbridge-time-sync.service
After=pawbridge-time-sync.service
UNIT
cat > /etc/systemd/system/kubelet.service.d/10-pawbridge-time-sync.conf <<'UNIT'
[Unit]
Requires=pawbridge-time-sync.service
After=pawbridge-time-sync.service
UNIT

systemctl daemon-reload
systemctl enable pawbridge-clocksource.service
systemctl enable pawbridge-time-sync.service

clock_baseline_was_restarted=false
if ! systemctl is-active --quiet pawbridge-clocksource.service ||
  [[ "$(< "${ACTIVE_CLOCKSOURCE}")" != "kvm-clock" ]]; then
  systemctl restart pawbridge-clocksource.service
  clock_baseline_was_restarted=true
fi

if [[ "$(< "${ACTIVE_CLOCKSOURCE}")" != "kvm-clock" ]]; then
  echo "failed to activate kvm-clock" >&2
  exit 1
fi

if [[ "${chrony_is_installed}" == "true" ]]; then
  if [[ "${clock_baseline_was_restarted}" == "true" && "${runtime_is_active}" == "false" ]]; then
    systemctl restart chrony.service
  else
    systemctl start chrony.service
  fi
  if [[ "${clock_baseline_was_restarted}" == "true" ]] ||
    ! systemctl is-active --quiet pawbridge-time-sync.service; then
    systemctl restart pawbridge-time-sync.service
  fi
fi

echo "clock stability provisioning completed with kvm-clock"
