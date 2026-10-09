import { StrictMode, useEffect, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import {
  api,
  ApiError,
  type Session,
  type Settings,
  type Snapshot,
  type Status,
} from "./api";
import "./style.css";
import { TelegramPanel } from "./TelegramPanel";
import { DialoguePanel } from "./DialoguePanel";
import { MemoryPanel } from "./MemoryPanel";
import { ResearchPanel } from "./ResearchPanel";
import { MediaPanel } from "./MediaPanel";
import { BehaviorPanel } from "./BehaviorPanel";
import { PilotPanel } from "./PilotPanel";
import { SettingsSection, revealInvalidField } from "./SettingsSection";

function Mark() {
  return (
    <svg className="mark" viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <circle cx="20" cy="20" r="16" stroke="currentColor" strokeWidth="1" />
      <path
        d="M20 3L23.5 16.5L37 20L23.5 23.5L20 37L16.5 23.5L3 20L16.5 16.5Z"
        fill="currentColor"
      />
    </svg>
  );
}
const portraitUrl = new URL("./assets/zarya-avatar-v1.png", import.meta.url)
  .href;
function Portrait({ className = "" }: { className?: string }) {
  return (
    <img
      className={`portrait ${className}`}
      src={portraitUrl}
      alt=""
      width={256}
      height={256}
    />
  );
}
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "Нет связи с сервером";

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [connectionError, setConnectionError] = useState("");
  async function loadSession() {
    setConnectionError("");
    try {
      setSession(await api<Session>("session"));
    } catch (error) {
      setConnectionError(errorText(error));
    }
  }
  useEffect(() => {
    void loadSession();
  }, []);
  if (connectionError)
    return (
      <div className="login-wrap">
        <div className="login-card">
          <Mark />
          <h1>Нет связи с Зарёй</h1>
          <p>{connectionError}</p>
          <button onClick={() => void loadSession()}>Повторить</button>
        </div>
      </div>
    );
  if (!session)
    return <div className="loading">Подключаемся к локальной панели…</div>;
  if (!session.authenticated)
    return <Login session={session} onLogin={setSession} />;
  return <Panel session={session} onExit={() => void loadSession()} />;
}

function Login({
  session,
  onLogin,
}: {
  session: Session;
  onLogin: (session: Session) => void;
}) {
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onLogin(
        await api<Session>(
          session.setup_required ? "setup" : "login",
          "POST",
          session.setup_required
            ? { password, token: token.trim() }
            : { password },
        ),
      );
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-wrap">
      <section className="login-card">
        <div className="brand">
          <Portrait className="portrait-small" />
          <span>
            Заря<small>ТВОЯ СОБЕСЕДНИЦА</small>
          </span>
        </div>
        <p className="eyebrow">ЛОКАЛЬНЫЙ ЦЕНТР УПРАВЛЕНИЯ</p>
        <h1>
          {session.setup_required ? "Давай познакомимся." : "С возвращением."}
        </h1>
        <p className="muted">
          {session.setup_required
            ? "Создай доступ к панели. Настройки и будущая память будут храниться на этом компьютере."
            : "Войди, чтобы настроить Зарю и проверить состояние приложения."}
        </p>
        <form onSubmit={submit}>
          {session.setup_required && (
            <label>
              Одноразовый ключ
              <input
                value={token}
                onChange={(e) => setToken(e.target.value)}
                type="password"
                required
                autoComplete="off"
              />
              <small>
                Открой файл <code>data/bootstrap-token.txt</code> в папке
                проекта и вставь ключ. После настройки файл удалится.
              </small>
            </label>
          )}
          <label>
            {session.setup_required ? "Новый пароль администратора" : "Пароль"}
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={12}
              maxLength={128}
              required
              autoComplete={
                session.setup_required ? "new-password" : "current-password"
              }
            />
            {session.setup_required && (
              <small>
                От 12 символов. Это пароль панели, отдельно от Telegram.
              </small>
            )}
          </label>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          <button className="primary full" disabled={busy}>
            {busy
              ? "Подождите…"
              : session.setup_required
                ? "Создать доступ →"
                : "Войти →"}
          </button>
        </form>
        <p className="login-note">
          <span className="dot" /> Только на этом компьютере
        </p>
      </section>
      <span className="login-edition">ЗАРЯ / ПЕРВАЯ ГЛАВА</span>
    </div>
  );
}

