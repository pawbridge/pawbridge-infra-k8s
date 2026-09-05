#!/usr/bin/env bash

# Remote sh snippets intentionally expand positional parameters in the target Pod.
# shellcheck disable=SC2016

set -Eeuo pipefail
umask 077

readonly MODE="${1:-check}"
readonly PHASE="${2:-}"
readonly EXPECTED_CONTEXT="pawbridge-vbox-k136"
readonly VAULT_NAMESPACE="vault"
readonly VAULT_POD="vault-0"
readonly DATABASE_NAMESPACE="databases"
readonly KAFKA_NAMESPACE="kafka"
readonly PAWBRIDGE_NAMESPACE="pawbridge"
readonly MYSQL_POD="mysql-0"
readonly MYSQL_ROOT_SECRET="mysql-auth"
readonly MYSQL_ROOT_SECRET_KEY="mysql-root-password"
readonly MYSQL_DATABASE="pawbridge_store"
readonly MYSQL_CDC_USERNAME="pawbridge_store_connector"
readonly ES_CA_SECRET="store-search-es-http-certs-public"
readonly ES_CA_SECRET_KEY="ca.crt"
readonly KV_MOUNT="secret"
readonly KUBERNETES_AUTH_MOUNT="kubernetes"
readonly TOKEN_AUDIENCE="vault"
readonly TOKEN_TTL_SECONDS="600"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly LOGIN_TIMEOUT_SECONDS="120"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TEMP_PREFIX="/tmp/pawbridge-store-search-bootstrap"

readonly ES_READER_PATH="pawbridge/dev/store/elasticsearch/reader"
readonly ES_WRITER_PATH="pawbridge/dev/store/elasticsearch/writer"
readonly ES_BOOTSTRAP_PATH="pawbridge/dev/store/elasticsearch/bootstrap"
readonly ES_TRUST_PATH="pawbridge/dev/store/elasticsearch/trust"
readonly MYSQL_CDC_PATH="pawbridge/dev/store/mysql-cdc"

readonly POLICY_DATABASES="store-search-databases-read"
readonly POLICY_PAWBRIDGE="store-search-pawbridge-read"
readonly POLICY_KAFKA="store-search-kafka-read"

TOKEN_SESSION_DIR=""
LOCAL_TEMP_DIR=""
MYSQL_ROOT_PASSWORD=""
MYSQL_CDC_PASSWORD=""

