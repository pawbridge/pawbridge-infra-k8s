-- Disposable recovery drill ONLY. Never package this file in a migration image.
CREATE TABLE recovery_probe (id BIGINT PRIMARY KEY);
INSERT INTO recovery_probe VALUES (73);
ALTER TABLE deliberately_missing_table ADD COLUMN impossible_column INT;
