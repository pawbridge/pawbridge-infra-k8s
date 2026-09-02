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
readonly MYSQL_NAMESPACE="databases"
readonly MYSQL_POD="mysql-0"
readonly MYSQL_ROOT_SECRET="mysql-auth"
readonly MYSQL_ROOT_SECRET_KEY="mysql-root-password"
readonly MYSQL_DATABASE="pawbridge_store"
readonly MYSQL_USERNAME="pawbridge_store_app"
readonly KV_MOUNT="secret"
readonly KV_PATH="pawbridge/dev/store/mysql"
readonly KV_PASSWORD_KEY="mysql-password"
readonly KUBERNETES_AUTH_MOUNT="kubernetes"
readonly KUBERNETES_HOST="https://kubernetes.default.svc:443"
readonly POLICY_NAME="store-mysql-auth-read"
readonly ROLE_NAME="store-mysql-auth-read"
readonly BOUND_SERVICE_ACCOUNT="store-mysql-vault-auth"
readonly BOUND_NAMESPACE="pawbridge"
readonly TOKEN_AUDIENCE="vault"
readonly TOKEN_TTL_SECONDS="600"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly LOGIN_TIMEOUT_SECONDS="120"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly POLICY_FILE="${SCRIPT_DIR}/policies/store-mysql-auth-read.hcl"

TOKEN_SESSION_DIR=""
MYSQL_ROOT_PASSWORD=""
MYSQL_APP_PASSWORD=""

usage() {
  cat <<'USAGE'
Usage:
  configure-store-mysql-auth-vso.sh check
  configure-store-mysql-auth-vso.sh apply

check verifies the Vault policy, Kubernetes auth role, Vault secret metadata,
and the dedicated MySQL account without changing them.

apply creates the Vault password once when it is missing, then creates or
repairs the dedicated MySQL account so it can access only pawbridge_store.
An existing Vault password is reused; this command does not rotate it.

Both modes use an isolated temporary token-helper directory inside vault-0.
That temporary CLI token is revoked and its directory is removed on exit. The
browser session and any existing CLI token-helper file are not touched. Secret
values are consumed through stdin or local shell memory and are never printed.

apply mutates Vault and MySQL. Run it only after the exact cluster context and
rollback boundary have been approved.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  echo "Store MySQL Vault bootstrap failed at line ${BASH_LINENO[0]}" >&2
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
      /tmp/pawbridge-store-mysql-bootstrap.*) ;;
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
        sh -c 'case "$1" in /tmp/pawbridge-store-mysql-bootstrap.*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
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

  MYSQL_ROOT_PASSWORD=""
  MYSQL_APP_PASSWORD=""
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
  require_command base64
  if [[ "${MODE}" == apply ]]; then
    require_command openssl
  fi

  [[ -s "${POLICY_FILE}" ]] || fail "missing policy file: ${POLICY_FILE}"
  [[ "$(grep -c '^path ' "${POLICY_FILE}")" -eq 2 ]] || fail "policy must contain exactly two paths"
  grep -Fqx 'path "secret/data/pawbridge/dev/store/mysql" {' "${POLICY_FILE}" || fail "missing exact KV data path"
  grep -Fqx 'path "secret/metadata/pawbridge/dev/store/mysql" {' "${POLICY_FILE}" || fail "missing exact KV metadata path"
  [[ "$(grep -Fc 'capabilities = ["read"]' "${POLICY_FILE}")" -eq 2 ]] || fail "policy must grant read only"
  if grep -Fq '*' "${POLICY_FILE}"; then
    fail "wildcards are not allowed in the Store MySQL policy"
  fi
}

