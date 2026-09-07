#!/usr/bin/env bash

# Remote sh snippets intentionally expand positional parameters in the target Pod.
# shellcheck disable=SC2016

set -Eeuo pipefail
umask 077

readonly MODE="${1:-check}"
readonly EXPECTED_CONTEXT="pawbridge-vbox-k136"
readonly VAULT_NAMESPACE="vault"
readonly VAULT_POD="vault-0"
readonly DATABASE_NAMESPACE="databases"
readonly PAWBRIDGE_NAMESPACE="pawbridge"
readonly ES_NAME="store-search"
readonly ES_CA_SECRET="store-search-es-http-certs-public"
readonly KV_MOUNT="secret"
readonly KUBERNETES_AUTH_MOUNT="kubernetes"
readonly TOKEN_AUDIENCE="vault"
readonly TOKEN_TTL_SECONDS="600"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly LOGIN_TIMEOUT_SECONDS="120"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TEMP_PREFIX="/tmp/pawbridge-animal-search-bootstrap"

readonly ANIMAL_WRITER_PATH="pawbridge/dev/animal/elasticsearch/writer"
readonly ANIMAL_BOOTSTRAP_PATH="pawbridge/dev/animal/elasticsearch/bootstrap"
readonly ANIMAL_TRUST_PATH="pawbridge/dev/animal/elasticsearch/trust"
readonly PYTHON_WRITER_PATH="pawbridge/dev/python/elasticsearch/writer"
readonly PYTHON_TRUST_PATH="pawbridge/dev/python/elasticsearch/trust"

readonly ANIMAL_WRITER_USERNAME="pawbridge_animal_writer"
readonly ANIMAL_WRITER_ROLE="pawbridge_animal_writer"
readonly ANIMAL_BOOTSTRAP_USERNAME="pawbridge_animal_bootstrap"
readonly ANIMAL_BOOTSTRAP_ROLE="pawbridge_animal_bootstrap"
readonly PYTHON_WRITER_USERNAME="pawbridge_python_writer"
readonly PYTHON_WRITER_ROLE="pawbridge_python_writer"

readonly POLICY_DATABASES="animal-search-databases-read"
readonly POLICY_ANIMAL="animal-search-pawbridge-read"
readonly POLICY_PYTHON="python-search-pawbridge-read"

TOKEN_SESSION_DIR=""
LOCAL_TEMP_DIR=""

usage() {
  cat <<'USAGE'
Usage:
  configure-animal-search-vso.sh check
  configure-animal-search-vso.sh apply

check verifies the Animal/Python search Vault policies, Kubernetes auth roles,
credential shapes, and ECK HTTP CA trust material without changing state.
apply creates or repairs them without printing secret values.

The script does not modify Elasticsearch roles, aliases, indices, or workloads.
Those changes remain explicit GitOps sync or cutover operations.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  echo "Animal search Vault bootstrap failed at line ${BASH_LINENO[0]}" >&2
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

vault_cli_stdin() {
  [[ -n "${TOKEN_SESSION_DIR}" ]] || fail "Vault CLI session is not initialized"
  kube -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
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
        sh -c 'case "$1" in /tmp/pawbridge-animal-search-bootstrap.*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
        sh "${TOKEN_SESSION_DIR}" >/dev/null 2>&1 || cleanup_failed=true
    else
      echo "Vault token revocation failed; recovery directory preserved in ${VAULT_NAMESPACE}/${VAULT_POD}: ${TOKEN_SESSION_DIR}" >&2
    fi
  fi

  if [[ -n "${LOCAL_TEMP_DIR}" ]]; then
    case "${LOCAL_TEMP_DIR}" in
      /tmp/pawbridge-animal-search-local.*) rm -rf -- "${LOCAL_TEMP_DIR}" ;;
      *)
        echo "ERROR: refusing to clean unexpected local temporary directory" >&2
        cleanup_failed=true
        ;;
    esac
  fi
  set -e

  if [[ "${cleanup_failed}" == true ]]; then
    echo "ERROR: temporary secret cleanup failed; manual recovery is required" >&2
    [[ "${exit_code}" -eq 0 ]] && exit_code=1
  else
    echo "Isolated Vault session and local secret files were removed."
  fi
  exit "${exit_code}"
}

normalize() {
  tr -d '[:space:][]",'
}

validate_inputs() {
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

  for command_name in kubectl timeout base64 openssl awk grep sha256sum tr; do
    command -v "${command_name}" >/dev/null 2>&1 || fail "missing command: ${command_name}"
  done

  for policy_name in "${POLICY_DATABASES}" "${POLICY_ANIMAL}" "${POLICY_PYTHON}"; do
    [[ -s "${SCRIPT_DIR}/policies/${policy_name}.hcl" ]] || \
      fail "missing policy file: ${policy_name}.hcl"
    grep -Fq 'capabilities = ["read"]' "${SCRIPT_DIR}/policies/${policy_name}.hcl" || \
      fail "policy does not grant the expected read-only capability: ${policy_name}"
    if grep -Fq '*' "${SCRIPT_DIR}/policies/${policy_name}.hcl"; then
      fail "Vault policy wildcards are not allowed: ${policy_name}"
    fi
  done
}

