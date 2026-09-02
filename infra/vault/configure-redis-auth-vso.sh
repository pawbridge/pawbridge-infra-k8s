#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly MODE="${1:-check}"
readonly EXPECTED_CONTEXT="pawbridge-vbox-k136"
readonly VAULT_NAMESPACE="vault"
readonly VAULT_POD="vault-0"
readonly KV_MOUNT="secret"
readonly KUBERNETES_AUTH_MOUNT="kubernetes"
readonly POLICY_NAME="redis-auth-read"
readonly SERVER_ROLE_NAME="redis-auth-read"
readonly SERVER_BOUND_SERVICE_ACCOUNT="redis-vault-auth"
readonly SERVER_BOUND_NAMESPACE="databases"
readonly CLIENT_ROLE_NAME="redis-client-auth-read"
readonly CLIENT_BOUND_SERVICE_ACCOUNT="redis-client-vault-auth"
readonly CLIENT_BOUND_NAMESPACE="pawbridge"
readonly TOKEN_AUDIENCE="vault"
readonly TOKEN_TTL_SECONDS="600"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly LOGIN_TIMEOUT_SECONDS="120"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly POLICY_FILE="${SCRIPT_DIR}/policies/redis-auth-read.hcl"

TOKEN_SESSION_DIR=""

usage() {
  cat <<'USAGE'
Usage:
  configure-redis-auth-vso.sh check
  configure-redis-auth-vso.sh apply

This script verifies or configures only the least-privilege Vault policy and
Kubernetes auth roles used by the Redis server and client VaultStaticSecrets.
It never accepts, reads, or writes the Redis password.

Both modes use an isolated temporary token-helper directory inside vault-0.
Vault prompts for the administrator password through an interactive userpass
login. The temporary token is revoked and removed on exit.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  echo "Redis Vault bootstrap failed at line ${BASH_LINENO[0]}" >&2
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
      /tmp/pawbridge-redis-auth-bootstrap.*) ;;
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
        sh -c 'case "$1" in /tmp/pawbridge-redis-auth-bootstrap.*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
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

  [[ -s "${POLICY_FILE}" ]] || fail "missing policy file: ${POLICY_FILE}"
  [[ "$(grep -c '^path ' "${POLICY_FILE}")" -eq 2 ]] || fail "policy must contain exactly two paths"
  grep -Fqx 'path "secret/data/pawbridge/dev/redis/auth" {' "${POLICY_FILE}" || fail "missing exact KV data path"
  grep -Fqx 'path "secret/metadata/pawbridge/dev/redis/auth" {' "${POLICY_FILE}" || fail "missing exact KV metadata path"
  [[ "$(grep -Fc 'capabilities = ["read"]' "${POLICY_FILE}")" -eq 2 ]] || fail "policy must grant read only"
  if grep -Fq '*' "${POLICY_FILE}"; then
    fail "wildcards are not allowed in the Redis policy"
  fi
}

validate_cluster_target() {
  local current_context
  local pod_phase
  local pod_ready

  current_context="$(kube config current-context 2>/dev/null || true)"
  [[ "${current_context}" == "${EXPECTED_CONTEXT}" ]] || \
    fail "unexpected kubectl context: ${current_context}"

  kube get namespace "${SERVER_BOUND_NAMESPACE}" >/dev/null
  kube get namespace "${CLIENT_BOUND_NAMESPACE}" >/dev/null
  pod_phase="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.phase}')"
  pod_ready="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')"
  [[ "${pod_phase}" == "Running" && "${pod_ready}" == "true" ]] || \
    fail "Vault Pod is not Running and Ready"

  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- vault status >/dev/null || \
    fail "Vault is unavailable or sealed"
}

validate_token_session() {
  local vault_username

  [[ -t 0 && -t 1 ]] || fail "an interactive terminal is required for Vault login"
  read -r -p "Vault admin username: " vault_username
  [[ "${vault_username}" =~ ^[A-Za-z0-9._-]+$ ]] || \
    fail "Vault username contains unsupported characters"

  TOKEN_SESSION_DIR="$(kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    mktemp -d /tmp/pawbridge-redis-auth-bootstrap.XXXXXX)"
  [[ "${TOKEN_SESSION_DIR}" == /tmp/pawbridge-redis-auth-bootstrap.* ]] || \
    fail "unexpected temporary Vault session directory"

  timeout --foreground "${LOGIN_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -it "${VAULT_POD}" -- \
    sh -c 'HOME="$1" vault login -method=userpass username="$2" >/dev/null' \
    sh "${TOKEN_SESSION_DIR}" "${vault_username}"

  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    sh -c 'test -s "$1/.vault-token"' sh "${TOKEN_SESSION_DIR}" || \
    fail "Vault login did not create the isolated token helper"
  vault_cli token lookup >/dev/null || fail "Vault CLI token is invalid or expired"
}

