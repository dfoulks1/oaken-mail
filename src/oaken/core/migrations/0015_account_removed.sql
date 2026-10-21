-- remove_account (prompt 24) is a soft delete: the account row stays so its stored mail keeps its
-- foreign key (messages.account_id and sync_cursors.account_id REFERENCE accounts; prompt 24 Q2
-- keeps the mail, and purging it is a separate confirmed operation). A removed account is hidden
-- from list_accounts/get_account, never synced, and its credential is dropped. Re-adding the same
-- id (create_account) clears the mark and the history is still owned.
ALTER TABLE accounts ADD COLUMN removed_at TEXT;
