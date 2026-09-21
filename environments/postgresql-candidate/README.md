# PostgreSQL application cutover candidate

Unapplied overlays for Animal/User/Community/Store/Payment. Existing dev values and
Argo Applications remain unchanged. Review each service chart independently; review
runtime Vault, CDC Vault/RBAC, and worker packaging as separate deployment units.
The shared contract is one database `pawbridge`, five owned schemas, dedicated app
roles, and preserved Outbox wire events.

## Offline render

Use each service chart, the existing dev values, then its candidate override:

```sh
helm template animal-service charts/animal-service --namespace pawbridge \
  -f environments/dev/values/animal-service.yaml \
  -f environments/postgresql-candidate/values/animal-service.yaml \
  --set-string image.digest='<APPROVED_MIGRATION_IMAGE_MANIFEST_DIGEST>'
python3 -m unittest discover -s infra/postgresql/tests -p test_service_cutover.py -v
```

`HELM_BIN` may select an existing renderer wrapper for the tests. All five charts
are version0.1.0 with no external chart dependencies. The candidate clears inherited
image.digest intentionally: an explicit migration-capable immutable image is required.
A local Docker image ID is not a published registry manifest digest. Never use the
render test's all-ones fixture digest for deployment.

Candidate replicas are zero, HPA/CronJob/Store ES preflight are disabled, and CDC
connectors are stopped. This is **not** a ready-to-apply production release. An apply
to existing Deployment names would stop them. No apply/upgrade command is provided.

## Required credentials and settings

| Consumer | Namespace / Secret | Vault KV path | Keys / identity |
| --- | --- | --- | --- |
| Spring SERVICE | pawbridge / SERVICE-postgresql-auth | pawbridge/dev/SERVICE/postgresql | postgres-password; pawbridge_SERVICE_app |
| CDC SERVICE | kafka / SERVICE-postgresql-cdc-auth | pawbridge/dev/SERVICE/postgresql-cdc | username,password; dedicated REPLICATION role |
| PG bootstrap admin | databases / pawbridge-postgresql-admin-auth | pawbridge/dev/postgresql/admin | postgres-password; never share with apps |

SERVICE is one of animal/user/community/store/payment. VSO candidates and exact-path
read policies do not create Vault values, Kubernetes auth role bindings, DB users or
grants. Provision them separately with review/approval. Bind each role only to its
matching service account and namespace, audience vault. Both namespaces need the
verified vault-internal-ca Secret; TLS verification is enabled. New Secrets refuse
overwrite and do not auto-restart deployments. Coordinate DB password changes with
Vault/Secret updates and controlled application restarts; VSO cannot rotate DB roles.

The host GPU vector role/DSN stays outside these Spring/CDC Secrets. Follow Python's
postgresql-cutover drop-ins and restricted vector-role contract. No credentials are
passed through Helm values or command arguments.

The overlays explicitly replace MySQL URL/driver/dialect/password and update-mode
DDL, not just the Spring profile. Hikari uses service schemas, UTC, max10 for Animal
and5 for the other four, totaling30 per steady generation. Two generations60 +
Python4 + CDC10 + operations10 =84 of100 proposed server connections. PG guards
reject HPA, more than one steady replica, oversized pools/min-idle and both DB
password sources. Terminating pods and extra processes still require live budget
checks and DB role limits; a chart guard is not a physical admission controller.

Null env entries are skipped during rendering. Tests verify default dev output
separately and confirm no duplicate password entries or inherited MySQL/ES references
in candidate Deployments. Preserve JWT/Google/R2/Redis/internal API Secrets.

## Activation and rollback gates

1. Finish full write-fence, external-side-effect and consumer drain rehearsal.
2. Freeze old API/batch/cleanup/gallery producers, drain existing work and CDC, and
   record old offsets/positions. Zero replicas alone is not a graceful write drain.
3. Final copy/reconcile, identities/permissions, migrations and search backfill must
   finish in the target before new writers start. Retain MySQL/ES data and offsets.
4. Install the approved PG-capable Connect image and narrowly scoped CDC Secret RBAC.
   `gitops/stateful/postgresql-outbox` keeps the original Kafka topic/key/header/payload
   contracts and no_data snapshot. Create/verify slots and publications while writes
   are fenced, then explicitly start connectors and establish positions before
   admitting any new target writes. Do not snapshot/replay historical Outbox rows.
5. Render the final approved app images/configuration and verify private service/GPU
   queries and role limits before write admission. Initial replicas0 is only staging;
   the later reviewed release must specify the intended one-per-service replicas.
6. New target writes make a URL-only rollback unsafe. Freeze again, reconcile target
   inserts/updates/deletes/identities/events and external effects back into MySQL,
   reconcile CDC offsets, then reopen the old writer only after verification.

WAL retention/alerts, idle publication slot progress, actual Strimzi/VSO CRD and
Secret-provider access, live network/TLS policy, and target-to-MySQL reconciliation
remain explicit gates. Local rendering and synthetic CDC do not prove these.
