import { useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type BehaviorData,
  type GroupPolicy,
  type Settings,
} from "./api";

const toneName: Record<string, string> = {
  neutral: "Спокойная",
  curious: "Любопытная",
  warm: "Тёплая",
  grateful: "Благодарная",
  tender: "Умилённая",
  angry: "Сердитая",
  confident: "Уверенная",
};
const modes: Record<string, string> = {
  addressed: "Ответ собеседнику",
  off: "Выключена",
  shadow: "Предпросмотр без отправки",
  live: "Самостоятельное участие",
};
const operationName: Record<string, string> = {
  completed: "Завершено",
  cancelled: "Отменено",
  failed: "Ошибка",
  invalid: "Некорректный результат",
  unknown: "Исход неизвестен",
  skipped: "Пропущено",
  running: "В работе",
};
type Draft = Pick<
  Settings,
  | "behavior_enabled"
  | "reactions_enabled"
  | "expressiveness"
  | "emotional_max_parts"
>;
const date = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString("ru-RU");

export function BehaviorPanel({
  csrf,
  onUnauthorized,
  onDirtyChange,
}: {
  csrf: string | null;
  onUnauthorized: () => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [scope, setScope] = useState("all"),
    [chat, setChat] = useState(""),
    [thread, setThread] = useState("");
  const [page, setPage] = useState(0),
    [data, setData] = useState<BehaviorData | null>(null);
  const [general, setGeneral] = useState<{
    version: number;
    settings: Draft;
  } | null>(null);
  const [policy, setPolicy] = useState<GroupPolicy | null>(null);
  const [readError, setReadError] = useState(""),
    [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const serial = useRef(0);
  const fail = (e: unknown, target: (text: string) => void) => {
    if (e instanceof ApiError && e.status === 401) onUnauthorized();
    target(e instanceof Error ? e.message : "Не удалось выполнить запрос");
  };
  async function refresh() {
    const ticket = ++serial.current;
    try {
      const next = await api<BehaviorData>(
        `behavior?scope=${scope}&page=${page}${chat ? `&chat_id=${encodeURIComponent(chat)}` : ""}`,
      );
      if (ticket === serial.current) {
        setData(next);
        setReadError("");
      }
    } catch (e) {
      if (ticket === serial.current) fail(e, setReadError);
    }
  }
  useEffect(() => {
    setData(null);
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => {
      clearInterval(timer);
      ++serial.current;
    };
  }, [scope, chat, page]);
  useEffect(() => {
    onDirtyChange(Boolean(general || policy));
  }, [general, policy, onDirtyChange]);
  const peer = data?.peers.find((p) => p.chat_id === chat);
  const fields = general?.settings ?? data?.settings;
  const group = policy ?? peer?.policy;
  function choose(kind: "scope" | "chat", value: string) {
    if (
      (general || policy) &&
      !window.confirm("Отменить несохранённые изменения поведения?")
    )
      return;
    setGeneral(null);
    setPolicy(null);
    setNotice("");
    setActionError("");
    setPage(0);
    setThread("");
    if (kind === "scope") {
      setScope(value);
      setChat("");
    } else setChat(value);
  }
  function edit<K extends keyof Draft>(key: K, value: Draft[K]) {
    if (!data || !fields) return;
    setGeneral({
      version: general?.version ?? data.settings_version,
      settings: { ...fields, [key]: value },
    });
    setNotice("");
  }
  async function perform(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    setActionError("");
    setNotice("");
    try {
      await action();
      setNotice(message);
      await refresh();
    } catch (e) {
      fail(e, setActionError);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="behavior-panel">
      {readError && (
        <div className="notice error" role="alert">
          {readError}{" "}
          <button className="text-button" onClick={() => void refresh()}>
            Повторить загрузку
          </button>
        </div>
      )}
      {actionError && (
        <div className="notice error" role="alert">
          {actionError}{" "}
          <button
            className="text-button"
            onClick={() => {
              setGeneral(null);
              setPolicy(null);
              void refresh();
            }}
          >
            Загрузить актуальные значения
          </button>
        </div>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <section className="card behavior-stop">
        <div>
          <h2>Активность Зари</h2>
          <p className="muted">
            {data
              ? data.settings.dialogue_enabled
                ? "Ответы и реакции разрешены настройками. Доступ каждого чата проверяется отдельно."
                : "Новые ответы и реакции остановлены. Возобновить можно в настройках диалога."
              : "Проверяем состояние…"}
          </p>
        </div>
        <button
          className="secondary"
          disabled={busy || !data || !data.settings.dialogue_enabled}
          onClick={() =>
            void perform(
              () => api("behavior/stop", "POST", {}, csrf),
              "Новые ответы и реакции остановлены",
            )
          }
        >
          Остановить ответы и реакции во всех чатах
        </button>
      </section>
      <div className="filter-bar">
        <label>
          Область
          <select
            value={scope}
            disabled={busy}
            onChange={(e) => choose("scope", e.target.value)}
          >
            <option value="all">Все разговоры</option>
            <option value="group">Группы</option>
            <option value="private">Личные</option>
          </select>
        </label>
        <label>
          Чат
          <select
            value={chat}
            disabled={busy}
            onChange={(e) => choose("chat", e.target.value)}
          >
            <option value="">Все чаты</option>
            {data?.peers.map((p) => (
              <option key={p.chat_id} value={p.chat_id}>
                {p.title} · {p.chat_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          Топик
          <select
            value={thread}
            disabled={busy || !chat}
            onChange={(e) => setThread(e.target.value)}
          >
            <option value="">Все топики</option>
            {data?.moods
              .filter((m) => m.chat_id === chat)
              .map((m) => (
                <option key={m.thread_id} value={m.thread_id}>
                  {m.thread_id ? `Топик ${m.thread_id}` : "Основной разговор"}
                </option>
              ))}
          </select>
        </label>
      </div>
      <div className="behavior-grid">
        <section className="card">
          <h2>Настроение разговоров</h2>
          <p className="muted">
            Настроение меняет выразительность. Разрешения и границы памяти
            сохраняются; память группы общая для топиков.
          </p>
          {!data ? (
            <p>Загружаем состояния…</p>
          ) : !data.moods.length ? (
            <p className="empty">Настроение ещё не определено</p>
          ) : (
            data.moods
              .filter((m) => thread === "" || m.thread_id === Number(thread))
              .map((m) => (
                <article
                  className="behavior-mood"
                  key={`${m.chat_id}/${m.thread_id}`}
                >
                  <h3>{m.title}</h3>
                  <p className="muted">
                    {m.scope === "private" ? "Личный разговор" : "Группа"} ·{" "}
                    {m.chat_id} ·{" "}
                    {m.thread_id ? `Топик ${m.thread_id}` : "Основной разговор"}
                  </p>
                  <p>
                    <strong>{toneName[m.tone] ?? m.tone}</strong> ·
                    Интенсивность: {Math.round(m.intensity * 100)} из 100
                  </p>
                  <progress
                    aria-label={`Интенсивность настроения ${m.title}, чат ${m.chat_id}, топик ${m.thread_id}`}
                    max={100}
                    value={m.intensity * 100}
                  />
                  <p>{m.reason || "Обычное состояние разговора"}</p>
                  <p className="muted">
                    Изменено {date(m.updated_at)} · версия {m.version}
                  </p>
                  <button
                    className="secondary"
                    aria-label={`Сбросить настроение: ${m.title}, чат ${m.chat_id}, топик ${m.thread_id}`}
                    disabled={busy}
                    onClick={() =>
                      void perform(
                        () =>
                          api(
                            `behavior/moods/${m.chat_id}/reset`,
                            "POST",
                            {
                              thread_id: m.thread_id,
                              expected_version: m.version,
                            },
                            csrf,
                          ),
                        "Настроение этого разговора сброшено",
                      )
                    }
                  >
                    Сбросить настроение этого разговора
                  </button>
                </article>
              ))
          )}
        </section>
        <div className="behavior-stack">
          <section className="card">
            <h2>Выразительность и реакции</h2>
            {fields && (
              <fieldset disabled={busy} className="behavior-fields">
                <label>
                  Настроение и план действий
                  <select
                    value={fields.behavior_enabled ? "on" : "off"}
                    onChange={(e) =>
                      edit("behavior_enabled", e.target.value === "on")
                    }
                  >
                    <option value="on">Включены</option>
                    <option value="off">Выключены</option>
                  </select>
                </label>
                <label>
                  Выразительность
                  <select
                    value={fields.expressiveness}
                    onChange={(e) =>
                      edit(
                        "expressiveness",
                        e.target.value as Draft["expressiveness"],
                      )
                    }
                  >
                    <option value="restrained">Сдержанная</option>
                    <option value="balanced">Обычная</option>
                    <option value="expressive">Выразительная</option>
                  </select>
                </label>
                <label>
                  Реакции на сообщения
                  <select
                    value={fields.reactions_enabled ? "on" : "off"}
                    onChange={(e) =>
                      edit("reactions_enabled", e.target.value === "on")
                    }
                  >
                    <option value="on">Включены</option>
                    <option value="off">Выключены</option>
                  </select>
                </label>
                <label>
                  Максимум реплик в эмоциональной серии
                  <input
                    type="number"
                    min={1}
                    max={4}
                    value={fields.emotional_max_parts}
                    onChange={(e) =>
                      edit("emotional_max_parts", Number(e.target.value))
                    }
                  />
                </label>
                <div className="save-bar">
                  <span>
                    {general
                      ? "Есть несохранённые изменения"
                      : "Настройки сохранены"}
                  </span>
                  <button
                    className="primary"
                    disabled={!general || busy}
                    onClick={() =>
                      void perform(async () => {
                        if (!general || !data) return;
                        await api(
                          "settings",
                          "PUT",
                          {
                            expected_version: general.version,
                            settings: { ...data.settings, ...general.settings },
                          },
                          csrf,
                        );
                        setGeneral(null);
                      }, "Настройки поведения сохранены")
                    }
                  >
                    Сохранить поведение
                  </button>
                </div>
              </fieldset>
            )}
          </section>
          <section className="card">
            <h2>Самостоятельное участие</h2>
            {peer?.scope === "group" && group ? (
              <fieldset
                disabled={busy || peer.access !== "approved"}
                className="behavior-fields"
              >
                <legend>
                  {peer.title} · {peer.chat_id}
                </legend>
                <p className="muted">
                  Настройка действует во всей группе, включая топики.
                </p>
                <label>
                  Режим инициативы
                  <select
                    value={group.mode}
                    onChange={(e) => {
                      setPolicy({
                        ...group,
                        mode: e.target.value as GroupPolicy["mode"],
                      });
                      setNotice("");
                    }}
                  >
                    {Object.entries(modes)
                      .filter(([key]) => key !== "addressed")
                      .map(([key, name]) => (
                        <option key={key} value={key}>
                          {name}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  Шанс инициативы, %
                  <input
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={group.chance_percent}
                    onChange={(e) => {
                      setPolicy({
                        ...group,
                        chance_percent: Number(e.target.value),
                      });
                      setNotice("");
                    }}
                  />
                </label>
                <p className="muted">
                  Шанс применяется отдельно к каждому подходящему сообщению
                  человека. Выбор не гарантирует ответ: Заря может промолчать.
                  Дневного лимита и пауз между вступлениями нет.
                </p>
                {group.mode === "shadow" && (
                  <p className="notice">
                    Предложения сохраняются в журнале и не отправляются в
                    Telegram. Оценка моделью расходует токены.
                  </p>
                )}
                <div className="save-bar">
                  <span>
                    {policy
                      ? "Есть несохранённые изменения"
                      : `Версия ${group.version}`}
                  </span>
                  <button
                    className="primary"
                    disabled={!policy || busy}
                    onClick={() =>
                      void perform(async () => {
                        if (!policy) return;
                        await api(
                          `behavior/groups/${chat}`,
                          "PUT",
                          {
                            expected_version: policy.version,
                            mode: policy.mode,
                            chance_percent: policy.chance_percent,
                          },
                          csrf,
                        );
                        setPolicy(null);
                      }, "Настройка инициативы сохранена")
                    }
                  >
                    Сохранить инициативу
                  </button>
                </div>
              </fieldset>
            ) : (
              <p className="empty">
                {peer?.scope === "private"
                  ? "В личной переписке Заря отвечает собеседнику; самостоятельная инициатива настраивается для групп."
                  : "Выбери группу для настройки инициативы"}
              </p>
            )}
          </section>
        </div>
      </div>
      <section className="card">
        <h2>Решения и предложения</h2>
        <p className="muted">
          Краткие основания действий и результат доставки. Скрытые рассуждения
          модели не сохраняются.
        </p>
        {!data ? (
          <p>Загружаем журнал…</p>
        ) : !data.decisions.length ? (
          <p className="empty">Для выбранного разговора предложений пока нет</p>
        ) : (
          data.decisions
            .filter(
              (d) => thread === "" || (d.thread_id ?? 0) === Number(thread),
            )
            .map((d) => (
              <details className="behavior-event" key={d.run_id}>
                <summary>
                  <strong>
                    {d.action === "not_selected"
                      ? "Оценка модели не запускалась"
                      : d.action === "silent"
                        ? "Заря решила промолчать"
                        : d.action === "reaction"
                          ? `Заря выбрала реакцию ${d.plan.reaction ?? ""}`
                          : "Заря предложила ответить"}
                  </strong>
                  <span className="muted">
                    {date(d.created_at)} · {d.chat_id} ·{" "}
                    {d.thread_id ? `Топик ${d.thread_id}` : "Основной разговор"}{" "}
                    · {modes[d.mode] ?? d.mode}
                  </span>
                </summary>
                <p>{d.reason}</p>
                {d.plan.text && (
                  <p className="behavior-proposal">{d.plan.text}</p>
                )}
                <p>
                  {d.mode === "shadow"
                    ? "Предпросмотр: ничего не отправлено"
                    : d.delivery
                      ? `Доставка: ${d.delivery
                          .split(",")
                          .map(
                            (s) =>
                              ({
                                sent: "отправлено",
                                pending: "ожидает",
                                sending: "отправляется",
                                failed: "отклонена",
                                unknown: "исход неизвестен",
                                cancelled: "отменена",
                              })[s] ?? s,
                          )
                          .join(", ")}`
                      : d.action === "silent"
                        ? "Молчание: отправка не планировалась"
                        : d.action === "not_selected"
                          ? "Вероятностный фильтр: без вызова модели"
                          : "Отправка не выполнена"}
                </p>
                <p className="muted">
                  Операция #{d.run_id} · {operationName[d.state] ?? d.state} ·{" "}
                  {d.call_state
                    ? `Расход: ${d.cost_usd === null ? "не определён" : `$${d.cost_usd.toFixed(6)}`}`
                    : "Вызова модели не было"}
                </p>
              </details>
            ))
        )}
        {data && (
          <div className="action-bar">
            <button
              className="secondary"
              disabled={page === 0 || busy}
              onClick={() => setPage(page - 1)}
            >
              Назад
            </button>
            <span>
              Страница {page + 1} · записей {data.total}
            </span>
            <button
              className="secondary"
              disabled={(page + 1) * 20 >= data.total || busy}
              onClick={() => setPage(page + 1)}
            >
              Дальше
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
