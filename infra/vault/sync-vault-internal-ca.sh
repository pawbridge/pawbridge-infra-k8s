#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly MODE="${1:-check}"
readonly EXPECTED_CONTEXT="pawbridge-vbox-k136"
readonly SOURCE_NAMESPACE="vault"
readonly SOURCE_SECRET="vault-server-tls"
readonly SOURCE_KEY="ca.crt"
readonly TARGET_SECRET="vault-internal-ca"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly TARGET_NAMESPACES=(databases pawbridge kafka)

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

kube() {
  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" "$@"
}

validate_target() {
  local current_context
  current_context="$(kube config current-context 2>/dev/null || true)"
  [[ "${current_context}" == "${EXPECTED_CONTEXT}" ]] || \
    fail "unexpected kubectl context: ${current_context}"

  kube get namespace "${SOURCE_NAMESPACE}" >/dev/null
  for namespace in "${TARGET_NAMESPACES[@]}"; do
    kube get namespace "${namespace}" >/dev/null
  done
}

source_ca_data() {
  kube -n "${SOURCE_NAMESPACE}" get secret "${SOURCE_SECRET}" \
    -o "jsonpath={.data.${SOURCE_KEY//./\\.}}"
}

target_ca_data() {
  local namespace="${1:?namespace is required}"
  kube -n "${namespace}" get secret "${TARGET_SECRET}" \
    -o "jsonpath={.data.${SOURCE_KEY//./\\.}}" 2>/dev/null || true
}

main() {
  local encoded_ca
  local actual_ca
  local namespace

  case "${MODE}" in
    check | apply) ;;
    *) fail "mode must be check or apply" ;;
  esac

  command -v kubectl >/dev/null 2>&1 || fail "missing command: kubectl"
  command -v timeout >/dev/null 2>&1 || fail "missing command: timeout"
  validate_target

  encoded_ca="$(source_ca_data)"
  [[ -n "${encoded_ca}" ]] || fail "Vault server CA is missing"

  for namespace in "${TARGET_NAMESPACES[@]}"; do
    actual_ca="$(target_ca_data "${namespace}")"
    if [[ "${actual_ca}" == "${encoded_ca}" ]]; then
      echo "Vault CA Secret already matches in namespace: ${namespace}"
      continue
    fi

    [[ "${MODE}" == apply ]] || \
      fail "Vault CA Secret is missing or stale in namespace: ${namespace}"

    printf '%s' "${encoded_ca}" | base64 --decode | \
      kube -n "${namespace}" create secret generic "${TARGET_SECRET}" \
        --from-file="${SOURCE_KEY}"=/dev/stdin \
        --dry-run=client -o yaml | \
      kube apply -f - >/dev/null

    [[ "$(target_ca_data "${namespace}")" == "${encoded_ca}" ]] || \
      fail "Vault CA Secret verification failed in namespace: ${namespace}"
    echo "Applied Vault CA Secret in namespace: ${namespace}"
  done

  encoded_ca=""
  actual_ca=""
  echo "Vault internal CA ${MODE} completed without printing certificate data."
}

main
