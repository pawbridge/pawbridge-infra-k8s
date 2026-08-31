#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly VERSIONS_FILE="/tmp/pawbridge-k136-versions.env"
readonly NODE_NAME="${1:?control-plane node name is required}"
readonly NODE_IP="${2:?control-plane node IP is required}"
readonly CLUSTER_NAME="${3:?cluster name is required}"
readonly POD_CIDR="${4:?pod CIDR is required}"
readonly SERVICE_CIDR="${5:?service CIDR is required}"
readonly JOIN_SERVER_PORT="${6:?join server port is required}"
readonly KUBECONFIG_PATH="/etc/kubernetes/admin.conf"
readonly JOIN_DIRECTORY="/run/pawbridge-kubeadm"

trap 'echo "control-plane provisioning failed at line ${LINENO}" >&2' ERR

if [[ "${EUID}" -ne 0 ]]; then
  echo "control-plane provisioning must run as root" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${VERSIONS_FILE}"
for variable_name in KUBERNETES_VERSION CALICO_VERSION CALICO_OPERATOR_SHA256 CALICO_OPERATOR_IMAGE CALICO_OPERATOR_DIGEST; do
  if [[ -z "${!variable_name:-}" ]]; then
    echo "missing required version variable: ${variable_name}" >&2
    exit 1
  fi
done
if [[ ! "${CALICO_OPERATOR_SHA256}" =~ ^[a-f0-9]{64}$ ]]; then
  echo "invalid Calico operator SHA-256" >&2
  exit 1
