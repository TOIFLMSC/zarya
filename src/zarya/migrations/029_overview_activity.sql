CREATE INDEX activity_events_received ON events(received_at) WHERE disposition='accepted';
CREATE INDEX activity_dialogue_created ON dialogue_runs(created_at);
CREATE INDEX activity_photo_request ON photo_batches(leader_job_id);
CREATE INDEX activity_media_request ON media_dependencies(media_run_id,job_id);
ALTER TABLE photo_batches ADD COLUMN request_job_id INTEGER REFERENCES jobs(id);
ALTER TABLE model_calls ADD COLUMN request_job_id INTEGER REFERENCES jobs(id);
CREATE INDEX activity_lazy_photo_request ON photo_batches(request_job_id);
CREATE INDEX activity_media_call_request ON model_calls(request_job_id);
CREATE INDEX activity_photo_event ON photo_items(event_id,batch_id);
