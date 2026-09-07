#!/usr/bin/env bash

# Remote sh snippets intentionally expand positional parameters in the target Pod.
# shellcheck disable=SC2016

set -Eeuo pipefail
umask 077

readonly MODE="${1:-check}"
readonly EXPECTED_CONTEXT="pawbridge-vbox-k136"
readonly VAULT_NAMESPACE="vault"
readonly VAULT_POD="vault-0"
readonly VAULT_SERVICE_ACCOUNT="vault"
readonly KV_MOUNT="secret"
readonly GOOGLE_KV_PATH="pawbridge/dev/user/google"
readonly JWT_KV_PATH="pawbridge/dev/target-edge/api-gateway-jwt"
readonly KUBERNETES_AUTH_MOUNT="kubernetes"
readonly KUBERNETES_HOST="https://kubernetes.default.svc:443"
readonly POLICY_NAME="user-runtime-read"
readonly ROLE_NAME="user-runtime-read"
readonly BOUND_SERVICE_ACCOUNT="user-runtime-vault-auth"
readonly BOUND_NAMESPACE="pawbridge"
readonly TOKEN_AUDIENCE="vault"
readonly TOKEN_TTL_SECONDS="600"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly LOGIN_TIMEOUT_SECONDS="120"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly POLICY_FILE="${SCRIPT_DIR}/policies/user-runtime-read.hcl"

TOKEN_SESSION_DIR=""
GOOGLE_EMAIL=""
GOOGLE_EMAIL_SECRET_KEY=""
GOOGLE_CLIENT_ID=""
GOOGLE_SECRET_KEY=""

usage() {
  cat <<'USAGE'
Usage:
  configure-user-runtime-vso.sh check
  configure-user-runtime-vso.sh apply
  configure-user-runtime-vso.sh rotate

check verifies the least-privilege Vault policy, Kubernetes auth role, Google
credential field contract, and the shared JWT field without printing values.

apply creates the Google credential only when it is missing. Existing Google
credentials are never overwritten by apply.

rotate explicitly replaces the existing Google credential using Vault KV v2
check-and-set protection. It does not rotate or rewrite the shared JWT.

Google email and OAuth client ID are entered visibly. The Gmail app password
and OAuth client secret are hidden. The isolated administrator token is revoked
and its temporary helper directory is removed on exit.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  echo "User runtime Vault bootstrap failed at line ${BASH_LINENO[0]}" >&2
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
      /tmp/pawbridge-user-runtime-bootstrap.*) ;;
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
        sh -c 'case "$1" in /tmp/pawbridge-user-runtime-bootstrap.*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
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

  GOOGLE_EMAIL=""
  GOOGLE_EMAIL_SECRET_KEY=""
  GOOGLE_CLIENT_ID=""
  GOOGLE_SECRET_KEY=""
  exit "${exit_code}"
}

normalize() {
  tr -d '[:space:][]",'
}

validate_local_inputs() {
  case "${MODE}" in
    check | apply | rotate) ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "mode must be check, apply, or rotate"
      ;;
  esac

  command -v kubectl >/dev/null 2>&1 || fail "missing command: kubectl"
  command -v timeout >/dev/null 2>&1 || fail "missing command: timeout"
  [[ -s "${POLICY_FILE}" ]] || fail "missing policy file: ${POLICY_FILE}"
  [[ "$(grep -c '^path ' "${POLICY_FILE}")" -eq 4 ]] || fail "policy must contain exactly four paths"
  grep -Fqx 'path "secret/data/pawbridge/dev/user/google" {' "${POLICY_FILE}" || fail "missing Google data path"
  grep -Fqx 'path "secret/metadata/pawbridge/dev/user/google" {' "${POLICY_FILE}" || fail "missing Google metadata path"
  grep -Fqx 'path "secret/data/pawbridge/dev/target-edge/api-gateway-jwt" {' "${POLICY_FILE}" || fail "missing JWT data path"
  grep -Fqx 'path "secret/metadata/pawbridge/dev/target-edge/api-gateway-jwt" {' "${POLICY_FILE}" || fail "missing JWT metadata path"
  [[ "$(grep -Fc 'capabilities = ["read"]' "${POLICY_FILE}")" -eq 4 ]] || fail "policy must grant read only"
  if grep -Fq '*' "${POLICY_FILE}"; then
    fail "wildcards are not allowed in the User runtime policy"
  fi
}

validate_cluster_target() {
  local current_context
  local pod_phase
  local pod_ready
  local tokenreview_allowed

  current_context="$(kube config current-context 2>/dev/null || true)"
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
    mktemp -d /tmp/pawbridge-user-runtime-bootstrap.XXXXXX)"
  [[ "${TOKEN_SESSION_DIR}" == /tmp/pawbridge-user-runtime-bootstrap.* ]] || fail "unexpected temporary Vault session directory"

  timeout --foreground "${LOGIN_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -it "${VAULT_POD}" -- \
    sh -c 'HOME="$1" vault login -method=userpass username="$2" >/dev/null' \
    sh "${TOKEN_SESSION_DIR}" "${vault_username}"

  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    sh -c 'test -s "$1/.vault-token"' sh "${TOKEN_SESSION_DIR}" || fail "Vault login did not create an isolated token helper"
  vault_cli token lookup >/dev/null || fail "Vault CLI token is invalid or expired"
}