fi
if [[ ! "${CALICO_OPERATOR_IMAGE}" =~ ^quay\.io/tigera/operator:v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "invalid Calico operator image" >&2
  exit 1
fi
if [[ ! "${CALICO_OPERATOR_DIGEST}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "invalid Calico operator digest" >&2
  exit 1
fi

if [[ ! -f "${KUBECONFIG_PATH}" ]]; then
  cat > /tmp/kubeadm-init.yaml <<EOF
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: ${NODE_IP}
  bindPort: 6443
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
  name: ${NODE_NAME}
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
clusterName: ${CLUSTER_NAME}
controlPlaneEndpoint: ${NODE_IP}:6443
kubernetesVersion: v${KUBERNETES_VERSION}
networking:
  dnsDomain: cluster.local
  podSubnet: ${POD_CIDR}
  serviceSubnet: ${SERVICE_CIDR}
EOF
  kubeadm config validate --config /tmp/kubeadm-init.yaml
  kubeadm init --config /tmp/kubeadm-init.yaml --skip-token-print
  rm -f /tmp/kubeadm-init.yaml
fi

install -d -m 0700 /root/.kube /home/vagrant/.kube
install -m 0600 "${KUBECONFIG_PATH}" /root/.kube/config
export KUBECONFIG=/root/.kube/config
current_context="$(kubectl config current-context)"
if [[ "${current_context}" != "${CLUSTER_NAME}" ]]; then
  kubectl config rename-context "${current_context}" "${CLUSTER_NAME}"
fi
install -o vagrant -g vagrant -m 0600 /root/.kube/config /home/vagrant/.kube/config

readonly CALICO_OPERATOR_MANIFEST="/tmp/tigera-operator-v${CALICO_VERSION}.yaml"
curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
  --retry 5 --retry-all-errors --connect-timeout 15 --max-time 900 \
  --output "${CALICO_OPERATOR_MANIFEST}" "https://raw.githubusercontent.com/projectcalico/calico/v${CALICO_VERSION}/manifests/tigera-operator.yaml"
printf '%s  %s\n' "${CALICO_OPERATOR_SHA256}" "${CALICO_OPERATOR_MANIFEST}" | sha256sum --check --strict
operator_image_count="$(grep -Fc "image: ${CALICO_OPERATOR_IMAGE}" "${CALICO_OPERATOR_MANIFEST}")"
readonly operator_image_count
if [[ "${operator_image_count}" -ne 1 ]]; then
  echo "Calico operator manifest image contract changed" >&2
  exit 1
fi
sed -i "s|image: ${CALICO_OPERATOR_IMAGE}|image: quay.io/tigera/operator@${CALICO_OPERATOR_DIGEST}|" "${CALICO_OPERATOR_MANIFEST}"
grep -Fq "image: quay.io/tigera/operator@${CALICO_OPERATOR_DIGEST}" "${CALICO_OPERATOR_MANIFEST}"

kubectl apply --server-side --field-manager=pawbridge-bootstrap --force-conflicts -f "${CALICO_OPERATOR_MANIFEST}"
kubectl wait --for=condition=Established crd/installations.operator.tigera.io --timeout=300s
kubectl wait --for=condition=Available deployment/tigera-operator -n tigera-operator --timeout=300s
cat <<EOF | kubectl apply --server-side --field-manager=pawbridge-bootstrap --force-conflicts -f -
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
      - name: default-ipv4-ippool
        blockSize: 26
        cidr: ${POD_CIDR}
        encapsulation: VXLANCrossSubnet
        natOutgoing: Enabled
        nodeSelector: all()
EOF

install -d -m 0700 "${JOIN_DIRECTORY}"
while IFS= read -r existing_token; do
  [[ -z "${existing_token}" ]] && continue
  kubeadm token delete "${existing_token%%.*}" >/dev/null
done < <(kubeadm token list | awk 'NR > 1 { print $1 }')
join_command="$(kubeadm token create --ttl 2h --print-join-command)"
if [[ ! "${join_command}" =~ ^kubeadm\ join\ ${NODE_IP//./\.}:6443\ --token\ [a-z0-9]{6}\.[a-z0-9]{16}\ --discovery-token-ca-cert-hash\ sha256:[a-f0-9]{64}$ ]]; then
  echo "kubeadm returned an unexpected join command" >&2
  exit 1
fi
printf '%s\n' "${join_command}" > "${JOIN_DIRECTORY}/join-command"
chmod 0600 "${JOIN_DIRECTORY}/join-command"
join_token="$(sed -n 's/.*--token \([^ ]*\).*/\1/p' "${JOIN_DIRECTORY}/join-command")"
printf '%s\n' "${join_token%%.*}" > "${JOIN_DIRECTORY}/token-id"
chmod 0600 "${JOIN_DIRECTORY}/token-id"

cat > /etc/systemd/system/pawbridge-kubeadm-join-server.service <<EOF
[Unit]
Description=PawBridge temporary kubeadm join endpoint
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m http.server ${JOIN_SERVER_PORT} --bind ${NODE_IP} --directory ${JOIN_DIRECTORY}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/pawbridge-clean-kubeadm-join <<'CLEANUP'
#!/usr/bin/env bash
set -Eeuo pipefail
systemctl disable --now pawbridge-kubeadm-join-server.service || true
if [[ -r /run/pawbridge-kubeadm/token-id ]]; then
  kubeadm token delete "$(cat /run/pawbridge-kubeadm/token-id)" || true
fi
rm -rf /run/pawbridge-kubeadm
CLEANUP
chmod 0700 /usr/local/sbin/pawbridge-clean-kubeadm-join

cat > /etc/systemd/system/pawbridge-kubeadm-join-cleanup.service <<'EOF'
[Unit]
Description=Remove the temporary PawBridge kubeadm join endpoint

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pawbridge-clean-kubeadm-join
EOF

cat > /etc/systemd/system/pawbridge-kubeadm-join-cleanup.timer <<'EOF'
[Unit]
Description=Expire the temporary PawBridge kubeadm join endpoint

[Timer]
OnActiveSec=2h
Unit=pawbridge-kubeadm-join-cleanup.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now pawbridge-kubeadm-join-server.service
systemctl is-active --quiet pawbridge-kubeadm-join-server.service
systemctl enable pawbridge-kubeadm-join-cleanup.timer
systemctl restart pawbridge-kubeadm-join-cleanup.timer

kubectl cluster-info
echo "control-plane provisioning completed; temporary join endpoint expires in 2 hours"