validate_cluster_target() {
  local current_context
  local pod_phase
  local pod_ready
  local tokenreview_allowed

  current_context="$(kube config current-context 2>/dev/null || true)"
  [[ "${current_context}" == "${EXPECTED_CONTEXT}" ]] || \
    fail "unexpected kubectl context: ${current_context}"

  kube get namespace "${VAULT_NAMESPACE}" >/dev/null
  kube get namespace "${MYSQL_NAMESPACE}" >/dev/null
  kube -n "${VAULT_NAMESPACE}" get serviceaccount "${VAULT_SERVICE_ACCOUNT}" >/dev/null

  pod_phase="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.phase}')"
  pod_ready="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')"
  [[ "${pod_phase}" == "Running" && "${pod_ready}" == "true" ]] || \
    fail "Vault Pod is not Running and Ready"

  pod_phase="$(kube -n "${MYSQL_NAMESPACE}" get pod "${MYSQL_POD}" -o jsonpath='{.status.phase}')"
  pod_ready="$(kube -n "${MYSQL_NAMESPACE}" get pod "${MYSQL_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')"
  [[ "${pod_phase}" == "Running" && "${pod_ready}" == "true" ]] || \
    fail "MySQL Pod is not Running and Ready"

  [[ -n "$(kube -n "${MYSQL_NAMESPACE}" get secret "${MYSQL_ROOT_SECRET}" \
    -o "jsonpath={.data.${MYSQL_ROOT_SECRET_KEY}}")" ]] || \
    fail "MySQL root Secret key is missing"

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
    mktemp -d /tmp/pawbridge-store-mysql-bootstrap.XXXXXX)"
  [[ "${TOKEN_SESSION_DIR}" == /tmp/pawbridge-store-mysql-bootstrap.* ]] || \
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

validate_vault_prerequisites() {
  local mount_type
  local mount_options
  local auth_type
  local actual_host
  local actual_disable_local_ca_jwt
  local actual_disable_iss_validation

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
  actual_host="$(vault_cli read -field=kubernetes_host "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  actual_disable_local_ca_jwt="$(vault_cli read -field=disable_local_ca_jwt "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  actual_disable_iss_validation="$(vault_cli read -field=disable_iss_validation "auth/${KUBERNETES_AUTH_MOUNT}/config")"
  [[ "${actual_host}" == "${KUBERNETES_HOST}" ]] || fail "unexpected Kubernetes API host in Vault"
  [[ "${actual_disable_local_ca_jwt}" == "false" ]] || fail "Vault must use its local Kubernetes token and CA"
  [[ "${actual_disable_iss_validation}" == "true" ]] || fail "Kubernetes issuer validation setting is unexpected"
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

vault_credential_exists() {
  vault_cli kv metadata get -mount="${KV_MOUNT}" "${KV_PATH}" >/dev/null 2>&1
}

write_new_vault_credential() {
  printf '%s' "${MYSQL_APP_PASSWORD}" | \
    timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
      kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
      -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
      env HOME="${TOKEN_SESSION_DIR}" \
      vault kv put -mount="${KV_MOUNT}" -cas=0 "${KV_PATH}" \
        "${KV_PASSWORD_KEY}=-" >/dev/null
}

read_vault_credential() {
  MYSQL_APP_PASSWORD="$(vault_cli kv get -mount="${KV_MOUNT}" \
    -field="${KV_PASSWORD_KEY}" "${KV_PATH}")"

  [[ "${MYSQL_APP_PASSWORD}" =~ ^[0-9a-f]{64}$ ]] || \
    fail "Vault Store MySQL password does not match the generated credential contract"
}

ensure_vault_credential() {
  if vault_credential_exists; then
    echo "Vault Store MySQL credential metadata exists; the current value will be reused."
  else
    [[ "${MODE}" == apply ]] || fail "Vault Store MySQL credential is missing"
    MYSQL_APP_PASSWORD="$(openssl rand -hex 32)"
    [[ "${MYSQL_APP_PASSWORD}" =~ ^[0-9a-f]{64}$ ]] || \
      fail "failed to generate the Store MySQL password"
    write_new_vault_credential
    vault_credential_exists || fail "Vault Store MySQL credential metadata is missing after write"
    echo "Created the Store MySQL credential in Vault without printing its value."
  fi

  read_vault_credential
}

read_mysql_root_credential() {
  local encoded_password

  encoded_password="$(kube -n "${MYSQL_NAMESPACE}" get secret "${MYSQL_ROOT_SECRET}" \
    -o "jsonpath={.data.${MYSQL_ROOT_SECRET_KEY}}")"
  [[ -n "${encoded_password}" ]] || fail "MySQL root password data is empty"

  MYSQL_ROOT_PASSWORD="$(printf '%s' "${encoded_password}" | base64 --decode)"
  [[ -n "${MYSQL_ROOT_PASSWORD}" ]] || fail "decoded MySQL root password is empty"
  [[ "${MYSQL_ROOT_PASSWORD}" != *$'\n'* && "${MYSQL_ROOT_PASSWORD}" != *$'\r'* ]] || \
    fail "MySQL root password contains an unsupported line break"
}

mysql_exec() {
  local username="${1:?MySQL username is required}"
  local password="${2:?MySQL password is required}"
  local database="${3:?MySQL database is required}"
  local sql="${4:?SQL is required}"

  {
    printf '%s\n' "${password}"
    printf '%s\n' "${sql}"
  } | kube -n "${MYSQL_NAMESPACE}" exec -i "${MYSQL_POD}" -- \
    sh -eu -c '
      IFS= read -r MYSQL_PWD
      export MYSQL_PWD
      MYSQL_HISTFILE=/dev/null
      export MYSQL_HISTFILE
      set +e
      mysql --protocol=TCP --host=127.0.0.1 --user="$1" \
        --batch --skip-column-names --raw "$2"
      mysql_status=$?
      set -e
      unset MYSQL_PWD
      exit "$mysql_status"
    ' sh "${username}" "${database}"
}

validate_mysql_grants() {
  local grants
  local grant_line
  local expected_database_grant
  local database_grant_found=false

  grants="$(mysql_exec root "${MYSQL_ROOT_PASSWORD}" mysql \
    "SHOW GRANTS FOR '${MYSQL_USERNAME}'@'%';")" || \
    fail "dedicated Store MySQL account is missing or unreadable"

  expected_database_grant="GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO \`${MYSQL_USERNAME}\`@\`%\`"

  while IFS= read -r grant_line; do
    case "${grant_line}" in
      "${expected_database_grant}")
        database_grant_found=true
        ;;
      "GRANT USAGE ON *.* TO \`${MYSQL_USERNAME}\`@\`%\`"*)
        ;;
      "")
        ;;
      *)
        fail "dedicated Store MySQL account has an unexpected privilege"
        ;;
    esac
  done <<< "${grants}"

  [[ "${database_grant_found}" == true ]] || \
    fail "dedicated Store MySQL account lacks the exact database-scoped grant"
}

