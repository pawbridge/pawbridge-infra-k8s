#!/usr/bin/env bash

set -Eeuo pipefail

readonly EXPECTED_CONTEXT="${1:?expected context is required}"
readonly EXPECTED_KUBERNETES_VERSION="v${2:?expected Kubernetes version is required}"
readonly EXPECTED_CONTAINERD_VERSION="containerd://${3:?expected containerd version is required}"
shift 3
readonly EXPECTED_NODES=("$@")
readonly KUBECONFIG="/root/.kube/config"
export KUBECONFIG

trap 'echo "cluster verification failed at line ${LINENO}" >&2' ERR

if [[ "${EUID}" -ne 0 ]]; then
  echo "cluster verification must run as root" >&2
  exit 1
fi
if [[ "${#EXPECTED_NODES[@]}" -ne 3 ]]; then
  echo "exactly three expected node names are required" >&2
  exit 1
fi
if [[ "$(kubectl config current-context)" != "${EXPECTED_CONTEXT}" ]]; then
  echo "unexpected kubectl context" >&2
  exit 1
fi

for node_name in "${EXPECTED_NODES[@]}"; do
  kubectl wait --for=condition=Ready "node/${node_name}" --timeout=600s
done

mapfile -t actual_nodes < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)
mapfile -t expected_nodes < <(printf '%s\n' "${EXPECTED_NODES[@]}" | sort)
if [[ "${actual_nodes[*]}" != "${expected_nodes[*]}" ]]; then
  echo "unexpected node set: ${actual_nodes[*]}" >&2
  exit 1
fi

while IFS='|' read -r node_name kubelet_version runtime_version; do
  if [[ "${kubelet_version}" != "${EXPECTED_KUBERNETES_VERSION}" ]]; then
    echo "${node_name} kubelet mismatch: ${kubelet_version}" >&2
    exit 1
  fi
  if [[ "${runtime_version}" != "${EXPECTED_CONTAINERD_VERSION}" ]]; then
    echo "${node_name} runtime mismatch: ${runtime_version}" >&2
    exit 1
  fi
done < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.nodeInfo.kubeletVersion}{"|"}{.status.nodeInfo.containerRuntimeVersion}{"\n"}{end}')

for condition_type in MemoryPressure DiskPressure PIDPressure; do
  condition_jsonpath="{range .items[*]}{.metadata.name}{\"=\"}{.status.conditions[?(@.type==\"${condition_type}\")].status}{\"\\n\"}{end}"
  condition_statuses="$(kubectl get nodes -o "jsonpath=${condition_jsonpath}")"
  if [[ "$(grep -c '=False$' <<< "${condition_statuses}")" -ne "${#EXPECTED_NODES[@]}" ]]; then
    echo "${condition_type} is not False on every node" >&2
    printf '%s\n' "${condition_statuses}" >&2
    exit 1
  fi
done

kubectl wait --for=condition=Available deployment/coredns -n kube-system --timeout=600s
kubectl wait --for=condition=Available deployment/tigera-operator -n tigera-operator --timeout=600s
if [[ "$(kubectl get pods -n calico-system --no-headers 2>/dev/null | wc -l)" -eq 0 ]]; then
  echo "Calico pods were not created" >&2
  exit 1
fi
kubectl wait --for=condition=Ready pod --all -n calico-system --timeout=600s
kubectl get nodes -o wide
kubectl get pods -A -o wide
echo "three-node Kubernetes base verification passed"