usage() {
  cat <<'USAGE'
Usage:
  configure-store-search-vso.sh check bootstrap
  configure-store-search-vso.sh apply bootstrap
  configure-store-search-vso.sh check trust
  configure-store-search-vso.sh apply trust
  configure-store-search-vso.sh check all
  configure-store-search-vso.sh apply all

bootstrap handles the three least-privilege Vault policies and Kubernetes auth
roles, Elasticsearch file-realm credentials, and the dedicated MySQL CDC
account. It does not require an Elasticsearch cluster or ECK HTTP CA.

trust verifies or stores the current ECK HTTP CA and its PKCS12 truststore after
Elasticsearch has become Ready. all runs bootstrap and trust for an existing
cluster. check never changes state; apply creates or repairs the selected phase.
Existing application credentials are reused and are not routinely rotated.

Secret values stay in isolated temporary files or shell memory, are never
printed, and are removed before exit. apply mutates Vault and MySQL and must be
run only after the exact live target and rollback boundary are approved.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  echo "Store search Vault bootstrap failed at line ${BASH_LINENO[0]}" >&2
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

cleanup() {
  local exit_code=$?
  local cleanup_failed=false

  trap - EXIT
  set +e
  if [[ -n "${TOKEN_SESSION_DIR}" ]]; then
    case "${TOKEN_SESSION_DIR}" in
      ${TEMP_PREFIX}.*) ;;
      *)
        echo "ERROR: refusing to clean unexpected Vault session directory" >&2
        cleanup_failed=true
        ;;
    esac

    if [[ "${cleanup_failed}" == false ]] && \
      kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
        sh -c 'test -s "$1/.vault-token"' sh "${TOKEN_SESSION_DIR}" >/dev/null 2>&1; then
      vault_cli token revoke -self >/dev/null 2>&1 || cleanup_failed=true
    fi

    if [[ "${cleanup_failed}" == false ]]; then
      kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
        sh -c 'case "$1" in /tmp/pawbridge-store-search-bootstrap.*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
        sh "${TOKEN_SESSION_DIR}" >/dev/null 2>&1 || cleanup_failed=true
    else
      echo "Vault token revocation failed; recovery directory preserved in ${VAULT_NAMESPACE}/${VAULT_POD}: ${TOKEN_SESSION_DIR}" >&2
    fi
  fi

  if [[ -n "${LOCAL_TEMP_DIR}" ]]; then
    case "${LOCAL_TEMP_DIR}" in
      /tmp/pawbridge-store-search-local.*) rm -rf -- "${LOCAL_TEMP_DIR}" ;;
      *)
        echo "ERROR: refusing to clean unexpected local temporary directory" >&2
        cleanup_failed=true
        ;;
    esac
  fi
  set -e

  MYSQL_ROOT_PASSWORD=""
  MYSQL_CDC_PASSWORD=""
  if [[ "${cleanup_failed}" == true ]]; then
    echo "ERROR: temporary secret cleanup failed; manual recovery is required" >&2
    [[ "${exit_code}" -eq 0 ]] && exit_code=1
  else
    echo "Isolated Vault session and local secret files were removed."
  fi
  exit "${exit_code}"
}

require_command() {
  command -v "${1:?command is required}" >/dev/null 2>&1 || fail "missing command: $1"
}

normalize() {
  tr -d '[:space:][]",'
}

validate_local_inputs() {
  local command_name
  local policy_name

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

  case "${PHASE}" in
    bootstrap | trust | all) ;;
    *)
      usage >&2
      fail "phase must be bootstrap, trust, or all"
      ;;
  esac

  for command_name in kubectl timeout base64 openssl keytool sha256sum jq; do
    require_command "${command_name}"
  done

  if [[ "${PHASE}" == bootstrap || "${PHASE}" == all ]]; then
    for policy_name in "${POLICY_DATABASES}" "${POLICY_PAWBRIDGE}" "${POLICY_KAFKA}"; do
      [[ -s "${SCRIPT_DIR}/policies/${policy_name}.hcl" ]] || \
        fail "missing policy file: ${policy_name}.hcl"
      grep -Fq 'capabilities = ["read"]' "${SCRIPT_DIR}/policies/${policy_name}.hcl" || \
        fail "policy does not grant the expected read-only capability: ${policy_name}"
      if grep -Fq '*' "${SCRIPT_DIR}/policies/${policy_name}.hcl"; then
        fail "Vault policy wildcards are not allowed: ${policy_name}"
      fi
    done
  fi
}

