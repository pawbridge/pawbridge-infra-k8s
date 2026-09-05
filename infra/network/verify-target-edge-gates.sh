#!/usr/bin/env bash

set -Eeuo pipefail

readonly MODE="${1:-vso-ready}"
readonly EXPECTED_CONTEXT="pawbridge-vbox-k136"
readonly NAMESPACE="pawbridge"
readonly ARGO_NAMESPACE="argocd"
readonly VSO_NAMESPACE="vault-secrets-operator-system"
readonly VSO_DEPLOYMENT="vault-secrets-operator-controller-manager"
readonly COMMAND_TIMEOUT_SECONDS="30"
readonly KUBECTL_REQUEST_TIMEOUT="15s"
readonly CLOUDFLARED_IMAGE="cloudflare/cloudflared:2026.8.3@sha256:51c9cefcb4569df44e1ad403ab1d3d8065aa8e84339bcfc6aee75502e1140339"
readonly GATEWAY_IMAGE="dorosiya/pawbridge-api-gateway@sha256:073f3a048d3e5b995639edbbc5afa38a74075922c19f740369d34393b34d72f2"
readonly -a CLOUDFLARE_EDGE_IPV4_CIDRS=(
  198.41.192.7/32
  198.41.192.27/32
  198.41.192.37/32
  198.41.192.47/32
  198.41.192.57/32
  198.41.192.67/32
  198.41.192.77/32
  198.41.192.107/32
  198.41.192.167/32
  198.41.192.227/32
  198.41.200.13/32
  198.41.200.23/32
  198.41.200.33/32
  198.41.200.43/32
  198.41.200.53/32
  198.41.200.63/32
  198.41.200.73/32
  198.41.200.113/32
  198.41.200.193/32
  198.41.200.233/32
)

usage() {
  cat <<'USAGE'
Usage:
  verify-target-edge-gates.sh vso-ready
  verify-target-edge-gates.sh store-edge-ready

vso-ready verifies the VSO controller, Vault CA reference, VaultStaticSecret
Ready conditions, and destination Secret key names without reading values.

store-edge-ready also verifies the three Argo Applications, API Gateway,
cloudflared, NetworkPolicies, and the current Store Service ready endpoint.
It does not claim other Gateway routes or a public Cloudflare hostname.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

kube() {
  timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
    kubectl --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" "$@"
}

expect_equal() {
  local actual="${1-}"
  local expected="${2-}"
  local label="${3:?label is required}"
  [[ "${actual}" == "${expected}" ]] || fail "${label}: expected '${expected}', got '${actual}'"
}

expected_cloudflare_edge_cidrs() {
  printf '%s\n' "${CLOUDFLARE_EDGE_IPV4_CIDRS[@]}" | sort
}

verify_cloudflare_edge_cidrs() {
  local expected_cidrs
  local policy_cidrs

  expected_cidrs="$(expected_cloudflare_edge_cidrs)"
  policy_cidrs="$(
    kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{range .spec.egress[2].to[*]}{.ipBlock.cidr}{"\n"}{end}' |
      sort
  )"
  expect_equal "${policy_cidrs}" "${expected_cidrs}" "cloudflared allowed edge CIDRs"
}

validate_target() {
  local current_context

  current_context="$(kube config current-context)"
  expect_equal "${current_context}" "${EXPECTED_CONTEXT}" "kubectl context"
  kube get namespace "${NAMESPACE}" >/dev/null
  kube get namespace "${ARGO_NAMESPACE}" >/dev/null
}

secret_keys() {
  local secret_name="${1:?secret name is required}"
  kube -n "${NAMESPACE}" get secret "${secret_name}" \
    -o go-template='{{range $key, $value := .data}}{{$key}}{{"\n"}}{{end}}' | sort
}

vso_ready_condition() {
  local resource_name="${1:?VaultStaticSecret name is required}"
  kube -n "${NAMESPACE}" get vaultstaticsecret "${resource_name}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
}

