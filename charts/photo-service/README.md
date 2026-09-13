# Photo service runtime contract

This chart serves only the internal CPU photo optimizer on port 8000. It has no
R2, APMS, database, Elasticsearch or GPU access. The application accepts binary
photos and returns a compressed candidate; animal-service owns durable archiving.

## Preparation and validation

The default is **zero replicas** and an unset digest. The standalone Argo
Application has no automated sync and is not registered in an existing root app.
A merge of these files does not create this Application or enable archiving.
Starting a replica requires the digest produced by Python's photo-specific CI.
A tag from the normal AI image workflow is not a photo image.

```sh
helm lint charts/photo-service -f environments/dev/values/photo-service.yaml
helm template photo-service charts/photo-service -n pawbridge \
  -f environments/dev/values/photo-service.yaml > /tmp/photo-runtime.yaml
python charts/photo-service/tests/check_render.py /tmp/photo-runtime.yaml
```

Before runtime approval, confirm node headroom, a working VSO installation, and
`python-runtime-vault-auth` in the same namespace. It must be able to read
`secret/pawbridge/dev/animal-python/internal`. This chart materializes a distinct
`photo-service-internal-auth` Secret and restarts only the photo Deployment on
rotation. It does not alter the existing AI Secret or its restart targets.
Readiness probes indicate process availability, not key equality or acceptance
of an authenticated photo; verify an authenticated request before activation.

Requests start at 128Mi/100m and limits at 512Mi/1 CPU. There is one replica,
one encoder and no surge during rollout. The photo image CI exercises 16MP RGBA
under the same memory ceiling; that fixture is not an exhaustive memory bound.
A zero-replica preparation render does not demonstrate live resource availability.

NetworkPolicy admits only pods labelled `app: animal-service` in the same
namespace to TCP 8000 and denies photo Pod egress. Policy enforcement must be
verified against the target CNI. There is no public ingress or NodePort.

## Animal service binding and activation

The optional animal chart value `photoOptimizerSecretRef` maps a Secret's
`INTERNAL_API_KEY` to `APMS_PHOTO_OPTIMIZER_INTERNAL_API_KEY`. For the existing
shared authentication contract, use `animal-python-internal-auth`, whose VSO
already rotates the animal Deployment. Leave this value empty until rollout.
The photo URL is `http://photo-service.pawbridge.svc.cluster.local:8000/internal/photos/optimize`.
The chart does not enable `APMS_PHOTO_ARCHIVE_ENABLED`, choose a bucket, or change
existing dev animal values. APMS/V4/R2 rollout needs its own approved change.

Infra image PR #219 includes a new migration image while schema migration is
enabled; treat applying that image pair as V4 database rollout, not just an API
restart. Confirm current migration history and recovery evidence first.

Stop the archiver with its enabled flag and scale this Deployment to zero via
the approved rollout process. Do not delete archived objects, database tables
or secrets as an automatic rollback. Recreate rollouts temporarily interrupt
requests; the archiver's retry/lease mechanism handles this interruption.
