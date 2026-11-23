"""Admin overview aggregates from retained Telegram operations and the cost ledger."""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from zarya.database import Database

ACTIVITY_BUCKET_MINUTES = {30: 1, 60: 1, 180: 5, 720: 15, 1440: 30}

ACTIVITY_SQL = """
WITH candidate_runs AS (
    SELECT r.id FROM events e JOIN jobs j ON j.event_id=e.id AND j.kind='telegram'
    JOIN dialogue_runs r ON r.job_id=j.id
    WHERE e.received_at>=:history_start AND e.received_at<=:now AND e.disposition='accepted'
    UNION ALL
    SELECT r.id FROM dialogue_runs r CROSS JOIN jobs j ON j.id=r.job_id AND j.kind='telegram'
    CROSS JOIN events e ON e.id=j.event_id
    WHERE r.created_at>=:range_start AND e.received_at<:history_start AND e.disposition='accepted'
), requests AS MATERIALIZED (
    SELECT r.id,r.job_id,r.sender_id,r.bot_id,r.chat_id,r.created_at,e.received_at,e.id event_id
    FROM candidate_runs a CROSS JOIN dialogue_runs r ON r.id=a.id
    CROSS JOIN jobs j ON j.id=r.job_id CROSS JOIN events e ON e.id=j.event_id
    WHERE e.payload!='{}'
      AND (:bot='' OR r.bot_id=:bot) AND r.mode='live' AND r.sender_id!=''
      AND r.trigger IN ('private','name','mention','reply','continuation')
      AND r.state NOT IN ('skipped','service')
), leader AS (
    SELECT sender_id,COUNT(*) requests FROM requests WHERE received_at>=:day_start
    GROUP BY sender_id ORDER BY requests DESC,sender_id LIMIT 1
), leader_requests AS MATERIALIZED (
    SELECT r.* FROM requests r JOIN leader l USING(sender_id) WHERE r.received_at>=:day_start
), leader_identity AS (
    SELECT COALESCE(json_extract(e.payload,'$.edited_message.from'),
                    json_extract(e.payload,'$.message.from')) profile
    FROM leader_requests r JOIN events e ON e.id=r.event_id
    ORDER BY r.received_at DESC,r.event_id DESC LIMIT 1
), photo_refs AS MATERIALIZED (
    SELECT DISTINCT r.id run_id,json_extract(ref.value,'$.batch_id') batch_id
    FROM requests r CROSS JOIN dialogue_runs d ON d.id=r.id,
         json_each(d.snapshot,'$.photo_refs') ref
    WHERE r.created_at>=:range_start
), request_photo_batches AS (
    SELECT r.job_id,b.id FROM requests r CROSS JOIN photo_batches b ON b.leader_job_id=r.job_id
    WHERE b.request_job_id IS NULL
    UNION ALL
    SELECT r.job_id,b.id FROM requests r CROSS JOIN photo_batches b ON b.request_job_id=r.job_id
), attachment_photo_batches AS (
    SELECT id FROM request_photo_batches
    UNION SELECT batch_id FROM photo_refs WHERE batch_id IS NOT NULL
), attachment_media_runs AS (
    SELECT DISTINCT md.media_run_id FROM requests r CROSS JOIN media_dependencies md USING(job_id)
), photo_attachments AS (
    SELECT i.bot_id,i.chat_id,i.message_id,i.event_id,b.finished_at,
           COALESCE(json_extract(e.payload,'$.edited_message'),
                    json_extract(e.payload,'$.message')) message
    FROM attachment_photo_batches a CROSS JOIN photo_batches b ON b.id=a.id
    CROSS JOIN photo_items i ON i.batch_id=b.id
    CROSS JOIN events e ON e.id=i.event_id
    WHERE b.finished_at>=:range_start AND b.finished_at<:range_end
      AND b.state IN ('completed','cache') AND b.result IS NOT NULL AND i.state='prepared'
), attachment_events AS (
    SELECT bot_id,chat_id,message_id,event_id,finished_at,0 native,
      CASE WHEN json_type(message,'$.photo')='array' THEN 'photo'
           WHEN json_type(message,'$.sticker')='object' THEN 'sticker'
           WHEN json_type(message,'$.video')='object' THEN 'video'
           WHEN json_type(message,'$.video_note')='object' THEN 'video_note'
           WHEN json_type(message,'$.animation')='object' THEN 'animation'
           ELSE 'other' END kind
    FROM photo_attachments
    UNION ALL
    SELECT m.bot_id,m.chat_id,m.message_id,m.event_id,COALESCE(m.finished_at,asr.finished_at),1,
           CASE WHEN m.kind IN ('voice','video','video_note','animation') THEN m.kind
                ELSE 'other' END kind
    FROM attachment_media_runs a CROSS JOIN media_runs m ON m.id=a.media_run_id
    LEFT JOIN model_calls asr ON m.finished_at IS NULL AND asr.media_run_id=m.id
      AND asr.operation='asr' AND asr.state='completed'
      AND NULLIF(TRIM(m.transcript),'') IS NOT NULL
    WHERE COALESCE(m.finished_at,asr.finished_at)>=:range_start
      AND COALESCE(m.finished_at,asr.finished_at)<:range_end AND m.state IN ('completed','partial')
      AND (NULLIF(TRIM(m.transcript),'') IS NOT NULL OR m.result IS NOT NULL)
), ranked_attachments AS (
    SELECT kind,MIN(finished_at) OVER item first_finished,
           ROW_NUMBER() OVER (item ORDER BY native DESC,(kind='other'),event_id DESC,kind) rank
    FROM attachment_events WINDOW item AS (PARTITION BY bot_id,chat_id,message_id)
), attachment_buckets AS (
    SELECT unixepoch(substr(first_finished,1,19))/:bucket_seconds bucket,kind,COUNT(*) count
    FROM ranked_attachments WHERE rank=1 GROUP BY bucket,kind
), request_buckets AS (
    SELECT unixepoch(substr(received_at,1,19))/:bucket_seconds bucket,COUNT(*) count FROM requests
    WHERE received_at>=:range_start AND received_at<:range_end GROUP BY bucket
), legacy_leader_media AS (
    SELECT DISTINCT c.id,
      CASE WHEN c.operation='vision' AND json_extract(m.analysis_context,'$.job_id') IS NOT NULL
      THEN json_extract(m.analysis_context,'$.job_id')=r.job_id
      WHEN NOT EXISTS (
          SELECT 1 FROM media_dependencies other JOIN jobs j ON j.id=other.job_id
          WHERE other.media_run_id=m.id AND other.job_id NOT IN (SELECT job_id FROM leader_requests)
            AND j.created_at<=c.created_at
      ) THEN 1 ELSE NULL END attributed
    FROM leader_requests r CROSS JOIN media_dependencies md USING(job_id)
    CROSS JOIN media_runs m ON m.id=md.media_run_id
    CROSS JOIN model_calls c ON c.media_run_id=m.id
    WHERE c.request_job_id IS NULL AND c.created_at>=r.received_at
), leader_calls AS (
    SELECT c.id FROM leader_requests r CROSS JOIN model_calls c ON c.run_id=r.id
    UNION
    SELECT c.id FROM leader_requests r CROSS JOIN model_calls c ON c.context_count_run_id=r.id
    UNION
    SELECT c.id FROM leader_requests r CROSS JOIN research_runs w USING(job_id)
    CROSS JOIN model_calls c ON c.research_run_id=w.id
    UNION
    SELECT c.id FROM leader_requests r CROSS JOIN avatar_runs w USING(job_id)
    CROSS JOIN model_calls c ON c.avatar_run_id=w.id
    UNION
    SELECT c.id FROM leader_requests r JOIN request_photo_batches b USING(job_id)
    CROSS JOIN model_calls c ON c.photo_batch_id=b.id
    UNION
    SELECT c.id FROM leader_requests r CROSS JOIN model_calls c ON c.request_job_id=r.job_id
    UNION
    SELECT id FROM legacy_leader_media WHERE attributed=1
), leader_sources AS (
    SELECT id run_id,event_id FROM leader_requests
    UNION
    SELECT r.id,json_extract(src.value,'$.event_id')
    FROM leader_requests r CROSS JOIN dialogue_runs d ON d.id=r.id,
         json_each(d.snapshot,'$.source_manifest') src
), unassigned_calls AS (
    SELECT id FROM legacy_leader_media WHERE attributed IS NULL
    UNION
    SELECT c.id FROM leader_requests r JOIN leader_sources src ON src.run_id=r.id
    CROSS JOIN photo_items i ON i.event_id=src.event_id
    CROSS JOIN photo_batches b ON b.id=i.batch_id CROSS JOIN model_calls c ON c.photo_batch_id=b.id
    WHERE b.leader_job_id IS NULL AND b.request_job_id IS NULL
      AND b.created_at>=r.received_at AND b.created_at<=r.created_at
), costs AS (
    SELECT COUNT(*) calls,COALESCE(SUM(COALESCE(c.cost_usd,0)+COALESCE(c.search_cost_usd,0)),0)
      known_cost_usd,
      COUNT(*) FILTER (WHERE c.cost_usd IS NOT NULL OR c.search_cost_usd IS NOT NULL) priced_calls,
      COUNT(*) FILTER (WHERE c.cost_usd IS NULL OR
        (c.research_run_id IS NOT NULL AND c.search_cost_usd IS NULL)) unknown_cost_calls
    FROM leader_calls l CROSS JOIN model_calls c USING(id)
)
SELECT
    (SELECT json_group_array(json_array(bucket,count)) FROM request_buckets),
    (SELECT json_group_array(json_array(bucket,kind,count)) FROM attachment_buckets),
    (SELECT requests FROM leader),
    (SELECT json_extract(profile,'$.username') FROM leader_identity),
    (SELECT json_extract(profile,'$.first_name') FROM leader_identity),
    costs.*,
    (SELECT COUNT(*) FROM unassigned_calls u WHERE u.id NOT IN (SELECT id FROM leader_calls))
FROM costs
"""