validate_cluster_target() {
  local es_health

  [[ "$(kube config current-context 2>/dev/null || true)" == "${EXPECTED_CONTEXT}" ]] || \
    fail "unexpected kubectl context"
  kube get namespace "${VAULT_NAMESPACE}" "${DATABASE_NAMESPACE}" "${PAWBRIDGE_NAMESPACE}" >/dev/null
  [[ "$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.phase}')" == Running ]] || \
    fail "Vault Pod is not Running"
  [[ "$(kube -n "${VAULT_NAMESPACE}" get pod "${VAULT_POD}" -o jsonpath='{.status.containerStatuses[0].ready}')" == true ]] || \
    fail "Vault Pod is not Ready"
  kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- vault status >/dev/null || \
    fail "Vault is unavailable or sealed"

  es_health="$(kube -n "${DATABASE_NAMESPACE}" get elasticsearch "${ES_NAME}" \
    -o jsonpath='{.status.health}:{.status.phase}')"
  [[ "${es_health}" == green:Ready || "${es_health}" == yellow:Ready ]] || \
    fail "Elasticsearch is not Ready"
  [[ -n "$(kube -n "${DATABASE_NAMESPACE}" get secret "${ES_CA_SECRET}" \
    -o 'jsonpath={.data.ca\.crt}')" ]] || fail "ECK HTTP CA Secret key is missing"
}

initialize_session() {
  local vault_username

  [[ -t 0 && -t 1 ]] || fail "an interactive terminal is required for Vault login"
  read -r -p "Vault admin username: " vault_username
  [[ "${vault_username}" =~ ^[A-Za-z0-9._-]+$ ]] || fail "Vault username contains unsupported characters"

  TOKEN_SESSION_DIR="$(kube -n "${VAULT_NAMESPACE}" exec "${VAULT_POD}" -- \
    mktemp -d "${TEMP_PREFIX}.XXXXXX")"
  [[ "${TOKEN_SESSION_DIR}" == ${TEMP_PREFIX}.* ]] || fail "unexpected Vault session directory"
  LOCAL_TEMP_DIR="$(mktemp -d /tmp/pawbridge-animal-search-local.XXXXXX)"

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
  local mount_options

  [[ "$(vault_cli read -field=type "sys/mounts/${KV_MOUNT}" 2>/dev/null || true)" == kv ]] || \
    fail "${KV_MOUNT}/ must be an existing KV mount"
  mount_options="$(vault_cli read -field=options "sys/mounts/${KV_MOUNT}")"
  [[ "${mount_options}" == *"version:2"* ]] || fail "${KV_MOUNT}/ must be KV v2"
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
  kube -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
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
  [[ "${field}" == "${TOKEN_TTL_SECONDS}" ]]
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
  local role="${3:?role is required}"
  local actual_username=""
  local actual_password=""
  local actual_role=""
  local credential_exists=false
  local credential_matches=false
  local credential_version=""
  local -a put_arguments=(kv put -mount="${KV_MOUNT}")

  if kv_exists "${path}"; then
    credential_exists=true
    credential_version="$(vault_cli read -field=current_version "${KV_MOUNT}/metadata/${path}")"
    [[ "${credential_version}" =~ ^[1-9][0-9]*$ ]] || fail "invalid Vault credential version: ${path}"
    actual_username="$(vault_cli kv get -mount="${KV_MOUNT}" -field=username "${path}" 2>/dev/null || true)"
    actual_password="$(vault_cli kv get -mount="${KV_MOUNT}" -field=password "${path}" 2>/dev/null || true)"
    actual_role="$(vault_cli kv get -mount="${KV_MOUNT}" -field=roles "${path}" 2>/dev/null || true)"
    [[ "${actual_username}" == "${username}" && "${actual_password}" =~ ^[0-9a-f]{64}$ && \
      "${actual_role}" == "${role}" ]] && credential_matches=true
  fi

  if [[ "${credential_matches}" == false ]]; then
    [[ "${MODE}" == apply ]] || fail "Vault credential is missing or invalid: ${path}"
    actual_password="$(openssl rand -hex 32)"
    [[ "${actual_password}" =~ ^[0-9a-f]{64}$ ]] || fail "failed to generate Vault credential: ${path}"
    if [[ "${credential_exists}" == false ]]; then
      put_arguments+=(-cas=0)
    else
      put_arguments+=(-cas="${credential_version}")
    fi
    put_arguments+=("${path}" username="${username}" password=- roles="${role}")
    printf '%s' "${actual_password}" | vault_cli_stdin "${put_arguments[@]}" >/dev/null || \
      fail "failed to create or repair Vault credential: ${path}"
    echo "Created or repaired Elasticsearch credential without printing its value: ${path}"
  fi

  actual_username="$(vault_cli kv get -mount="${KV_MOUNT}" -field=username "${path}")"
  actual_password="$(vault_cli kv get -mount="${KV_MOUNT}" -field=password "${path}")"
  actual_role="$(vault_cli kv get -mount="${KV_MOUNT}" -field=roles "${path}")"
  [[ "${actual_username}" == "${username}" && "${actual_password}" =~ ^[0-9a-f]{64}$ && \
    "${actual_role}" == "${role}" ]] || fail "Vault credential contract mismatch: ${path}"
  actual_password=""
  echo "Verified Elasticsearch credential metadata and shape: ${path}"
}

