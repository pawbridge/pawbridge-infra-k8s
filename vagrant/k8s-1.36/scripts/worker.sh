#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly NODE_NAME="${1:?worker node name is required}"
readonly CONTROL_PLANE_IP="${2:?control-plane IP is required}"
readonly JOIN_SERVER_PORT="${3:?join server port is required}"
readonly JOIN_URL="http://${CONTROL_PLANE_IP}:${JOIN_SERVER_PORT}/join-command"
readonly CONTAINERD_SOCKET="unix:///run/containerd/containerd.sock"

trap 'echo "worker provisioning failed at line ${LINENO}" >&2' ERR

if [[ "${EUID}" -ne 0 ]]; then
  echo "worker provisioning must run as root" >&2
  exit 1
fi
if [[ -f /etc/kubernetes/kubelet.conf ]]; then
  systemctl is-active --quiet kubelet
  echo "${NODE_NAME} is already joined"
  exit 0
fi

join_command=""
for attempt in $(seq 1 180); do
  if join_command="$(curl --fail --show-error --silent --max-time 5 "${JOIN_URL}")"; then
    break
  fi
  echo "waiting for the control-plane join endpoint (${attempt}/180)"
  sleep 10
done

join_pattern="^kubeadm join ${CONTROL_PLANE_IP//./\\.}:6443 --token ([a-z0-9]{6}\\.[a-z0-9]{16}) --discovery-token-ca-cert-hash sha256:([a-f0-9]{64})$"
if [[ ! "${join_command}" =~ ${join_pattern} ]]; then
  echo "the join endpoint returned an invalid command" >&2
  exit 1
fi
readonly BOOTSTRAP_TOKEN="${BASH_REMATCH[1]}"
readonly CA_CERT_HASH="${BASH_REMATCH[2]}"

kubeadm join "${CONTROL_PLANE_IP}:6443" \
  --token "${BOOTSTRAP_TOKEN}" \
  --discovery-token-ca-cert-hash "sha256:${CA_CERT_HASH}" \
  --node-name "${NODE_NAME}" \
  --cri-socket "${CONTAINERD_SOCKET}"
systemctl is-active --quiet kubelet
echo "worker join completed for ${NODE_NAME}"
