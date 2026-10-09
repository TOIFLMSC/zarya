import { useEffect, useState } from "react";
import { api, ApiError, type ResearchList, type ResearchDetail } from "./api";

const states: Record<string, string> = {
  queued: "В очереди",
  fetching: "Загрузка материала",
  search_started: "Поиск и проверка",
  completed: "Завершено",
  invalid: "Неполный результат",
  rejected: "Запрос отклонён",
  unknown: "Состояние неизвестно",
  cancelled: "Отменено",
};
const coverage: Record<string, string> = {
  partial_text: "Фрагмент текста",
  metadata_only: "Только заголовок и описание",
  youtube_metadata: "YouTube: описание и доступные главы",
  youtube_transcript: "YouTube: метаданные и субтитры",
  unavailable: "Материал недоступен",
  search_source: "Источник из поиска",
  realtime_feed: "Поток актуальных данных",
};
const kinds: Record<string, string> = {
  fact: "Проверяемое утверждение",
  opinion: "Мнение",
  uncertain: "Неопределённость",
};
const verdicts: Record<string, string> = {
  supported: "Подтверждается",
  refuted: "Опровергается",
  uncertain: "Недостаточно данных",
};
const money = (v: number | null | undefined) =>
  v == null ? "Неизвестно" : `$${v.toFixed(6)}`;
const link = (v: string | null | undefined) =>
  v && /^https?:\/\//i.test(v) ? v : undefined;