read_eck_ca() {
  kube -n "${DATABASE_NAMESPACE}" get secret "${ES_CA_SECRET}" \
    -o 'jsonpath={.data.ca\.crt}' | base64 --decode > "${LOCAL_TEMP_DIR}/ca.crt"
  openssl x509 -in "${LOCAL_TEMP_DIR}/ca.crt" -noout -checkend 86400 >/dev/null || \
    fail "ECK HTTP CA is invalid or expires within 24 hours"
}

trust_matches() {
  local path="${1:?path is required}"
  local expected_hash
  local actual_hash

  kv_exists "${path}" || return 1
  expected_hash="$(sha256sum "${LOCAL_TEMP_DIR}/ca.crt" | awk '{print $1}')"
  actual_hash="$(vault_cli kv get -mount="${KV_MOUNT}" -field=ca-sha256 "${path}" 2>/dev/null || true)"
  [[ "${actual_hash}" == "${expected_hash}" ]] || return 1
  vault_cli kv get -mount="${KV_MOUNT}" -field=ca.crt "${path}" > "${LOCAL_TEMP_DIR}/vault-ca.crt"
  [[ "$(sha256sum "${LOCAL_TEMP_DIR}/vault-ca.crt" | awk '{print $1}')" == "${expected_hash}" ]] || return 1
  openssl x509 -in "${LOCAL_TEMP_DIR}/vault-ca.crt" -noout -checkend 86400 >/dev/null 2>&1
}

ensure_trust() {
  local path="${1:?path is required}"
  local ca_hash

  if trust_matches "${path}"; then
    echo "Vault trust material matches the current ECK HTTP CA: ${path}"
    return
  fi
  [[ "${MODE}" == apply ]] || fail "Vault trust material is missing, invalid, or stale: ${path}"
  ca_hash="$(sha256sum "${LOCAL_TEMP_DIR}/ca.crt" | awk '{print $1}')"
  kube -n "${VAULT_NAMESPACE}" exec -i "${VAULT_POD}" -- \
    env HOME="${TOKEN_SESSION_DIR}" vault kv put -mount="${KV_MOUNT}" "${path}" \
    ca.crt=- ca-sha256="${ca_hash}" < "${LOCAL_TEMP_DIR}/ca.crt" >/dev/null
  trust_matches "${path}" || fail "Vault trust material verification failed after write: ${path}"
  echo "Stored and verified the ECK HTTP CA without printing its value: ${path}"
}

main() {
  validate_inputs
  validate_cluster_target
  initialize_session
  validate_vault_prerequisites

  ensure_policy "${POLICY_DATABASES}"
  ensure_policy "${POLICY_ANIMAL}"
  ensure_policy "${POLICY_PYTHON}"
  ensure_role "${POLICY_DATABASES}" animal-search-databases-vault-auth "${DATABASE_NAMESPACE}"
  ensure_role "${POLICY_ANIMAL}" animal-search-pawbridge-vault-auth "${PAWBRIDGE_NAMESPACE}"
  ensure_role "${POLICY_PYTHON}" python-search-pawbridge-vault-auth "${PAWBRIDGE_NAMESPACE}"

  ensure_credential "${ANIMAL_WRITER_PATH}" "${ANIMAL_WRITER_USERNAME}" "${ANIMAL_WRITER_ROLE}"
  ensure_credential "${ANIMAL_BOOTSTRAP_PATH}" "${ANIMAL_BOOTSTRAP_USERNAME}" "${ANIMAL_BOOTSTRAP_ROLE}"
  ensure_credential "${PYTHON_WRITER_PATH}" "${PYTHON_WRITER_USERNAME}" "${PYTHON_WRITER_ROLE}"

  read_eck_ca
  ensure_trust "${ANIMAL_TRUST_PATH}"
  ensure_trust "${PYTHON_TRUST_PATH}"
  echo "Animal/Python search Vault bootstrap ${MODE} completed without printing secret values."
}

trap on_error ERR
trap cleanup EXIT

main