validate_vault_prerequisites() {
  local mount_type
  local mount_options
  local auth_type

  mount_type="$(vault_cli read -field=type "sys/mounts/${KV_MOUNT}" 2>/dev/null || true)"
  [[ "${mount_type}" == "kv" ]] || fail "${KV_MOUNT}/ must be an existing KV mount"
  mount_options="$(vault_cli read -field=options "sys/mounts/${KV_MOUNT}")"
  [[ "${mount_options}" == *"version:2"* ]] || fail "${KV_MOUNT}/ must be KV v2"

  auth_type="$(vault_cli auth list -detailed | awk -v path="${KUBERNETES_AUTH_MOUNT}/" \
    '$1 == path { print $2; exit }')"
  [[ "${auth_type}" == "kubernetes" ]] || \
    fail "${KUBERNETES_AUTH_MOUNT}/ must be an existing Kubernetes auth mount"
  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/config" >/dev/null || \
    fail "Kubernetes auth config is missing"
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

  [[ "${MODE}" == apply ]] || fail "Vault policy is missing or differs: ${POLICY_NAME}"
  write_policy
  policy_matches || fail "Vault policy verification failed after write"
  echo "Applied least-privilege Vault policy: ${POLICY_NAME}"
}

normalized_role_field() {
  local role_name="${1:?role name is required}"
  local field_name="${2:?role field name is required}"
  vault_cli read -field="${field_name}" \
    "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" | normalize
}

role_matches() {
  local role_name="${1:?role name is required}"
  local bound_service_account="${2:?bound service account is required}"
  local bound_namespace="${3:?bound namespace is required}"

  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" >/dev/null 2>&1 || return 1
  [[ "$(normalized_role_field "${role_name}" bound_service_account_names)" == "${bound_service_account}" ]] || return 1
  [[ "$(normalized_role_field "${role_name}" bound_service_account_namespaces)" == "${bound_namespace}" ]] || return 1
  [[ "$(normalized_role_field "${role_name}" audience)" == "${TOKEN_AUDIENCE}" ]] || return 1
  [[ "$(normalized_role_field "${role_name}" token_policies)" == "${POLICY_NAME}" ]] || return 1
  [[ "$(normalized_role_field "${role_name}" token_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
  [[ "$(normalized_role_field "${role_name}" token_max_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
}

ensure_role() {
  local role_name="${1:?role name is required}"
  local bound_service_account="${2:?bound service account is required}"
  local bound_namespace="${3:?bound namespace is required}"

  if role_matches "${role_name}" "${bound_service_account}" "${bound_namespace}"; then
    echo "Vault Kubernetes role already matches: ${role_name}"
    return
  fi

  [[ "${MODE}" == apply ]] || fail "Vault Kubernetes role is missing or differs: ${role_name}"
  vault_cli write "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" \
    bound_service_account_names="${bound_service_account}" \
    bound_service_account_namespaces="${bound_namespace}" \
    audience="${TOKEN_AUDIENCE}" \
    token_policies="${POLICY_NAME}" \
    token_ttl="${TOKEN_TTL_SECONDS}" \
    token_max_ttl="${TOKEN_TTL_SECONDS}" >/dev/null
  role_matches "${role_name}" "${bound_service_account}" "${bound_namespace}" || \
    fail "Vault Kubernetes role verification failed after write: ${role_name}"
  echo "Applied Vault Kubernetes role: ${role_name}"
}

main() {
  validate_local_inputs
  validate_cluster_target
  validate_token_session
  validate_vault_prerequisites
  ensure_policy
  ensure_role "${SERVER_ROLE_NAME}" "${SERVER_BOUND_SERVICE_ACCOUNT}" "${SERVER_BOUND_NAMESPACE}"
  ensure_role "${CLIENT_ROLE_NAME}" "${CLIENT_BOUND_SERVICE_ACCOUNT}" "${CLIENT_BOUND_NAMESPACE}"
  echo "Redis Vault bootstrap ${MODE} completed. No Redis password was read or written."
}

trap on_error ERR
trap cleanup_token_session EXIT

main