verify_vso_ready() {
  local operator_available
  local ca_keys
  local gateway_keys
  local tunnel_keys

  kube get crd vaultstaticsecrets.secrets.hashicorp.com >/dev/null
  operator_available="$(kube -n "${VSO_NAMESPACE}" get deployment "${VSO_DEPLOYMENT}" -o jsonpath='{.status.availableReplicas}')"
  expect_equal "${operator_available}" "1" "VSO available replicas"

  ca_keys="$(secret_keys vault-internal-ca)"
  expect_equal "${ca_keys}" "ca.crt" "vault-internal-ca keys"

  expect_equal "$(vso_ready_condition api-gateway-jwt)" "True" "api-gateway-jwt VSO Ready"
  expect_equal "$(vso_ready_condition cloudflare-tunnel-token)" "True" "cloudflare-tunnel-token VSO Ready"

  gateway_keys="$(secret_keys api-gateway-secrets-vso)"
  tunnel_keys="$(secret_keys cloudflare-tunnel-token-vso)"
  expect_equal "${gateway_keys}" "JWT_SECRET" "api-gateway-secrets-vso keys"
  expect_equal "${tunnel_keys}" "token" "cloudflare-tunnel-token-vso keys"
  echo "VSO gate passed without reading Secret values."
}

argo_application_status() {
  local application_name="${1:?application name is required}"
  local sync_status
  local health_status

  sync_status="$(kube -n "${ARGO_NAMESPACE}" get application "${application_name}" -o jsonpath='{.status.sync.status}')"
  health_status="$(kube -n "${ARGO_NAMESPACE}" get application "${application_name}" -o jsonpath='{.status.health.status}')"
  expect_equal "${sync_status}" "Synced" "${application_name} sync status"
  expect_equal "${health_status}" "Healthy" "${application_name} health status"
}