async def overview_activity(
    db: Database, bot: str = "", *, period_minutes: int = 60, now: datetime | None = None
) -> dict[str, Any]:
    now = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    bucket_minutes = ACTIVITY_BUCKET_MINUTES[period_minutes]
    bucket_seconds = bucket_minutes * 60
    range_end = now.replace(second=0, microsecond=0)
    range_start = range_end - timedelta(minutes=period_minutes)
    day_start = now - timedelta(hours=24)
    params = {
        "history_start": min(day_start, range_start).isoformat(),
        "day_start": day_start.isoformat(),
        "now": now.isoformat(),
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "bucket_seconds": bucket_seconds,
        "bot": bot,
    }
    async with db.read_lock, db.reader.execute(ACTIVITY_SQL, params) as cursor:
        value = await cursor.fetchone()
    assert value is not None
    (
        request_json,
        attachment_json,
        count,
        username,
        name,
        calls,
        known_cost,
        priced,
        unknown,
        unassigned,
    ) = value
    requests = dict(json.loads(request_json))
    attachments: dict[int, dict[str, int]] = defaultdict(dict)
    for bucket, kind, amount in json.loads(attachment_json):
        attachments[bucket][kind] = amount
    start, end = int(range_start.timestamp()), int(range_end.timestamp())
    return {
        "period_minutes": period_minutes,
        "bucket_minutes": bucket_minutes,
        "minutes": [
            {
                "bucket_time": bucket * bucket_seconds,
                "time": max(start, bucket * bucket_seconds),
                "end": min(end, (bucket + 1) * bucket_seconds),
                "requests": requests.get(bucket, 0),
                "attachments": sum(attachments[bucket].values()),
                "attachment_types": attachments[bucket],
            }
            for bucket in range(start // bucket_seconds, (end - 1) // bucket_seconds + 1)
        ],
        "leader": {
            "username": username,
            "name": name,
            "requests": count,
            "cost_usd": known_cost if priced or not (calls or unassigned) else None,
            "unknown_cost_calls": unknown,
            "unassigned_calls": unassigned,
        }
        if count
        else None,
    }
