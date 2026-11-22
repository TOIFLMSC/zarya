import { useEffect, useRef, useState } from "react";
import { TimingDiagnostics } from "./TimingDiagnostics";
import { PersonalSummaries } from "./PersonalSummaries";
import { ContextBudgetDiagnostics } from "./ContextBudgetDiagnostics";
import {
  api,
  ApiError,
  type DialogList,
  type DialogDetail,
  type Snapshot,
} from "./api";

const states: Record<string, string> = {
  generating: "Готовит ответ",
  completed: "Ответ готов",
  skipped: "Без ответа",
  service: "Сервисный ответ",
  cancelled: "Отменён",
  unknown: "Результат неизвестен",
  rejected: "Запрос отклонён",
  invalid: "Неполный ответ",
  pending: "В очереди",
  sent: "Отправлено",
  sending: "Отправляется",
  failed: "Ошибка",
  test_only: "Только тест",
  dismissed: "Разобрано",
  started: "Запрос начат",
  accepted: "Telegram принял",
  not_attempted: "Запросов пока нет",
  rate_limited: "Ограничение частоты",
  uncertain: "Подтверждение не получено",
  expired: "Истёк срок",
};
const retrievalReasons: Record<string, string> = {
  query_match: "совпадение с темой",
  accepted_reply: "связь с проверенным реплаем",
  same_topic: "тот же топик",
  speaker: "текущий собеседник",
  recent: "недавняя запись",
  support_edge: "связь с подтверждением",
  related_episode: "связанный эпизод",
  dependency_edge: "контекстная зависимость",
};
const triggers: Record<string, string> = {
  private: "Личное сообщение",
  name: "Обращение по имени",
  mention: "Упоминание",
  reply: "Ответ Заре",
  continuation: "Продолжение разговора",
  ambient: "Общий контекст",
  forwarded: "Пересылка",
  edited: "Правка сообщения",
  bot_or_channel: "Бот или канал",
  no_text: "Нет текста",
  disabled: "Диалоги выключены",
  not_configured: "Ключ не настроен",
  superseded: "Вопрос обновлён",
  replay: "Тестовый прогон",
  legacy: "Событие этапа 2",
  expired: "Вопрос устарел",
};
const money = (value: number | null | undefined) =>
  value == null ? "Неизвестно" : `$${value.toFixed(6)}`;

