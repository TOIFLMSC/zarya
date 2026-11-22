import { useEffect, useState } from "react";
import { api, ApiError } from "./api";

type Change = {
  id: string;
  chat_id: string;
  fact_id: number | null;
  path?: string;
  state?: string;
  reason?: string | null;
  created_at: string;
  before_text: string | null;
  proposed_text?: string | null;
  current_text: string | null;
  current_version: number | null;
  after_text?: string | null;
  origin?: string;
  can_undo?: boolean;
};
type Bridge = {
  configured: boolean;
  path: string | null;
  error: string | null;
  scanning: boolean;
  last_scan: string | null;
  file_states: Record<string, number>;
  changes: Change[];
  total: number;
  page: number;
  history: Change[];
  history_total: number;
  history_page: number;
};
const date = (v: string) => new Date(v).toLocaleString("ru-RU");

export function ObsidianPanel({
  csrf,
  onUnauthorized,
  onChanged,
}: {
  csrf: string | null;
  onUnauthorized: () => void;
  onChanged: () => void;
}) {
  const [data, setData] = useState<Bridge | null>(null);
  const [page, setPage] = useState(0);
  const [historyPage, setHistoryPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [opened, setOpened] = useState("");
  function fail(e: unknown) {
    if (e instanceof ApiError && e.status === 401) onUnauthorized();
    setError(
      e instanceof Error ? e.message : "Не удалось проверить синхронизацию",
    );
  }
  useEffect(() => {
    if (opened || busy) return;
    let active = true;
    api<Bridge>(`obsidian?page=${page}&history_page=${historyPage}`)
      .then((value) => {
        if (!active) return;
        if (page > 0 && page * 10 >= value.total)
          setPage(Math.max(0, Math.ceil(value.total / 10) - 1));
        if (historyPage > 0 && historyPage * 10 >= value.history_total)
          setHistoryPage(Math.max(0, Math.ceil(value.history_total / 10) - 1));
        setData(value);
      })
      .catch((e) => {
        if (active) fail(e);
      });
    return () => {
      active = false;
    };
  }, [page, historyPage, revision, opened, busy]);
  useEffect(() => {
    // Follow a long scan without replacing a comparison the owner is reviewing.
    if (!data?.scanning || opened || busy) return;
    const timer = window.setTimeout(() => setRevision((v) => v + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [data?.scanning, opened, busy, revision]);
  async function act(path: string, body: unknown, success: string) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api(path, "POST", body, csrf);
      setMessage(success);
      setOpened("");
      setRevision((v) => v + 1);
      onChanged();
    } catch (e) {
      fail(e);
      setRevision((v) => v + 1);
    } finally {
      setBusy(false);
    }
  }
  function pages(total: number, current: number, update: (v: number) => void) {
    if (!total) return null;
    return (
      <div className="pagination library-pagination">
        <button
          type="button"
          disabled={busy || current === 0}
          onClick={() => {
            setOpened("");
            update(current - 1);
          }}
        >
          Назад
        </button>
        <span>
          {current * 10 + 1}–{Math.min(total, (current + 1) * 10)} из {total}
        </span>
        <button
          type="button"
          disabled={busy || (current + 1) * 10 >= total}
          onClick={() => {
            setOpened("");
            update(current + 1);
          }}
        >
          Далее
        </button>
      </div>
    );
  }
  function text(label: string, value: string | null | undefined) {
    return (
      <div>
        <p className="muted">{label}</p>
        <div className="obsidian-text">{value ?? "Недоступно"}</div>
      </div>
    );
  }
  return (
    <section className="card obsidian-panel">
      <div className="obsidian-heading">
        <div>
          <h2>Obsidian</h2>
          <p className="muted">
            Заметки памяти и предложенные правки. Персональные сводки доступны
            для чтения и содержат ссылки на исходные реплики.
          </p>
        </div>
        {data?.configured && (
          <button
            type="button"
            disabled={busy || data.scanning}
            onClick={() =>
              act(
                "obsidian/scan",
                {},
                "Синхронизация завершена. Списки обновлены",
              )
            }
          >
            {busy ? "Выполняем…" : "Обновить синхронизацию"}
          </button>
        )}
      </div>
      {!data && !error && <p role="status">Проверяем состояние хранилища…</p>}
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {message && (
        <div className="notice success" role="status">
          {message}
        </div>
      )}
      {data && (
        <>
          {data.configured ? (
            <>
              <p>
                Папка проекта:{" "}
                <code className="obsidian-path">{data.path}</code>
              </p>
              <p className="muted">
                {data.scanning
                  ? "Проверяем хранилище…"
                  : data.last_scan
                    ? `Последняя проверка: ${date(data.last_scan)}`
                    : "Настроено. Проверка ещё не выполнялась"}
              </p>
              <p className="muted">
                Готовых заметок: {data.file_states.ready ?? 0} · правок для
                просмотра: {data.total} · отсутствующих заметок:{" "}
                {data.file_states.missing ?? 0}
              </p>
            </>
          ) : (
            <>
              <p>
                Папка Obsidian не подключена на стороне Зари. Память чатов
                доступна в панели.
              </p>
              <details>
                <summary>Как подключается Obsidian</summary>
                <p>
                  Файловый мост использует <code>obsidian.local.json</code>{" "}
                  из каталога данных установки.
                </p>
              </details>
            </>
          )}
          {data.error && (
            <div className="notice error" role="alert">
              {data.error}
            </div>
          )}
          {data.configured && (
            <>
              <p className="muted">
                Правки текста применяются после твоего решения здесь. Исходники и
                эпизоды доступны только для чтения. Удаление файла не удаляет
                память.
              </p>
              <p className="muted">
                Группы и одобренные лички остаются отдельными областями. Мост
                очищает управляемые заметки; версии Sync, корзины и резервные копии
                не очищаются автоматически.
              </p>
            </>
          )}
          <details
            className="obsidian-section"
            open={data.total > 0 || undefined}
          >
            <summary>Правки из Obsidian ({data.total})</summary>
            {!data.total && (
              <p className="muted">Правок, ожидающих решения, нет</p>
            )}
            {data.changes.map((c) => (
              <div className="obsidian-row" key={c.id}>
                <span className="badge">
                  {c.state === "pending"
                    ? "Ожидает решения"
                    : c.state === "conflict"
                      ? "Конфликт"
                      : "Некорректная заметка"}
                </span>
                <p>
                  Чат {c.chat_id} ·{" "}
                  {c.fact_id ? `запись №${c.fact_id}` : "заметка для чтения"} ·{" "}
                  {date(c.created_at)}
                </p>
                <p className="muted obsidian-path">{c.path}</p>
                <button
                  type="button"
                  aria-expanded={opened === c.id}
                  onClick={() => setOpened(opened === c.id ? "" : c.id)}
                >
                  {opened === c.id ? "Скрыть сравнение" : "Показать сравнение"}
                </button>
                {opened === c.id && (
                  <>
                    <div className="obsidian-compare">
                      {text("При выгрузке", c.before_text)}
                      {text(
                        `Сейчас в Заре · версия ${c.current_version ?? "—"}`,
                        c.current_text,
                      )}
                      {text("Правка из Obsidian", c.proposed_text)}
                    </div>
                    {c.reason && (
                      <p className="notice">
                        {c.reason}. Отклони правку и обнови синхронизацию.
                        Автоматического объединения нет.
                      </p>
                    )}
                    <p className="muted">
                      Ручная правка сделает запись активной, отзовёт разрешения
                      на её перенос и очистит зависимые сводки и ответы. Область
                      и собеседник сохраняются.
                    </p>
                    <div className="action-bar">
                      <button
                        className="primary"
                        type="button"
                        disabled={busy || c.state !== "pending"}
                        onClick={() =>
                          act(
                            `obsidian/changes/${c.id}`,
                            { action: "apply" },
                            "Текст применён. Обнови синхронизацию для выгрузки новой версии",
                          )
                        }
                      >
                        Применить текст
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          act(
                            `obsidian/changes/${c.id}`,
                            { action: "reject" },
                            "Правка отклонена. Обнови синхронизацию для выгрузки текущего текста",
                          )
                        }
                      >
                        Отклонить правку
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))}
            {pages(data.total, page, setPage)}
          </details>
          <details className="obsidian-section">
            <summary>История текстовых правок ({data.history_total})</summary>
            {!data.history_total && (
              <p className="muted">Новых ручных правок пока нет</p>
            )}
            {data.history.map((c) => (
              <div className="obsidian-row" key={c.id}>
                <p>
                  Чат {c.chat_id} · запись №{c.fact_id} · {date(c.created_at)} ·{" "}
                  {c.origin === "web"
                    ? "Web UI"
                    : c.origin === "undo"
                      ? "Отмена правки"
                      : "Obsidian"}
                </p>
                <button
                  type="button"
                  aria-expanded={opened === c.id}
                  onClick={() => setOpened(opened === c.id ? "" : c.id)}
                >
                  {opened === c.id ? "Скрыть" : "Посмотреть правку"}
                </button>
                {opened === c.id && (
                  <>
                    <div className="obsidian-compare obsidian-history-compare">
                      {text("До изменения", c.before_text)}
                      {text("После изменения", c.after_text)}
                    </div>
                    <p className="muted">
                      {c.can_undo
                        ? "Отмена создаст новую ручную правку текста. Источники, состояние до правки и согласия не восстанавливаются."
                        : "Запись изменилась или недоступна. Отмена недоступна."}
                    </p>
                    <button
                      type="button"
                      disabled={busy || !c.can_undo}
                      onClick={() =>
                        act(
                          `obsidian/history/${c.id}/undo`,
                          { operation_id: crypto.randomUUID() },
                          "Текст восстановлен новой правкой",
                        )
                      }
                    >
                      Отменить изменение текста
                    </button>
                  </>
                )}
              </div>
            ))}
            {pages(data.history_total, historyPage, setHistoryPage)}
          </details>
        </>
      )}
    </section>
  );
}