validate_cluster_target() {
  local current_context
  local namespace
  local pod_phase
  local pod_ready

  current_context="$(kube config current-context 2>/dev/null || true)"
  [[ "${current_context}" == "${EXPECTED_CONTEXT}" ]] || \
    fail "unexpected kubectl context: ${current_context}"

  for namespace in "${VAULT_NAMESPACE}" "${DATABASE_NAMESPACE}" "${KAFKA_NAMESPACE}" "${PAWBRIDGE_NAMESPACE}"; do
    kube get namespace "${namespace}" >/dev/null
  done

  pod_phase="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.phase}')"
  pod_ready="$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')"
  [[ "${pod_phase}" == Running && "${pod_ready}" == true ]] || fail "Vault Pod is not Running and Ready"
  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- vault status >/dev/null || \
    fail "Vault is unavailable or sealed"

  if [[ "${PHASE}" == bootstrap || "${PHASE}" == all ]]; then
    pod_phase="$(kube -n "${DATABASE_NAMESPACE}" get pod "${MYSQL_POD}" -o jsonpath='{.status.phase}')"
    pod_ready="$(kube -n "${DATABASE_NAMESPACE}" get pod "${MYSQL_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')"
    [[ "${pod_phase}" == Running && "${pod_ready}" == true ]] || fail "MySQL Pod is not Running and Ready"

    [[ -n "$(kube -n "${DATABASE_NAMESPACE}" get secret "${MYSQL_ROOT_SECRET}" \
      -o "jsonpath={.data.${MYSQL_ROOT_SECRET_KEY}}")" ]] || fail "MySQL root Secret key is missing"
  fi

  if [[ "${PHASE}" == trust || "${PHASE}" == all ]]; then
    [[ -n "$(kube -n "${DATABASE_NAMESPACE}" get secret "${ES_CA_SECRET}" \
      -o 'jsonpath={.data.ca\.crt}')" ]] || fail "ECK HTTP CA Secret key is missing"
  fi
}

initialize_sessions() {
  local vault_username

  [[ -t 0 && -t 1 ]] || fail "an interactive terminal is required for Vault login"
  read -r -p "Vault admin username: " vault_username
  [[ "${vault_username}" =~ ^[A-Za-z0-9._-]+$ ]] || fail "Vault username contains unsupported characters"

  TOKEN_SESSION_DIR="$(kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    mktemp -d "${TEMP_PREFIX}.XXXXXX")"
  [[ "${TOKEN_SESSION_DIR}" == ${TEMP_PREFIX}.* ]] || fail "unexpected Vault session directory"
  LOCAL_TEMP_DIR="$(mktemp -d /tmp/pawbridge-store-search-local.XXXXXX)"

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
  [[ "${mount_type}" == kv ]] || fail "${KV_MOUNT}/ must be an existing KV mount"
  mount_options="$(vault_cli read -field=options "sys/mounts/${KV_MOUNT}")"
  [[ "${mount_options}" == *"version:2"* ]] || fail "${KV_MOUNT}/ must be KV v2"

  auth_type="$(vault_cli auth list -detailed | awk -v path="${KUBERNETES_AUTH_MOUNT}/" \
    '$1 == path { print $2; exit }')"
  [[ "${auth_type}" == kubernetes ]] || fail "Kubernetes auth mount is missing"
  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/config" >/dev/null || \
    fail "Kubernetes auth config is missing"
}

policy_matches() {
  local policy_name="${1:?policy name is required}"
  local expected_policy
  local actual_policy
  vault_cli policy read "${policy_name}" >/dev/null 2>&1 || return 1
  expected_policy="$(normalize < "${SCRIPT_DIR}/policies/${policy_name}.hcl")"
  actual_policy="$(vault_cli policy read "${policy_name}" | normalize)"
  [[ "${actual_policy}" == "${expected_policy}" ]]
}

ensure_policy() {
  local policy_name="${1:?policy name is required}"
  if policy_matches "${policy_name}"; then
    echo "Vault policy already matches: ${policy_name}"
    return
  fi
  [[ "${MODE}" == apply ]] || fail "Vault policy is missing or differs: ${policy_name}"
  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" vault policy write "${policy_name}" - \
    < "${SCRIPT_DIR}/policies/${policy_name}.hcl" >/dev/null
  policy_matches "${policy_name}" || fail "Vault policy verification failed: ${policy_name}"
  echo "Applied least-privilege Vault policy: ${policy_name}"
}

role_matches() {
  local role_name="${1:?role name is required}"
  local service_account="${2:?service account is required}"
  local namespace="${3:?namespace is required}"
  local field

  vault_cli read "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" >/dev/null 2>&1 || return 1
  field="$(vault_cli read -field=bound_service_account_names "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" | normalize)"
  [[ "${field}" == "${service_account}" ]] || return 1
  field="$(vault_cli read -field=bound_service_account_namespaces "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" | normalize)"
  [[ "${field}" == "${namespace}" ]] || return 1
  field="$(vault_cli read -field=audience "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" | normalize)"
  [[ "${field}" == "${TOKEN_AUDIENCE}" ]] || return 1
  field="$(vault_cli read -field=token_policies "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" | normalize)"
  [[ "${field}" == "${role_name}" ]] || return 1
  field="$(vault_cli read -field=token_ttl "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" | normalize)"
  [[ "${field}" == "${TOKEN_TTL_SECONDS}" ]] || return 1
}

ensure_role() {
  local role_name="${1:?role name is required}"
  local service_account="${2:?service account is required}"
  local namespace="${3:?namespace is required}"
  if role_matches "${role_name}" "${service_account}" "${namespace}"; then
    echo "Vault Kubernetes role already matches: ${role_name}"
    return
  fi
  [[ "${MODE}" == apply ]] || fail "Vault Kubernetes role is missing or differs: ${role_name}"
  vault_cli write "auth/${KUBERNETES_AUTH_MOUNT}/role/${role_name}" \
    bound_service_account_names="${service_account}" \
    bound_service_account_namespaces="${namespace}" \
    audience="${TOKEN_AUDIENCE}" \
    token_policies="${role_name}" \
    token_ttl="${TOKEN_TTL_SECONDS}" \
    token_max_ttl="${TOKEN_TTL_SECONDS}" >/dev/null
  role_matches "${role_name}" "${service_account}" "${namespace}" || \
    fail "Vault Kubernetes role verification failed: ${role_name}"
  echo "Applied Vault Kubernetes role: ${role_name}"
}

kv_exists() {
  vault_cli kv metadata get -mount="${KV_MOUNT}" "${1:?path is required}" >/dev/null 2>&1
}

ensure_credential() {
  local path="${1:?path is required}"
  local username="${2:?username is required}"
  local roles="${3:-}"
  local actual_username
  local actual_password
  local actual_roles=""

  if ! kv_exists "${path}"; then
    [[ "${MODE}" == apply ]] || fail "Vault credential is missing: ${path}"
    actual_password="$(openssl rand -hex 32)"
    if [[ -n "${roles}" ]]; then
      printf '%s' "${actual_password}" | vault_cli kv put -mount="${KV_MOUNT}" -cas=0 \
        "${path}" username="${username}" password=- roles="${roles}" >/dev/null
    else
      printf '%s' "${actual_password}" | vault_cli kv put -mount="${KV_MOUNT}" -cas=0 \
        "${path}" username="${username}" password=- >/dev/null
    fi
    actual_password=""
    echo "Created Vault credential without printing its value: ${path}"
  fi

  actual_username="$(vault_cli kv get -mount="${KV_MOUNT}" -field=username "${path}")"
  actual_password="$(vault_cli kv get -mount="${KV_MOUNT}" -field=password "${path}")"
  [[ "${actual_username}" == "${username}" ]] || fail "unexpected username at Vault path: ${path}"
  [[ "${actual_password}" =~ ^[0-9a-f]{64}$ ]] || fail "password contract mismatch at Vault path: ${path}"
  if [[ -n "${roles}" ]]; then
    actual_roles="$(vault_cli kv get -mount="${KV_MOUNT}" -field=roles "${path}")"
    [[ "${actual_roles}" == "${roles}" ]] || fail "role contract mismatch at Vault path: ${path}"
  fi
  actual_password=""
  echo "Verified Vault credential metadata and shape: ${path}"
}

read_eck_ca() {
  kube -n "${DATABASE_NAMESPACE}" get secret "${ES_CA_SECRET}" \
    -o 'jsonpath={.data.ca\.crt}' | base64 --decode > "${LOCAL_TEMP_DIR}/ca.crt"
  openssl x509 -in "${LOCAL_TEMP_DIR}/ca.crt" -noout -checkend 86400 >/dev/null || \
    fail "ECK HTTP CA is invalid or expires within 24 hours"
}

trust_material_matches() {
  local expected_ca_hash
  local actual_ca_hash
  local expected_truststore_hash
  local actual_truststore_hash
  local truststore_password

  kv_exists "${ES_TRUST_PATH}" || return 1
  expected_ca_hash="$(sha256sum "${LOCAL_TEMP_DIR}/ca.crt" | awk '{print $1}')"
  actual_ca_hash="$(vault_cli kv get -mount="${KV_MOUNT}" -field=ca-sha256 "${ES_TRUST_PATH}" 2>/dev/null || true)"
  [[ "${actual_ca_hash}" == "${expected_ca_hash}" ]] || return 1

  vault_cli kv get -mount="${KV_MOUNT}" -field=ca.crt "${ES_TRUST_PATH}" > "${LOCAL_TEMP_DIR}/vault-ca.crt"
  [[ "$(sha256sum "${LOCAL_TEMP_DIR}/vault-ca.crt" | awk '{print $1}')" == "${expected_ca_hash}" ]] || return 1

  vault_cli kv get -mount="${KV_MOUNT}" -field=truststore.p12-b64 "${ES_TRUST_PATH}" \
    | base64 --decode > "${LOCAL_TEMP_DIR}/truststore.p12"
  expected_truststore_hash="$(vault_cli kv get -mount="${KV_MOUNT}" -field=truststore-sha256 "${ES_TRUST_PATH}" 2>/dev/null || true)"
  actual_truststore_hash="$(sha256sum "${LOCAL_TEMP_DIR}/truststore.p12" | awk '{print $1}')"
  [[ "${actual_truststore_hash}" == "${expected_truststore_hash}" ]] || return 1

  truststore_password="$(vault_cli kv get -mount="${KV_MOUNT}" -field=truststore-password "${ES_TRUST_PATH}")"
  [[ "${truststore_password}" =~ ^[0-9a-f]{64}$ ]] || return 1
  keytool -list -storetype PKCS12 -keystore "${LOCAL_TEMP_DIR}/truststore.p12" \
    -storepass "${truststore_password}" -alias store-search-http-ca >/dev/null 2>&1 || return 1
  truststore_password=""
}

write_trust_material() {
  local ca_hash
  local truststore_hash
  local truststore_password

  truststore_password="$(openssl rand -hex 32)"
  printf '%s' "${truststore_password}" > "${LOCAL_TEMP_DIR}/truststore-password"
  keytool -importcert -noprompt -storetype PKCS12 \
    -alias store-search-http-ca \
    -file "${LOCAL_TEMP_DIR}/ca.crt" \
    -keystore "${LOCAL_TEMP_DIR}/truststore.p12" \
    -storepass "${truststore_password}" >/dev/null 2>&1
  base64 --wrap=0 "${LOCAL_TEMP_DIR}/truststore.p12" > "${LOCAL_TEMP_DIR}/truststore.p12.b64"
  ca_hash="$(sha256sum "${LOCAL_TEMP_DIR}/ca.crt" | awk '{print $1}')"
  truststore_hash="$(sha256sum "${LOCAL_TEMP_DIR}/truststore.p12" | awk '{print $1}')"

  jq -n \
    --rawfile ca "${LOCAL_TEMP_DIR}/ca.crt" \
    --rawfile truststore "${LOCAL_TEMP_DIR}/truststore.p12.b64" \
    --rawfile password "${LOCAL_TEMP_DIR}/truststore-password" \
    --arg ca_hash "${ca_hash}" \
    --arg truststore_hash "${truststore_hash}" \
    '{"ca.crt": $ca, "ca-sha256": $ca_hash, "truststore.p12-b64": $truststore, "truststore-password": $password, "truststore-sha256": $truststore_hash}' \
    > "${LOCAL_TEMP_DIR}/trust.json"

  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" \
    -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" vault kv put -mount="${KV_MOUNT}" "${ES_TRUST_PATH}" - \
    < "${LOCAL_TEMP_DIR}/trust.json" >/dev/null
  truststore_password=""
  echo "Stored ECK CA and PKCS12 trust material in Vault without printing values."
}

ensure_trust_material() {
  read_eck_ca
  if trust_material_matches; then
    echo "Vault trust material matches the current ECK HTTP CA."
    return
  fi
  [[ "${MODE}" == apply ]] || fail "Vault trust material is missing, invalid, or stale"
  write_trust_material
  trust_material_matches || fail "Vault trust material verification failed after write"
  echo "Verified Vault trust material hashes and PKCS12 contents."
}

read_mysql_root_password() {
  MYSQL_ROOT_PASSWORD="$(kube -n "${DATABASE_NAMESPACE}" get secret "${MYSQL_ROOT_SECRET}" \
    -o "jsonpath={.data.${MYSQL_ROOT_SECRET_KEY}}" | base64 --decode)"
  [[ -n "${MYSQL_ROOT_PASSWORD}" ]] || fail "decoded MySQL root password is empty"
  [[ "${MYSQL_ROOT_PASSWORD}" != *$'\n'* && "${MYSQL_ROOT_PASSWORD}" != *$'\r'* ]] || \
    fail "MySQL root password contains an unsupported line break"
}

mysql_exec() {
  local username="${1:?username is required}"
  local password="${2:?password is required}"
  local database="${3:?database is required}"
  local sql="${4:?SQL is required}"
  {
    printf '%s\n' "${password}"
    printf '%s\n' "${sql}"
  } | kube -n "${DATABASE_NAMESPACE}" exec -i "${MYSQL_POD}" -- \
    sh -eu -c '
      IFS= read -r MYSQL_PWD
      export MYSQL_PWD
      MYSQL_HISTFILE=/dev/null
      export MYSQL_HISTFILE
      mysql --protocol=TCP --host=127.0.0.1 --user="$1" \
        --batch --skip-column-names --raw "$2"
      unset MYSQL_PWD
    ' sh "${username}" "${database}"
}

validate_mysql_cdc_grants() {
  local grants
  local grant_line
  local privilege_list
  local privilege
  local -a seen=()

  grants="$(mysql_exec root "${MYSQL_ROOT_PASSWORD}" mysql \
    "SHOW GRANTS FOR '${MYSQL_CDC_USERNAME}'@'%';")" || fail "MySQL CDC account is missing"
  grant_line="$(printf '%s\n' "${grants}" | grep -F ' ON *.* TO ' || true)"
  [[ -n "${grant_line}" ]] || fail "MySQL CDC account lacks a global grant"
  [[ "$(printf '%s\n' "${grants}" | grep -c '^GRANT ' || true)" -eq 1 ]] || \
    fail "MySQL CDC account has unexpected additional grants"

  privilege_list="${grant_line#GRANT }"
  privilege_list="${privilege_list%% ON *}"
  IFS=',' read -r -a seen <<< "${privilege_list}"
  [[ "${#seen[@]}" -eq 6 ]] || fail "MySQL CDC privilege count differs from the contract"
  for privilege in "${seen[@]}"; do
    privilege="$(printf '%s' "${privilege}" | sed 's/^ *//;s/ *$//')"
    case "${privilege}" in
      SELECT | RELOAD | "SHOW DATABASES" | "LOCK TABLES" | "REPLICATION SLAVE" | "REPLICATION CLIENT") ;;
      *) fail "MySQL CDC account has an unexpected privilege" ;;
    esac
  done

  for privilege in SELECT RELOAD "SHOW DATABASES" "LOCK TABLES" "REPLICATION SLAVE" "REPLICATION CLIENT"; do
    [[ ",${privilege_list}," == *",${privilege},"* || ",${privilege_list}," == *", ${privilege},"* ]] || \
      fail "MySQL CDC account is missing privilege: ${privilege}"
  done
}

