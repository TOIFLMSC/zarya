"""Privacy hooks for the local edit journal. No domain mutations live here."""

import aiosqlite


async def scrub_scope(
    c: aiosqlite.Connection, bot: str, chat: str, *, groups: bool = False
) -> None:
    scope = "bot_id=? AND (chat_id=?" + (" OR chat_id LIKE '-%'" if groups else "") + ")"
    await c.execute(
        "UPDATE obsidian_files SET baseline=NULL,state='purge' "
        "WHERE " + scope + " AND state!='unowned'",
        (bot, chat),
    )
    await c.execute(
        "UPDATE obsidian_changes SET before_text=NULL,proposed_text=NULL,state='invalidated',"
        "reason='Сведения отозваны или источник изменён' WHERE " + scope,
        (bot, chat),
    )
    await c.execute(
        "UPDATE memory_commands SET before_text=NULL,after_text=NULL WHERE " + scope,
        (bot, chat),
    )
