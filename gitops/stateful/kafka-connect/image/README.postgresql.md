# Optional PostgreSQL connector image

`Dockerfile.postgresql` extends the exact existing worker digest, preserves MySQL
and ES plugin directories, and adds Debezium PostgreSQL3.5.2.Final separately.
The official Maven SHA512 is pinned and verified before extraction. The final
runtime user remains1001. The existing Dockerfile and KafkaConnect spec.image remain unchanged. The dedicated
`kafka-connect-postgresql-image.yml` workflow builds this file on PRs and publishes
`postgresql-sha-<commit>` tags on dev pushes. The original workflow excludes these
two PostgreSQL-only files. Publication does not update the running worker image.

```sh
docker build -f Dockerfile.postgresql -t '<LOCAL_TEST_TAG>' .
```

Build from this directory only. Do not use the whole repository as context. Check
plugin discovery/config validation and the existing wire contract with the actual
built worker. In the backend repository, `infrastructure/kafka/postgresql/tests/rehearse.py`
accepts the cached worker ID and PG image; omit --plugin-dir to test image-packaged
plugins without an external mount. It uses only synthetic data/internal networking
and removes its test containers, volumes and network. Use a new evidence path.

Image publication, changing the live worker image, connector start and role/secret
installation are separate approved steps. Preserve worker group ID, internal config/
offset/status topics and existing source offsets. Do not replace the worker with a
second competing instance. The application/stateful cutover runbook is
`environments/postgresql-candidate/README.md` at repository root.

The Strimzi connector candidates use state: stopped, supported by the documented
[KafkaConnectorSpec](https://strimzi.io/docs/operators/latest/configuring.html#type-KafkaConnectorSpec-reference).
Verify the installed CRD before live use; local Kustomize rendering is not that proof.