export function DialoguePanel({
  csrf,
  onUnauthorized,
}: {
  csrf: string | null;
  onUnauthorized: () => void;
}) {
  const [list, setList] = useState<DialogList | null>(null);
  const [scope, setScope] = useState("all");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<DialogDetail | null>(null);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [replayError, setReplayError] = useState("");
  const [paidSettings, setPaidSettings] = useState<Snapshot | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [paid, setPaid] = useState(false);
  const [test, setTest] = useState<DialogDetail | null>(null);
  const sequence = useRef(0);
  const localQuote = detail?.snapshot.verbatim_quote;
  function fail(e: unknown, target = setError) {
    if (e instanceof ApiError && e.status === 401) onUnauthorized();
    target(e instanceof Error ? e.message : "Не удалось загрузить журнал");
  }
  useEffect(() => {
    let active = true,
      fetching = false;
    setList(null);
    async function load() {
      if (fetching) return;
      fetching = true;
      try {
        const result = await api<DialogList>(
          `dialogues?scope=${scope}&page=${page}`,
        );
        if (active) {
          setList(result);
          setError("");
        }
      } catch (e) {
        if (active) {
          setList(null);
          fail(e);
        }
      } finally {
        fetching = false;
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [scope, page, revision]);
  useEffect(() => {
    const seq = ++sequence.current;
    setDetail(null);
    setTest(null);
    setPaid(false);
    setPaidSettings(null);
    setDetailError("");
    setReplayError("");
    if (selected === null) return;
    async function load() {
      try {
        const result = await api<DialogDetail>(`dialogues/${selected}`);
        if (seq === sequence.current) {
          setDetail(result);
          setDetailError("");
        }
      } catch (e) {
        if (seq === sequence.current) fail(e, setDetailError);
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      ++sequence.current;
      window.clearInterval(timer);
    };
  }, [selected]);
  async function preparePaid() {
    if (paid) {
      setPaid(false);
      return;
    }
    const seq = sequence.current;
    setBusy(true);
    setReplayError("");
    setPaidSettings(null);
    try {
      const saved = await api<Snapshot>("settings");
      if (seq === sequence.current) {
        setPaidSettings(saved);
        setPaid(true);
      }
    } catch (e) {
      if (seq === sequence.current) fail(e, setReplayError);
    } finally {
      setBusy(false);
    }
  }
  async function replay(mode: "recorded" | "paid") {
    if (!detail || busy) return;
    setBusy(true);
    setReplayError("");
    setTest(null);
    const seq = sequence.current;
    try {
      const result = await api<DialogDetail>(
        "dialogues/replay",
        "POST",
        {
          run_id: detail.id,
          mode,
          ...(mode === "paid"
            ? { expected_settings_version: paidSettings?.version }
            : {}),
        },
        csrf,
      );
      if (seq === sequence.current) {
        setTest(result);
        setPaid(false);
      }
    } catch (e) {
      if (seq === sequence.current) {
        fail(e, setReplayError);
        setPaid(false);
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="dialogue-panel">
      <section className="card connection-card">
        <div>
          <span className="card-label">ДИАЛОГ И НАБЛЮДЕНИЕ</span>
          <h2>
            {list?.configured ? "Ключ OpenAI настроен" : "Ожидаем подключение"}
          </h2>
          <p className="muted">
            Журнал входящих сообщений, контекста, ответов и доставки. Здесь
            показаны события приложения.
            Новые ответы должны учитывать уточнения собеседника и пересматривать
            ранее предложенные варианты, если они недоступны.
          </p>
        </div>
        <div>
          <strong>{money(list?.known_cost_usd)}</strong>
          <p className="muted">Известная оценка расходов</p>
          <small>
            Без оценки: {list?.unknown_cost_calls ?? "…"} вызовов. Включая
            платные тесты.
          </small>
        </div>
      </section>
      <div className="dialogue-toolbar">
        <label>
          Область
          <select
            value={scope}
            disabled={busy}
            onChange={(e) => {
              setScope(e.target.value);
              setPage(0);
              setSelected(null);
            }}
          >
            <option value="all">Все чаты</option>
            <option value="group">Группы</option>
            <option value="private">Личные</option>
          </select>
        </label>
        <button onClick={() => setRevision((r) => r + 1)}>
          Обновить список
        </button>
      </div>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {!list ? (
        <p className="muted">
          {error ? "Журнал недоступен." : "Загружаем журнал…"}
        </p>
      ) : list.items.length === 0 ? (
        <section className="card">
          <h3>Диалогов пока нет</h3>
          <p>Напиши Заре в разрешённой личке или обратись к ней в группе.</p>
        </section>
      ) : (
        <div className="dialogue-list">
          {list.items.map((item) => (
            <article className="card dialog-item" key={item.id}>
              <div className="dialogue-row">
                <span className="card-label">
                  №{item.id} ·{" "}
                  {item.chat_id.startsWith("-") ? "Группа" : "Личка"}{" "}
                  {item.chat_id}
                  {item.thread_id ? ` · топик ${item.thread_id}` : ""}
                </span>
                <span className="pill">{states[item.state] ?? item.state}</span>
              </div>
              <p className="dialogue-preview">
                {item.invalidated
                  ? "Содержимое очищено после изменения или удаления исходных данных"
                  : item.input || "Без текста"}
              </p>
              <div className="dialogue-row">
                <small>
                  {triggers[item.trigger] ?? item.trigger} ·{" "}
                  {new Date(item.created_at).toLocaleString("ru-RU")}
                  {item.mode !== "live" ? " · ТЕСТ" : ""}
                </small>
                <button
                  aria-expanded={selected === item.id}
                  disabled={busy}
                  onClick={() =>
                    setSelected(selected === item.id ? null : item.id)
                  }
                >
                  {selected === item.id ? "Свернуть" : "Подробности"}
                </button>
              </div>
              {selected === item.id &&
                (detail ? (
                  <div className="dialog-trace">
                    {detail.snapshot.invalidated && (
                      <p className="notice">
                        Содержимое этой записи очищено: исходные данные изменились
                        или стали недоступны. Это прежняя запись журнала, а не новый
                        пустой запрос. Учёт вызовов и расходов сохранён.
                      </p>
                    )}
                    <section>
                      <h3>01 · Вход и триггер</h3>
                      <p>
                        {localQuote ? "Дословная цитата из реплая" : (triggers[detail.trigger] ?? detail.trigger)}.
                        Отправитель {detail.snapshot.incoming.sender_id} ·
                        сообщение {detail.snapshot.incoming.message_id}
                      </p>
                      <p className="dialogue-text">
                        {detail.snapshot.current_messages
                          ? `${detail.snapshot.current_messages.length} сообщений в серии`
                          : detail.snapshot.incoming.text}
                      </p>
                      {detail.snapshot.current_messages?.map((m) => (
                        <div className="context-message" key={m.event_id}>
                          <small>Сообщение {m.message_id}</small>
                          <p className="dialogue-text">{m.text}</p>
                        </div>
                      ))}
                    </section>
                    <section>
                      <h3>02 · Контекст</h3>
                      <p className="muted">
                        Настройки v{detail.snapshot.settings_version} ·{" "}
                        {detail.snapshot.owner
                          ? "Владелец подтверждён по ID"
                          : "Обычный собеседник"}{" "}
                        · элементов контекста: {detail.snapshot.context.length}
                      </p>
                      <ContextBudgetDiagnostics
                        budget={detail.snapshot.context_budget}
                        recorded={detail.mode === "recorded"}
                      />
                      {detail.snapshot.participant_context ? (
                        <details className="timing-diagnostics">
                          <summary>Состав персональных окон</summary>
                          <p className="muted">
                            Предел: {detail.snapshot.participant_context.limit}{" "}
                            человеческих сообщений на человека. Текущее
                            сообщение{" "}
                            {detail.snapshot.participant_context
                              .current_included
                              ? "входит в предел и передано отдельно один раз"
                              : "не расходует человеческое окно"}
                            . Ответы Зари учитываются отдельно.
                          </p>
                          {detail.snapshot.participant_context.participants.map(
                            (p) => (
                              <div
                                className="context-message"
                                key={p.sender_id}
                              >
                                <p>
                                  {p.name} · {p.sender_id}
                                </p>
                                <small>
                                  {p.reason === "speaker"
                                    ? "Текущий собеседник"
                                    : "Цепочка ответов"}
                                </small>
                                <dl className="dialogue-metrics">
                                  <div>
                                    <dt>Человеческих в окне / доступно</dt>
                                    <dd>
                                      {p.human_messages} / {p.available}
                                    </dd>
                                  </div>
                                  <div>
                                    <dt>Связанных ответов Зари</dt>
                                    <dd>{p.assistant_messages}</dd>
                                  </div>
                                </dl>
                                {p.summaries ? (
                                  <PersonalSummaries
                                    data={p.summaries}
                                    current={
                                      detail.snapshot.participant_context!
                                        .current_included &&
                                      p.reason === "speaker"
                                        ? 1
                                        : 0
                                    }
                                  />
                                ) : (
                                  <p className="muted">
                                    Покрытие сводками не сохранено.
                                  </p>
                                )}
                              </div>
                            ),
                          )}
                          {detail.snapshot.participant_context
                            .reply_chain_truncated && (
                            <p className="muted">
                              Участники выбраны по первым шести шагам цепочки
                              ответов.
                            </p>
                          )}
                        </details>
                      ) : (
                        <p className="muted">
                          Диагностика персональных окон не сохранена.
                        </p>
                      )}
                      {!!detail.snapshot.topic_context?.length && (
                        <details>
                          <summary>Хронология топика</summary>
                          {detail.snapshot.topic_context.map((m) => (
                            <div className="context-message" key={m.event_id}>
                              <small>
                                {m.name} · сообщение {m.message_id} ·{" "}
                                {new Date(m.received_at * 1000).toLocaleString()}
                                {m.forwarded ? " · пересылка" : ""}
                              </small>
                              <p className="dialogue-text">{m.text}</p>
                            </div>
                          ))}
                        </details>
                      )}
                      <details>
                        <summary>Показать использованные сообщения</summary>
                        {detail.snapshot.context.map((m, i) => (
                          <div className="context-message" key={i}>
                            <small>
                              {m.role === "assistant" ? "Заря" : m.name} ·{" "}
                              {m.sender_id} ·{" "}
                              {m.role === "summary"
                                ? `сводка блока ${m.block_id}, источники ${m.source_ids?.join(", ")}`
                                : `сообщение ${m.message_id}`}
                              {m.thread_id ? ` · топик ${m.thread_id}` : ""}
                            </small>
                            <p className="dialogue-text">{m.text}</p>
                          </div>
                        ))}
                        {!detail.snapshot.context.length && (
                          <p>Контекст пока пуст.</p>
                        )}
                      </details>
                      {detail.snapshot.retrieval && (
                        <details>
                          <summary>
                            Подбор памяти ·{" "}
                            {detail.snapshot.retrieval.mode === "graph"
                              ? "графовый"
                              : detail.snapshot.retrieval.mode === "shadow"
                                ? "теневое сравнение"
                                : "прежний"}
                          </summary>
                          <p className="muted">
                            {detail.snapshot.retrieval.mode === "shadow"
                              ? "Ответ использует прежний подбор. Графовый рассчитан для сравнения, без второго запроса к модели."
                              : detail.snapshot.retrieval.mode === "graph"
                                ? "В ответ передан графовый подбор; при ошибке используется прежний."
                                : "В ответ передан прежний подбор."}
                          </p>
                          <p>
                            Проверенные кандидаты:{" "}
                            {detail.snapshot.retrieval.candidates.facts} фактов,{" "}
                            {detail.snapshot.retrieval.candidates.episodes}{" "}
                            эпизодов. Загрузка:{" "}
                            {detail.snapshot.retrieval.load_ms} мс.
                          </p>
                          {[
                            [
                              "Прежний",
                              detail.snapshot.retrieval.legacy,
                              detail.snapshot.retrieval.legacy_ms,
                            ],
                            [
                              "Графовый",
                              detail.snapshot.retrieval.graph,
                              detail.snapshot.retrieval.graph_ms,
                            ],
                          ].map(([label, raw, latency]) => {
                            const selection = raw as
                              | typeof detail.snapshot.retrieval.legacy
                              | undefined;
                            return (
                              selection && (
                                <div
                                  className="context-message"
                                  key={label as string}
                                >
                                  <strong>{label as string}</strong>
                                  <p>
                                    Фактов: {selection.facts.length} · эпизодов:{" "}
                                    {selection.episodes.length} · спорных пар:{" "}
                                    {selection.conflicts.length} · реплаев:{" "}
                                    {selection.reply_messages.length} ·{" "}
                                    {selection.chars} символов ·{" "}
                                    {latency as number} мс
                                  </p>
                                  <p className="muted">
                                    Факты:{" "}
                                    {selection.facts
                                      .map((f) => `#${f.id} v${f.version}`)
                                      .join(", ") || "нет"}
                                    . Эпизоды:{" "}
                                    {selection.episodes
                                      .map((e) => `#${e.id}`)
                                      .join(", ") || "нет"}
                                    .
                                  </p>
                                  <details>
                                    <summary>Причины выбора</summary>
                                    {selection.facts.map((f) => (
                                      <p key={`f:${f.id}`} className="muted">
                                        Факт #{f.id}:{" "}
                                        {f.reasons
                                          .map((r) => retrievalReasons[r] ?? r)
                                          .join(", ")}
                                      </p>
                                    ))}
                                    {selection.episodes.map((e) => (
                                      <p key={`e:${e.id}`} className="muted">
                                        Эпизод #{e.id}:{" "}
                                        {e.reasons
                                          .map((r) => retrievalReasons[r] ?? r)
                                          .join(", ")}
                                      </p>
                                    ))}
                                  </details>
                                </div>
                              )
                            );
                          })}
                          {detail.snapshot.retrieval.fallback && (
                            <p role="status">
                              Использован прежний подбор:{" "}
                              {detail.snapshot.retrieval.capped
                                ? "достигнут лимит связей"
                                : "графовый расчёт недоступен"}
                              .
                            </p>
                          )}
                          <p className="muted">
                            {detail.snapshot.retrieval.algorithm} · эпоха{" "}
                            {detail.snapshot.retrieval.epoch}. Метаданные
                            выбора; скрытые рассуждения модели здесь не
                            записываются.
                          </p>
                        </details>
                      )}
                      {detail.snapshot.avatar_profile && (
                        <details>
                          <summary>Аватарки выбранного собеседника</summary>
                          <p>
                            {detail.snapshot.avatar_profile.name} · Telegram ID{" "}
                            {detail.snapshot.avatar_profile.user_id ||
                              "не определён"}
                          </p>
                          <p>
                            Доступно при проверке:{" "}
                            {detail.snapshot.avatar_profile.available_count ??
                              "неизвестно"}{" "}
                            · разобраны №{" "}
                            {detail.snapshot.avatar_profile.analysed_indices.join(
                              ", ",
                            ) || "нет"}
                          </p>
                          <p className="muted">
                            Статичные изображения профиля.{" "}
                            {detail.snapshot.avatar_profile.has_more
                              ? "Есть ещё фотографии."
                              : ""}{" "}
                            {detail.snapshot.avatar_profile.list_changed
                              ? "Список изменился с предыдущего запроса."
                              : ""}{" "}
                            {detail.snapshot.avatar_profile.listing_truncated
                              ? "Список получен не полностью."
                              : ""}
                          </p>
                          {detail.snapshot.avatar_profile.observations?.descriptions.map(
                            (text, i) => (
                              <p className="dialogue-text" key={i}>
                                №{" "}
                                {
                                  detail.snapshot.avatar_profile!
                                    .analysed_indices[i]
                                }
                                : {text}
                              </p>
                            ),
                          )}
                          {detail.snapshot.avatar_profile.observations
                            ?.uncertainty && (
                            <p>
                              {
                                detail.snapshot.avatar_profile.observations
                                  .uncertainty
                              }
                            </p>
                          )}
                          {detail.snapshot.avatar_profile.error && (
                            <p>
                              Результат недоступен:{" "}
                              {detail.snapshot.avatar_profile.error}
                            </p>
                          )}
                        </details>
                      )}
                    </section>
                    <section>
                      <h3>03 · Модель и расход</h3>
                      <Metrics detail={detail} />
                    </section>
                    <TimingDiagnostics timing={detail.timing} />
                    <section>
                      <h3>04 · Части ответа и доставка</h3>
                      {detail.error === "unknown_source" && (
                        <p className="notice">
                          Ответ модели отклонён: ссылка или номер источника не
                          совпали с результатами поиска. Повторного запроса к
                          модели нет. Если подготовлено уведомление с найденными
                          источниками, его текст и статус доставки показаны ниже.
                        </p>
                      )}
                      <p className="muted">
                        Индикатор «печатает»: последний запрос —{" "}
                        {states[detail.typing.state] ?? detail.typing.state}.
                        Попыток: {detail.typing.attempts}
                        {detail.typing.error ? ` · ${detail.typing.error}` : ""}
                        . Принятие API не гарантирует отображение в клиенте
                        Telegram.
                      </p>
                      {detail.parts.map((part) => (
                        <div className="context-message" key={part.index}>
                          <small>
                            {detail.error === "unknown_source"
                              ? "Служебное уведомление"
                              : `Часть ${part.index + 1}`} ·{" "}
                            {detail.snapshot.current_messages && part.reply_id
                              ? `ответ на ${part.reply_id} · `
                              : ""}
                            {states[part.state] ?? part.state}
                            {part.error ? ` · ${part.error}` : ""}
                          </small>
                          <p className="dialogue-text">{part.text}</p>
                        </div>
                      ))}
                      {!detail.parts.length && (
                        <p className="muted">
                          Отправок нет.
                          {detail.error === "memory_changed"
                            ? " Память изменилась или больше недоступна. Ответ отменён."
                            : detail.error
                              ? ` Причина: ${detail.error}`
                              : ""}
                        </p>
                      )}
                    </section>
                    <section className="replay-area">
                      <h3>Тестовая область</h3>
                      <p>
                        {localQuote
                          ? "Цитата копируется напрямую. Для проверки отправьте новый реплай в Telegram."
                          : "Отправка в Telegram и запись в рабочую память отключены. Новый платный запрос использует текущие настройки и сохранённый контекст."}
                      </p>
                      <div className="peer-actions">
                        <button
                          disabled={busy || !detail.response || localQuote}
                          onClick={() => void replay("recorded")}
                        >
                          Проверить сохранённый ответ
                        </button>
                        <button
                          disabled={busy || !list.configured || detail.snapshot.invalidated || localQuote}
                          onClick={() => void preparePaid()}
                          aria-expanded={paid}
                        >
                          Новый запрос к модели (платно)
                        </button>
                      </div>
                      <small>
                        Проверка сохранённого ответа не обращается к API.
                      </small>
                      {paid && paidSettings && (
                        <div className="notice">
                          <p>
                            Будет один новый запрос к OpenAI с отдельным учётом
                            расходов. После ответа покажем доступный usage и
                            оценку стоимости; при сбое они могут остаться
                            неизвестными.
                          </p>
                          <p>
                            Модель: {paidSettings.settings.model} · reasoning:{" "}
                            {paidSettings.settings.reasoning} · предел:{" "}
                            {paidSettings.settings.max_output_tokens} токенов ·
                            настройки v{paidSettings.version}
                          </p>
                          <button
                            className="primary"
                            disabled={busy}
                            onClick={() => void replay("paid")}
                          >
                            Выполнить платный запрос
                          </button>
                        </div>
                      )}
                      {replayError && (
                        <p className="notice error" role="alert">
                          {replayError}
                        </p>
                      )}
                      {busy && <p role="status">Выполняем тестовый прогон…</p>}
                      {test && (
                        <div className="replay-result" role="status">
                          <h3>
                            Тест №{test.id} · {states[test.state] ?? test.state}
                          </h3>
                          <Metrics detail={test} />
                          <p className="dialogue-text">
                            {test.response || test.error || "Нет ответа"}
                          </p>
                        </div>
                      )}
                    </section>
                  </div>
                ) : (
                  <p>{detailError || "Загружаем подробности…"}</p>
                ))}
            </article>
          ))}
        </div>
      )}
      {list && (
        <div className="pagination">
          <button
            disabled={busy || page === 0}
            onClick={() => {
              setPage((p) => p - 1);
              setSelected(null);
            }}
          >
            Назад
          </button>
          <span>
            Страница {page + 1} · операций: {list.total}
          </span>
          <button
            disabled={busy || (page + 1) * 20 >= list.total}
            onClick={() => {
              setPage((p) => p + 1);
              setSelected(null);
            }}
          >
            Далее
          </button>
        </div>
      )}
    </div>
  );
}

function Metrics({ detail }: { detail: DialogDetail }) {
  const call = detail.call;
  const absent = call ? "Неизвестно" : "Не применяется";
  return (
    <>
      <p>
        {call?.model ?? "Без вызова модели"}
        {call && !detail.snapshot.invalidated
          ? ` · reasoning ${detail.snapshot.settings.reasoning}`
          : ""}
      </p>
      <dl className="dialogue-metrics">
        <div>
          <dt>Входные токены</dt>
          <dd>{call?.usage?.input_tokens ?? absent}</dd>
        </div>
        <div>
          <dt>Выходные токены</dt>
          <dd>{call?.usage?.output_tokens ?? absent}</dd>
        </div>
        <div>
          <dt>Обработка вызова · с подготовкой</dt>
          <dd>
            {call?.latency_ms == null
              ? absent
              : `${(call.latency_ms / 1000).toFixed(1)} с`}
          </dd>
        </div>
        <div>
          <dt>Оценка стоимости</dt>
          <dd>
            {detail.mode === "recorded" || detail.snapshot.verbatim_quote
              ? "$0 · без API"
              : money(call?.cost_usd)}
          </dd>
        </div>
      </dl>
      {call && (
        <small>
          Тариф: {call.pricing}. Оценка не заменяет счёт провайдера.
          {call.error ? ` Ошибка: ${call.error}` : ""}
        </small>
      )}
    </>
  );
}
