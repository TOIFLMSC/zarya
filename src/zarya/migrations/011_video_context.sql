ALTER TABLE media_runs ADD COLUMN analysis_context TEXT NOT NULL DEFAULT '{}';

UPDATE settings SET document=json_set(document,'$.video_max_frames',24),
version=version+1,updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
WHERE COALESCE(json_extract(document,'$.video_max_frames'),6)=6;
INSERT OR IGNORE INTO settings_versions(version,document,updated_at)
SELECT version,document,updated_at FROM settings WHERE id=1;
