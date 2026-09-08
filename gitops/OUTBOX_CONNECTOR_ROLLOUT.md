# Outbox connector activation gate

The `user-outbox-source`, `animal-outbox-source`, `community-outbox-source`,
and `payment-outbox-source` Argo CD Applications are manual and non-pruning.
Merging or rendering these manifests does not create a connector; synchronize
only one selected Application after its gate has been recorded.

`community-outbox-source` is deliberately declared with `state: stopped`.
`PostEventConsumer` currently expects an envelope `payload`, while the
configured EventRouter expands the JSON payload into the event value. Do not
start that connector until the Backend consumer contract is repaired and one
controlled Community event has been verified end to end. Synchronizing its
Application only registers the stopped connector; it is not activation.

1. Confirm each intended backend image is the approved immutable image and its
   `outbox_events`/`outbox` table matches the connector declaration. Read and
   record each current outbox row count before synchronization.
2. Confirm the existing `store-mysql-cdc-auth` Secret keys are present without
   reading their values, MySQL binlog is enabled with `ROW` and `FULL` image,
   and the Kafka Connect worker is Ready.
3. Read and record the existing Connect offset for each connector name before
   its first synchronization. Do not reset, delete, or edit offsets or MySQL
   binlog positions as part of this rollout.
4. Synchronize one approved connector at a time, then verify its connector and
   task are `RUNNING`, its new offset advances after one controlled new outbox
   write, and the expected Kafka topic receives that new event.

All four declarations use `snapshot.mode: no_data`. Their first start records
the source position but does not publish rows already in an outbox table; this
is deliberate and is not a hidden historical replay. Any historical replay
requires a separately approved, observable plan that creates new outbox events
or otherwise defines the offset and duplicate-handling contract.
