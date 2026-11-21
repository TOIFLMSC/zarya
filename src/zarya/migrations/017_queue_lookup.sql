CREATE INDEX model_calls_started_job ON model_calls(job_id) WHERE state='started';
