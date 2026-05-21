-- Run against the middleware DB if login_lockouts already existed before lock_tier support.
ALTER TABLE login_lockouts
  ADD COLUMN IF NOT EXISTS lock_tier INTEGER NOT NULL DEFAULT 0;

ALTER TABLE login_lockouts
  ADD COLUMN IF NOT EXISTS permanently_locked BOOLEAN NOT NULL DEFAULT FALSE;
