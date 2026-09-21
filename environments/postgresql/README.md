# PostgreSQL production activation

These overlays select the immutable images and PostgreSQL configuration verified in
production on 2026-09-21. Apply after each service's existing dev values. Unlike the
replicas0 candidate overlays, these run one replica per service, retain photo archive
and gallery-feed settings, and resume the APMS trigger. Store HPA remains disabled.

The matching changes to application Helm valueFiles, Hikari property spellings,
Connect image, PostgreSQL state, retained source fences and CDC heartbeat resources
form one runtime transition contract. Updating only the service images or enabling
old Argo sources can restore MySQL settings and must be avoided.

## Reconciliation after merge

1. Keep application automated sync disabled while updating source valueFiles to
   include this directory. Preserve existing authentication/monitoring parameters;
   change Store's explicit autoscaling.enabled parameter to false and remove its old
   replica-count ignoreDifferences rule so the one-replica budget remains managed.
2. Verify the selected revision and rendered image digests match these overlays.
   Compare Deployment env/Secret references with the verified runtime. Never export
   live Secret values into Helm overrides or this repository.
3. Heartbeat SQL must already be provisioned before the running CDC manifests. Run
   `infra/postgresql/cdc-heartbeat.sql` as the approved DB administrator; it validates
   the database, requires existing roles/publications and is repeatable. The five
   compact Kafka heartbeat topics are required even when tasks report RUNNING.
4. Restore application automated sync only after source alignment and verify Ready,
   API reads, photo search, slot LSN progress and consumer lag. The stateful database
   and CDC resources remain explicitly managed; this change does not enroll them in
   an automatic app-of-apps deployment.
5. PostgreSQL uses OnDelete updates. A new hashed ConfigMap changes the desired Pod
   template but does not restart the live Pod. The live WAL setting is already2GB;
   compare configuration before planning any later restart.

## Source retention and recovery

MySQL has replicas0 and Retain PVC policy. Elasticsearch orchestration is paused,
while its CR node count stays1: setting it to0 with DeleteOnScaledownOnly can delete
source data. Its existing StatefulSet was separately scaled to0 during the cutover.
The host gallery Elasticsearch container is stopped, with its volume retained.

New PostgreSQL business writes have occurred. Reverting this commit or URLs alone
is not a valid data rollback. Fence writes, preserve the current target, reconcile
new records/events/external effects, and only then select forward recovery or a
verified reverse migration. Source PVCs are not a substitute for a database backup.

## Local verification

```sh
HELM_BIN=<existing-helm-renderer> python3 -m unittest discover -s infra/postgresql/tests -p 'test_*.py' -v
kubectl kustomize gitops/stateful/postgresql
kubectl kustomize gitops/stateful/postgresql-outbox
```

The safety tests check six production overlays, five isolated candidate overlays,
connection limits, secrets separation, heartbeat topics/filters, retained storage,
and rejection of the environment spelling that prematurely opens Hikari connections.
Actual production evidence and troubleshooting remain in the canonical Obsidian log.
