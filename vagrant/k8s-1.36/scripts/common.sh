#!/usr/bin/env bash

set -Eeuo pipefail
umask 022

readonly VERSIONS_FILE="/tmp/pawbridge-k136-versions.env"
readonly NODE_NAME="${1:?node name is required}"
readonly NODE_IP="${2:?node IP is required}"
readonly EXPECTED_DISK_GIB="${3:?expected disk GiB is required}"
readonly CONTAINERD_SOCKET="unix:///run/containerd/containerd.sock"

trap 'echo "common provisioning failed at line ${LINENO}" >&2' ERR

if [[ "${EUID}" -ne 0 ]]; then
  echo "common provisioning must run as root" >&2
  exit 1
fi

if [[ ! -r "${VERSIONS_FILE}" ]]; then
  echo "missing ${VERSIONS_FILE}" >&2
  exit 1
fi

# This file is version-controlled and contains versions/checksums only.
# shellcheck disable=SC1090
source "${VERSIONS_FILE}"

required_variables=(KUBERNETES_VERSION KUBERNETES_PACKAGE_VERSION KUBERNETES_REPOSITORY_MINOR CONTAINERD_VERSION CONTAINERD_SHA256 RUNC_VERSION RUNC_SHA256 CNI_PLUGINS_VERSION CNI_PLUGINS_SHA256 CRICTL_VERSION CRICTL_SHA256)
for variable_name in "${required_variables[@]}"; do
  if [[ -z "${!variable_name:-}" ]]; then
    echo "missing required version variable: ${variable_name}" >&2
    exit 1
  fi
done

for checksum_name in CONTAINERD_SHA256 RUNC_SHA256 CNI_PLUGINS_SHA256 CRICTL_SHA256; do
  if [[ ! "${!checksum_name}" =~ ^[a-f0-9]{64}$ ]]; then
    echo "invalid SHA-256 value: ${checksum_name}" >&2
    exit 1
  fi
done

if ! ip -4 address show | grep -Fq "${NODE_IP}/"; then
  echo "approved node IP ${NODE_IP} is not configured on ${NODE_NAME}" >&2
  exit 1
fi
if ! ip route show default | grep -q '^default '; then
  echo "NAT default route is missing" >&2
  exit 1
fi

ROOT_BYTES="$(findmnt --bytes --noheadings --output SIZE /)"
readonly ROOT_BYTES
readonly MINIMUM_ROOT_BYTES="$(( (EXPECTED_DISK_GIB - 5) * 1024 * 1024 * 1024 ))"
if (( ROOT_BYTES < MINIMUM_ROOT_BYTES )); then
  echo "root filesystem was not expanded: expected at least $((EXPECTED_DISK_GIB - 5)) GiB" >&2
  exit 1
fi

download_and_verify() {
  local url="${1:?download URL is required}"
  local expected_sha256="${2:?expected SHA-256 is required}"
  local destination="${3:?download destination is required}"

  curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
    --retry 5 --retry-all-errors --connect-timeout 15 --max-time 900 \
    --output "${destination}" "${url}"
  printf '%s  %s\n' "${expected_sha256}" "${destination}" | sha256sum --check --strict
}

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends apt-transport-https ca-certificates chrony conntrack curl ebtables ethtool gnupg iproute2 socat

timedatectl set-timezone Asia/Seoul
systemctl enable --now chrony
chronyc makestep || true

swapoff --all
sed -ri '/\sswap\s/s/^([^#])/#\1/' /etc/fstab

install -d -m 0755 /etc/modules-load.d /etc/sysctl.d
cat > /etc/modules-load.d/pawbridge-kubernetes.conf <<'MODULES'
overlay
br_netfilter
MODULES
modprobe overlay
modprobe br_netfilter

cat > /etc/sysctl.d/99-pawbridge-kubernetes.conf <<'SYSCTL'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
SYSCTL
sysctl --system >/dev/null

sed -i '/# BEGIN PAWBRIDGE K136/,/# END PAWBRIDGE K136/d' /etc/hosts
cat >> /etc/hosts <<'HOSTS'
# BEGIN PAWBRIDGE K136
192.168.57.11 pawbridge-k136-cp1
192.168.57.12 pawbridge-k136-w1
192.168.57.13 pawbridge-k136-w2
# END PAWBRIDGE K136
HOSTS

TEMP_DIR="$(mktemp --directory /tmp/pawbridge-k136.XXXXXX)"
readonly TEMP_DIR
trap 'rm -rf "${TEMP_DIR}"' EXIT

readonly CONTAINERD_ARCHIVE="${TEMP_DIR}/containerd.tar.gz"
download_and_verify "https://github.com/containerd/containerd/releases/download/v${CONTAINERD_VERSION}/containerd-${CONTAINERD_VERSION}-linux-amd64.tar.gz" "${CONTAINERD_SHA256}" "${CONTAINERD_ARCHIVE}"
tar --extract --gzip --file "${CONTAINERD_ARCHIVE}" --directory /usr/local

