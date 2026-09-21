# PostgreSQL provisioning candidate

This directory is unapplied and not referenced by an Argo CD Application.
`replicas: 0` is intentional. Do not scale until the capacity, copy, CDC and recovery
gates in the canonical Obsidian PostgreSQL transition plan pass. This is a separate
provisioning review slice from `gitops/security/postgresql-admin-vso`, application
chart cutover, and GPU runtime installation.

## Render without contacting a cluster

```sh
kubectl kustomize gitops/stateful/postgresql
python3 -m unittest discover -s infra/postgresql/tests -p test_candidate.py -v
```

Pinned image: pgvector 0.8.6 / PostgreSQL 17, verified digest in StatefulSet.
Service `pawbridge-postgresql.databases.svc.cluster.local:5432`, database `pawbridge`.
Schemas, extensions, migrations, app roles, publications and slots are NOT created
by this StatefulSet. The host GPU tunnel uses loopback15432 and the same Service.

## Before an explicitly approved start

- Recheck exact node `pawbridge-k136-w1`, available memory and disk. The candidate
  replaces ES resource use after an approved stop; it is not approved to run beside
  the current ES/MySQL load. Keep the old ES/MySQL PVCs and source rollback evidence.
- Use an independent local-path PVC. 20Gi is a request, not a verified filesystem
  quota. Node pinning plus local-path means no transparent failover to another node.
- Provision the admin Vault path and Kubernetes auth role, TLS CA in `databases`,
  then the separately reviewed VSO resources. Only key `postgres-password` is synced.
  No admin credential is supplied to Spring, Python or CDC. No secret values in Git.
- `POSTGRES_PASSWORD_FILE` is initialization-only. Changing Vault/VSO does not rotate
  an existing PostgreSQL password. Do not restart as a substitute for ALTER ROLE.
  No automatic restart target is attached to the admin VaultStaticSecret.
- Require dedicated migration/app/vector/CDC identities and prior tested grants.
  Run Flyway before app admission. The existing migration launchers are loopback
  guarded; a reviewed private port-forward is required, not an unrestricted URL.
- `max_slot_wal_keep_size=1GB` is a finite rehearsal value, not a production retention
  SLA. Measure WAL generation, slot progress during idle Outbox periods, alerts and
  invalid-slot recovery before cutover. `max_wal_size` is not a disk quota.
- TCP connections require SCRAM; this candidate does not configure PostgreSQL TLS.
  Approve the private-network/TLS policy before live use. SSH protects the host-to-VM
  leg but does not encrypt the subsequent cluster TCP connection.

## Restart and recovery boundary

OnDelete prevents an image/config commit from restarting the database. Hashed
ConfigMaps update the desired Pod template, not a running Pod. Plan each restart.
The Pod runs as UID/GID999; fsGroup grants PVC access and PGDATA is a child directory.
`/dev/shm` is a 128Mi tmpfs counted against the 2Gi container budget. Startup allows
up to ten minutes; pg_isready proves accepting connections, not app/schema readiness.

Both StatefulSet retention policies are Retain, but verify the actual PV reclaim
policy and independent backup before delete/recreate. This flag alone does not
protect against an explicit PVC deletion or node disk loss. The prior same-cluster
logical restore did not prove fresh-cluster secrets/owners/ACL restoration or PITR.
After target writes start, reverting the app URL to MySQL is NOT a safe rollback;
reverse reconciliation and CDC drain must be proven first. No live cutover command
is provided by this candidate.
