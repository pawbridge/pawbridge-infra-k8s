#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly MODE="${1:-check}"
readonly EXPECTED_CONTEXT="pawbridge-vbox-k136"
readonly VAULT_NAMESPACE="vault"
readonly VAULT_POD="vault-0"
readonly VAULT_SERVICE_ACCOUNT="vault"
readonly KV_MOUNT="secret"
readonly KUBERNETES_AUTH_MOUNT="kubernetes"
readonly KUBERNETES_HOST="https://kubernetes.default.svc:443"
readonly BOUND_NAMESPACE="pawbridge"
readonly TOKEN_AUDIENCE="vault"
readonly TOKEN_TTL_SECONDS="600"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly LOGIN_TIMEOUT_SECONDS="120"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly API_GATEWAY_POLICY="api-gateway-jwt-read"
readonly API_GATEWAY_ROLE="api-gateway-jwt-read"
readonly API_GATEWAY_SERVICE_ACCOUNT="api-gateway-vault-auth"
readonly API_GATEWAY_POLICY_FILE="${SCRIPT_DIR}/policies/api-gateway-jwt-read.hcl"
readonly API_GATEWAY_DATA_PATH="secret/data/pawbridge/dev/target-edge/api-gateway-jwt"
readonly API_GATEWAY_METADATA_PATH="secret/metadata/pawbridge/dev/target-edge/api-gateway-jwt"
readonly TUNNEL_POLICY="cloudflare-tunnel-read"
readonly TUNNEL_ROLE="cloudflare-tunnel-read"
readonly TUNNEL_SERVICE_ACCOUNT="cloudflare-tunnel-vault-auth"
readonly TUNNEL_POLICY_FILE="${SCRIPT_DIR}/policies/cloudflare-tunnel-read.hcl"
readonly TUNNEL_DATA_PATH="secret/data/pawbridge/dev/target-edge/cloudflare-tunnel"
readonly TUNNEL_METADATA_PATH="secret/metadata/pawbridge/dev/target-edge/cloudflare-tunnel"

TOKEN_SESSION_DIR=""

usage() {
  cat <<'USAGE'
Usage:
  configure-target-edge-vso.sh check
  configure-target-edge-vso.sh apply

This script configures only the least-privilege Vault policies and Kubernetes
auth roles used by the target API Gateway and target-only Cloudflare Tunnel
VSO resources.
It never reads or writes JWT or Cloudflare Tunnel secret values.

Both modes use an isolated temporary token-helper directory inside vault-0.
Vault prompts for the userpass password. The temporary token is revoked and the
directory is removed on exit.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  echo "Target edge Vault bootstrap failed at line ${BASH_LINENO[0]}" >&2
  return "${exit_code}"
}

kube() {
  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" "$@"
}

vault_cli() {
  [[ -n "${TOKEN_SESSION_DIR}" ]] || fail "Vault CLI session is not initialized"
  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" vault "$@"
}

cleanup_token_session() {
  local exit_code=$?
  local cleanup_failed=false

  trap - EXIT
  if [[ -n "${TOKEN_SESSION_DIR}" ]]; then
    case "${TOKEN_SESSION_DIR}" in
      /tmp/pawbridge-target-edge-bootstrap.*) ;;
      *)
        echo "ERROR: refusing to clean unexpected Vault session directory" >&2
        [[ "${exit_code}" -eq 0 ]] && exit_code=1
        exit "${exit_code}"
        ;;
    esac

    set +e
    if kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
      sh -c 'test -s "$1/.vault-token"' sh "${TOKEN_SESSION_DIR}"; then
      if ! vault_cli token revoke -self >/dev/null 2>&1; then
        cleanup_failed=true
      fi
    fi

    if [[ "${cleanup_failed}" == false ]]; then
      if ! kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
        sh -c 'case "$1" in /tmp/pawbridge-target-edge-bootstrap.*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
        sh "${TOKEN_SESSION_DIR}" >/dev/null 2>&1; then
        cleanup_failed=true
      fi
    fi
    set -e

    if [[ "${cleanup_failed}" == true ]]; then
      echo "ERROR: isolated Vault CLI session cleanup failed; manual recovery is required" >&2
      [[ "${exit_code}" -eq 0 ]] && exit_code=1
    else
      echo "Isolated temporary Vault CLI session was revoked and removed."
    fi
  fi

  exit "${exit_code}"
}

normalize() {
  tr -d '[:space:][]",'
}

validate_policy_file() {
  local policy_file="${1:?policy file is required}"
  local data_path="${2:?data path is required}"
  local metadata_path="${3:?metadata path is required}"

  [[ -s "${policy_file}" ]] || fail "missing policy file: ${policy_file}"
  [[ "$(grep -c '^path ' "${policy_file}")" -eq 2 ]] || fail "policy must contain exactly two paths: ${policy_file}"
  grep -Fqx "path \"${data_path}\" {" "${policy_file}" || fail "missing exact data path: ${data_path}"
  grep -Fqx "path \"${metadata_path}\" {" "${policy_file}" || fail "missing exact metadata path: ${metadata_path}"
  [[ "$(grep -Fc 'capabilities = ["read"]' "${policy_file}")" -eq 2 ]] || fail "policy must grant read only: ${policy_file}"
  if grep -Fq '*' "${policy_file}"; then
    fail "wildcards are not allowed in target edge policies"
  fi
}

