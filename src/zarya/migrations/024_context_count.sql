ALTER TABLE model_calls ADD COLUMN context_count_run_id INTEGER REFERENCES dialogue_runs(id);
CREATE UNIQUE INDEX one_count_per_run ON model_calls(context_count_run_id);