validate_mysql_login() {
  local result
  result="$(mysql_exec "${MYSQL_USERNAME}" "${MYSQL_APP_PASSWORD}" \
    "${MYSQL_DATABASE}" 'SELECT 1;')" || \
    fail "dedicated Store MySQL login failed"
  [[ "${result}" == "1" ]] || fail "dedicated Store MySQL validation returned an unexpected result"
}

ensure_mysql_account() {
  local sql

  read_mysql_root_credential

  if [[ "${MODE}" == apply ]]; then
    sql="CREATE USER IF NOT EXISTS '${MYSQL_USERNAME}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
ALTER USER '${MYSQL_USERNAME}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${MYSQL_USERNAME}'@'%';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USERNAME}'@'%';"
    if ! mysql_exec root "${MYSQL_ROOT_PASSWORD}" mysql "${sql}" >/dev/null 2>&1; then
      fail "failed to create or repair the dedicated Store MySQL account"
    fi
    echo "Created or repaired the database-scoped Store MySQL account."
  fi

  validate_mysql_grants
  validate_mysql_login
  MYSQL_ROOT_PASSWORD=""
  MYSQL_APP_PASSWORD=""
  echo "Verified the Store MySQL account grant and login."
}

main() {
  validate_local_inputs
  validate_cluster_target
  validate_token_session

  validate_vault_prerequisites
  ensure_policy
  ensure_role
  ensure_vault_credential
  ensure_mysql_account

  echo "Store MySQL Vault bootstrap ${MODE} completed without printing secret values."
}

trap on_error ERR
trap cleanup_token_session EXIT

main