validate_vault_foundation() {
  local mount_type
  local mount_options
  local auth_type
  local actual_host
  local disable_local_ca_jwt
  local disable_iss_validation

  mount_type="$(vault_cli read -field=type "sys/mounts/${KV_MOUNT}" 2>/dev/null || true)"
  [[ "${mount_type}" == "kv" ]] || fail "${KV_MOUNT}/ must be an existing KV mount"
  mount_options="$(vault_cli read -field=options "sys/mounts/${KV_MOUNT}")"
  [[ "${mount_options}" == *"version:2"* ]] || fail "${KV_MOUNT}/ must be KV v2"

  auth_type="$(vault_cli auth list -detailed | awk -v path="${KUBERNETES_AUTH_MOUNT}/" '$1 == path { print $2; exit }')"
  [[ "${auth_type}" == "kubernetes" ]] || fail "${KUBERNETES_AUTH_MOUNT}/ must be an existing Kubernetes auth mount"
  actual_host="$(vault_cli read -field=kubernetes_host "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  disable_local_ca_jwt="$(vault_cli read -field=disable_local_ca_jwt "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  disable_iss_validation="$(vault_cli read -field=disable_iss_validation "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  [[ "${actual_host}" == "${KUBERNETES_HOST}" ]] || fail "unexpected Kubernetes API host in Vault"
  [[ "${disable_local_ca_jwt}" == "false" ]] || fail "Vault must use its local Kubernetes token and CA"
  [[ "${disable_iss_validation}" == "true" ]] || fail "Kubernetes issuer validation setting is unexpected"
}

policy_matches() {
  local expected_policy
  local actual_policy

  actual_policy="$(vault_cli policy read "${POLICY_NAME}" 2>/dev/null | normalize)" || return 1
  expected_policy="$(normalize < "${POLICY_FILE}")"
  [[ "${actual_policy}" == "${expected_policy}" ]]
}

ensure_policy() {
  if policy_matches; then
    echo "Vault policy already matches: ${POLICY_NAME}"
    return
  fi
  [[ "${MODE}" != "check" ]] || fail "Vault policy is missing or differs: ${POLICY_NAME}"

  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" vault policy write "${POLICY_NAME}" - < "${POLICY_FILE}" >/dev/null
  policy_matches || fail "Vault policy verification failed after write"
  echo "Applied least-privilege Vault policy: ${POLICY_NAME}"
}

role_field() {
  local field_name="${1:?role field name is required}"
  vault_cli read -field="${field_name}" "auth/${KUBERNETES_AUTH_MOUNT}/role/${ROLE_NAME}" | normalize
}

