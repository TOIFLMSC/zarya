ALTER TABLE dialogue_runs ADD COLUMN typing_state TEXT NOT NULL DEFAULT 'not_attempted';
ALTER TABLE dialogue_runs ADD COLUMN typing_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dialogue_runs ADD COLUMN typing_at REAL NOT NULL DEFAULT 0;
ALTER TABLE dialogue_runs ADD COLUMN typing_error TEXT;
ALTER TABLE dialogue_runs ADD COLUMN typing_due REAL NOT NULL DEFAULT 0;
