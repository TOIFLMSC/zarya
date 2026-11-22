import { useEffect, useRef, useState } from "react";
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
type PromptPackage = {
  version: number;
  fragments: Record<string, string>;
  hash?: string;
};
type PromptEditor = PromptPackage & {
  id?: string;
  saved: Record<string, string>;
};
type Bridge = {
  configured: boolean;
  path: string | null;
  error: string | null;
  scanning: boolean;
  last_scan: string | null;
  current: Release;
  drafts: Draft[];
  draft_total: number;
  prompt_draft_id: string | null;
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
    prompt_editor: "Редактор промптов",
    rollback: "Откат",
  })[s] ?? s;

const promptGroups: Record<string, string> = {
  dialogue: "Диалог",
  links: "Ссылки",
  behavior: "Поведение",
  memory: "Память",
  research: "Интернет",
  media: "Медиа",
};
const promptNames: Record<string, string> = {
  "dialogue.identity_prefix": "Вступление",
  "dialogue.identity": "Роль Зари",
  "dialogue.photo_available": "Фото доступно",
  "dialogue.photo_unavailable": "Фото недоступно",
  "dialogue.context": "Контекст разговора",
  "dialogue.style_general": "Общий стиль",
  "dialogue.length_brief": "Короткий ответ",
  "dialogue.length_analysis": "Аналитический ответ",
  "dialogue.length_detailed": "Подробный ответ",
  "dialogue.tone_label": "Подпись тона",
  "dialogue.persona_label": "Подпись характера",
  "dialogue.style_clean": "Нейтральное оформление",
  "dialogue.style_relaxed": "Неформальный ответ",
  "dialogue.style_rushed": "Быстрый ответ",
  "dialogue.owner": "Общение с владельцем",
  "dialogue.other": "Общение с другими",
  "dialogue.voice": "Сила авторского голоса",
  "dialogue.discretion": "Инициатива и уместность",
  "dialogue.avatar": "Контекст аватара",
  "links.requested": "Когда попросили ссылки",
  "links.not_requested": "Когда не попросили ссылки",
  "behavior.plan": "План реакции и ответа",
  "memory.extract": "Извлечение воспоминаний",
  "research.analyse": "Оценка источников",
  "media.photo": "Анализ фото",
  "media.video": "Анализ видео",
  "media.avatar": "Анализ аватара",
};

