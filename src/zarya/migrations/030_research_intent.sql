ALTER TABLE research_runs ADD COLUMN public_question TEXT;

-- Recreate unstarted research from the retained Telegram job and its current sources.
DELETE FROM research_runs
WHERE state IN ('queued','fetching')
  AND NOT EXISTS (SELECT 1 FROM model_calls WHERE research_run_id=research_runs.id)
  AND EXISTS (SELECT 1 FROM jobs WHERE id=research_runs.job_id AND state='pending');
