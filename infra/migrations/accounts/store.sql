-- APPROVAL REQUIRED. Run only for the reviewed service and cluster.
-- Fail if the account already exists; never reset an existing password/grants.
-- This account stays LOCKED until a separately approved Vault credential bootstrap.
CREATE USER 'pawbridge_store_migrator'@'%' ACCOUNT LOCK;
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
ON `pawbridge_store`.* TO 'pawbridge_store_migrator'@'%';
-- No global grants, GRANT OPTION, DROP or other service schemas.
