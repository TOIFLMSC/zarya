import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type MediaLibrary,
  type MediaDetail,
  type PhotoDetail,
} from "./api";
import { MediaResult } from "./MediaResult";
import { PhotoResult } from "./PhotoResult";

const kinds: Record<string, string> = {
  photo: "Фото",
  voice: "Голосовое",
  video: "Видео",
  video_note: "Кружочек",
  animation: "GIF / анимация",
};
const states: Record<string, string> = {
  queued: "В очереди",
  downloading: "Загрузка фото",
  preparing: "Подготовка",
  transcribing: "Распознавание речи",
  analyzing: "Анализ",
  completed: "Готово",
  cache: "Готово · из кэша",
  partial: "Частичный результат",
  unavailable: "Недоступно",
  unknown: "Результат неизвестен",
  cancelled: "Отменено",
  rejected: "Отклонено",
  started: "Выполняется",
  invalid: "Неполный результат",
  error: "Ошибка",
};
const money = (n: number | null) =>
  n == null ? "Неизвестно" : `$${n.toFixed(6)}`;
const duration = (n: number) =>
  `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, "0")}`;
type Item = MediaLibrary["items"][number];
const itemKey = (item: Item) => `${item.source}-${item.id}`;

export function MediaPanel({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [scope, setScope] = useState("all"),
    [chat, setChat] = useState(""),
    [kind, setKind] = useState("all");
  const [page, setPage] = useState(0),
    [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [list, setList] = useState<MediaLibrary | null>(null),
    [error, setError] = useState("");
  // Refresh the list explicitly: incoming messages must not shift rows while
  // the owner is reading a result or navigating historical pages.
  useEffect(() => {
    let active = true;
    setList(null);
    setError("");
    api<MediaLibrary>(
      `media-library?scope=${scope}&chat_id=${encodeURIComponent(chat)}&kind=${kind}&page=${page}`,
    )
      .then((value) => {
        if (active) {
          setList(value);
          setPage(value.page);
        }
      })
      .catch((e) => {
        if (active) {
          if (e instanceof ApiError && e.status === 401) onUnauthorized();
          setError(e instanceof Error ? e.message : "Нет связи с сервером");
        }
      });
    return () => {
      active = false;
    };
  }, [scope, chat, kind, page, revision]);
  function reset() {
    setPage(0);
    setSelected(null);
  }
  const pages = Math.max(1, Math.ceil((list?.total ?? 0) / 20));
  function pagination(position: string) {
    return (
      list && (
        <nav
          className="pagination library-pagination"
          aria-label={`Страницы медиа · ${position}`}
        >
          <span className="pagination-count">
            {list.total
              ? `${list.page * 20 + 1}–${Math.min((list.page + 1) * 20, list.total)} из ${list.total}`
              : "Нет записей"}
          </span>
          <button
            className="quiet"
            disabled={!list.page}
            onClick={() => {
              setPage(list.page - 1);
              setSelected(null);
            }}
          >
            Назад
          </button>
          <span>
            Страница {list.page + 1} из {pages}
          </span>
          <button
            className="quiet"
            disabled={list.page + 1 >= pages}
            onClick={() => {
              setPage(list.page + 1);
              setSelected(null);
            }}
          >
            Далее
          </button>
        </nav>
      )
    );
  }
  return (
    <div className="media-library">
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
                reset();
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
                reset();
              }}
            >
              <option value="">Все чаты</option>
              {list?.chats.map((c) => (
                <option key={`${c.scope}-${c.id}`} value={c.id}>
                  {c.title ? `${c.title} · ${c.id}` : c.id}
                </option>
              ))}
              {!list && chat && <option value={chat}>{chat}</option>}
            </select>
          </label>
          <label>
            Тип материала
            <select
              aria-label="Тип материала"
              value={kind}
              onChange={(e) => {
                setKind(e.target.value);
                reset();
              }}
            >
              <option value="all">Все материалы</option>
              {Object.entries(kinds).map(([value, label]) => (
                <option key={value} value={value}>
                  {value === "photo" ? "Фото и альбомы" : label}
                </option>
              ))}
            </select>
          </label>
          <button
            className="quiet"
            onClick={() => {
              setSelected(null);
              setRevision((v) => v + 1);
            }}
          >
            Обновить
          </button>
        </div>
        {list && (
          <>
            <p className="library-cost">
              Расход по выбранным фильтрам:{" "}
              <strong>{money(list.known_cost_usd)}</strong> · без оценки:{" "}
              {list.unknown_cost_calls}
            </p>
            <details className="library-settings">
              <summary>Модели и ограничения обработки</summary>
              <p>
                Фото: {list.photo_enabled ? "включены" : "выключены"} ·{" "}
                {list.photo_model}. Речь и видео:{" "}
                {list.enabled ? "включены" : "выключены"} · {list.asr_model} /{" "}
                {list.model}.
              </p>
              <p>
                Голосовые до {duration(list.limits.voice)}, видео до{" "}
                {duration(list.limits.video)} · до {list.limits.frames} кадров ·
                20 МиБ · до {list.limits.items} вложений на запрос.
              </p>
              <p>
                Фото пополняют контекст автоматически; речь и видео разбираются
                по обращению. Открытие результата не запускает повторный платный
                анализ.
              </p>
            </details>
            {(!list.photo_configured ||
              !list.configured ||
              !list.tools_available) && (
              <p className="notice error" role="status">
                {!list.photo_configured && "Анализ фото не подключён. "}
                {!list.configured && "Модели речи или видео не подключены. "}
                {!list.tools_available && "FFmpeg / ffprobe недоступен. "}
                Сохранённые операции остаются в журнале.
              </p>
            )}
          </>
        )}
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        {!list && !error && <p role="status">Загружаем медиа…</p>}
        {pagination("сверху")}
        {list?.total === 0 && (
          <p className="empty-state">По этим фильтрам материалов пока нет.</p>
        )}
        <div className="media-list">
          {list?.items.map((item) => {
            const key = itemKey(item),
              open = selected === key;
            return (
              <article
                className={`media-entry ${open ? "is-open" : ""}`}
                key={key}
              >
                <button
                  className="media-row"
                  aria-expanded={open}
                  aria-controls={`detail-${key}`}
                  onClick={() => setSelected(open ? null : key)}
                >
                  <span className="media-row-main">
                    <strong>
                      {item.kind === "photo" && item.count > 1
                        ? "Альбом"
                        : (kinds[item.kind] ?? item.kind)}{" "}
                      · № {item.id}
                    </strong>
                    <span className="media-row-meta">
                      {item.chat_title || item.chat_id} ·{" "}
                      {item.scope === "private" ? "Личная переписка" : "Группа"}
                    </span>
                    <span className="media-row-meta">
                      {new Date(item.created_at).toLocaleString("ru")}
                    </span>
                  </span>
                  <span className="media-row-status">
                    <span className="badge">
                      {states[item.state] ?? item.state}
                    </span>
                    <span className="media-row-meta">
                      {item.kind === "photo"
                        ? `${item.count} фото`
                        : item.duration == null
                          ? "Длительность неизвестна"
                          : duration(item.duration)}
                    </span>
                  </span>
                  <span className="media-chevron" aria-hidden="true">
                    {open ? "⌃" : "⌄"}
                  </span>
                </button>
                {open && (
                  <div
                    id={`detail-${key}`}
                    className="media-inline-detail"
                    role="region"
                    aria-label={`Разбор ${kinds[item.kind] ?? item.kind} № ${item.id}`}
                  >
                    <EntryResult
                      source={item.source}
                      id={item.id}
                      onUnauthorized={onUnauthorized}
                    />
                  </div>
                )}
              </article>
            );
          })}
        </div>
        {!!list?.items.length && pagination("снизу")}
      </section>
    </div>
  );
}
function EntryResult({
  source,
  id,
  onUnauthorized,
}: {
  source: "photo" | "media";
  id: number;
  onUnauthorized: () => void;
}) {
  const [detail, setDetail] = useState<PhotoDetail | MediaDetail | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true,
      busy = false;
    async function load() {
      if (busy) return;
      busy = true;
      try {
        const value = await api<PhotoDetail | MediaDetail>(
          `${source === "photo" ? "photos" : "media"}/${id}`,
        );
        if (active) {
          setDetail(value);
          setError("");
        }
      } catch (e) {
        if (active) {
          setDetail(null);
          setError(e instanceof Error ? e.message : "Нет связи с сервером");
          if (e instanceof ApiError && e.status === 401) onUnauthorized();
        }
      } finally {
        busy = false;
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [source, id]);
  return (
    <>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {!detail && !error && <p role="status">Загружаем разбор…</p>}
      {detail &&
        (source === "photo" ? (
          <PhotoResult detail={detail as PhotoDetail} />
        ) : (
          <MediaResult detail={detail as MediaDetail} />
        ))}
    </>
  );
}