validate_local_inputs() {
  case "${MODE}" in
    check | apply) ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "mode must be check or apply"
      ;;
  esac

  command -v kubectl >/dev/null 2>&1 || fail "missing command: kubectl"
  command -v timeout >/dev/null 2>&1 || fail "missing command: timeout"
  validate_policy_file "${API_GATEWAY_POLICY_FILE}" "${API_GATEWAY_DATA_PATH}" "${API_GATEWAY_METADATA_PATH}"
  validate_policy_file "${TUNNEL_POLICY_FILE}" "${TUNNEL_DATA_PATH}" "${TUNNEL_METADATA_PATH}"
}

validate_cluster_target() {
  local current_context
  local pod_phase
  local pod_ready
  local tokenreview_allowed

  current_context="$(kube config current-context)"
  [[ "${current_context}" == "${EXPECTED_CONTEXT}" ]] || fail "unexpected kubectl context: ${current_context}"

  kube get namespace "${VAULT_NAMESPACE}" >/dev/null
  kube -n "${VAULT_NAMESPACE}" get serviceaccount "${VAULT_SERVICE_ACCOUNT}" >/dev/null
  pod_phase="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.phase}')"
  pod_ready="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')"
  [[ "${pod_phase}" == "Running" && "${pod_ready}" == "true" ]] || fail "Vault Pod is not Running and Ready"

  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- vault status >/dev/null
  tokenreview_allowed="$(kube auth can-i create tokenreviews.authentication.k8s.io \
    --as="system:serviceaccount:${VAULT_NAMESPACE}:${VAULT_SERVICE_ACCOUNT}" 2>/dev/null)"
  [[ "${tokenreview_allowed}" == "yes" ]] || fail "Vault ServiceAccount cannot create Kubernetes TokenReviews"
}

create_token_session() {
  local vault_username

  [[ -t 0 && -t 1 ]] || fail "an interactive terminal is required for Vault login"
  read -r -p "Vault admin username: " vault_username
  [[ "${vault_username}" =~ ^[A-Za-z0-9._-]+$ ]] || fail "Vault username contains unsupported characters"

  TOKEN_SESSION_DIR="$(kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    mktemp -d /tmp/pawbridge-target-edge-bootstrap.XXXXXX)"
  [[ "${TOKEN_SESSION_DIR}" == /tmp/pawbridge-target-edge-bootstrap.* ]] || fail "unexpected temporary Vault session directory"

  timeout --foreground "${LOGIN_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -it "${VAULT_POD}" -- \
    sh -c 'HOME="$1" vault login -method=userpass username="$2" >/dev/null' \
    sh "${TOKEN_SESSION_DIR}" "${vault_username}"

  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    sh -c 'test -s "$1/.vault-token"' sh "${TOKEN_SESSION_DIR}" || fail "Vault login did not create an isolated token helper"
  vault_cli token lookup >/dev/null || fail "Vault CLI token is invalid or expired"
}

ensure_foundation() {
  local mount_type
  local mount_options
  local auth_type
  local disable_local_ca_jwt
  local disable_iss_validation

  mount_type="$(vault_cli read -field=type "sys/mounts/${KV_MOUNT}" 2>/dev/null || true)"
  if [[ -z "${mount_type}" ]]; then
    [[ "${MODE}" == apply ]] || fail "missing KV v2 mount: ${KV_MOUNT}/"
    vault_cli secrets enable -path="${KV_MOUNT}" -version=2 kv >/dev/null
    mount_type="$(vault_cli read -field=type "sys/mounts/${KV_MOUNT}")"
  fi
  [[ "${mount_type}" == kv ]] || fail "${KV_MOUNT}/ exists with unexpected type: ${mount_type}"
  mount_options="$(vault_cli read -field=options "sys/mounts/${KV_MOUNT}")"
  [[ "${mount_options}" == *"version:2"* ]] || fail "${KV_MOUNT}/ is not KV v2"

  auth_type="$(vault_cli auth list -detailed | awk -v path="${KUBERNETES_AUTH_MOUNT}/" '$1 == path { print $2; exit }')"
  if [[ -z "${auth_type}" ]]; then
    [[ "${MODE}" == apply ]] || fail "missing auth mount: ${KUBERNETES_AUTH_MOUNT}/"
    vault_cli auth enable -path="${KUBERNETES_AUTH_MOUNT}" kubernetes >/dev/null
    auth_type="$(vault_cli auth list -detailed | awk -v path="${KUBERNETES_AUTH_MOUNT}/" '$1 == path { print $2; exit }')"
  fi
  [[ "${auth_type}" == kubernetes ]] || fail "unexpected auth type at ${KUBERNETES_AUTH_MOUNT}/"

  if ! vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/config" >/dev/null 2>&1; then
    [[ "${MODE}" == apply ]] || fail "Kubernetes auth config is missing"
    vault_cli write "auth/${KUBERNETES_AUTH_MOUNT}/config" \
      kubernetes_host="${KUBERNETES_HOST}" \
      disable_local_ca_jwt=false \
      disable_iss_validation=true >/dev/null
  fi
  [[ "$(vault_cli read -field=kubernetes_host "auth/${KUBERNETES_AUTH_MOUNT}/config")" == "${KUBERNETES_HOST}" ]] || \
    fail "unexpected Kubernetes API host in Vault"
  disable_local_ca_jwt="$(vault_cli read -field=disable_local_ca_jwt "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  disable_iss_validation="$(vault_cli read -field=disable_iss_validation "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  [[ "${disable_local_ca_jwt}" == false ]] || fail "Vault must use its local Kubernetes token and CA"
  [[ "${disable_iss_validation}" == true ]] || fail "Kubernetes issuer validation setting is unexpected"
}

