# PawBridge Infrastructure Agent Guide

## Scope

This file applies to the entire `pawbridge-infra-k8s` repository.
Read the closest `AGENTS.md` before changing files. A more specific nested
`AGENTS.md` may add or override rules for its directory.

## Source of Truth

- Verify current facts from Git, manifests, rendered output, and the actual
  target cluster. Do not rely on an old runbook for live state.
- Treat Obsidian `Projects/pawbridge` notes as the canonical home for project
  plans, decisions, migration records, recovery evidence, and investigation
  notes.
- Keep repository Markdown only when it must version with manifests or scripts,
  such as the root README, PR template, this guide, or an executable runbook.
- Never describe a local manifest edit, render, dry-run, image build, or backup
  plan as an applied or production-verified change.

## Git and Pull Requests

- Use the latest `origin/dev` as the default base unless the user explicitly
  selects another base.
- Before creating or switching a branch or worktree, verify whether the remote
  base ref is current, record the exact base SHA, inspect `HEAD...base`
  divergence and the base-relative effective diff, then show the proposed base,
  branch name, and worktree path and wait for approval. If the remote could not
  be refreshed, report that limitation instead of calling the ref current.
- One branch equals one pull request with one primary review purpose.
- Separate documentation, provisioning, stateful recovery, secrets, CI,
  application chart, and live cutover changes unless one explicit contract
  makes them inseparable. Explain any exception before implementation.
- Use a clean worktree based on `origin/dev` when the current worktree already
  contains changes. Do not stash, reset, discard, or rewrite user changes
  without explicit approval.
- Never stage a dirty repository with `git add -A` or `git add .`. Stage only
  the exact paths owned by the current PR.
- Do not commit CRLF-only changes, rendered secrets, generated archives, logs,
  dumps, kubeconfigs, changes whose normalized content is already on the base,
  or untracked copies of paths already tracked with the same base content.
- Use branch prefixes that state the change type: `feat/`, `fix/`, `ci/`,
  `docs/`, or `chore/`.
- Use a concise `type: Korean noun phrase` commit title and show the exact title
  before committing, then wait for approval.
- Before pushing, show the exact push command and wait for approval.
- Before creating a PR, show the exact command, Korean PR title, and full PR
  body, then wait for approval.
- Use `.github/PULL_REQUEST_TEMPLATE.md` and check only items proven by the
  actual diff and verification evidence.
- Do not force-push, rewrite shared history, or merge without explicit approval.

## Infrastructure Safety

- Read-only inspection is the default. Commands such as `kubectl apply`,
  `kubectl delete`, `helm upgrade`, `helm uninstall`, `vagrant up`,
  `vagrant reload`, provider installation, and public cutover require explicit
  approval before execution.
- Before a live or external mutation, resolve every identifier relevant to that
  operation, such as kubeconfig, context, namespace, release, VM, or target
  path. Never assume the current context is safe.
- Keep source and target environments distinct during migration. A target
  smoke test must not reuse source IP addresses, tunnel ownership, persistent
  volumes, or production secrets.
- Stateful changes require a verified backup, restore procedure, rollback
  boundary, and post-restart persistence check before cutover.
- Do not change MySQL binlog, Kafka/Connect offsets, Elasticsearch aliases,
  persistent-volume reclaim behavior, or Vault state as an incidental part of
  another PR.
- Split bootstrap logic, chart-version pinning, data recovery, and application
  rollout into independently reviewable changes.
- Historical documentation cleanup stays in a dedicated PR. An executable
  runbook that defines the inputs, recovery, or rollback contract of a changed
  script or manifest may be included with that implementation.

## Validation

- Validate YAML and shell syntax before broader checks.
- Render Helm charts with the exact chart and dependency versions used by the
  PR. Treat an unavailable renderer or CRD as an environment blocker.
- Prefer client-side validation first. Use server-side dry-run only after the
  exact non-production target context is confirmed.
- For scripts, verify fail-fast behavior, idempotency, required input checks,
  timeout handling, and refusal to overwrite recovery evidence.
- For Kafka and Connect changes, verify topic partitions, replication,
  cleanup policy, connector plugins, offsets, restart behavior, and failure
  propagation.
- For stateful resources, verify PVC binding, reclaim behavior, backup hashes,
  isolated restore, and persistence after restart.

## Secrets and External Effects

- Never commit Kubernetes Secret values, runtime `.env` files, Vault tokens,
  R2 keys, registry credentials, kubeconfigs, database dumps, or unredacted live
  output. A reviewed `.env.example` containing no secret is allowed.
- Do not print or decode live Secret values, Pod environment values, or database
  credentials during routine inspection. Without explicit approval, inspect
  only Secret names, key names, metadata, and byte lengths.
- Secret and credential fields in templates and examples must use placeholders.
  Safe namespaces, service names, image repositories, public endpoints, and
  resource defaults may use real version-controlled values.
- PR creation and immutable artifact publication require explicit approval and
  provenance verification. Live deploys, mutable tag changes, DNS or tunnel
  cutovers, cluster or VM mutations, and overwrite or delete operations also
  require an explicit rollback plan.

## Documentation Cleanup

- Before deleting repository documentation, confirm that reusable information
  is preserved in Obsidian or in a version-coupled runbook that remains beside
  the relevant script or manifest.
- Perform documentation cleanup in a dedicated PR and verify links, secret
  candidates, and references to removed paths.
