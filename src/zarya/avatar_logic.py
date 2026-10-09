"""Avatar provenance is distinct from chat attachments; no global profile cache."""

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

from zarya.dialogue_logic import topic_id
from zarya.models import Settings
from zarya.source_material import one
from zarya.source_material import valid as source_valid

AVATAR_WAIT = (
    "EXISTS (SELECT 1 FROM avatar_runs ar WHERE ar.job_id=j.id "
    "AND ar.state IN ('queued','fetching','analyzing')) "
)
AVATAR_WORD = re.compile(r"\b(?:ав[аыуе]|авк\w*|аватар\w*)\b|фото\s+профиля", re.I)
FOLLOWUP = re.compile(
    r"^(?:заря[,! ]+)?(?:а\s+)?(?:ну\s+)?"
    r"(?:(?:покажи|посмотри|оцени|разбери|сравни|как тебе)\s+)?"
    r"(?:(?:его|её|ее|мои)\s+)?"
    r"(?:остальн\w*|предыдущ\w*|следующ\w*|ещ[её]|перв\w*|втор\w*|треть\w*|"
    r"какая(?:\s+из них)?\s+лучше|какую(?:\s+из них)?\s+(?:ты\s+)?(?:выбрала|выберешь))"
    r"(?:\s+(?:фото|картинки|изображения))?[?!. ]*$",
    re.I,
)


async def valid(conn: Any, run: int | None) -> bool:
    if run is None:
        return True
    item = await one(
        conn,
        "SELECT bot_id,chat_id,access_version,binding,state,error_code FROM avatar_runs WHERE id=?",
        (run,),
    )
    if not item or item[4] == "cancelled":
        return False
    setting = await one(conn, "SELECT document FROM settings WHERE id=1", ())
    settings = Settings.model_validate_json(setting[0])
    return bool(
        settings.dialogue_enabled
        and (settings.photo_enabled or (item[4] == "unavailable" and item[5] == "photos_disabled"))
        and await source_valid(
            conn,
            item[0],
            item[1],
            item[2],
            json.loads(item[3]),
        )
    )


async def previous(
    conn: Any, bot: str, chat: str, grant: int, message: dict[str, Any]
) -> dict[str, Any] | None:
    reply = message.get("reply_to_message") or {}
    # Explicit replies bind only to an actually delivered assistant response.
    extra = "o.message_id=?" if reply else "r.sender_id=? AND r.created_at>=?"
    params: tuple[Any, ...] = (
        (reply["message_id"],)
        if reply
        else (
            str(message.get("from", {}).get("id", "")),
            datetime.fromtimestamp(time.time() - 600, UTC).isoformat(),
        )
    )
    found = await one(
        conn,
        "SELECT r.snapshot FROM outbox o JOIN dialogue_runs r ON r.job_id=o.job_id "
        "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.state='sent' "
        "AND r.state='completed' AND r.mode='live' "
        "AND COALESCE(r.thread_id,0)=? AND " + extra + " ORDER BY o.id DESC LIMIT 1",
        (bot, chat, grant, topic_id(message) or 0, *params),
    )
    if not found:
        return None
    snapshot = json.loads(found[0])
    run = snapshot.get("avatar_id")
    if not run or snapshot.get("invalidated") or not await valid(conn, run):
        return None
    result = await one(
        conn, "SELECT user_id,target_name,binding,selection FROM avatar_runs WHERE id=?", (run,)
    )
    return {
        "id": run,
        "user_id": result[0],
        "name": result[1],
        "binding": json.loads(result[2]),
        "selection": json.loads(result[3]),
    }


async def purge(conn: Any, where: str, args: tuple[Any, ...]) -> None:
    from zarya.source_material import purge as purge_material

    ids = "SELECT id FROM avatar_runs WHERE " + where
    await purge_material(conn, "json_extract(snapshot,'$.avatar_id') IN (" + ids + ")", args)
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE avatar_run_id IN (" + ids + ")", args
    )
    await conn.execute(
        "UPDATE avatar_runs SET state='cancelled',selection='{}',result=NULL,"
        "target_name='',user_id='',binding='[]',error_code='source_changed' WHERE " + where,
        args,
    )