role_matches() {
  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/role/${ROLE_NAME}" >/dev/null 2>&1 || return 1
  [[ "$(role_field bound_service_account_names)" == "${BOUND_SERVICE_ACCOUNT}" ]] || return 1
  [[ "$(role_field bound_service_account_namespaces)" == "${BOUND_NAMESPACE}" ]] || return 1
  [[ "$(role_field audience)" == "${TOKEN_AUDIENCE}" ]] || return 1
  [[ "$(role_field token_policies)" == "${POLICY_NAME}" ]] || return 1
  [[ "$(role_field token_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
  [[ "$(role_field token_max_ttl)" == "${TOKEN_TTL_SECONDS}" ]] || return 1
}

ensure_role() {
  if role_matches; then
    echo "Vault Kubernetes role already matches: ${ROLE_NAME}"
    return
  fi
  [[ "${MODE}" != "check" ]] || fail "Vault Kubernetes role is missing or differs: ${ROLE_NAME}"

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

google_credential_exists() {
  vault_cli kv metadata get -mount="${KV_MOUNT}" "${GOOGLE_KV_PATH}" >/dev/null 2>&1
}

prompt_google_credential() {
  read -r -p "Google sender email: " GOOGLE_EMAIL
  read -r -s -p "Gmail app password (hidden): " GOOGLE_EMAIL_SECRET_KEY
  echo
  read -r -p "Google OAuth client ID: " GOOGLE_CLIENT_ID
  read -r -s -p "Google OAuth client secret (hidden): " GOOGLE_SECRET_KEY
  echo

  GOOGLE_EMAIL_SECRET_KEY="${GOOGLE_EMAIL_SECRET_KEY// /}"
  [[ "${GOOGLE_EMAIL}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || fail "Google sender email format is invalid"
  [[ "${GOOGLE_EMAIL_SECRET_KEY}" =~ ^[A-Za-z0-9]{16}$ ]] || fail "Gmail app password must contain exactly 16 letters or digits after spaces are removed"
  [[ "${GOOGLE_CLIENT_ID}" =~ ^[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$ ]] || fail "Google OAuth client ID format is invalid"
  [[ "${GOOGLE_SECRET_KEY}" =~ ^[A-Za-z0-9_-]{16,256}$ ]] || fail "Google OAuth client secret format is invalid"
}

write_google_credential() {
  local cas_version="${1:?CAS version is required}"

  {
    printf '%s\n' "${GOOGLE_EMAIL}"
    printf '%s\n' "${GOOGLE_EMAIL_SECRET_KEY}"
    printf '%s\n' "${GOOGLE_CLIENT_ID}"
    printf '%s\n' "${GOOGLE_SECRET_KEY}"
  } | kube -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" sh -eu -c '
      IFS= read -r google_email
      IFS= read -r google_email_secret_key
      IFS= read -r google_client_id
      IFS= read -r google_secret_key
      vault kv put -mount="$1" -cas="$3" "$2" \
        GOOGLE_EMAIL="$google_email" \
        GOOGLE_EMAIL_SECRET_KEY="$google_email_secret_key" \
        GOOGLE_CLIENT_ID="$google_client_id" \
        GOOGLE_SECRET_KEY="$google_secret_key" >/dev/null
      unset google_email google_email_secret_key google_client_id google_secret_key
    ' sh "${KV_MOUNT}" "${GOOGLE_KV_PATH}" "${cas_version}"
}

validate_google_credential() {
  local google_email
  local google_email_secret_key
  local google_client_id
  local google_secret_key

  google_email="$(vault_cli kv get -mount="${KV_MOUNT}" -field=GOOGLE_EMAIL "${GOOGLE_KV_PATH}")"
  google_email_secret_key="$(vault_cli kv get -mount="${KV_MOUNT}" -field=GOOGLE_EMAIL_SECRET_KEY "${GOOGLE_KV_PATH}")"
  google_client_id="$(vault_cli kv get -mount="${KV_MOUNT}" -field=GOOGLE_CLIENT_ID "${GOOGLE_KV_PATH}")"
  google_secret_key="$(vault_cli kv get -mount="${KV_MOUNT}" -field=GOOGLE_SECRET_KEY "${GOOGLE_KV_PATH}")"

  google_email_secret_key="${google_email_secret_key// /}"
  [[ "${google_email}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || fail "stored Google sender email contract is invalid"
  [[ "${google_email_secret_key}" =~ ^[A-Za-z0-9]{16}$ ]] || fail "stored Gmail app password contract is invalid"
  [[ "${google_client_id}" =~ ^[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$ ]] || fail "stored Google OAuth client ID contract is invalid"
  [[ "${google_secret_key}" =~ ^[A-Za-z0-9_-]{16,256}$ ]] || fail "stored Google OAuth client secret contract is invalid"
  google_email=""
  google_email_secret_key=""
  google_client_id=""
  google_secret_key=""
}

ensure_google_credential() {
  local current_version

  case "${MODE}" in
    check)
      google_credential_exists || fail "Vault User Google credential is missing"
      ;;
    apply)
      if google_credential_exists; then
        echo "Vault User Google credential metadata exists; apply will not overwrite it."
      else
        prompt_google_credential
        write_google_credential 0
        google_credential_exists || fail "Vault User Google credential metadata is missing after create"
        echo "Created the User Google credential in Vault without printing values."
      fi
      ;;
    rotate)
      google_credential_exists || fail "Vault User Google credential is missing; run apply first"
      current_version="$(vault_cli kv metadata get -mount="${KV_MOUNT}" -field=current_version "${GOOGLE_KV_PATH}")"
      [[ "${current_version}" =~ ^[1-9][0-9]*$ ]] || fail "unable to resolve current Google credential version"
      prompt_google_credential
      write_google_credential "${current_version}"
      echo "Rotated the User Google credential with check-and-set protection."
      ;;
  esac

  validate_google_credential
  GOOGLE_EMAIL=""
  GOOGLE_EMAIL_SECRET_KEY=""
  GOOGLE_CLIENT_ID=""
  GOOGLE_SECRET_KEY=""
}

validate_shared_jwt() {
  local jwt_secret

  vault_cli kv metadata get -mount="${KV_MOUNT}" "${JWT_KV_PATH}" >/dev/null 2>&1 || fail "shared API Gateway JWT credential is missing"
  jwt_secret="$(vault_cli kv get -mount="${KV_MOUNT}" -field=JWT_SECRET "${JWT_KV_PATH}")"
  [[ "${#jwt_secret}" -ge 32 ]] || fail "shared JWT secret is shorter than 32 characters"
  jwt_secret=""
  echo "Verified the shared JWT credential contract without printing its value."
}

main() {
  validate_local_inputs
  validate_cluster_target
  create_token_session
  validate_vault_foundation
  ensure_policy
  ensure_role
  ensure_google_credential
  validate_shared_jwt
  echo "User runtime Vault bootstrap ${MODE} completed without printing secret values."
}

trap on_error ERR
trap cleanup_token_session EXIT

main