function Panel({ session, onExit }: { session: Session; onExit: () => void }) {
  const [page, rawSetPage] = useState<
    | "overview"
    | "settings"
    | "telegram"
    | "dialogues"
    | "memory"
    | "research"
    | "media"
    | "behavior"
    | "pilot"
  >("overview");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const pageHeading = useRef<HTMLHeadingElement>(null);
  const pageNames = {
    overview: "Обзор",
    settings: "Настройки",
    telegram: "Telegram и доступ",
    dialogues: "Диалоги",
    memory: "Память",
    research: "Интернет",
    media: "Медиа",
    behavior: "Поведение",
    pilot: "Пилот",
  };
  const [memoryDirty, setMemoryDirty] = useState(false);
  function setPage(value: typeof page) {
    if (value !== page) {
      if (memoryDirty && !window.confirm("Отменить несохранённые изменения?"))
        return;
      setMemoryDirty(false);
      rawSetPage(value);
    }
    if (mobileMenuOpen) {
      setMobileMenuOpen(false);
      requestAnimationFrame(() => pageHeading.current?.focus());
    }
  }
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [saveMessage, setSaveMessage] = useState("");
  const [saveError, setSaveError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  function handleError(error: unknown) {
    if (error instanceof ApiError && error.status === 401) onExit();
    setError(errorText(error));
  }
  async function load() {
    setError("");
    setSaveError("");
    setSaveMessage("");
    try {
      const [saved, state] = await Promise.all([
        api<Snapshot>("settings"),
        api<Status>("status"),
      ]);
      setSnapshot(saved);
      setDraft(saved.settings);
      setStatus(state);
    } catch (error) {
      handleError(error);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  const dirty = Boolean(
    draft &&
    snapshot &&
    JSON.stringify(draft) !== JSON.stringify(snapshot.settings),
  );
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty || memoryDirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, memoryDirty]);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !snapshot || !draft) return;
    if (revealInvalidField(event.currentTarget)) return;
    setBusy(true);
    setError("");
    setSaveError("");
    setSaveMessage("");
    try {
      const saved = await api<Snapshot>(
        "settings",
        "PUT",
        { expected_version: snapshot.version, settings: draft },
        session.csrf,
      );
      setSnapshot(saved);
      setDraft(saved.settings);
      setSaveMessage("Настройки сохранены. Они останутся после перезапуска.");
      try {
        setStatus(await api<Status>("status"));
      } catch (statusError) {
        if (statusError instanceof ApiError && statusError.status === 401)
          onExit();
        setSaveMessage(
          "Настройки сохранены; состояние подключения не обновилось.",
        );
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExit();
      setSaveError(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  async function logout() {
    if (
      (dirty || memoryDirty) &&
      !window.confirm("Выйти без сохранения изменений?")
    )
      return;
    try {
      await api("logout", "POST", {}, session.csrf);
      onExit();
    } catch (error) {
      handleError(error);
    }
  }
  function change<K extends keyof Settings>(key: K, value: Settings[K]) {
    if (draft) setDraft({ ...draft, [key]: value });
    setSaveMessage("");
  }
  function sectionChanged(...keys: (keyof Settings)[]) {
    return Boolean(
      draft &&
      snapshot &&
      keys.some((key) => draft[key] !== snapshot.settings[key]),
    );
  }
  const modelName = (model: string) =>
    model === "gpt-6-luna" ? "GPT-6 Luna" : "GPT-6.1 Sol";
  const effortName = (effort: string) =>
    ({ low: "низкая", medium: "средняя", high: "высокая" })[effort] ?? effort;
  return (
    <div className="shell">
      <aside
        className="sidebar"
        onKeyDown={(event) => {
          if (event.key === "Escape" && mobileMenuOpen) {
            event.preventDefault();
            setMobileMenuOpen(false);
            menuButton.current?.focus();
          }
        }}
      >
        <div className="sidebar-heading">
          <div className="brand">
            <Mark />
            <span>
              Заря<small>ЦЕНТР УПРАВЛЕНИЯ</small>
            </span>
          </div>
          <button
            type="button"
            className="mobile-nav-toggle"
            ref={menuButton}
            aria-label={`Разделы. Текущий раздел: ${pageNames[page]}`}
            aria-expanded={mobileMenuOpen}
            aria-controls="main-navigation"
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
          >
            {pageNames[page]}{" "}
            <span aria-hidden="true">{mobileMenuOpen ? "−" : "+"}</span>
            {dirty && <span className="mobile-dirty">Есть изменения</span>}
          </button>
        </div>
        <div className="workspace">
          <span className="dot" /> Локальное пространство
        </div>
        <nav
          id="main-navigation"
          className={mobileMenuOpen ? "is-open" : ""}
          aria-label="Основная навигация"
        >
          <button
            className={page === "overview" ? "active" : ""}
            aria-current={page === "overview" ? "page" : undefined}
            onClick={() => setPage("overview")}
          >
            <span aria-hidden="true">◫</span>Обзор
          </button>
          <button
            className={page === "settings" ? "active" : ""}
            aria-current={page === "settings" ? "page" : undefined}
            onClick={() => setPage("settings")}
          >
            <span aria-hidden="true">☷</span>Настройки
            {dirty && (
              <i
                className="unsaved"
                role="img"
                aria-label="Есть несохранённые изменения"
              />
            )}
          </button>
          <button
            className={page === "telegram" ? "active" : ""}
            aria-current={page === "telegram" ? "page" : undefined}
            onClick={() => setPage("telegram")}
          >
            <span aria-hidden="true">↗</span>Telegram и доступ
          </button>
          <button
            className={page === "dialogues" ? "active" : ""}
            aria-current={page === "dialogues" ? "page" : undefined}
            onClick={() => setPage("dialogues")}
          >
            <span aria-hidden="true">◌</span>Диалоги
          </button>
          <button
            className={page === "memory" ? "active" : ""}
            aria-current={page === "memory" ? "page" : undefined}
            onClick={() => setPage("memory")}
          >
            <span aria-hidden="true">◇</span>Память
          </button>
          <button
            className={page === "research" ? "active" : ""}
            aria-current={page === "research" ? "page" : undefined}
            onClick={() => setPage("research")}
          >
            <span aria-hidden="true">↗</span>Интернет
          </button>
          <button
            className={page === "media" ? "active" : ""}
            aria-current={page === "media" ? "page" : undefined}
            onClick={() => setPage("media")}
          >
            <span aria-hidden="true">▷</span>Медиа
          </button>
          <button
            className={page === "behavior" ? "active" : ""}
            aria-current={page === "behavior" ? "page" : undefined}
            onClick={() => setPage("behavior")}
          >
            <span aria-hidden="true">♡</span>Поведение
          </button>
          <button
            className={page === "pilot" ? "active" : ""}
            aria-current={page === "pilot" ? "page" : undefined}
            onClick={() => setPage("pilot")}
          >
            <span aria-hidden="true">◉</span>Пилот
          </button>
        </nav>
        <div className="sidebar-bottom">
          <div className="companion">
            <Portrait className="portrait-small" />
            <div>
              Заря<small>Всё начинается с разговора.</small>
            </div>
          </div>
          <span>ЛОКАЛЬНАЯ ВЕРСИЯ · {status?.version ?? "0.1.0"}</span>
        </div>
      </aside>
      <main>
        <header>
          <div className="breadcrumb">
            Пространство /{" "}
            <strong>
              {page === "overview"
                ? "Обзор"
                : page === "telegram"
                  ? "Telegram и доступ"
                  : page === "dialogues"
                    ? "Диалоги"
                    : page === "memory"
                      ? "Память"
                      : page === "research"
                        ? "Интернет"
                        : page === "media"
                          ? "Медиа"
                          : page === "behavior"
                            ? "Поведение"
                            : page === "pilot"
                              ? "Пилот"
                              : "Настройки"}
            </strong>
          </div>
          <button className="quiet" onClick={() => void logout()}>
            Выйти ↗
          </button>
        </header>
        <div className="content">
          <div className="page-heading">
            <div>
              <p className="eyebrow">ЭТАП 09 / ЛОКАЛЬНЫЙ ПИЛОТ</p>
              <h1 ref={pageHeading} tabIndex={-1}>
                {page === "overview"
                  ? "Заря начинается здесь."
                  : page === "telegram"
                    ? "Telegram и доступ."
                    : page === "dialogues"
                      ? "Диалоги."
                      : page === "memory"
                        ? "Память."
                        : page === "research"
                          ? "Интернет и проверка."
                          : page === "media"
                            ? "Медиа."
                            : page === "behavior"
                              ? "Поведение."
                              : page === "pilot"
                                ? "Локальный пилот."
                                : "Настройки Зари."}
              </h1>
              <p className="muted">
                {page === "overview"
                  ? "Состояние приложения и первые настройки твоей собеседницы."
                  : page === "telegram"
                    ? "Подключение бота и разрешения для групп и личных разговоров."
                    : page === "dialogues"
                      ? "Сообщения, ответы и доставка."
                      : page === "memory"
                        ? "Сведения о собеседниках, источники и сводки обсуждений."
                        : page === "research"
                          ? "Материалы, источники и результаты проверки утверждений."
                          : page === "media"
                            ? "Фото, альбомы, речь и видео: исходники и результаты разбора."
                            : page === "behavior"
                              ? "Настроение разговоров, выразительность и самостоятельное участие."
                              : page === "pilot"
                                ? "Расходы, задержки и состояние локального запуска."
                                : "Характер, модели и обработка сообщений. Изменения применяются после сохранения."}
              </p>
            </div>
            <span className="badge">
              <span className="dot" /> Локальный режим
            </span>
          </div>
          {error && (
            <div className="notice error" role="alert">
              {error}{" "}
              <button
                className="text-button"
                onClick={() => {
                  if (
                    !dirty ||
                    window.confirm(
                      "Загрузить сохранённые настройки и отменить свои изменения?",
                    )
                  )
                    void load();
                }}
              >
                Загрузить заново
              </button>
            </div>
          )}
          {page === "telegram" ? (
            <TelegramPanel csrf={session.csrf} onUnauthorized={onExit} />
          ) : page === "dialogues" ? (
            <DialoguePanel csrf={session.csrf} onUnauthorized={onExit} />
          ) : page === "memory" ? (
            <MemoryPanel
              csrf={session.csrf}
              onUnauthorized={onExit}
              onDirty={setMemoryDirty}
            />
          ) : page === "research" ? (
            <ResearchPanel onUnauthorized={onExit} />
          ) : page === "media" ? (
            <MediaPanel onUnauthorized={onExit} />
          ) : page === "behavior" ? (
            <BehaviorPanel
              csrf={session.csrf}
              onUnauthorized={onExit}
              onDirtyChange={setMemoryDirty}
            />
          ) : page === "pilot" ? (
            <PilotPanel
              csrf={session.csrf}
              onUnauthorized={onExit}
              onDirtyChange={setMemoryDirty}
            />
          ) : !status || !draft || !snapshot ? (
            <p>Загружаем настройки…</p>
          ) : page === "overview" ? (
            <>
              <section className="hero">
                <div>
                  <span className="eyebrow">ПЕРВАЯ ГЛАВА</span>
                  <h2>
                    Место, где у Зари
                    <br />
                    появится свой голос.
                  </h2>
                  <p>
                    Настрой характер и разреши разговоры. Историю ответов и
                    использованный контекст можно проверить в журнале.
                  </p>
                  <button
                    className="primary"
                    onClick={() => setPage("settings")}
                  >
                    Настроить Зарю <span>↗</span>
                  </button>
                </div>
                <div className="portrait-panel" aria-hidden="true">
                  <Portrait />
                </div>
              </section>
              <div className="cards">
                <section className="card">
                  <span className="card-label">ПРИЛОЖЕНИЕ</span>
                  <h3>
                    <span className="dot" /> Работает
                  </h3>
                  <p>Python · локальный сервер</p>
                  <small>
                    Время работы: {Math.floor(status.uptime_seconds / 60)} мин.
                  </small>
                </section>
                <section className="card">
                  <span className="card-label">ХРАНИЛИЩЕ</span>
                  <h3>Настройки сохранены</h3>
                  <p>{status.database.engine}</p>
                  <small>
                    Версия настроек {snapshot.version} · схема{" "}
                    {status.database.schema_version}
                  </small>
                </section>
                <section className="card">
                  <span className="card-label">ВНЕШНИЕ ПОДКЛЮЧЕНИЯ</span>
                  <h3>Telegram и доступ</h3>
                  <p>
                    OpenAI:{" "}
                    {status.integrations.openai === "configured"
                      ? "ключ настроен"
                      : "ключ не настроен"}
                    . Подключение Telegram и разрешения — в отдельном разделе.
                  </p>
                  <button onClick={() => setPage("telegram")}>
                    Открыть подключение ↗
                  </button>
                </section>
              </div>
              <section className="next-step">
                <div className="step-number">10</div>
                <div>
                  <p className="eyebrow">ДАЛЬШЕ ПО ПЛАНУ</p>
                  <h3>Перенос на VPS</h3>
                  <p>
                    После локального пилота: постоянный запуск, защищённый
                    доступ и резервные копии.
                  </p>
                </div>
                <span className="pill">Следующий этап</span>
              </section>
              <footer>
                Данные хранятся на этом компьютере.{" "}
                <span>ЗАРЯ · {status.version}</span>
              </footer>
            </>
          ) : (
            <form onSubmit={save} noValidate className="settings-form">
              <fieldset disabled={busy}>
                <div className="save-bar settings-save-bar">
                  <div aria-live="polite" aria-atomic="true">
                    <strong>
                      {busy
                        ? "Сохраняем…"
                        : dirty
                          ? "Есть несохранённые изменения"
                          : `Сохранено · версия ${snapshot.version}`}
                    </strong>
                    <small>
                      {saveError && dirty
                        ? "Сохранение не завершено. Изменения остались в черновике."
                        : dirty
                          ? "Сохраняются изменения во всех разделах."
                          : "Можно раскрыть нужный раздел и изменить параметры."}
                    </small>
                  </div>
                  <button className="primary" disabled={busy || !dirty}>
                    {busy ? "Сохраняем…" : "Сохранить настройки"}
                  </button>
                  {saveError && (
                    <div
                      className="notice error settings-save-feedback"
                      role="alert"
                    >
                      {saveError} Черновик остался в этой форме.{" "}
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => {
                          if (
                            !dirty ||
                            window.confirm(
                              "Загрузить сохранённые настройки и отменить свои изменения?",
                            )
                          )
                            void load();
                        }}
                      >
                        Загрузить сохранённые настройки
                      </button>
                    </div>
                  )}
                  {saveMessage && (
                    <div
                      className="notice success settings-save-feedback"
                      role="status"
                    >
                      {saveMessage}
                    </div>
                  )}
                </div>
                <SettingsSection
                  title="Личность"
                  summary={`${draft.display_name} · ${{ balanced: "сбалансированный", warm: "тёплый", reserved: "сдержанный" }[draft.tone]} тон`}
                  changed={sectionChanged("display_name", "tone", "persona")}
                  initiallyOpen
                >
                  <div>
                    <p>
                      Базовая манера общения. <br />
                      Применяется к новым операциям.
                    </p>
                  </div>
                  <div className="fields">
                    <label>
                      Имя собеседницы
                      <input
                        value={draft.display_name}
                        onChange={(e) => change("display_name", e.target.value)}
                        required
                        maxLength={40}
                      />
                    </label>
                    <label>
                      Тон общения
                      <select
                        value={draft.tone}
                        onChange={(e) =>
                          change("tone", e.target.value as Settings["tone"])
                        }
                      >
                        <option value="balanced">Сбалансированный</option>
                        <option value="warm">Тёплый</option>
                        <option value="reserved">Сдержанный</option>
                      </select>
                    </label>
                    <label>
                      Основа характера
                      <textarea
                        rows={5}
                        value={draft.persona}
                        onChange={(e) => change("persona", e.target.value)}
                        required
                        maxLength={4000}
                      />
                      <small>
                        Любопытство, привычки речи и отношение к собеседникам.
                        До 4000 символов. Сохраняется локально в базе.
                        Полные инструкции задаются в приватном файле промптов;
                        после изменения файла нужен перезапуск.
                      </small>
                    </label>
                  </div>
                </SettingsSection>
                <SettingsSection
                  title="Близкий человек"
                  summary={
                    draft.owner_telegram_id
                      ? `${draft.owner_name || "Владелец"} · ID ${draft.owner_telegram_id}`
                      : "Владелец не указан"
                  }
                  changed={sectionChanged(
                    "owner_telegram_id",
                    "owner_username",
                    "owner_name",
                  )}
                >
                  <div>
                    <p>
                      Кого Заря узнает как владельца. <br />
                      Это не пароль администратора.
                    </p>
                  </div>
                  <div className="fields">
                    <label>
                      Telegram ID
                      <input
                        value={draft.owner_telegram_id}
                        onChange={(e) =>
                          change("owner_telegram_id", e.target.value)
                        }
                        inputMode="numeric"
                        pattern="[1-9][0-9]{0,15}"
                        placeholder="Пока можно оставить пустым"
                      />
                      <small>
                        Постоянный числовой ID для узнавания владельца.
                        Разрешения на общение задаются отдельно.
                      </small>
                    </label>
                    <div className="field-row">
                      <label>
                        Ник в Telegram
                        <input
                          value={draft.owner_username}
                          onChange={(e) =>
                            change("owner_username", e.target.value)
                          }
                          placeholder="@username"
                          maxLength={33}
                          pattern="@?[A-Za-z0-9_]{1,32}"
                        />
                      </label>
                      <label>
                        Как к тебе обращаться
                        <input
                          value={draft.owner_name}
                          onChange={(e) => change("owner_name", e.target.value)}
                          placeholder="Твоё имя"
                          maxLength={80}
                        />
                      </label>
                    </div>
                  </div>
                </SettingsSection>
                <SettingsSection
                  title="Модель и диалог"
                  summary={`${draft.dialogue_enabled ? "Включены" : "Выключены"} · ${modelName(draft.model)} · ${effortName(draft.reasoning)} глубина`}
                  changed={sectionChanged(
                    "dialogue_enabled",
                    "model",
                    "reasoning",
                    "max_output_tokens",
                  )}
                >
                  <div>
                    <p>
                      Применяется к новым операциям. Ключ хранится локально,
                      отдельно от настроек.
                    </p>
                    <small>
                      {status.integrations.openai === "configured"
                        ? "Ключ настроен"
                        : "Ключ не настроен"}
                    </small>
                  </div>
                  <div className="fields">
                    <label>
                      Диалоги
                      <select
                        value={draft.dialogue_enabled ? "on" : "off"}
                        onChange={(e) =>
                          change("dialogue_enabled", e.target.value === "on")
                        }
                      >
                        <option value="on">Включены в разрешённых чатах</option>
                        <option value="off">Выключены</option>
                      </select>
                    </label>
                    <label>
                      Модель
                      <select
                        value={draft.model}
                        onChange={(e) =>
                          change("model", e.target.value as Settings["model"])
                        }
                      >
                        <option value="gpt-6-luna">GPT-6 Luna</option>
                        <option value="gpt-6.1-sol">GPT-6.1 Sol</option>
                      </select>
                    </label>
                    <label>
                      Глубина обработки
                      <select
                        value={draft.reasoning}
                        onChange={(e) =>
                          change(
                            "reasoning",
                            e.target.value as Settings["reasoning"],
                          )
                        }
                      >
                        <option value="low">Низкая · обычный диалог</option>
                        <option value="medium">Средняя</option>
                        <option value="high">Высокая</option>
                      </select>
                    </label>
                    <details className="settings-advanced">
                      <summary>Лимиты обработки</summary>
                      <label>
                        Предел выходных токенов
                        <input
                          type="number"
                          min={1000}
                          max={6000}
                          step={100}
                          required
                          value={draft.max_output_tokens}
                          onChange={(e) =>
                            change("max_output_tokens", Number(e.target.value))
                          }
                        />
                        <small>
                          Включает скрытые reasoning tokens и текст ответа. От
                          1000 до 6000.
                        </small>
                      </label>
                    </details>
                  </div>
                </SettingsSection>
                <SettingsSection
                  title="Фото"
                  summary={`${draft.photo_enabled ? "Фоновый анализ включён" : "Фоновый анализ выключен"} · ${modelName(draft.photo_model)}`}
                  changed={sectionChanged("photo_enabled", "photo_model")}
                >
                  <div>
                    <p>
                      Фото из разрешённых чатов пополняют контекст. Ответ в
                      группе появляется при обращении.
                    </p>
                    <small>
                      Настройки применяются к новым фото. Сохранённые результаты
                      остаются в журнале.
                    </small>
                  </div>
                  <div className="fields">
                    <label>
                      Фоновый анализ фото
                      <select
                        value={draft.photo_enabled ? "on" : "off"}
                        onChange={(e) =>
                          change("photo_enabled", e.target.value === "on")
                        }
                      >
                        <option value="on">Включён</option>
                        <option value="off">Выключен</option>
                      </select>
                      <small>
                        Выключение диалогов также останавливает новые запросы
                        анализа.
                      </small>
                    </label>
                    <label>
                      Модель анализа фото
                      <select
                        value={draft.photo_model}
                        onChange={(e) =>
                          change(
                            "photo_model",
                            e.target.value as Settings["photo_model"],
                          )
                        }
                      >
                        <option value="gpt-6-luna">GPT-6 Luna</option>
                        <option value="gpt-6.1-sol">GPT-6.1 Sol</option>
                      </select>
                      <small>
                        Высокая глубина обработки фото и стикеров. Модель
                        диалога настраивается отдельно.
                      </small>
                    </label>
                  </div>
                </SettingsSection>
                <SettingsSection
                  title="Память"
                  summary={`${draft.memory_enabled ? "Включена" : "Выключена"} · ${modelName(draft.memory_model)}`}
                  changed={sectionChanged("memory_enabled", "memory_model")}
                >
                  <div>
                    <p>
                      Общая для топиков группы, отдельная для лички. Контекст —
                      90 дней, факты — до удаления с переоценкой.
                    </p>
                  </div>
                  <div className="fields">
                    <label>
                      Фоновая память
                      <select
                        value={draft.memory_enabled ? "on" : "off"}
                        onChange={(e) =>
                          change("memory_enabled", e.target.value === "on")
                        }
                      >
                        <option value="on">Включена</option>
                        <option value="off">Выключена</option>
                      </select>
                      <small>
                        Выключение останавливает новые разборы и использование
                        долговременных записей.
                      </small>
                    </label>
                    <label>
                      Модель памяти
                      <select
                        value={draft.memory_model}
                        onChange={(e) =>
                          change(
                            "memory_model",
                            e.target.value as Settings["memory_model"],
                          )
                        }
                      >
                        <option value="gpt-6-luna">GPT-6 Luna</option>
                        <option value="gpt-6.1-sol">GPT-6.1 Sol</option>
                      </select>
                      <small>
                        Низкая глубина обработки. Пакет до 20 сообщений.
                      </small>
                    </label>
                  </div>
                </SettingsSection>
                <SettingsSection
                  title="Интернет"
                  summary={`${draft.research_enabled ? "Включён" : "Выключен"} · ${modelName(draft.research_model)} · ${effortName(draft.research_reasoning)} глубина`}
                  changed={sectionChanged(
                    "research_enabled",
                    "research_model",
                    "research_reasoning",
                  )}
                >
                  <div>
                    <p>
                      Разбор ссылок и пересланных постов. Явная просьба
                      проверить или найти запускает поиск.
                    </p>
                  </div>
                  <div className="fields">
                    <label>
                      Поиск и проверка в интернете
                      <select
                        value={draft.research_enabled ? "on" : "off"}
                        onChange={(e) =>
                          change("research_enabled", e.target.value === "on")
                        }
                      >
                        <option value="on">Включены</option>
                        <option value="off">Выключены</option>
                      </select>
                    </label>
                    <label>
                      Модель исследования
                      <select
                        value={draft.research_model}
                        onChange={(e) =>
                          change(
                            "research_model",
                            e.target.value as Settings["research_model"],
                          )
                        }
                      >
                        <option value="gpt-6-luna">GPT-6 Luna</option>
                        <option value="gpt-6.1-sol">GPT-6.1 Sol</option>
                      </select>
                    </label>
                    <label>
                      Глубина обработки исследования
                      <select
                        value={draft.research_reasoning}
                        onChange={(e) =>
                          change(
                            "research_reasoning",
                            e.target.value as Settings["research_reasoning"],
                          )
                        }
                      >
                        <option value="low">Низкая</option>
                        <option value="medium">Средняя</option>
                        <option value="high">Высокая</option>
                      </select>
                      <small>
                        До трёх действий поиска. Модель финального ответа
                        задаётся отдельно.
                      </small>
                    </label>
                  </div>
                </SettingsSection>
                <SettingsSection
                  title="Голос и видео"
                  summary={`${draft.media_enabled ? "Включены" : "Выключены"} · до ${draft.video_max_frames} кадров`}
                  changed={sectionChanged(
                    "media_enabled",
                    "asr_model",
                    "media_model",
                    "media_reasoning",
                    "voice_max_seconds",
                    "video_max_seconds",
                    "video_max_frames",
                  )}
                >
                  <div>
                    <p className="muted">
                      Разбор по обращению. Длинные и внешние видео пока не
                      загружаются.
                    </p>
                  </div>
                  <div className="fields">
                    <label>
                      Разбор медиа
                      <select
                        value={draft.media_enabled ? "on" : "off"}
                        onChange={(e) =>
                          change("media_enabled", e.target.value === "on")
                        }
                      >
                        <option value="on">Включён</option>
                        <option value="off">Выключен</option>
                      </select>
                    </label>
                    <label>
                      Распознавание речи
                      <select
                        value={draft.asr_model}
                        onChange={(e) =>
                          change(
                            "asr_model",
                            e.target.value as Settings["asr_model"],
                          )
                        }
                      >
                        <option value="gpt-4o-mini-transcribe">
                          GPT-4o Mini Transcribe
                        </option>
                        <option value="gpt-4o-transcribe">
                          GPT-4o Transcribe
                        </option>
                      </select>
                    </label>
                    <label>
                      Модель анализа кадров
                      <select
                        value={draft.media_model}
                        onChange={(e) =>
                          change(
                            "media_model",
                            e.target.value as Settings["media_model"],
                          )
                        }
                      >
                        <option value="gpt-6-luna">GPT-6 Luna</option>
                        <option value="gpt-6.1-sol">GPT-6.1 Sol</option>
                      </select>
                    </label>
                    <label>
                      Глубина обработки кадров
                      <select
                        value={draft.media_reasoning}
                        onChange={(e) =>
                          change(
                            "media_reasoning",
                            e.target.value as Settings["media_reasoning"],
                          )
                        }
                      >
                        <option value="low">Низкая</option>
                        <option value="medium">Средняя</option>
                        <option value="high">Высокая</option>
                      </select>
                      <small>
                        Применяется к новым разборам видео, GIF и кружочков.
                      </small>
                    </label>
                    <details className="settings-advanced">
                      <summary>Лимиты обработки</summary>
                      <label>
                        Голосовые: предел, секунд
                        <input
                          type="number"
                          min={10}
                          max={300}
                          value={draft.voice_max_seconds}
                          onChange={(e) =>
                            change("voice_max_seconds", Number(e.target.value))
                          }
                        />
                      </label>
                      <label>
                        Видео: предел, секунд
                        <input
                          type="number"
                          min={5}
                          max={120}
                          value={draft.video_max_seconds}
                          onChange={(e) =>
                            change("video_max_seconds", Number(e.target.value))
                          }
                        />
                      </label>
                      <label>
                        Максимум кадров
                        <input
                          type="number"
                          min={1}
                          max={32}
                          value={draft.video_max_frames}
                          onChange={(e) =>
                            change("video_max_frames", Number(e.target.value))
                          }
                        />
                        <small>
                          Кадры примерно через 2 секунды с учётом лимита,
                          включая начало и конец при лимите от двух. 20 МиБ на
                          файл, до трёх вложений на запрос. Применяется к новым
                          разборам.
                        </small>
                      </label>
                    </details>
                  </div>
                </SettingsSection>
              </fieldset>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
