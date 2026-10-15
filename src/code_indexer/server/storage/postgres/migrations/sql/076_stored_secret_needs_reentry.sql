-- Migration 076: mark stored secrets that a node cannot decrypt.
--
-- needs_reentry_at is set when a stored git credential or CI token cannot be
-- decrypted with the stored-secret key or any legacy key on a node, and
-- needs_reentry_node names that node, so the health report and the Web
-- credentials pages can ask for re-entry. The row is never deleted; saving a
-- new value or a successful rewrite clears both columns.
--
-- Nullable add-column only: backward compatible with rolling restarts (a
-- node that predates it never reads or writes the columns).

ALTER TABLE ci_tokens
    ADD COLUMN IF NOT EXISTS needs_reentry_at TEXT;

ALTER TABLE ci_tokens
    ADD COLUMN IF NOT EXISTS needs_reentry_node TEXT;

ALTER TABLE user_git_credentials
    ADD COLUMN IF NOT EXISTS needs_reentry_at TEXT;

ALTER TABLE user_git_credentials
    ADD COLUMN IF NOT EXISTS needs_reentry_node TEXT;
