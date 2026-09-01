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
readonly POLICY_NAME="store-r2-read"
readonly ROLE_NAME="store-r2-read"
readonly BOUND_SERVICE_ACCOUNT="store-r2-vault-auth"
readonly BOUND_NAMESPACE="pawbridge"
readonly TOKEN_AUDIENCE="vault"
readonly TOKEN_TTL_SECONDS="600"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly LOGIN_TIMEOUT_SECONDS="120"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly POLICY_FILE="${SCRIPT_DIR}/policies/store-r2-read.hcl"

TOKEN_SESSION_DIR=""

usage() {
  cat <<'USAGE'
Usage:
  configure-store-r2-vso.sh check
  configure-store-r2-vso.sh apply

The script never accepts or writes R2 values. It asks for the non-secret Vault
username, then Vault itself prompts for the password through an interactive
userpass login. The password is not read or stored by this script.

Both modes use an isolated temporary token-helper directory inside vault-0.
That temporary CLI token is revoked and its directory is removed on exit. The
browser session and any existing CLI token-helper file are not touched.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  echo "Store R2 Vault bootstrap failed at line ${BASH_LINENO[0]}" >&2
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

vault_status() {
  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- vault status
}

cleanup_token_session() {
  local exit_code=$?
  local cleanup_failed=false

  trap - EXIT
  if [[ -n "${TOKEN_SESSION_DIR}" ]]; then
    case "${TOKEN_SESSION_DIR}" in
      /tmp/pawbridge-store-r2-bootstrap.*) ;;
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
        sh -c 'case "$1" in /tmp/pawbridge-store-r2-bootstrap.*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
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

require_command() {
  local command_name="${1:?command name is required}"
  command -v "${command_name}" >/dev/null 2>&1 || fail "missing command: ${command_name}"
}

normalize() {
  tr -d '[:space:][]",'
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

  require_command kubectl
  require_command timeout

  [[ -s "${POLICY_FILE}" ]] || fail "missing policy file: ${POLICY_FILE}"
  [[ "$(grep -c '^path ' "${POLICY_FILE}")" -eq 2 ]] || fail "policy must contain exactly two paths"
  grep -Fqx 'path "secret/data/pawbridge/dev/store/r2" {' "${POLICY_FILE}" || fail "missing exact KV data path"
  grep -Fqx 'path "secret/metadata/pawbridge/dev/store/r2" {' "${POLICY_FILE}" || fail "missing exact KV metadata path"
  [[ "$(grep -Fc 'capabilities = ["read"]' "${POLICY_FILE}")" -eq 2 ]] || fail "policy must grant read only"
  if grep -Fq '*' "${POLICY_FILE}"; then
    fail "wildcards are not allowed in the Store R2 policy"
  fi
}

validate_cluster_target() {
  local current_context
  local pod_phase
  local pod_ready
  local tokenreview_allowed

  current_context="$(kube config current-context)"
  [[ "${current_context}" == "${EXPECTED_CONTEXT}" ]] || \
    fail "unexpected kubectl context: ${current_context}"

  kube get namespace "${VAULT_NAMESPACE}" >/dev/null
  kube -n "${VAULT_NAMESPACE}" get serviceaccount "${VAULT_SERVICE_ACCOUNT}" >/dev/null

  pod_phase="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.phase}')"
  pod_ready="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')"
  [[ "${pod_phase}" == "Running" && "${pod_ready}" == "true" ]] || \
    fail "Vault Pod is not Running and Ready"

  vault_status >/dev/null

  tokenreview_allowed="$(kube auth can-i create tokenreviews.authentication.k8s.io \
    --as="system:serviceaccount:${VAULT_NAMESPACE}:${VAULT_SERVICE_ACCOUNT}" 2>/dev/null)"
  [[ "${tokenreview_allowed}" == "yes" ]] || \
    fail "Vault ServiceAccount cannot create Kubernetes TokenReviews"
}

validate_token_session() {
  local vault_username

  [[ -t 0 && -t 1 ]] || fail "an interactive terminal is required for Vault login"

  read -r -p "Vault admin username: " vault_username
  [[ "${vault_username}" =~ ^[A-Za-z0-9._-]+$ ]] || \
    fail "Vault username contains unsupported characters"

  TOKEN_SESSION_DIR="$(kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    mktemp -d /tmp/pawbridge-store-r2-bootstrap.XXXXXX)"
  [[ "${TOKEN_SESSION_DIR}" == /tmp/pawbridge-store-r2-bootstrap.* ]] || \
    fail "unexpected temporary Vault session directory"

  timeout --foreground "${LOGIN_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -it "${VAULT_POD}" -- \
    sh -c 'HOME="$1" vault login -method=userpass username="$2" >/dev/null' \
    sh "${TOKEN_SESSION_DIR}" "${vault_username}"

  if ! kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    sh -c 'test -s "$1/.vault-token"' sh "${TOKEN_SESSION_DIR}"; then
    fail "Vault login did not create the isolated token helper"
  fi

  vault_cli token lookup >/dev/null || fail "Vault CLI token is invalid or expired"
}

kv_mount_type() {
  vault_cli read -field=type "sys/mounts/${KV_MOUNT}" 2>/dev/null || true
}

validate_kv_mount() {
  local mount_type
  local mount_options

  mount_type="$(kv_mount_type)"
  [[ "${mount_type}" == "kv" ]] || fail "${KV_MOUNT}/ exists with unexpected type: ${mount_type}"

  mount_options="$(vault_cli read -field=options "sys/mounts/${KV_MOUNT}")"
  [[ "${mount_options}" == *"version:2"* ]] || fail "${KV_MOUNT}/ is not KV v2"
}

ensure_kv_mount() {
  local mount_type
  mount_type="$(kv_mount_type)"

  if [[ -z "${mount_type}" ]]; then
    if [[ "${MODE}" == check ]]; then
      fail "missing KV v2 mount: ${KV_MOUNT}/"
    fi
    vault_cli secrets enable -path="${KV_MOUNT}" -version=2 kv >/dev/null
    echo "Created KV v2 mount: ${KV_MOUNT}/"
  fi

  validate_kv_mount
}

auth_mount_type() {
  vault_cli auth list -detailed | awk -v path="${KUBERNETES_AUTH_MOUNT}/" \
    '$1 == path { print $2; exit }'
}

validate_auth_mount() {
  local mount_type
  mount_type="$(auth_mount_type)"
  [[ "${mount_type}" == "kubernetes" ]] || \
    fail "${KUBERNETES_AUTH_MOUNT}/ exists with unexpected type: ${mount_type}"
}

ensure_auth_mount() {
  local mount_type
  mount_type="$(auth_mount_type)"

  if [[ -z "${mount_type}" ]]; then
    if [[ "${MODE}" == check ]]; then
      fail "missing auth mount: ${KUBERNETES_AUTH_MOUNT}/"
    fi
    vault_cli auth enable -path="${KUBERNETES_AUTH_MOUNT}" kubernetes >/dev/null
    echo "Created Kubernetes auth mount: ${KUBERNETES_AUTH_MOUNT}/"
  fi

  validate_auth_mount
}

kubernetes_config_exists() {
  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/config" >/dev/null 2>&1
}

validate_kubernetes_config() {
  local actual_host
  local actual_disable_local_ca_jwt
  local actual_disable_iss_validation

  kubernetes_config_exists || fail "Kubernetes auth config is missing"

  actual_host="$(vault_cli read -field=kubernetes_host "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  actual_disable_local_ca_jwt="$(vault_cli read -field=disable_local_ca_jwt "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  actual_disable_iss_validation="$(vault_cli read -field=disable_iss_validation "auth/${KUBERNETES_AUTH_MOUNT}/config")"

  [[ "${actual_host}" == "${KUBERNETES_HOST}" ]] || fail "unexpected Kubernetes API host in Vault"
  [[ "${actual_disable_local_ca_jwt}" == "false" ]] || fail "Vault must use its local Kubernetes token and CA"
  [[ "${actual_disable_iss_validation}" == "true" ]] || fail "Kubernetes issuer validation setting is unexpected"
}

ensure_kubernetes_config() {
  if kubernetes_config_exists; then
    validate_kubernetes_config
    echo "Kubernetes auth config already matches the approved contract."
    return
  fi

  if [[ "${MODE}" == check ]]; then
    fail "Kubernetes auth config is missing"
  fi

  vault_cli write "auth/${KUBERNETES_AUTH_MOUNT}/config" \
    kubernetes_host="${KUBERNETES_HOST}" \
    disable_local_ca_jwt=false \
    disable_iss_validation=true >/dev/null
  validate_kubernetes_config
  echo "Configured Vault to use its local Kubernetes token and CA."
}

policy_exists() {
  vault_cli policy read "${POLICY_NAME}" >/dev/null 2>&1
}

policy_matches() {
  local expected_policy
  local actual_policy

  policy_exists || return 1
  expected_policy="$(normalize < "${POLICY_FILE}")"
  actual_policy="$(vault_cli policy read "${POLICY_NAME}" | normalize)"
  [[ "${actual_policy}" == "${expected_policy}" ]]
}

write_policy() {
  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" \
    vault policy write "${POLICY_NAME}" - < "${POLICY_FILE}" >/dev/null
}

ensure_policy() {
  if policy_matches; then
    echo "Vault policy already matches: ${POLICY_NAME}"
    return
  fi

  if [[ "${MODE}" == check ]]; then
    fail "Vault policy is missing or differs: ${POLICY_NAME}"
  fi

  write_policy
  policy_matches || fail "Vault policy verification failed after write"
  echo "Applied least-privilege Vault policy: ${POLICY_NAME}"
}

role_exists() {
  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/role/${ROLE_NAME}" >/dev/null 2>&1
}

normalized_role_field() {
  local field_name="${1:?role field name is required}"
  vault_cli read -field="${field_name}" \
    "auth/${KUBERNETES_AUTH_MOUNT}/role/${ROLE_NAME}" | normalize
}

role_matches() {
  role_exists || return 1

  [[ "$(normalized_role_field bound_service_account_names)" == "${BOUND_SERVICE_ACCOUNT}" ]] || return 1
  [[ "$(normalized_role_field bound_service_account_namespaces)" == "${BOUND_NAMESPACE}" ]] || return 1
  [[ "$(normalized_role_field audience)" == "${TOKEN_AUDIENCE}" ]] || return 1
  [[ "$(normalized_role_field token_policies)" == "${POLICY_NAME}" ]] || return 1
  [[ "$(normalized_role_field token_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
  [[ "$(normalized_role_field token_max_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
}

ensure_role() {
  if role_matches; then
    echo "Vault Kubernetes role already matches: ${ROLE_NAME}"
    return
  fi

  if [[ "${MODE}" == check ]]; then
    fail "Vault Kubernetes role is missing or differs: ${ROLE_NAME}"
  fi

  vault_cli write "auth/${KUBERNETES_AUTH_MOUNT}/role/${ROLE_NAME}" \
    bound_service_account_names="${BOUND_SERVICE_ACCOUNT}" \
    bound_service_account_namespaces="${BOUND_NAMESPACE}" \
    audience="${TOKEN_AUDIENCE}" \
    token_policies="${POLICY_NAME}" \
    token_ttl="${TOKEN_TTL_SECONDS}" \
    token_max_ttl="${TOKEN_TTL_SECONDS}" >/dev/null

  role_matches || fail "Vault Kubernetes role verification failed after write"
  echo "Applied Vault Kubernetes role: ${ROLE_NAME}"
}

main() {
  validate_local_inputs
  validate_cluster_target
  validate_token_session

  ensure_kv_mount
  ensure_auth_mount
  ensure_kubernetes_config
  ensure_policy
  ensure_role

  echo "Store R2 Vault bootstrap ${MODE} completed. No R2 values were read or written."
}

trap on_error ERR
trap cleanup_token_session EXIT

main