export function ResearchPanel({
  onUnauthorized,
}: {
  onUnauthorized: () => void;
}) {
  const [scope, setScope] = useState("all"),
    [chat, setChat] = useState(""),
    [page, setPage] = useState(0),
    [selected, setSelected] = useState<number | null>(null),
    [revision, setRevision] = useState(0);
  const [list, setList] = useState<ResearchList | null>(null),
    [detail, setDetail] = useState<ResearchDetail | null>(null),
    [error, setError] = useState(""),
    [detailError, setDetailError] = useState("");
  function fail(e: unknown, target: (v: string) => void) {
    if (e instanceof ApiError && e.status === 401) onUnauthorized();
    target(e instanceof Error ? e.message : "Нет связи с сервером");
  }
  useEffect(() => {
    let active = true,
      busy = false;
    setList(null);
    async function load() {
      if (busy) return;
      busy = true;
      try {
        const v = await api<ResearchList>(
          `research?scope=${scope}&chat=${encodeURIComponent(chat)}&page=${page}`,
        );
        if (active) {
          setList(v);
          setError("");
        }
      } catch (e) {
        if (active) fail(e, setError);
      } finally {
        busy = false;
      }
    }
    void load();
    const t = window.setInterval(() => void load(), 3000);
    return () => {
      active = false;
      window.clearInterval(t);
    };
  }, [scope, chat, page, revision]);
  useEffect(() => {
    let active = true,
      busy = false;
    setDetail(null);
    setDetailError("");
    if (selected === null) return;
    async function load() {
      if (busy) return;
      busy = true;
      try {
        const v = await api<ResearchDetail>(`research/${selected}`);
        if (active) {
          setDetail(v);
          setDetailError("");
        }
      } catch (e) {
        if (active) fail(e, setDetailError);
      } finally {
        busy = false;
      }
    }
    void load();
    const t = window.setInterval(() => void load(), 3000);
    return () => {
      active = false;
      window.clearInterval(t);
    };
  }, [selected, revision]);
  return (
    <div className="photo-panel research-panel">
      <section className="card">
        <div className="filter-bar">
          <label>
            Область
            <select
              aria-label="Область"
              value={scope}
              onChange={(e) => {
                setScope(e.target.value);
                setChat("");
                setPage(0);
                setSelected(null);
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
              aria-label="Чат"
              value={chat}
              onChange={(e) => {
                setChat(e.target.value);
                setPage(0);
                setSelected(null);
              }}
            >
              <option value="">Все</option>
              {list?.chats
                .filter((c) => scope === "all" || c.scope === scope)
                .map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id}
                  </option>
                ))}
            </select>
          </label>
          <button className="quiet" onClick={() => setRevision((v) => v + 1)}>
            Обновить
          </button>
        </div>
        {list && (
          <p className="muted">
            {list.bot_id ?? "Telegram не подключён"} ·{" "}
            {list.enabled ? "Поиск включён" : "Поиск выключен"} · {list.model}
          </p>
        )}
        {list && !list.configured && (
          <p className="notice">Ключ OpenAI не настроен. Поиск недоступен.</p>
        )}
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        {!list && !error && <p role="status">Загружаем исследования…</p>}
        {list?.items.length === 0 && (
          <p>Для выбранных чатов исследований пока нет.</p>
        )}
        <div className="photo-list">
          {list?.items.map((item) => (
            <button
              key={item.id}
              className={`photo-row ${selected === item.id ? "selected" : ""}`}
              aria-expanded={selected === item.id}
              aria-controls="research-detail"
              onClick={() => setSelected(selected === item.id ? null : item.id)}
            >
              <span>
                <strong>
                  {item.mode === "search" ? "Проверка" : "Разбор материала"} · #
                  {item.id}
                </strong>
                <small>
                  {item.scope === "private" ? "Личная переписка" : "Группа"} ·{" "}
                  {item.chat_id} ·{" "}
                  {new Date(item.created_at).toLocaleString("ru")}
                </small>
                <span className="photo-caption">
                  {item.question || "Содержимое удалено"}
                </span>
              </span>
              <span className="badge">{states[item.state] ?? item.state}</span>
            </button>
          ))}
        </div>
        {list && list.total > 20 && (
          <div className="action-bar">
            <button
              disabled={page === 0}
              onClick={() => {
                setPage(page - 1);
                setSelected(null);
              }}
            >
              Назад
            </button>
            <span>{page + 1}</span>
            <button
              disabled={(page + 1) * 20 >= list.total}
              onClick={() => {
                setPage(page + 1);
                setSelected(null);
              }}
            >
              Далее
            </button>
          </div>
        )}
      </section>
      {selected !== null && (
        <section className="card" id="research-detail" aria-live="polite">
          {detailError && (
            <p className="notice error" role="alert">
              {detailError}
            </p>
          )}
          {!detail && !detailError && <p role="status">Открываем проверку…</p>}
          {detail && (
            <>
              <div className="section-head">
                <h2>Проверка #{detail.id}</h2>
                <span className="badge">
                  {states[detail.state] ?? detail.state}
                </span>
              </div>
              <p className="muted">
                {detail.scope === "private" ? "Личная переписка" : "Группа"} ·{" "}
                {detail.chat_id}
              </p>
              <p className="research-text">{detail.question}</p>
              {detail.material && (
                <details>
                  <summary>Переданный пост</summary>
                  <p className="research-text">{detail.material}</p>
                </details>
              )}
              {detail.error && (
                <p className="notice">Код ограничения: {detail.error}</p>
              )}
              {detail.result && (
                <>
                  <h3>Результат проверки</h3>
                  <p className="research-text">{detail.result.summary}</p>
                  <h3>Тезисы</h3>
                  {detail.result.claims.map((c, i) => (
                    <div className="research-source" key={i}>
                      <p className="research-text">{c.text}</p>
                      <p className="muted">
                        {kinds[c.kind]} · {verdicts[c.verdict]}
                      </p>
                      <div className="research-links">
                        {c.source_ids.map((id) => {
                          const s = detail.sources.find((s) => s.id === id);
                          return s && link(s.url) ? (
                            <a
                              key={id}
                              href={s.url!}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              Источник {id} ↗
                            </a>
                          ) : s?.coverage === "realtime_feed" ? (
                            <span key={id}>
                              Источник {id}: {s.title}
                            </span>
                          ) : null;
                        })}
                      </div>
                    </div>
                  ))}
                </>
              )}
              <h3>
                {detail.mode === "search" ? "Найденные источники" : "Материалы"}
              </h3>
              {detail.sources.length === 0 && (
                <p className="muted">Источников пока нет.</p>
              )}
              {detail.sources.map((s) => (
                <article className="research-source" key={s.id}>
                  <h4>{s.title || "Недоступный материал"}</h4>
                  <p className="muted">
                    Источник {s.id} · {coverage[s.coverage] ?? s.coverage}
                    {detail.result?.claims.some((c) =>
                      c.source_ids.includes(s.id),
                    )
                      ? " · Использован в выводах"
                      : ""}
                  </p>
                  {link(s.url) && (
                    <a href={s.url!} target="_blank" rel="noopener noreferrer">
                      {s.url} ↗
                    </a>
                  )}
                  {!s.url && s.requested_url && (
                    <p className="muted research-text">{s.requested_url}</p>
                  )}
                  {s.coverage === "realtime_feed" && s.retrieved_at && (
                    <p className="muted">
                      Получено: {new Date(s.retrieved_at).toLocaleString("ru")}.
                      Это время получения ответа, а не отметка времени
                      котировки.
                    </p>
                  )}
                  {s.coverage === "metadata_only" && (
                    <p className="notice">
                      Содержание видео не проверено. Доступны только метаданные.
                    </p>
                  )}
                  {s.youtube && (
                    <div>
                      <p className="muted">
                        {s.youtube.channel && `Канал: ${s.youtube.channel} · `}
                        {s.youtube.duration_seconds != null &&
                          `Длительность: ${Math.ceil(s.youtube.duration_seconds / 60)} мин · `}
                        Глав: {s.youtube.chapters?.length ?? 0}
                      </p>
                      <p>
                        {s.youtube.transcript_status === "available"
                          ? `Субтитры: ${s.youtube.transcript_automatic ? "автоматические" : "авторские"}, ${s.youtube.transcript_language ?? "язык не указан"}`
                          : "Субтитры получить не удалось"}
                        {s.youtube.transcript_truncated
                          ? " · прочитан только начальный фрагмент"
                          : ""}
                      </p>
                      {s.youtube.description_truncated && (
                        <p className="muted">Описание сокращено лимитом.</p>
                      )}
                      <p className="notice">
                        Разбор по текстовым материалам. Видеоряд не
                        просматривался.
                      </p>
                      {s.youtube.fallback_used && (
                        <p className="muted">
                          Использован запасной поиск сведений о ролике.
                        </p>
                      )}
                      {!!s.youtube.chapters?.length && (
                        <details>
                          <summary>Главы и таймкоды</summary>
                          {s.youtube.chapters.map((c) => (
                            <p key={c.seconds}>
                              {Math.floor(c.seconds / 60)}:
                              {String(c.seconds % 60).padStart(2, "0")} —{" "}
                              {c.title}
                            </p>
                          ))}
                        </details>
                      )}
                      {!!s.youtube.diagnostics?.length && (
                        <details>
                          <summary>Диагностика загрузки YouTube</summary>
                          {s.youtube.diagnostics.map((d, i) => (
                            <p key={i}>
                              {d.step}: {d.code}
                            </p>
                          ))}
                        </details>
                      )}
                    </div>
                  )}
                  {s.coverage === "search_source" && (
                    <p className="muted">
                      Источник предоставлен поиском. Текст отдельно не
                      загружался.
                    </p>
                  )}
                  {s.error && <p className="muted">Ограничение: {s.error}</p>}
                  {s.text && (
                    <details>
                      <summary>Сохранённый текст</summary>
                      <p className="research-text">{s.text}</p>
                    </details>
                  )}
                </article>
              ))}
              <details>
                <summary>Поисковые действия и расход</summary>
                <p>
                  {detail.call?.model ?? detail.model} · {detail.reasoning}
                </p>
                {detail.actions.length === 0 && (
                  <p className="muted">Поисковых действий нет.</p>
                )}
                {detail.actions.map((a, i) => (
                  <div className="research-source" key={i}>
                    <strong>
                      {(
                        {
                          search: "Поиск",
                          open_page: "Открытие страницы",
                          find_in_page: "Поиск на странице",
                        } as Record<string, string>
                      )[a.type] ?? a.type}
                    </strong>
                    <p className="research-text">
                      {a.queries?.join("\n") ||
                        a.query ||
                        a.url ||
                        "Поисковый запрос не предоставлен"}
                    </p>
                    <small>{states[a.status ?? ""] ?? a.status}</small>
                  </div>
                ))}
                {detail.call ? (
                  <>
                    <p>
                      Токены:{" "}
                      {detail.call.usage
                        ? `${detail.call.usage.input_tokens} вход / ${detail.call.usage.output_tokens} выход`
                        : "Неизвестно"}
                    </p>
                    <p>
                      Длительность:{" "}
                      {detail.call.latency_ms == null
                        ? "Неизвестно"
                        : `${detail.call.latency_ms} мс`}
                    </p>
                    <p>
                      Оценка модели: {money(detail.call.cost_usd)} · поиска:{" "}
                      {money(detail.call.search_cost_usd)}
                    </p>
                    <p className="muted">
                      Это оценка по usage и наблюдаемым поисковым действиям, а
                      не подтверждённое списание. Финальный ответ учитывается
                      отдельно в «Диалогах».
                    </p>
                  </>
                ) : (
                  <p className="muted">
                    Платный поиск не выполнялся. Расход на ответ — в «Диалогах».
                  </p>
                )}
              </details>
            </>
          )}
        </section>
      )}
    </div>
  );
}
