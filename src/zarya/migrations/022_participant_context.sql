CREATE TABLE participant_messages (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
 sender_id TEXT NOT NULL, access_version INTEGER NOT NULL,
 event_id INTEGER NOT NULL REFERENCES events(id), received_at REAL NOT NULL,
 PRIMARY KEY(bot_id,chat_id,message_id)
);
CREATE INDEX participant_window ON participant_messages
 (bot_id,chat_id,access_version,sender_id,message_id DESC);
CREATE TABLE context_run_edges (
 run_id INTEGER NOT NULL REFERENCES dialogue_runs(id),
 parent_run_id INTEGER NOT NULL REFERENCES dialogue_runs(id),
 PRIMARY KEY(run_id,parent_run_id), CHECK(parent_run_id<run_id)
);
CREATE INDEX context_children ON context_run_edges(parent_run_id,run_id);
WITH retained AS (
 SELECT m.*,COALESCE(json_extract(e.payload,'$.edited_message'),
 json_extract(e.payload,'$.message')) AS message,
 COALESCE(r.received_at,json_extract(e.payload,'$.edited_message.date'),
 CASE WHEN json_type(e.payload,'$.message')='object'
 THEN CAST(strftime('%s',e.received_at) AS REAL) END) AS original_at
 FROM research_messages m JOIN events e ON e.id=m.event_id
 JOIN telegram_access a ON a.bot_id=m.bot_id AND a.subject_id=m.chat_id
 AND a.version=m.access_version AND a.state='approved'
 AND a.member_state NOT IN ('left','kicked','migrated')
 LEFT JOIN recent_messages r ON r.bot_id=m.bot_id AND r.chat_id=m.chat_id
 AND r.message_id=m.message_id AND r.role='user'
 WHERE e.bot_id=m.bot_id AND e.chat_id=m.chat_id
 AND e.disposition='accepted' AND e.payload!='{}'
 AND CAST(strftime('%s',e.received_at) AS REAL)>unixepoch()-7776000
)
INSERT INTO participant_messages
SELECT bot_id,chat_id,message_id,CAST(json_extract(message,'$.from.id') AS TEXT),
 access_version,event_id,original_at FROM retained
WHERE json_extract(message,'$.from.id') IS NOT NULL
 AND COALESCE(json_extract(message,'$.from.is_bot'),0)=0
 AND json_extract(message,'$.sender_chat') IS NULL
 AND substr(COALESCE(json_extract(message,'$.text'),''),1,1)!='/'
 AND original_at>unixepoch()-7776000
 AND (COALESCE(json_extract(message,'$.text'),json_extract(message,'$.caption'),'')!=''
 OR json_extract(message,'$.photo') IS NOT NULL
 OR json_extract(message,'$.voice') IS NOT NULL
 OR json_extract(message,'$.video') IS NOT NULL
 OR json_extract(message,'$.video_note') IS NOT NULL
 OR json_extract(message,'$.animation') IS NOT NULL
 OR json_extract(message,'$.sticker') IS NOT NULL
 OR json_extract(message,'$.audio') IS NOT NULL
 OR json_extract(message,'$.document') IS NOT NULL
 OR json_extract(message,'$.contact') IS NOT NULL
 OR json_extract(message,'$.location') IS NOT NULL
 OR json_extract(message,'$.venue') IS NOT NULL
 OR json_extract(message,'$.poll') IS NOT NULL
 OR json_extract(message,'$.dice') IS NOT NULL);
