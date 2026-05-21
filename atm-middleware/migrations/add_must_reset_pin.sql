-- Run once on existing middleware DBs (also applied at startup via db._migrate_login_lockouts).
ALTER TABLE login_lockouts
  ADD COLUMN IF NOT EXISTS must_reset_pin BOOLEAN NOT NULL DEFAULT FALSE;
