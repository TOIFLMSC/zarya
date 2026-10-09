ALTER TABLE jobs ADD COLUMN typing_state TEXT NOT NULL DEFAULT 'not_attempted';
ALTER TABLE jobs ADD COLUMN typing_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN typing_at REAL NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN typing_due REAL NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN typing_error TEXT;
UPDATE jobs SET
    typing_state=(SELECT r.typing_state FROM dialogue_runs r WHERE r.job_id=jobs.id),
    typing_attempts=(SELECT r.typing_attempts FROM dialogue_runs r WHERE r.job_id=jobs.id),
    typing_at=(SELECT r.typing_at FROM dialogue_runs r WHERE r.job_id=jobs.id),
    typing_due=(SELECT r.typing_due FROM dialogue_runs r WHERE r.job_id=jobs.id),
    typing_error=(SELECT r.typing_error FROM dialogue_runs r WHERE r.job_id=jobs.id)
WHERE EXISTS (SELECT 1 FROM dialogue_runs r WHERE r.job_id=jobs.id);
