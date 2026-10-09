import { useEffect, useState } from "react";
import { api, ApiError } from "./api";

type Metrics = {
  calls: number;
  known_cost_usd: number;
  unknown_cost_calls: number;
  avg_latency_ms: number | null;
  unknown_outcomes: number;
  first_call_at: string | null;
};
type Review = {
  case_id: string;
  status: string;
  note: string;
  version: number;
  updated_at: string;
};
type Data = {
  generated_at: string;
  from_at: string;
  bot_id: string;
  summary: Metrics & { p95_latency_ms: number | null; latency_samples: number };
  breakdown: Record<string, (Metrics & { label: string })[]>;
  calls: {
    id: number;
    model: string;
    task: string;
    scope: string;
    chat_id: string;
    state: string;
    created_at: string;
    latency_ms: number | null;
    known_cost_usd: number;
    unknown_cost: number;
    error_code: string | null;
    settings_version: number | null;
  }[];
  total: number;
  pages: number;
  chats: { bot_id: string; scope: string; chat_id: string; title: string }[];
  queues: { queue: string; state: string; count: number; oldest_at: string }[];
  cases: { id: string; title: string; scenario: string }[];
  reviews: Review[];
  preferences: { version: number; warning_usd: number | null };
  warning_summary: Metrics;
};
const names: Record<string, string> = {
  avatar: "Аватарки",
  dialogue: "Диалоги и поведение",
  replay: "Платные тесты",
  photo: "Фото",
  memory: "Память",
  research: "Поиск",
  asr: "Распознавание речи",
  video: "Анализ кадров",
  media: "Медиа",
  jobs: "Задания",
  delivery: "Доставка",
  other: "Прочее",
};
const states: Record<string, string> = {
  pending: "Ожидает",
  queued: "В очереди",
  started: "Выполняется",
  completed: "Завершено",
  unknown: "Исход неизвестен",
  failed: "Ошибка",
  rejected: "Отклонено",
  cancelled: "Отменено",
  invalid: "Некорректный результат",
  sending: "Отправляется",
  fetching: "Загрузка",
  analyzing: "Анализ",
  transcribing: "Распознавание",
  preparing: "Подготовка",
  search_started: "Поиск",
  downloading: "Загрузка",
};
const reviews: Record<string, string> = {
  pending: "Не проверено",
  passed: "Пройдено",
  failed: "Нужна доработка",
};
const money = (v: number) => `$${v.toFixed(6)}`;
const duration = (v: number | null) =>
  v == null ? "Нет измерений" : `${(v / 1000).toFixed(1)} с`;
const date = (v: string) => new Date(v).toLocaleString("ru");

