import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type MemoryList,
  type MemoryProfile,
  type MemoryFact,
} from "./api";

const labels: Record<string, string> = {
  self: "Самоописание",
  other: "Слова другого участника",
  inference: "Предположение модели",
  active: "Активно",
  proposed: "Предложено",
  disputed: "Оспаривается",
  outdated: "Устарело",
  name: "Обращение",
  interest: "Интерес",
  preference: "Предпочтение",
  topic: "Тема",
  joke: "Локальная шутка",
  observation: "Наблюдение",
  started: "Обрабатываем",
  completed: "Готово",
  cancelled: "Отменено",
  unknown: "Результат неизвестен",
  invalid: "Некорректный результат",
  rejected: "Отклонено",
};
const money = (value: number | null) =>
  value == null ? "Неизвестно" : `$${value.toFixed(6)}`;

export function MemoryPanel({
  csrf,
  onUnauthorized,
  onDirty,
}: {
  csrf: string | null;
  onUnauthorized: () => void;
  onDirty: (value: boolean) => void;
}) {
  const [list, setList] = useState<MemoryList | null>(null);
  const [scope, setScope] = useState("all");
  const [chat, setChat] = useState("");
  const [profile, setProfile] = useState<MemoryProfile | null>(null);
  const [person, setPerson] = useState("");
  const [revision, setRevision] = useState(0);
  const [tab, setTab] = useState("people");
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<MemoryFact | null>(null);
  const [draft, setDraft] = useState("");
  const dirty = editing !== null && draft !== editing.text;
  function fail(e: unknown, target: (v: string) => void) {
    if (e instanceof ApiError && e.status === 401) onUnauthorized();
    target(e instanceof Error ? e.message : "Нет связи с сервером");
  }
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty]);
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  function leave() {
    if (
      busy ||
      (dirty && !window.confirm("Отменить несохранённые изменения записи?"))
    )
      return false;
    setEditing(null);
    setDraft("");
    setActionError("");
    setMessage("");
    return true;
  }
  useEffect(() => {
    let active = true,
      loading = false;
    setList(null);
    async function load() {
      if (loading) return;
      loading = true;
      try {
        const result = await api<MemoryList>(`memory?scope=${scope}`);
        if (active) {
          setList(result);
          setError("");
        }
      } catch (e) {
        if (active) {
          setList(null);
          fail(e, setError);
        }
      } finally {
        loading = false;
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [scope, revision]);
  useEffect(() => {
    let active = true,
      loading = false;
    setProfile(null);
    setDetailError("");
    if (!chat) return;
    async function load() {
      if (loading) return;
      loading = true;
      try {
        const result = await api<MemoryProfile>(
          `memory/chats/${encodeURIComponent(chat)}`,
        );
        if (active) {
          setProfile(result);
          setDetailError("");
        }
      } catch (e) {
        if (active) {
          setProfile(null);
          fail(e, setDetailError);
        }
      } finally {
        loading = false;
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [chat, revision]);
  async function mutate(fact: MemoryFact, action: string) {
    if (!profile || busy) return;
    if (
      action === "delete" &&
      !window.confirm(
        `Удалить «${fact.text}» из памяти чата ${chat}? Исходная реплика и зависимые сводки/журналы будут исключены из контекста.`,
      )
    )
      return;
    if (
      action === "share" &&
      !window.confirm(
        `Использовать «${fact.text}» в других одобренных группах, где пишет участник ${fact.sender_id}?`,
      )
    )
      return;
    setBusy(true);
    setActionError("");
    setMessage("");
    try {
      await api(
        `memory/facts/${fact.id}`,
        "POST",
        {
          bot_id: profile.bot_id,
          expected_version: fact.version,
          action,
          ...(action === "edit" ? { text: draft.trim() } : {}),
        },
        csrf,
      );
      setEditing(null);
      setDraft("");
      onDirty(false);
      setRevision((v) => v + 1);
      setMessage(
        action === "delete"
          ? "Запись удалена. Зависимый контекст очищен."
          : "Запись обновлена. Следующие ответы учтут изменение.",
      );
    } catch (e) {
      fail(e, setActionError);
    } finally {
      setBusy(false);
    }
  }
  const visible =
    profile?.facts.filter((f) => !person || f.sender_id === person) ?? [];
  return (
    <div className="memory-panel">
      <section className="card">
        <div className="filter-bar">
          <label>
            Область
            <select
              disabled={busy}
              value={scope}
              onChange={(e) => {
                if (leave()) {
                  setScope(e.target.value);
                  setChat("");
                  setPerson("");
                }
              }}
            >
              <option value="all">Все чаты</option>
              <option value="group">Группы</option>
              <option value="private">Личные переписки</option>
            </select>
          </label>
          <label>
            Чат
            <select
              disabled={busy}
              value={chat}
              onChange={(e) => {
                if (leave()) {
                  setChat(e.target.value);
                  setPerson("");
                }
              }}
            >
              <option value="">Выберите чат</option>
              {list?.chats.map((c) => (
                <option key={c.chat_id} value={c.chat_id}>
                  {c.title} · {c.chat_id}
                </option>
              ))}
            </select>
          </label>
          <button
            className="quiet"
            disabled={busy}
            onClick={() => setRevision((v) => v + 1)}
          >
            Обновить
          </button>
        </div>
        {error && (
          <div className="notice error" role="alert">
            {error}
          </div>
        )}
        {!list && !error && <p role="status">Загружаем память…</p>}
        {list && (
          <>
            <p className="muted">
              {list.enabled ? "Память включена" : "Память выключена"} ·{" "}
              {list.model} · контекст {list.retention_days} дней · факты до
              удаления
            </p>
            <p className="muted">
              Расход памяти: {money(list.known_cost_usd)} · операций с
              неизвестным расходом: {list.unknown_cost_calls}
            </p>
            {!list.configured && (
              <div className="notice">
                Ключ OpenAI не настроен. Фоновый разбор недоступен.
              </div>
            )}
            {list.chats.length === 0 && (
              <p>
                Пока нет разрешённых чатов. Разреши общение в разделе «Telegram
                и доступ».
              </p>
            )}
          </>
        )}
      </section>
      {detailError && (
        <div className="notice error" role="alert">
          {detailError}
        </div>
      )}
      {chat && !profile && !detailError && (
        <p role="status">Загружаем профиль чата…</p>
      )}
      {profile && (
        <section className="card memory-profile">
          <p className="eyebrow">
            {profile.scope === "private"
              ? "ЛИЧНАЯ ПЕРЕПИСКА"
              : "ГРУППА · ОБЩАЯ ПАМЯТЬ"}
          </p>
          <h2>{profile.title}</h2>
          <p className="muted">Chat ID: {profile.chat_id}</p>
          <div className="notice">
            {profile.scope === "private"
              ? "Личный контекст отделён от групп. Перенос требует согласия самого собеседника через /memory в личке."
              : "В другие группы переносятся только выбранные имена, интересы и предпочтения. Сводки и локальные шутки остаются здесь."}
          </div>
          <div className="peer-actions" aria-label="Содержимое памяти">
            <button
              aria-pressed={tab === "people"}
              onClick={() => setTab("people")}
            >
              Собеседники
            </button>
            <button
              aria-pressed={tab === "summaries"}
              onClick={() => {
                if (leave()) setTab("summaries");
              }}
            >
              События и сводки
            </button>
          </div>
          {actionError && (
            <div className="notice error" role="alert">
              {actionError}
            </div>
          )}
          {message && (
            <div className="notice success" role="status">
              {message}
            </div>
          )}
          {tab === "people" ? (
            <>
              <label>
                Собеседник
                <select
                  disabled={busy}
                  value={person}
                  onChange={(e) => {
                    if (leave()) setPerson(e.target.value);
                  }}
                >
                  <option value="">Все собеседники</option>
                  {profile.participants.map((p) => (
                    <option key={p.sender_id} value={p.sender_id}>
                      {p.name} · {p.sender_id}
                    </option>
                  ))}
                </select>
              </label>
              <p className="muted">
                «Активно» означает использование в ответах, а не доказанную
                истинность. Самоописания активируются автоматически.
                Повторяющиеся наблюдения учитываются только здесь и как
                предположения; остальные чужие слова и догадки ждут проверки.
              </p>
              {!visible.length && (
                <p>
                  Записей пока нет. Фоновый разбор начнётся после новых
                  сообщений: обычно через минуту паузы в разговоре.
                </p>
              )}
              {visible.map((f) => (
                <article className="memory-fact" key={f.id}>
                  <div className="memory-meta">
                    <span className="badge">{labels[f.state] ?? f.state}</span>
                    <span>{labels[f.provenance]}</span>
                    <span>{labels[f.category]}</span>
                  </div>
                  <p className="muted">
                    Telegram ID: {f.sender_id} · запись №{f.id} ·{" "}
                    {new Date(f.updated_at).toLocaleString("ru-RU")}
                  </p>
                  {f.confidence && (
                    <div className="notice">
                      <strong>{f.confidence.label}</strong>
                      {f.confidence.kind !== "none" && (
                        <>
                          <p>
                            Подходящих сообщений: {f.confidence.messages} ·
                            авторов: {f.confidence.authors} · эпизодов:{" "}
                            {f.confidence.episodes}
                          </p>
                          {f.confidence.first_seen !== null &&
                            f.confidence.last_seen !== null && (
                              <p>
                                {new Date(
                                  f.confidence.first_seen * 1000,
                                ).toLocaleString("ru-RU")}
                                {" — "}
                                {new Date(
                                  f.confidence.last_seen * 1000,
                                ).toLocaleString("ru-RU")}
                              </p>
                            )}
                          <p>
                            {f.confidence.reason}. Повторяемость не гарантирует
                            истинность.
                          </p>
                        </>
                      )}
                    </div>
                  )}
                  {editing?.id === f.id ? (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        void mutate(editing, "edit");
                      }}
                    >
                      <label>
                        Текст записи
                        <textarea
                          required
                          maxLength={500}
                          disabled={busy}
                          value={draft}
                          onChange={(e) => setDraft(e.target.value)}
                        />
                      </label>
                      <div className="peer-actions">
                        <button
                          className="primary"
                          disabled={busy || !draft.trim()}
                        >
                          Сохранить запись
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            if (leave()) setEditing(null);
                          }}
                        >
                          Отмена
                        </button>
                      </div>
                      {f.version !== editing.version && (
                        <p role="alert">
                          Запись изменилась. Черновик сохранён; обнови запись
                          перед сохранением.
                        </p>
                      )}
                    </form>
                  ) : (
                    <p className="memory-text">{f.text}</p>
                  )}
                  <p className="muted">
                    {f.share?.state === "active" &&
                    f.share.fact_version === f.version
                      ? "Используется в других одобренных группах"
                      : "Только этот чат"}
                  </p>
                  {profile.scope === "private" && f.share && (
                    <p className="muted">
                      Согласие:{" "}
                      {f.share.state === "active" ? "действует" : "отозвано"} ·
                      участник {f.share.actor_id} · событие{" "}
                      {f.share.consent_event_id ?? "не указано"}
                    </p>
                  )}
                  <details>
                    <summary>Источники ({f.sources.length})</summary>
                    {f.sources.map((s) => (
                      <div className="memory-source" key={s.id}>
                        <p className="muted">
                          {s.name} · {s.sender_id} · сообщение {s.message_id}
                          {s.thread_id ? ` · топик ${s.thread_id}` : ""} ·{" "}
                          {new Date(s.received_at * 1000).toLocaleString(
                            "ru-RU",
                          )}
                        </p>
                        <p>
                          {s.text ||
                            "Исходный текст удалён, изменён или вышел за срок хранения."}
                        </p>
                        {f.confidence?.kind !== "none" &&
                          f.confidence?.source_ids.includes(s.id) && (
                            <p className="muted">
                              Учтено в повторяемости наблюдения
                            </p>
                          )}
                      </div>
                    ))}
                  </details>
                  <div className="peer-actions">
                    {f.state !== "active" && (
                      <button
                        disabled={busy || editing !== null}
                        onClick={() => void mutate(f, "accept")}
                      >
                        Принять
                      </button>
                    )}
                    <button
                      disabled={busy || editing !== null}
                      onClick={() => {
                        setEditing(f);
                        setDraft(f.text);
                        setActionError("");
                      }}
                    >
                      Редактировать
                    </button>
                    <button
                      disabled={busy || editing !== null}
                      onClick={() => void mutate(f, "delete")}
                    >
                      Удалить
                    </button>
                    {f.share?.state === "active" ? (
                      <button
                        disabled={busy || editing !== null}
                        onClick={() => void mutate(f, "unshare")}
                      >
                        Прекратить перенос
                      </button>
                    ) : (
                      profile.scope === "group" &&
                      ["name", "interest", "preference"].includes(
                        f.category,
                      ) && (
                        <button
                          disabled={
                            busy || editing !== null || f.state !== "active"
                          }
                          onClick={() => void mutate(f, "share")}
                        >
                          Использовать в других группах
                        </button>
                      )
                    )}
                  </div>
                </article>
              ))}
            </>
          ) : (
            <>
              <p className="muted">
                Здесь память о разговорах: темы, истории, шутки, договорённости
                и открытые вопросы. Для ответа Заря выбирает две свежие сводки и
                до трёх подходящих по теме. Это отдельный слой от сведений о
                людях.
              </p>
              <p className="muted">
                Показаны последние 30 операций. Завершённая обработка не
                обязательно создаёт факт о человеке. Отменённые и неопределённые
                операции автоматически не повторяются.
              </p>
              {!profile.batches.length && (
                <p>Сводок пока нет. Они появятся после фонового разбора.</p>
              )}
              {profile.batches.map((b) => (
                <article className="memory-fact" key={b.id}>
                  <p>
                    <span className="badge">{labels[b.state] ?? b.state}</span>{" "}
                    · пакет №{b.id} ·{" "}
                    {new Date(b.created_at).toLocaleString("ru-RU")}
                  </p>
                  <p className="memory-text">
                    {b.summary ||
                      "Сводка не создана или исключена из-за изменения её источников. Старые сводки могли быть очищены общим сбросом памяти."}
                  </p>
                  <details>
                    <summary>Обработка и расход</summary>
                    <p>
                      {b.model ?? "Модель не указана"} ·{" "}
                      {b.latency_ms == null
                        ? "Задержка неизвестна"
                        : `${b.latency_ms} мс`}{" "}
                      · {money(b.cost_usd)}
                    </p>
                    <p className="muted">
                      {b.error === "memory_changed"
                        ? "Источники или связанная память изменились. Результат исключён."
                        : b.error
                          ? `Диагностика: ${b.error}`
                          : "Без ошибок обработки"}
                    </p>
                    <pre>{b.usage ?? "Usage неизвестен"}</pre>
                  </details>
                </article>
              ))}
            </>
          )}
        </section>
      )}
    </div>
  );
}