verify_store_edge_ready() {
  local gateway_service_type
  local gateway_service_port
  local gateway_node_ports
  local gateway_image
  local gateway_available
  local gateway_automount
  local gateway_endpoints
  local gateway_endpoint_ports
  local gateway_ingress_backend_count
  local store_service_port
  local store_endpoints
  local store_endpoint_ports
  local cloudflared_image
  local cloudflared_available
  local cloudflared_ready
  local cloudflared_automount
  local pdb_min_available
  local network_policy_names

  verify_vso_ready
  argo_application_status target-edge-vso
  argo_application_status api-gateway-dev
  argo_application_status cloudflared-dev

  gateway_service_type="$(kube -n "${NAMESPACE}" get service api-gateway -o jsonpath='{.spec.type}')"
  gateway_service_port="$(kube -n "${NAMESPACE}" get service api-gateway -o jsonpath='{.spec.ports[0].port}')"
  gateway_node_ports="$(kube -n "${NAMESPACE}" get service api-gateway -o jsonpath='{range .spec.ports[*]}{.nodePort}{end}')"
  expect_equal "${gateway_service_type}" "ClusterIP" "API Gateway Service type"
  expect_equal "${gateway_service_port}" "8080" "API Gateway Service port"
  expect_equal "${gateway_node_ports}" "" "API Gateway nodePorts"

  gateway_image="$(kube -n "${NAMESPACE}" get deployment api-gateway -o jsonpath='{.spec.template.spec.containers[0].image}')"
  gateway_available="$(kube -n "${NAMESPACE}" get deployment api-gateway -o jsonpath='{.status.availableReplicas}')"
  gateway_automount="$(kube -n "${NAMESPACE}" get deployment api-gateway -o jsonpath='{.spec.template.spec.automountServiceAccountToken}')"
  gateway_endpoints="$(kube -n "${NAMESPACE}" get endpointslice -l kubernetes.io/service-name=api-gateway -o jsonpath='{range .items[*].endpoints[?(@.conditions.ready==true)].addresses[*]}{.}{"\n"}{end}' | sed '/^$/d' | wc -l | tr -d '[:space:]')"
  gateway_endpoint_ports="$(kube -n "${NAMESPACE}" get endpointslice -l kubernetes.io/service-name=api-gateway -o jsonpath='{range .items[*].ports[*]}{.port}{"\n"}{end}' | sed '/^$/d' | sort -u)"
  gateway_ingress_backend_count="$(
    kube -n "${NAMESPACE}" get ingress -o go-template='{{range .items}}{{$name := .metadata.name}}{{with .spec.defaultBackend}}{{with .service}}{{printf "%s\t%s\n" $name .name}}{{end}}{{end}}{{range .spec.rules}}{{range .http.paths}}{{printf "%s\t%s\n" $name .backend.service.name}}{{end}}{{end}}{{end}}' |
      awk '$2 == "api-gateway" { count++ } END { print count + 0 }'
  )"
  expect_equal "${gateway_image}" "${GATEWAY_IMAGE}" "API Gateway image"
  [[ "${gateway_available:-0}" -ge 1 ]] || fail "API Gateway has no available replica"
  expect_equal "${gateway_automount}" "false" "API Gateway ServiceAccount token automount"
  [[ "${gateway_endpoints}" -ge 1 ]] || fail "API Gateway has no ready EndpointSlice address"
  expect_equal "${gateway_endpoint_ports}" "8080" "API Gateway EndpointSlice ports"
  expect_equal "${gateway_ingress_backend_count}" "0" "Ingress backends targeting API Gateway"

  store_service_port="$(kube -n "${NAMESPACE}" get service store-service -o jsonpath='{.spec.ports[0].port}')"
  store_endpoints="$(kube -n "${NAMESPACE}" get endpointslice -l kubernetes.io/service-name=store-service -o jsonpath='{range .items[*].endpoints[?(@.conditions.ready==true)].addresses[*]}{.}{"\n"}{end}' | sed '/^$/d' | wc -l | tr -d '[:space:]')"
  store_endpoint_ports="$(kube -n "${NAMESPACE}" get endpointslice -l kubernetes.io/service-name=store-service -o jsonpath='{range .items[*].ports[*]}{.port}{"\n"}{end}' | sed '/^$/d' | sort -u)"
  expect_equal "${store_service_port}" "8083" "Store Service port"
  [[ "${store_endpoints}" -ge 1 ]] || fail "Store Service has no ready EndpointSlice address"
  expect_equal "${store_endpoint_ports}" "8083" "Store EndpointSlice ports"

  network_policy_names="$(kube -n "${NAMESPACE}" get networkpolicy -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sed '/^$/d' | sort)"
  expect_equal "${network_policy_names}" $'api-gateway-network-boundary\ncloudflared-egress' "pawbridge NetworkPolicy set"

  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.podSelector.matchLabels.app}')" "api-gateway" "API Gateway NetworkPolicy pod selector"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.policyTypes[*]}')" "Ingress Egress" "API Gateway NetworkPolicy types"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o go-template='{{len .spec.ingress}}')" "1" "API Gateway NetworkPolicy ingress rule count"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o go-template='{{len (index .spec.ingress 0).from}}')" "1" "API Gateway NetworkPolicy source count"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.ingress[0].from[0].podSelector.matchLabels.app}')" "cloudflared" "API Gateway NetworkPolicy source selector"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o go-template='{{len (index .spec.ingress 0).ports}}')" "1" "API Gateway NetworkPolicy port count"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.ingress[0].ports[0].protocol}:{.spec.ingress[0].ports[0].port}')" "TCP:8080" "API Gateway NetworkPolicy allowed port"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o go-template='{{len .spec.egress}}')" "7" "API Gateway NetworkPolicy egress rule count"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[0].to[0].namespaceSelector.matchLabels.kubernetes\.io/metadata\.name}:{.spec.egress[0].to[0].podSelector.matchLabels.k8s-app}')" "kube-system:kube-dns" "API Gateway DNS selectors"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[0].ports[0].protocol}:{.spec.egress[0].ports[0].port},{.spec.egress[0].ports[1].protocol}:{.spec.egress[0].ports[1].port}')" "UDP:53,TCP:53" "API Gateway DNS ports"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[1].to[0].podSelector.matchLabels.app}:{.spec.egress[1].ports[0].protocol}:{.spec.egress[1].ports[0].port}')" "user-service:TCP:8080" "API Gateway User Service egress"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[2].to[0].podSelector.matchLabels.app}:{.spec.egress[2].ports[0].protocol}:{.spec.egress[2].ports[0].port}')" "animal-service:TCP:8081" "API Gateway Animal Service egress"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[3].to[0].podSelector.matchLabels.app}:{.spec.egress[3].ports[0].protocol}:{.spec.egress[3].ports[0].port}')" "community-service:TCP:8082" "API Gateway Community Service egress"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[4].to[0].podSelector.matchLabels.app}:{.spec.egress[4].ports[0].protocol}:{.spec.egress[4].ports[0].port}')" "store-service:TCP:8083" "API Gateway Store Service egress"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[5].to[0].podSelector.matchLabels.app}:{.spec.egress[5].ports[0].protocol}:{.spec.egress[5].ports[0].port}')" "payment-service:TCP:8084" "API Gateway Payment Service egress"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy api-gateway-network-boundary -o jsonpath='{.spec.egress[6].to[0].namespaceSelector.matchLabels.kubernetes\.io/metadata\.name}:{.spec.egress[6].to[0].podSelector.matchLabels.app\.kubernetes\.io/name}:{.spec.egress[6].ports[0].protocol}:{.spec.egress[6].ports[0].port}')" "tracing:zipkin:TCP:9411" "API Gateway Zipkin egress"

  cloudflared_image="$(kube -n "${NAMESPACE}" get deployment cloudflared -o jsonpath='{.spec.template.spec.containers[0].image}')"
  cloudflared_available="$(kube -n "${NAMESPACE}" get deployment cloudflared -o jsonpath='{.status.availableReplicas}')"
  cloudflared_ready="$(kube -n "${NAMESPACE}" get deployment cloudflared -o jsonpath='{.status.readyReplicas}')"
  cloudflared_automount="$(kube -n "${NAMESPACE}" get deployment cloudflared -o jsonpath='{.spec.template.spec.automountServiceAccountToken}')"
  pdb_min_available="$(kube -n "${NAMESPACE}" get poddisruptionbudget cloudflared -o jsonpath='{.spec.minAvailable}')"
  expect_equal "${cloudflared_image}" "${CLOUDFLARED_IMAGE}" "cloudflared image"
  expect_equal "${cloudflared_available}" "2" "cloudflared available replicas"
  expect_equal "${cloudflared_ready}" "2" "cloudflared ready replicas"
  expect_equal "${cloudflared_automount}" "false" "cloudflared ServiceAccount token automount"
  expect_equal "${pdb_min_available}" "1" "cloudflared PDB minAvailable"

  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.podSelector.matchLabels.app}')" "cloudflared" "cloudflared NetworkPolicy pod selector"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.policyTypes[*]}')" "Ingress Egress" "cloudflared NetworkPolicy types"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o go-template='{{len .spec.ingress}}')" "0" "cloudflared NetworkPolicy ingress rule count"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o go-template='{{len .spec.egress}}')" "3" "cloudflared NetworkPolicy egress rule count"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.egress[0].to[0].namespaceSelector.matchLabels.kubernetes\.io/metadata\.name}')" "kube-system" "cloudflared DNS namespace selector"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.egress[0].to[0].podSelector.matchLabels.k8s-app}')" "kube-dns" "cloudflared DNS pod selector"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.egress[0].ports[0].protocol}:{.spec.egress[0].ports[0].port},{.spec.egress[0].ports[1].protocol}:{.spec.egress[0].ports[1].port}')" "UDP:53,TCP:53" "cloudflared DNS ports"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.egress[1].to[0].podSelector.matchLabels.app}')" "api-gateway" "cloudflared Gateway selector"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.egress[1].ports[0].protocol}:{.spec.egress[1].ports[0].port}')" "TCP:8080" "cloudflared Gateway port"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o go-template='{{len (index .spec.egress 2).to}}')" "20" "cloudflared edge CIDR count"
  expect_equal "$(kube -n "${NAMESPACE}" get networkpolicy cloudflared-egress -o jsonpath='{.spec.egress[2].ports[0].protocol}:{.spec.egress[2].ports[0].port}')" "TCP:7844" "cloudflared edge port"
  verify_cloudflare_edge_cidrs
  echo "Target Store edge gate passed. Other Gateway routes and the public hostname remain unverified."
}

main() {
  case "${MODE}" in
    vso-ready)
      validate_target
      verify_vso_ready
      ;;
    store-edge-ready)
      validate_target
      verify_store_edge_ready
      ;;
    -h | --help)
      usage
      ;;
    *)
      usage >&2
      fail "mode must be vso-ready or store-edge-ready"
      ;;
  esac
}

main