readonly RUNC_BINARY="${TEMP_DIR}/runc.amd64"
download_and_verify "https://github.com/opencontainers/runc/releases/download/v${RUNC_VERSION}/runc.amd64" "${RUNC_SHA256}" "${RUNC_BINARY}"
install -m 0755 "${RUNC_BINARY}" /usr/local/sbin/runc

readonly CNI_ARCHIVE="${TEMP_DIR}/cni-plugins.tgz"
download_and_verify "https://github.com/containernetworking/plugins/releases/download/v${CNI_PLUGINS_VERSION}/cni-plugins-linux-amd64-v${CNI_PLUGINS_VERSION}.tgz" "${CNI_PLUGINS_SHA256}" "${CNI_ARCHIVE}"

readonly CRICTL_ARCHIVE="${TEMP_DIR}/crictl.tar.gz"
download_and_verify "https://github.com/kubernetes-sigs/cri-tools/releases/download/v${CRICTL_VERSION}/crictl-v${CRICTL_VERSION}-linux-amd64.tar.gz" "${CRICTL_SHA256}" "${CRICTL_ARCHIVE}"
tar --extract --gzip --file "${CRICTL_ARCHIVE}" --directory /usr/local/bin crictl

cat > /etc/systemd/system/containerd.service <<'UNIT'
[Unit]
Description=containerd container runtime
Documentation=https://containerd.io
After=network.target local-fs.target

[Service]
ExecStartPre=-/sbin/modprobe overlay
ExecStart=/usr/local/bin/containerd
Type=notify
Delegate=yes
KillMode=process
Restart=always
RestartSec=5
LimitNPROC=infinity
LimitCORE=0
LimitNOFILE=1048576
TasksMax=infinity
OOMScoreAdjust=-999

[Install]
WantedBy=multi-user.target
UNIT

install -d -m 0755 /etc/containerd
containerd config default > "${TEMP_DIR}/containerd-config.toml"
if ! grep -q 'SystemdCgroup = false' "${TEMP_DIR}/containerd-config.toml"; then
  echo "containerd default config does not contain the expected cgroup setting" >&2
  exit 1
fi
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' "${TEMP_DIR}/containerd-config.toml"
grep -q 'SystemdCgroup = true' "${TEMP_DIR}/containerd-config.toml"
install -m 0644 "${TEMP_DIR}/containerd-config.toml" /etc/containerd/config.toml

systemctl daemon-reload
systemctl enable --now containerd
systemctl is-active --quiet containerd

cat > /etc/crictl.yaml <<EOF
runtime-endpoint: ${CONTAINERD_SOCKET}
image-endpoint: ${CONTAINERD_SOCKET}
timeout: 30
debug: false
EOF
crictl info >/dev/null

install -d -m 0755 /etc/apt/keyrings
curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 --retry 5 --retry-all-errors \
  --output "${TEMP_DIR}/kubernetes-release.key" "https://pkgs.k8s.io/core:/stable:/${KUBERNETES_REPOSITORY_MINOR}/deb/Release.key"
gpg --dearmor --yes --output /etc/apt/keyrings/kubernetes-apt-keyring.gpg "${TEMP_DIR}/kubernetes-release.key"
cat > /etc/apt/sources.list.d/kubernetes.list <<EOF
deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${KUBERNETES_REPOSITORY_MINOR}/deb/ /
EOF

apt-get update
apt-get install --yes --allow-downgrades "kubeadm=${KUBERNETES_PACKAGE_VERSION}" "kubectl=${KUBERNETES_PACKAGE_VERSION}" "kubelet=${KUBERNETES_PACKAGE_VERSION}"
apt-mark hold kubeadm kubectl kubelet

# kubelet dependencies may install their own CNI binaries. Install the pinned
# upstream archive afterwards and hold the distro packages to prevent overwrite.
install -d -m 0755 /opt/cni/bin
tar --extract --gzip --file "${CNI_ARCHIVE}" --directory /opt/cni/bin
for dependency_name in cri-tools kubernetes-cni; do
  if dpkg-query --show "${dependency_name}" >/dev/null 2>&1; then
    apt-mark hold "${dependency_name}"
  fi
done

for package_name in kubeadm kubectl kubelet; do
  installed_version="$(dpkg-query --show --showformat='${Version}' "${package_name}")"
  if [[ "${installed_version}" != "${KUBERNETES_PACKAGE_VERSION}" ]]; then
    echo "${package_name} version mismatch: ${installed_version}" >&2
    exit 1
  fi
done

cat > /etc/default/kubelet <<EOF
KUBELET_EXTRA_ARGS=--node-ip=${NODE_IP}
EOF
systemctl daemon-reload
systemctl enable --now kubelet

containerd --version | grep -F "v${CONTAINERD_VERSION}"
runc --version | grep -F "runc version ${RUNC_VERSION}"
crictl --version | grep -F "v${CRICTL_VERSION}"
/opt/cni/bin/bridge --version 2>&1 | grep -F "v${CNI_PLUGINS_VERSION}"
kubeadm version -o short | grep -Fx "v${KUBERNETES_VERSION}"

echo "common provisioning completed for ${NODE_NAME} (${NODE_IP})"
