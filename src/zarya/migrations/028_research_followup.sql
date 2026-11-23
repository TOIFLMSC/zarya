DROP INDEX one_call_per_research;
CREATE UNIQUE INDEX research_call_once_per_phase ON model_calls(
 research_run_id,COALESCE(operation,'research_initial')
) WHERE research_run_id IS NOT NULL;
ALTER TABLE research_runs ADD COLUMN profile TEXT NOT NULL DEFAULT 'answer'
 CHECK(profile IN ('answer', 'source', 'study'));
