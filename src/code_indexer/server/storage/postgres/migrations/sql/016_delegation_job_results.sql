-- Bug #577: Delegation job results for cluster mode
-- DB is the cross-node source of truth for delegation job completion.
CREATE TABLE IF NOT EXISTS delegation_job_results (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    output TEXT,
    exit_code INTEGER,
    error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);