policy_matches() {
  local policy_name="${1:?policy name is required}"
  local policy_file="${2:?policy file is required}"
  local expected_policy
  local actual_policy

  actual_policy="$(vault_cli policy read "${policy_name}" 2>/dev/null | normalize)" || return 1
  expected_policy="$(normalize < "${policy_file}")"
  [[ "${actual_policy}" == "${expected_policy}" ]]
}

ensure_policy() {
  local policy_name="${1:?policy name is required}"
  local policy_file="${2:?policy file is required}"

  if policy_matches "${policy_name}" "${policy_file}"; then
    echo "Vault policy already matches: ${policy_name}"
    return
  fi
  [[ "${MODE}" == apply ]] || fail "Vault policy is missing or differs: ${policy_name}"

  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" vault policy write "${policy_name}" - < "${policy_file}" >/dev/null
  policy_matches "${policy_name}" "${policy_file}" || fail "Vault policy verification failed: ${policy_name}"
  echo "Applied least-privilege Vault policy: ${policy_name}"
}

role_field() {
  local role_name="${1:?role name is required}"
  local field_name="${2:?field name is required}"
  vault_cli read -field="${field_name}" "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" | normalize
}

role_matches() {
  local role_name="${1:?role name is required}"
  local policy_name="${2:?policy name is required}"
  local service_account="${3:?service account is required}"

  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" >/dev/null 2>&1 || return 1
  [[ "$(role_field "${role_name}" bound_service_account_names)" == "${service_account}" ]] || return 1
  [[ "$(role_field "${role_name}" bound_service_account_namespaces)" == "${BOUND_NAMESPACE}" ]] || return 1
  [[ "$(role_field "${role_name}" audience)" == "${TOKEN_AUDIENCE}" ]] || return 1
  [[ "$(role_field "${role_name}" token_policies)" == "${policy_name}" ]] || return 1
  [[ "$(role_field "${role_name}" token_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
  [[ "$(role_field "${role_name}" token_max_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
}

ensure_role() {
  local role_name="${1:?role name is required}"
  local policy_name="${2:?policy name is required}"
  local service_account="${3:?service account is required}"

  if role_matches "${role_name}" "${policy_name}" "${service_account}"; then
    echo "Vault Kubernetes role already matches: ${role_name}"
    return
  fi
  [[ "${MODE}" == apply ]] || fail "Vault Kubernetes role is missing or differs: ${role_name}"

  vault_cli write "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" \
    bound_service_account_names="${service_account}" \
    bound_service_account_namespaces="${BOUND_NAMESPACE}" \
    audience="${TOKEN_AUDIENCE}" \
    token_policies="${policy_name}" \
    token_ttl="${TOKEN_TTL_SECONDS}" \
    token_max_ttl="${TOKEN_TTL_SECONDS}" >/dev/null
  role_matches "${role_name}" "${policy_name}" "${service_account}" || fail "Vault role verification failed: ${role_name}"
  echo "Applied Vault Kubernetes role: ${role_name}"
}

main() {
  validate_local_inputs
  validate_cluster_target
  create_token_session
  ensure_foundation

  ensure_policy "${API_GATEWAY_POLICY}" "${API_GATEWAY_POLICY_FILE}"
  ensure_role "${API_GATEWAY_ROLE}" "${API_GATEWAY_POLICY}" "${API_GATEWAY_SERVICE_ACCOUNT}"
  ensure_policy "${TUNNEL_POLICY}" "${TUNNEL_POLICY_FILE}"
  ensure_role "${TUNNEL_ROLE}" "${TUNNEL_POLICY}" "${TUNNEL_SERVICE_ACCOUNT}"

  echo "Target edge Vault bootstrap ${MODE} completed. No secret values were read or written."
}

trap on_error ERR
trap cleanup_token_session EXIT

main
