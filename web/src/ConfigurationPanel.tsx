import { useEffect, useState } from "react";
import { api, ApiError } from "./api";

type Release = {
  version: number;
  hash: string;
  created_at: string;
  origin: string;
};
type Draft = {
  id: string;
  base_version: number;
  hash: string;
  origin: string;
  conflict: boolean;
  created_at: string;
};
type Node = { path: string; before: string; current: string; proposed: string };
type Comparison = Draft & {
  current_version: number;
  current_hash: string;
  state: string;
  nodes: Node[];
};
type Bridge = {
  configured: boolean;
  path: string | null;
  error: string | null;
  scanning: boolean;
  last_scan: string | null;
  current: Release;
  drafts: Draft[];
  history: Release[];
  total: number;
  page: number;
};
const date = (s: string) => new Date(s).toLocaleString("ru-RU");
const origin = (s: string) =>
  ({
    bootstrap: "Начальная конфигурация",
    obsidian: "Obsidian",
    web: "Web UI",
    rollback: "Откат",
  })[s] ?? s;

export function ConfigurationPanel({
  csrf,
  dirty,
  currentVersion,
  onUnauthorized,
  onChanged,
}: {
  csrf: string | null;
  dirty: boolean;
  currentVersion: number;
  onUnauthorized: () => void;
  onChanged: () => void;
}) {
  const [data, setData] = useState<Bridge | null>(null);
  const [detail, setDetail] = useState<Comparison | null>(null);
  const [selected, setSelected] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  function fail(e: unknown) {
    if (e instanceof ApiError && e.status === 401) onUnauthorized();
    setError(
      e instanceof Error
        ? e.message
        : "Не удалось подтвердить результат. Обнови состояние выпуска",
    );
    setConfirmed(false);
  }
  useEffect(() => {
    if (busy) return;
    let active = true;
    api<Bridge>(`configuration?page=${page}`)
      .then((v) => {
        if (active) setData(v);
      })
      .catch((e) => {
        if (active) fail(e);
      });
    return () => {
      active = false;
    };
  }, [page, revision, detail, busy, currentVersion]);
  useEffect(() => {
    if (detail || busy || !data?.scanning) return;
    const timer = window.setTimeout(() => setRevision((v) => v + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [detail, busy, revision, data?.scanning]);
  useEffect(() => {
    setConfirmed(false);
  }, [currentVersion, dirty]);
  const stale =
    !!detail &&
    detail.current_version <
      Math.max(currentVersion, data?.current.version ?? 0);
  async function open(id: string) {
    setBusy(true);
    setError("");
    setConfirmed(false);
    try {
      const value = await api<Comparison>(`configuration/drafts/${id}`);
      setDetail(value);
      setSelected(value.nodes[0]?.path ?? "");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function scan() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api("configuration/scan", "POST", {}, csrf);
      setMessage("Проверка завершена. Активация не выполнялась");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
      setRevision((v) => v + 1);
    }
  }
  async function decide(action: "apply" | "reject") {
    if (!detail) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api<{ version: number }>(
        `configuration/drafts/${detail.id}`,
        "POST",
        { action, expected_version: detail.current_version, hash: detail.hash },
        csrf,
      );
      setMessage(
        action === "apply"
          ? `Выпуск №${result.version} активирован`
          : "Черновик отклонён. Файлы обновятся при следующей синхронизации",
      );
      setDetail(null);
      setConfirmed(false);
      if (action === "apply") onChanged();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
      setRevision((v) => v + 1);
    }
  }
  async function rollback(version: number) {
    if (!data) return;
    setBusy(true);
    setError("");
    setConfirmed(false);
    try {
      const result = await api<{ id: string }>(
        "configuration/rollback",
        "POST",
        { version, expected_version: data.current.version },
        csrf,
      );
      const value = await api<Comparison>(`configuration/drafts/${result.id}`);
      setDetail(value);
      setSelected(value.nodes[0]?.path ?? "");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  const node = detail?.nodes.find((n) => n.path === selected);
  return (
    <section className="card obsidian-panel configuration-panel">
      <div className="obsidian-heading">
        <div>
          <h2>Конфигурация · Obsidian</h2>
          <p className="muted">Характер, поведение и инструкции по маршрутам</p>
        </div>
        <button
          type="button"
          disabled={busy || !!detail || !data?.configured || data.scanning}
          onClick={() => void scan()}
        >
          Проверить правки из Obsidian
        </button>
      </div>
      <p>
        Правки из Obsidian становятся рабочими только после проверки и активации
        выпуска здесь
      </p>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {message && (
        <p className="notice success" role="status">
          {message}
        </p>
      )}
      {!data && !error && <p role="status">Загружаем конфигурацию…</p>}
      {data && (
        <>
          <p>
            <strong>Активный выпуск №{data.current.version}</strong> ·{" "}
            {date(data.current.created_at)}
          </p>
          <details>
            <summary>Технические сведения</summary>
            <p className="obsidian-path">SHA-256: {data.current.hash}</p>
          </details>
          {data.configured ? (
            <p>
              Папка: <code className="obsidian-path">{data.path}</code>
            </p>
          ) : (
            <p className="muted">
              Папка Obsidian для конфигурации не подключена на стороне Зари.
            </p>
          )}
          {data.last_scan && (
            <p className="muted">
              {data.scanning
                ? "Проверяем правки…"
                : `Последняя проверка: ${date(data.last_scan)}`}
            </p>
          )}
          {data.error && (
            <p className="notice error" role="alert">
              {data.error}
            </p>
          )}
          {dirty && (
            <p className="notice">
              Есть несохранённые настройки в форме. Сохрани или отмени их перед
              активацией выпуска
            </p>
          )}
          <details className="obsidian-section" open={!!detail}>
            <summary>Черновики конфигурации ({data.drafts.length})</summary>
            {!data.drafts.length && !detail && (
              <p className="muted">
                Новых правок конфигурации нет. Заря использует выпуск №
                {data.current.version}
              </p>
            )}
            {data.drafts.map((d) => (
              <div className="obsidian-row" key={d.id}>
                <span>
                  {origin(d.origin)} · {date(d.created_at)} · основа №
                  {d.base_version} ·{" "}
                  {d.conflict ? "Конфликт версий" : "Проверка пройдена"}
                </span>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void open(d.id)}
                >
                  Просмотреть выпуск
                </button>
              </div>
            ))}
            {detail && (
              <div className="configuration-comparison">
                <p>
                  Основа №{detail.base_version} · сейчас №
                  {detail.current_version} · изменено узлов:{" "}
                  {detail.nodes.length}
                </p>
                <label>
                  Узел конфигурации
                  <select
                    value={selected}
                    onChange={(e) => setSelected(e.target.value)}
                  >
                    {detail.nodes.map((n) => (
                      <option value={n.path} key={n.path}>
                        {n.path.replace(/\.md$/, "")}
                      </option>
                    ))}
                  </select>
                </label>
                {node ? (
                  <div className="obsidian-compare">
                    {[
                      [
                        `В основе черновика · №${detail.base_version}`,
                        node.before,
                      ],
                      [
                        `Сейчас в Заре · №${detail.current_version}`,
                        node.current,
                      ],
                      ["Предложено", node.proposed],
                    ].map(([label, text]) => (
                      <div key={label}>
                        <p className="muted">{label}</p>
                        <div className="obsidian-text configuration-text">
                          {text}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p>Нет изменений</p>
                )}
                {detail.conflict || stale ? (
                  <p className="notice error">
                    Конфликт версий. После создания черновика конфигурация
                    изменилась. Обнови сравнение; автоматического объединения
                    нет. Сохрани нужный черновик отдельно, отклони его и обнови
                    синхронизацию
                  </p>
                ) : (
                  <p className="muted">
                    Проверка структуры пройдена · hash{" "}
                    {detail.hash.slice(0, 12)}. При активации версия и файлы
                    будут проверены повторно
                  </p>
                )}
                {detail.origin === "rollback" && (
                  <p>
                    Откат создаст новый выпуск с выбранной конфигурацией.
                    Отменённые задания и удалённые данные не восстанавливаются
                  </p>
                )}
                <p className="muted">
                  Будут применены все изменения выпуска. Новые операции
                  используют новую конфигурацию, начатые сохраняют свой снимок.
                  Режим инициативы отдельных групп и права доступа здесь не
                  меняются
                </p>
                <label className="configuration-confirm">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    disabled={busy || detail.conflict || stale || dirty}
                    onChange={(e) => setConfirmed(e.target.checked)}
                  />
                  Я просмотрел изменения этого выпуска
                </label>
                <div className="action-bar">
                  <button
                    className="primary"
                    type="button"
                    disabled={
                      busy ||
                      dirty ||
                      detail.conflict ||
                      stale ||
                      !confirmed ||
                      detail.state !== "pending"
                    }
                    onClick={() => void decide("apply")}
                  >
                    {detail.origin === "rollback"
                      ? "Активировать откат"
                      : "Активировать выпуск"}
                  </button>
                  <button
                    type="button"
                    disabled={
                      busy || (detail.origin === "obsidian" && !data.configured)
                    }
                    onClick={() => void decide("reject")}
                  >
                    Отклонить черновик
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setDetail(null);
                      setConfirmed(false);
                    }}
                  >
                    Закрыть сравнение
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void open(detail.id)}
                  >
                    Обновить сравнение
                  </button>
                </div>
              </div>
            )}
          </details>
          <details className="obsidian-section">
            <summary>История выпусков ({data.total})</summary>
            {data.history.map((r) => (
              <div className="obsidian-row" key={r.version}>
                <span>
                  №{r.version} · {date(r.created_at)} · {origin(r.origin)}
                  {r.version === data.current.version ? " · Текущий" : ""}
                </span>
                {r.version !== data.current.version && (
                  <button
                    type="button"
                    disabled={busy || dirty}
                    onClick={() => void rollback(r.version)}
                  >
                    Подготовить откат к выпуску №{r.version}
                  </button>
                )}
              </div>
            ))}
            <div className="pagination">
              <button
                type="button"
                disabled={busy || !!detail || page === 0}
                onClick={() => setPage((v) => v - 1)}
              >
                Назад
              </button>
              <span>
                Страница {page + 1} из {Math.max(1, Math.ceil(data.total / 10))}
              </span>
              <button
                type="button"
                disabled={busy || !!detail || (page + 1) * 10 >= data.total}
                onClick={() => setPage((v) => v + 1)}
              >
                Далее
              </button>
            </div>
          </details>
        </>
      )}
    </section>
  );
}