ensure_mysql_cdc_account() {
  local sql
  local binlog_settings

  MYSQL_CDC_PASSWORD="$(vault_cli kv get -mount="${KV_MOUNT}" -field=password "${MYSQL_CDC_PATH}")"
  [[ "${MYSQL_CDC_PASSWORD}" =~ ^[0-9a-f]{64}$ ]] || fail "MySQL CDC password contract mismatch"
  read_mysql_root_password

  binlog_settings="$(mysql_exec root "${MYSQL_ROOT_PASSWORD}" mysql \
    'SELECT @@global.log_bin, @@global.binlog_format, @@global.binlog_row_image, @@global.server_id;')" || \
    fail "failed to inspect MySQL binlog settings"
  [[ "${binlog_settings}" == $'1\tROW\tFULL\t13601' ]] || \
    fail "MySQL binlog settings differ from the CDC contract"
  echo "Verified MySQL binlog, ROW/FULL image, and server-id settings."

  if [[ "${MODE}" == apply ]]; then
    sql="CREATE USER IF NOT EXISTS '${MYSQL_CDC_USERNAME}'@'%' IDENTIFIED BY '${MYSQL_CDC_PASSWORD}';
ALTER USER '${MYSQL_CDC_USERNAME}'@'%' IDENTIFIED BY '${MYSQL_CDC_PASSWORD}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${MYSQL_CDC_USERNAME}'@'%';
GRANT SELECT, RELOAD, SHOW DATABASES, LOCK TABLES, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '${MYSQL_CDC_USERNAME}'@'%';"
    mysql_exec root "${MYSQL_ROOT_PASSWORD}" mysql "${sql}" >/dev/null || \
      fail "failed to create or repair the MySQL CDC account"
    echo "Created or repaired the least-privilege MySQL CDC account."
  fi

  validate_mysql_cdc_grants
  [[ "$(mysql_exec "${MYSQL_CDC_USERNAME}" "${MYSQL_CDC_PASSWORD}" "${MYSQL_DATABASE}" 'SELECT 1;')" == 1 ]] || \
    fail "MySQL CDC account login validation failed"
  MYSQL_ROOT_PASSWORD=""
  MYSQL_CDC_PASSWORD=""
  echo "Verified MySQL CDC grants and login."
}

