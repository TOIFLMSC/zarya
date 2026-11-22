import type { SummaryCoverage } from "./api";

type Original = {
  message_id: number;
  text: string;
  name: string;
  received_at: number;
};
const states: Record<string, string> = {
  queued: "В очереди",
  started: "Готовится",
  completed: "Готова",
  unknown: "Результат вызова неизвестен",
  error: "Сводка не создана",
  cancelled: "Источники изменились или недоступны",
};
const reasons: Record<string, string> = {
  fresh: "Свежий хвост",
  reply: "Выбранная цепочка ответов",
  protected: "Точные детали",
  boundary: "Граница окна",
  failed: "Использован оригинал после ошибки или изменения источника",
  pending: "Сводка ещё не готова",
};
const calls: Record<string, string> = {
  started: "Выполняется",
  completed: "Завершён",
  unknown: "Результат неизвестен",
  cancelled: "Отменён",
  failed: "Ошибка",
  refused: "Отклонён",
  incomplete: "Незавершён",
};

export function PersonalSummaries({
  data,
  messages = [],
  current = 0,
}: {
  data: SummaryCoverage;
  messages?: Original[];
  current?: number;
}) {
  const used = data.blocks.filter((b) => b.used_ids.length);
  return (
    <div className="personal-summaries">
      <dl className="dialogue-metrics">
        <div>
          <dt>Заменено сводками / блоков</dt>
          <dd>
            {data.covered_ids.length} / {used.length}
          </dd>
        </div>
        <div>
          <dt>Оригиналами</dt>
          <dd>{data.original_ids.length + current}</dd>
        </div>
        <div>
          <dt>{current ? "Свежий хвост и текущий вопрос" : "Свежий хвост"}</dt>
          <dd>{data.fresh_ids.length + current}</dd>
        </div>
      </dl>
      <p className="muted">
        Сводки пересказывают реплики, а не подтверждают факты о человеке. Точные
        числа, ссылки, вложения, отрицания и выбранные реплаи сохраняются
        оригиналами. Свежий хвост входит в число оригиналов.
      </p>
      {!data.enabled && (
        <p>Персональные сводки выключены. Используются оригиналы.</p>
      )}
      {!data.blocks.length ? (
        <p className="muted">
          Сводок для этого окна пока нет. Полные блоки ставятся в фоновую
          обработку при обращении к Заре; последние две реплики остаются
          оригиналами.
        </p>
      ) : (
        <details>
          <summary>Сводки в этом окне ({data.blocks.length})</summary>
          {data.blocks.map((b) => (
            <div className="context-message" key={b.id}>
              <strong>
                Блок {b.id} · {states[b.state] ?? b.state}
              </strong>
              <p className="muted">
                {b.manifest.length} исходных сообщений · {b.used_ids.length}{" "}
                заменены сводкой
                {b.manifest.length > 0 &&
                  ` · ${new Date(b.manifest[0].received_at * 1000).toLocaleString("ru-RU")} — ${new Date(b.manifest[b.manifest.length - 1].received_at * 1000).toLocaleString("ru-RU")}`}
              </p>
              {!b.full_window && (
                <p>
                  Блок пересекает границу окна. Используются доступные
                  оригиналы.
                </p>
              )}
              {b.state === "unknown" ? (
                <p>
                  Используются оригиналы; автоматического повторного платного
                  запроса нет.
                </p>
              ) : (
                !b.used_ids.length && (
                  <p className="muted">
                    Пока используются оригиналы: сводка не готова, выключена или
                    не сокращает этот участок окна.
                  </p>
                )
              )}
              {b.summary && <p className="dialogue-text">{b.summary}</p>}
              {messages.length > 0 && (
                <details>
                  <summary>Оригиналы блока</summary>
                  {messages
                    .filter((m) =>
                      b.manifest.some((r) => r.message_id === m.message_id),
                    )
                    .map((m) => (
                      <div className="context-message" key={m.message_id}>
                        <small>
                          {m.name} · сообщение {m.message_id}
                        </small>
                        <p className="dialogue-text">{m.text}</p>
                      </div>
                    ))}
                </details>
              )}
              <details>
                <summary>Обработка и расход</summary>
                <p className="muted">
                  Создание {new Date(b.created_at).toLocaleString("ru-RU")} ·
                  настройки {b.settings_version ?? "не назначены"}
                </p>
                <dl className="dialogue-metrics">
                  <div>
                    <dt>Модель / вызов</dt>
                    <dd>
                      {b.model ?? "не назначена"} ·{" "}
                      {b.call_state
                        ? (calls[b.call_state] ?? b.call_state)
                        : "не начат"}
                    </dd>
                  </div>
                  <div>
                    <dt>Время модели</dt>
                    <dd>
                      {b.latency_ms == null
                        ? "Неизвестно"
                        : `${(b.latency_ms / 1000).toFixed(2)} с`}
                    </dd>
                  </div>
                  <div>
                    <dt>Расход на создание</dt>
                    <dd>
                      {b.cost_usd == null
                        ? "Неизвестно"
                        : `$${b.cost_usd.toFixed(6)}`}
                    </dd>
                  </div>
                </dl>
                {b.usage && (
                  <pre className="dialogue-text">
                    {JSON.stringify(b.usage, null, 2)}
                  </pre>
                )}
                {b.error_code && <p className="muted">Код: {b.error_code}</p>}
              </details>
            </div>
          ))}
        </details>
      )}
      {messages.length > 0 && (
        <details>
          <summary>
            Оригиналы без сводки · {data.original_ids.length} сообщений
          </summary>
          {messages
            .filter((m) => data.original_ids.includes(m.message_id))
            .map((m) => (
              <div className="context-message" key={m.message_id}>
                <small>
                  {m.name} · сообщение {m.message_id} ·{" "}
                  {reasons[data.raw_reasons?.[m.message_id]] ?? "Оригинал"}
                </small>
                <p className="dialogue-text">{m.text}</p>
              </div>
            ))}
        </details>
      )}
    </div>
  );
}