export function ConfigurationPanel({
  csrf,
  dirty,
  currentVersion,
  onUnauthorized,
  onChanged,
  onPromptDirtyChange,
}: {
  csrf: string | null;
  dirty: boolean;
  currentVersion: number;
  onUnauthorized: () => void;
  onChanged: () => void;
  onPromptDirtyChange: (dirty: boolean) => void;
}) {
  const [data, setData] = useState<Bridge | null>(null);
  const [detail, setDetail] = useState<Comparison | null>(null);
  const [selected, setSelected] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(0);
  const [draftPage, setDraftPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [editor, setEditor] = useState<PromptEditor | null>(null);
  const [fragment, setFragment] = useState("");
  const editorSelect = useRef<HTMLSelectElement>(null);
  const reviewSummary = useRef<HTMLParagraphElement>(null);
  const editorOpen = editor !== null;
  const reviewedId = detail?.id;
  const promptKeys = Object.keys(editor?.fragments ?? {});
  const changedPrompts = promptKeys.filter(
    (key) => editor?.fragments[key] !== editor?.saved[key],
  );
  const promptDirty = changedPrompts.length > 0;
  useEffect(() => onPromptDirtyChange(promptDirty), [onPromptDirtyChange, promptDirty]);
  useEffect(() => {
    if (editorOpen) editorSelect.current?.focus();
  }, [editorOpen]);
  useEffect(() => {
    if (reviewedId) reviewSummary.current?.focus();
  }, [reviewedId]);
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
    api<Bridge>(`configuration?page=${page}&draft_page=${draftPage}`)
      .then((v) => {
        if (!active) return;
        setData(v);
        if (draftPage * 10 >= v.draft_total && draftPage > 0) {
          setDraftPage(Math.max(0, Math.ceil(v.draft_total / 10) - 1));
        }
      })
      .catch((e) => {
        if (active) fail(e);
      });
    return () => {
      active = false;
    };
  }, [page, draftPage, revision, detail, busy, currentVersion]);
  useEffect(() => {
    if (detail || busy || !data?.scanning) return;
    const timer = window.setTimeout(() => setRevision((v) => v + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [detail, busy, revision, data?.scanning]);
  useEffect(() => {
    setConfirmed(false);
  }, [currentVersion, dirty]);
  const activeVersion = Math.max(currentVersion, data?.current.version ?? 0);
  const stale = !!detail && detail.current_version < activeVersion;
  const editorStale = !!editor && editor.version < activeVersion;
  const draftChanged =
    !!detail && data?.drafts.some((d) => d.id === detail.id && d.hash !== detail.hash);
  function readComparison(value: Comparison) {
    setDetail(value);
    setEditor(null);
    setSelected(value.nodes[0]?.path ?? "");
  }
  async function open(id: string) {
    setBusy(true);
    setError("");
    setConfirmed(false);
    try {
      const value = await api<Comparison>(`configuration/drafts/${id}`);
      readComparison(value);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function openPrompt(id?: string) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const query = id ? `?draft_id=${encodeURIComponent(id)}` : "";
      const value = await api<PromptPackage>(`configuration/prompts${query}`);
      setEditor({ ...value, id, saved: value.fragments });
      setFragment((previous) =>
        value.fragments[previous] !== undefined ? previous : "dialogue.identity",
      );
      setDetail(null);
      setConfirmed(false);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function savePrompt() {
    if (!editor || editorStale || !promptDirty || busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const saved = await api<{ id: string; hash: string }>(
        "configuration/prompts",
        "POST",
        {
          expected_version: editor.version,
          fragments: editor.fragments,
          draft_id: editor.id,
          expected_hash: editor.hash,
        },
        csrf,
      );
      setEditor({ ...editor, ...saved, saved: editor.fragments });
      setMessage("Черновик сохранён. Активный выпуск остался прежним");
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
          : "Черновик отклонён",
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
      readComparison(value);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  const node = detail?.nodes.find((n) => n.path === selected);
  const groups = [...new Set([...Object.keys(promptGroups), ...promptKeys.map(
    (key) => key.split(".")[0],
  )])];
  return (
    <section className="card obsidian-panel configuration-panel">
      <div className="obsidian-heading">
        <div>
          <h2>Промпты Зари</h2>
          <p className="muted">Инструкции для диалога, памяти и других функций</p>
        </div>
        <button
          type="button"
          className="primary"
          disabled={busy || !data || !!editor || !!detail}
          onClick={() => void openPrompt(data?.prompt_draft_id ?? undefined)}
        >
          {data?.prompt_draft_id ? "Продолжить черновик" : "Редактировать промпты"}
        </button>
      </div>
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
          {data.configured && (
            <details className="obsidian-section">
              <summary>Файловый мост Obsidian</summary>
              <p>Папка <code className="obsidian-path">{data.path}</code></p>
              {data.last_scan && (
                <p className="muted">
                  {data.scanning
                    ? "Проверяем правки…"
                    : `Последняя проверка: ${date(data.last_scan)}`}
                </p>
              )}
              <button
                type="button"
                disabled={busy || !!detail || !!editor || data.scanning}
                onClick={() => void scan()}
              >
                Проверить правки из Obsidian
              </button>
            </details>
          )}
          {data.error && <p className="notice error" role="alert">{data.error}</p>}
          {editor && (
            <div className="configuration-comparison prompt-editor">
              <div className="obsidian-heading">
                <h3>{editor.id ? "Черновик промптов" : "Редактирование промптов"}</h3>
                <span className="muted">Основа №{editor.version}</span>
              </div>
              <label>
                <span id="prompt-fragment-label">Фрагмент</span>
                <select
                  ref={editorSelect}
                  aria-labelledby="prompt-fragment-label"
                  value={fragment}
                  disabled={busy}
                  onChange={(e) => setFragment(e.target.value)}
                >
                  {groups.map((group) => {
                    const keys = promptKeys.filter((key) => key.split(".")[0] === group);
                    return keys.length > 0 && (
                      <optgroup key={group} label={promptGroups[group] ?? group}>
                        {keys.map((key) => (
                          <option key={key} value={key}>
                            {promptNames[key] ?? key}{changedPrompts.includes(key) ? " · изменён" : ""}
                          </option>
                        ))}
                      </optgroup>
                    );
                  })}
                </select>
              </label>
              <label>
                <span id="prompt-text-label">Текст промпта</span>
                <code className="prompt-key" id="prompt-fragment-key">{fragment}</code>
                <textarea
                  aria-labelledby="prompt-text-label"
                  aria-describedby="prompt-fragment-key"
                  rows={12}
                  value={editor.fragments[fragment] ?? ""}
                  spellCheck={false}
                  readOnly={editorStale || busy}
                  onChange={(e) => setEditor({
                    ...editor,
                    fragments: { ...editor.fragments, [fragment]: e.target.value },
                  })}
                />
              </label>
              {promptDirty && (
                <p className="muted" role="status">
                  Несохранённых фрагментов: {changedPrompts.length}
                </p>
              )}
              {editorStale && (
                <p className="notice" role="status">
                  Активный выпуск изменился. Текст здесь можно скопировать в новый черновик.
                </p>
              )}
              <div className="action-bar">
                <button
                  type="button"
                  className="primary"
                  disabled={busy || editorStale || !promptDirty}
                  onClick={() => void savePrompt()}
                >
                  Сохранить черновик
                </button>
                {editor.id && (
                  <button
                    type="button"
                    disabled={busy || promptDirty}
                    onClick={() => { if (editor.id) void open(editor.id); }}
                  >
                    Сравнить изменения
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    if (!promptDirty || window.confirm("Закрыть редактор без сохранения?")) {
                      setEditor(null);
                      setError("");
                    }
                  }}
                >
                  Закрыть редактор
                </button>
              </div>
            </div>
          )}
          {dirty && detail && (
            <p className="notice">
              Есть несохранённые настройки в форме. Сохрани или отмени их перед
              активацией выпуска
            </p>
          )}
          <details className="obsidian-section" open={!!detail}>
            <summary>Черновики конфигурации ({data.draft_total})</summary>
            {!data.draft_total && !detail && (
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
                  {d.conflict ? "Основа устарела" : "Готов к сравнению"}
                </span>
                <button
                  type="button"
                  disabled={busy || !!editor}
                  onClick={() => void open(d.id)}
                >
                  Сравнить изменения
                </button>
              </div>
            ))}
            {data.draft_total > 10 && (
              <div className="pagination" aria-label="Страницы черновиков">
                <button
                  type="button"
                  disabled={busy || !!detail || !!editor || draftPage === 0}
                  onClick={() => setDraftPage((v) => v - 1)}
                >
                  Предыдущие черновики
                </button>
                <span>Страница {draftPage + 1} из {Math.ceil(data.draft_total / 10)}</span>
                <button
                  type="button"
                  disabled={busy || !!detail || !!editor || (draftPage + 1) * 10 >= data.draft_total}
                  onClick={() => setDraftPage((v) => v + 1)}
                >
                  Следующие черновики
                </button>
              </div>
            )}
            {detail && (
              <div className="configuration-comparison">
                <p tabIndex={-1} ref={reviewSummary}>
                  Основа №{detail.base_version} · активный выпуск №
                  {detail.current_version} · изменений: {detail.nodes.length}
                </p>
                <label>
                  <span id="configuration-node-label">Изменённый фрагмент</span>
                  <select
                    aria-labelledby="configuration-node-label"
                    value={selected}
                    onChange={(e) => setSelected(e.target.value)}
                  >
                    {detail.nodes.map((n) => (
                      <option value={n.path} key={n.path}>
                        {promptNames[n.path.replace(/^Инструкции\//, "").replace(/\.md$/, "")] ??
                          n.path.replace(/\.md$/, "")}
                      </option>
                    ))}
                  </select>
                </label>
                {node ? (
                  <div className={`obsidian-compare${detail.conflict ? "" : " obsidian-history-compare"}`}>
                    {[
                      ...(detail.conflict
                        ? [[
                        `В основе черновика · №${detail.base_version}`,
                        node.before,
                        ]]
                        : []),
                      [
                        `Активный выпуск №${detail.current_version}`,
                        node.current,
                      ],
                      ["Черновик", node.proposed],
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
                {(detail.conflict || stale || draftChanged) && (
                  <p className="notice error">
                    {draftChanged
                      ? "Черновик изменился. Обнови сравнение перед активацией."
                      : "Активный выпуск изменился. Черновик доступен для просмотра. Нужные правки можно перенести в новый черновик."}
                  </p>
                )}
                {detail.origin === "rollback" && (
                  <p>
                    Откат создаст новый выпуск с промптами и настройками выбранной версии.
                    Отменённые задания и удалённые данные не восстанавливаются
                  </p>
                )}
                <p className="muted">
                  Новые операции используют активированный выпуск, начатые
                  продолжаются с прежней конфигурацией.
                </p>
                <label className="configuration-confirm">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    disabled={busy || detail.conflict || stale || draftChanged || dirty}
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
                      draftChanged ||
                      (detail.origin === "obsidian" && !data.configured) ||
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
                  {detail.origin === "prompt_editor" && detail.state === "pending" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void openPrompt(detail.id)}
                    >
                      {detail.conflict || stale ? "Открыть текст черновика" : "Вернуться к редактированию"}
                    </button>
                  )}
                  <button
                    className="quiet"
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
                    className="quiet"
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
                    disabled={busy || dirty || !!editor}
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
