CREATE TABLE photo_messages (
    bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL REFERENCES events(id), access_version INTEGER NOT NULL,
    album_key TEXT, PRIMARY KEY(bot_id,chat_id,message_id)
);
WITH latest AS (
SELECT i.bot_id,i.chat_id,i.message_id,
    (SELECT MAX(e.id) FROM events e WHERE e.bot_id=i.bot_id AND e.chat_id=i.chat_id
     AND e.disposition='accepted' AND COALESCE(json_extract(e.payload,'$.message.message_id'),
     json_extract(e.payload,'$.edited_message.message_id'))=i.message_id) AS event_id,
    MAX(b.access_version) AS access_version,
    CASE WHEN b.album_key LIKE 'album:%' THEN b.album_key ELSE NULL END AS album_key
FROM photo_items i JOIN photo_batches b ON b.id=i.batch_id
GROUP BY i.bot_id,i.chat_id,i.message_id
)
INSERT INTO photo_messages(bot_id,chat_id,message_id,event_id,access_version,album_key)
SELECT bot_id,chat_id,message_id,event_id,
    COALESCE((SELECT j.access_version FROM jobs j WHERE j.event_id=latest.event_id
              ORDER BY j.id DESC LIMIT 1),access_version),album_key FROM latest;
CREATE INDEX photo_message_album ON photo_messages(bot_id,chat_id,access_version,album_key);