main() {
  validate_local_inputs
  validate_cluster_target
  initialize_sessions
  validate_vault_prerequisites

  if [[ "${PHASE}" == bootstrap || "${PHASE}" == all ]]; then
    ensure_policy "${POLICY_DATABASES}"
    ensure_policy "${POLICY_PAWBRIDGE}"
    ensure_policy "${POLICY_KAFKA}"
    ensure_role "${POLICY_DATABASES}" store-search-databases-vault-auth "${DATABASE_NAMESPACE}"
    ensure_role "${POLICY_PAWBRIDGE}" store-search-pawbridge-vault-auth "${PAWBRIDGE_NAMESPACE}"
    ensure_role "${POLICY_KAFKA}" store-search-kafka-vault-auth "${KAFKA_NAMESPACE}"

    ensure_credential "${ES_READER_PATH}" pawbridge_store_reader pawbridge_store_reader
    ensure_credential "${ES_WRITER_PATH}" pawbridge_store_writer pawbridge_store_writer
    ensure_credential "${ES_BOOTSTRAP_PATH}" pawbridge_store_bootstrap pawbridge_store_bootstrap
    ensure_credential "${MYSQL_CDC_PATH}" "${MYSQL_CDC_USERNAME}" ""
    ensure_mysql_cdc_account
  fi

  if [[ "${PHASE}" == trust || "${PHASE}" == all ]]; then
    ensure_trust_material
  fi

  echo "Store search Vault bootstrap ${MODE}/${PHASE} completed without printing secret values."
}

trap on_error ERR
trap cleanup EXIT

main