export function PilotPanel({
  csrf,
  onUnauthorized,
  onDirtyChange,
}: {
  csrf: string | null;
  onUnauthorized: () => void;
  onDirtyChange: (v: boolean) => void;
}) {
  const [days, setDays] = useState("7"),
    [scope, setScope] = useState("all"),
    [chat, setChat] = useState(""),
    [page, setPage] = useState(0),
    [problems, setProblems] = useState(false),
    [revision, setRevision] = useState(0);
  const [data, setData] = useState<Data | null>(null),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [dimension, setDimension] = useState("task");
  const [edit, setEdit] = useState<string | null>(null),
    [note, setNote] = useState(""),
    [status, setStatus] = useState("pending"),
    [warning, setWarning] = useState(""),
    [version, setVersion] = useState(0),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    onDirtyChange(edit !== null);
    const prevent = (e: BeforeUnloadEvent) => {
      if (edit !== null) e.preventDefault();
    };
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [edit, onDirtyChange]);
  function change(action: () => void) {
    if (busy) return;
    if (edit !== null && !window.confirm("Отменить несохранённые изменения?"))
      return;
    setEdit(null);
    setMessage("");
    action();
  }
  useEffect(() => {
    let active = true;
    setData(null);
    setError("");
    api<Data>(
      `operations?days=${days}&scope=${scope}&chat=${encodeURIComponent(chat)}&page=${page}&problems=${problems}`,
    )
      .then((v) => {
        if (active) setData(v);
      })
      .catch((e) => {
        if (!active) return;
        if (e instanceof ApiError && e.status === 401) onUnauthorized();
        setError(e.message || "Нет связи с сервером");
      });
    return () => {
      active = false;
    };
  }, [days, scope, chat, page, problems, revision]);
  async function save() {
    setBusy(true);
    setError("");
    try {
      await api(
        edit === "budget"
          ? "operations/preferences"
          : `operations/reviews/${edit}`,
        "PUT",
        edit === "budget"
          ? {
              expected_version: version,
              warning_usd: warning === "" ? null : Number(warning),
            }
          : { expected_version: version, status, note },
        csrf,
      );
      setEdit(null);
      setMessage(edit === "budget" ? "Порог сохранён" : "Оценка сохранена");
      setRevision((v) => v + 1);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized();
      setError(e instanceof Error ? e.message : "Не удалось сохранить");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="pilot-panel">
      <section className="card">
        <div className="filter-bar">
          <label>
            Период
            <select
              aria-label="Период"
              value={days}
              onChange={(e) =>
                change(() => {
                  setDays(e.target.value);
                  setPage(0);
                })
              }
            >
              {[7, 30, 90].map((n) => (
                <option key={n} value={n}>
                  {n} дней
                </option>
              ))}
            </select>
          </label>
          <label>
            Область
            <select
              aria-label="Область"
              value={scope}
              onChange={(e) =>
                change(() => {
                  setScope(e.target.value);
                  setChat("");
                  setPage(0);
                })
              }
            >
              <option value="all">Все чаты</option>
              <option value="group">Группы</option>
              <option value="private">Личные</option>
            </select>
          </label>
          <label>
            Чат
            <select
              aria-label="Чат"
              value={chat}
              onChange={(e) =>
                change(() => {
                  setChat(e.target.value);
                  setPage(0);
                })
              }
            >
              <option value="">Все</option>
              {data?.chats
                .filter((c) => scope === "all" || c.scope === scope)
                .map((c) => (
                  <option key={`${c.bot_id}:${c.chat_id}`} value={c.chat_id}>
                    {c.title} · {c.chat_id}
                  </option>
                ))}
            </select>
          </label>
          <button onClick={() => change(() => setRevision((v) => v + 1))}>
            Обновить
          </button>
        </div>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        {message && <p role="status">{message}</p>}
        {!data && !error && <p role="status">Загружаем статистику…</p>}
        {data && (
          <>
            <p className="muted">
              {date(data.from_at)} — {date(data.generated_at)} ·{" "}
              {data.bot_id ? `Бот ${data.bot_id}` : "Все сохранённые боты"}
            </p>
            <dl className="dialogue-metrics">
              <div>
                <dt>Известная оценка расходов</dt>
                <dd>{money(data.summary.known_cost_usd)}</dd>
              </div>
              <div>
                <dt>Вызовов без полной оценки</dt>
                <dd>{data.summary.unknown_cost_calls}</dd>
              </div>
              <div>
                <dt>Среднее / 95-й процентиль</dt>
                <dd>
                  {duration(data.summary.avg_latency_ms)} /{" "}
                  {duration(data.summary.p95_latency_ms)}
                </dd>
              </div>
              <div>
                <dt>Неизвестный исход</dt>
                <dd>{data.summary.unknown_outcomes}</dd>
              </div>
            </dl>
            <p className="muted">
              Оценка по сохранённым вызовам, не подтверждённое списание.
              Включены платные тесты; поиск учитывается отдельно внутри суммы.
              Неоценённая часть не считается нулевой.
            </p>
            <p className="muted">
              Время вызова модели, без очереди и доставки. Измерений:{" "}
              {data.summary.latency_samples}.{" "}
              {data.summary.first_call_at
                ? `Первый вызов в выборке: ${date(data.summary.first_call_at)}. История может покрывать не весь период.`
                : "Вызовов за период нет."}
            </p>
          </>
        )}
      </section>
      {data && (
        <>
          <section className="card">
            <h2>Расходы по направлениям</h2>
            <div className="peer-actions">
              {[
                ["task", "По задачам"],
                ["model", "По моделям"],
                ["chat_id", "По чатам"],
              ].map(([k, n]) => (
                <button
                  key={k}
                  aria-pressed={dimension === k}
                  onClick={() => setDimension(k)}
                >
                  {n}
                </button>
              ))}
            </div>
            {data.breakdown[dimension].length === 0 && (
              <p>За выбранный период вызовов нет.</p>
            )}
            {data.breakdown[dimension].map((row) => (
              <div className="pilot-row" key={row.label}>
                <strong>
                  {dimension === "task"
                    ? names[row.label] || row.label
                    : row.label || "Не определено"}
                </strong>
                <span>
                  {row.calls} вызовов · {money(row.known_cost_usd)} · без
                  оценки: {row.unknown_cost_calls} · среднее:{" "}
                  {duration(row.avg_latency_ms)}
                </span>
              </div>
            ))}
          </section>
          <section className="card">
            <h2>Очереди сейчас</h2>
            <p className="muted">
              По выбранным области и чату. Период статистики не влияет на
              очереди. Неизвестные отправки не повторяются автоматически;
              управление ими — в «Telegram и доступ».
            </p>
            {data.queues.length === 0 ? (
              <p>Ожидающих операций и неизвестных отправок нет.</p>
            ) : (
              data.queues.map((q) => (
                <div className="pilot-row" key={q.queue + q.state}>
                  <strong>
                    {names[q.queue]} · {states[q.state] || q.state}: {q.count}
                  </strong>
                  <span>Самая ранняя: {date(q.oldest_at)}</span>
                </div>
              ))
            )}
          </section>
          <section className="card">
            <h2>Журнал вызовов</h2>
            <label>
              Показывать вызовы
              <select
                value={problems ? "problems" : "all"}
                onChange={(e) =>
                  change(() => {
                    setProblems(e.target.value === "problems");
                    setPage(0);
                  })
                }
              >
                <option value="all">Все</option>
                <option value="problems">Незавершённые и с ошибками</option>
              </select>
            </label>
            <p className="muted">
              Технические сведения без текстов переписок. Фильтр журнала не
              меняет общую сумму.
            </p>
            {!data.calls.length && (
              <p>За выбранный период таких вызовов нет.</p>
            )}
            {data.calls.map((c) => (
              <details className="pilot-row" key={c.id}>
                <summary>
                  #{c.id} · {names[c.task] || c.task} ·{" "}
                  {states[c.state] || c.state}
                  <span>
                    {date(c.created_at)} ·{" "}
                    {c.scope === "private"
                      ? "Личка"
                      : c.scope === "group"
                        ? "Группа"
                        : "Неизвестная область"}{" "}
                    {c.chat_id}
                  </span>
                </summary>
                <p>
                  {c.model} · настройки v{c.settings_version ?? "?"}
                </p>
                <p>
                  {duration(c.latency_ms)} · {money(c.known_cost_usd)}
                  {c.unknown_cost ? " + неоценённая часть" : ""}
                </p>
                {c.error_code && <p>Код: {c.error_code}</p>}
              </details>
            ))}
            <div className="action-bar">
              <button
                disabled={page === 0}
                onClick={() => change(() => setPage((p) => p - 1))}
              >
                Назад
              </button>
              <span>
                {page + 1} / {Math.max(1, data.pages)}
              </span>
              <button
                disabled={page + 1 >= data.pages}
                onClick={() => change(() => setPage((p) => p + 1))}
              >
                Далее
              </button>
            </div>
          </section>
          <section className="card">
            <h2>Предупреждение о расходе</h2>
            <p className="muted">
              За последние 30 дней по всем чатам выбранного бота, включая
              платные тесты. Обновляется при открытии экрана и по кнопке
              «Обновить». Порог не останавливает ответы.
            </p>
            <p>
              Оценено: {money(data.warning_summary.known_cost_usd)} · без полной
              цены: {data.warning_summary.unknown_cost_calls}
            </p>
            {data.preferences.warning_usd !== null ? (
              <p
                className={
                  data.warning_summary.known_cost_usd >=
                  data.preferences.warning_usd
                    ? "notice error"
                    : "notice"
                }
              >
                {data.warning_summary.known_cost_usd >=
                data.preferences.warning_usd
                  ? "Порог достигнут"
                  : "Оценённая сумма ниже порога"}
                : {money(data.preferences.warning_usd)}
                {data.warning_summary.unknown_cost_calls > 0
                  ? ". Полная сумма неизвестна"
                  : ""}
              </p>
            ) : (
              <p>Предупреждение выключено</p>
            )}
            {edit === "budget" ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void save();
                }}
              >
                <label>
                  Порог за 30 дней, USD
                  <input
                    aria-label="Порог за 30 дней, USD"
                    type="number"
                    min="0.000001"
                    max="1000000"
                    step="any"
                    value={warning}
                    disabled={busy}
                    onChange={(e) => setWarning(e.target.value)}
                  />
                </label>
                <p className="muted">
                  Оставь поле пустым, чтобы выключить предупреждение.
                </p>
                <div className="action-bar">
                  <button disabled={busy} type="submit">
                    Сохранить порог
                  </button>
                  <button
                    disabled={busy}
                    type="button"
                    onClick={() => setEdit(null)}
                  >
                    Отмена
                  </button>
                </div>
              </form>
            ) : (
              <button
                onClick={() =>
                  change(() => {
                    setEdit("budget");
                    setWarning(
                      data.preferences.warning_usd === null
                        ? ""
                        : String(data.preferences.warning_usd),
                    );
                    setVersion(data.preferences.version);
                  })
                }
              >
                Настроить порог
              </button>
            )}
          </section>
          <section className="card">
            <h2>Приёмка пилота</h2>
            <p className="muted">
              Общие оценки владельца, независимо от фильтра чатов. Отметь чат,
              модель и пример без копирования личной переписки. Для сравнения
              моделей используй платный тест в «Диалогах»; он не отправляет
              сообщения в Telegram.
            </p>
            {data.cases.map((c) => {
              const r = data.reviews.find((v) => v.case_id === c.id);
              return (
                <div className="pilot-row" key={c.id}>
                  <strong>
                    {c.title} · {reviews[r?.status || "pending"]}
                  </strong>
                  <p>{c.scenario}</p>
                  {r && (
                    <>
                      <small>Оценка владельца · {date(r.updated_at)}</small>
                      {r.note && <p className="research-text">{r.note}</p>}
                    </>
                  )}
                  {edit === c.id ? (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        void save();
                      }}
                    >
                      <label>
                        Результат проверки
                        <select
                          aria-label="Результат проверки"
                          disabled={busy}
                          value={status}
                          onChange={(e) => setStatus(e.target.value)}
                        >
                          {Object.entries(reviews).map(([k, n]) => (
                            <option key={k} value={k}>
                              {n}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Заметка к проверке
                        <textarea
                          aria-label="Заметка к проверке"
                          disabled={busy}
                          maxLength={1000}
                          value={note}
                          onChange={(e) => setNote(e.target.value)}
                        />
                      </label>
                      <div className="action-bar">
                        <button disabled={busy} type="submit">
                          Сохранить оценку
                        </button>
                        <button
                          disabled={busy}
                          type="button"
                          onClick={() => setEdit(null)}
                        >
                          Отмена
                        </button>
                      </div>
                    </form>
                  ) : (
                    <button
                      onClick={() =>
                        change(() => {
                          setEdit(c.id);
                          setNote(r?.note || "");
                          setStatus(r?.status || "pending");
                          setVersion(r?.version || 0);
                        })
                      }
                    >
                      Оценить: {c.title}
                    </button>
                  )}
                </div>
              );
            })}
          </section>
          <section className="card">
            <h2>Резервная копия</h2>
            <p>
              Копирование выполняется после полного завершения процесса Зари.
              Проверка восстановления создаёт отдельную копию в карантине и не
              запускает бота.
            </p>
            <details>
              <summary>Команды локальной проверки</summary>
              <pre className="research-text">{`.venv/Scripts/python.exe -m zarya.backup backup data backups/pilot-01\n.venv/Scripts/python.exe -m zarya.backup verify backups/pilot-01\n.venv/Scripts/python.exe -m zarya.backup restore backups/pilot-01 restore-checks/pilot-01`}</pre>
              <p>
                Каталоги назначения должны быть новыми. Ключи API не копируются.
                Внутри остаются приватные данные и хеш пароля администратора.
                Инструкция: docs/operations.md.
              </p>
            </details>
          </section>
        </>
      )}
    </div>
  );
}
