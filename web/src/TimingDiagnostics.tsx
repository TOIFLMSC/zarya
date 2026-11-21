export type Timing = {
  stages: Partial<Record<string, number>>;
  attempts: number;
  status: "historical" | "pending" | "partial" | "complete" | "test";
};
type Metric = { samples: number; avg_ms: number | null; p95_ms: number | null };
export type TimingSummary = {
  operations: number;
  measured: number;
  historical: number;
  partial: number;
  retried: number;
  stages: Record<string, Metric>;
};
const labels: Record<string, string> = {
  queue_ms: "Очередь и ожидание материалов",
  claim_ms: "Сбор запроса и контекста",
  context_ms: "Подбор памяти · внутри сбора контекста",
  prepare_ms: "Подготовка и проверки",
  model_ms: "Вызов API модели",
  postprocess_ms: "Обработка результата API",
  finish_wait_ms: "Ожидание сохранения",
  finish_work_ms: "Подготовка записи ответа",
  outbox_wait_ms: "Ожидание отправки",
  delivery_check_ms: "Проверки перед доставкой",
  telegram_ms: "Отправка в Telegram",
  result_write_ms: "Подготовка записи доставки",
};
export function timingDuration(ms: number | null | undefined) {
  if (ms == null) return "Нет измерения";
  if (ms === 0) return "0 с";
  if (ms < 100) return "< 0,1 с";
  return `${(ms / 1000).toFixed(1).replace(".", ",")} с`;
}
const notes = {
  historical: "Для этой записи этапы времени не измерялись",
  pending: "Измерения ещё не завершены",
  partial: "Частичные измерения. Итог не восстанавливается",
  complete: "Измерения до первой части завершены",
  test: "Тестовый прогон: без отправки в Telegram",
};
function Help() {
  return (
    <p className="muted">
      До принятия первой части API Telegram. Это не подтверждение отображения
      ответа у собеседника. Итог измеряется отдельно и не вычисляется сложением
      этапов. Вызов API включает работу адаптера и сеть, без подготовки запроса.
      Запись измеряется до commit. При смене процесса или скачке часов некоторые
      интервалы недоступны.
    </p>
  );
}
export function TimingDiagnostics({ timing }: { timing: Timing }) {
  return (
    <section className="timing-diagnostics">
      <h3>Время ответа</h3>
      <dl className="dialogue-metrics">
        <div>
          <dt>До первой части</dt>
          <dd>{timingDuration(timing.stages.total_first_reply_ms)}</dd>
        </div>
      </dl>
      <p className="muted">{notes[timing.status]}</p>
      {timing.attempts > 1 && (
        <p className="muted">
          Были повторные попытки · {timing.attempts}. Проверки, отправка и
          запись доставки суммируются по попыткам; ожидание - до последней
          попытки.
        </p>
      )}
      <details>
        <summary>Показать этапы времени</summary>
        <dl className="dialogue-metrics">
          {Object.entries(labels).map(([key, label]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>{timingDuration(timing.stages[key])}</dd>
            </div>
          ))}
        </dl>
        <Help />
      </details>
    </section>
  );
}
export function TimingOverview({
  timing,
  expanded,
  onExpand,
}: {
  timing: TimingSummary;
  expanded: boolean;
  onExpand: (open: boolean) => void;
}) {
  const total = timing.stages.total_first_reply_ms;
  function metric(value: Metric | undefined) {
    return value?.samples
      ? `${timingDuration(value.avg_ms)} / ${timingDuration(value.p95_ms)} · N=${value.samples}`
      : "Нет измерений · N=0";
  }
  return (
    <section className="card timing-diagnostics">
      <h2>Время текстовых ответов</h2>
      <dl className="dialogue-metrics">
        <div>
          <dt>До первой части · среднее / P95</dt>
          <dd>{metric(total)}</dd>
        </div>
        <div>
          <dt>Операций с измерениями / всего</dt>
          <dd>
            {timing.measured} / {timing.operations}
          </dd>
        </div>
        <div>
          <dt>Неполные / исторические записи</dt>
          <dd>
            {timing.partial} / {timing.historical}
          </dd>
        </div>
        <div>
          <dt>С повторными попытками доставки</dt>
          <dd>{timing.retried}</dd>
        </div>
      </dl>
      <p className="muted">
        Только живые текстовые диалоги за выбранный период, область и чат.
        Тестовые прогоны и реакции исключены. У каждого этапа своя выборка N.
        Проверки, отправка и запись доставки включают все её попытки.
      </p>
      {!timing.measured && (
        <p className="muted">
          За выбранный период новых текстовых операций с измерениями нет
        </p>
      )}
      <details
        open={expanded}
        onToggle={(event) => onExpand(event.currentTarget.open)}
      >
        <summary>Этапы времени · среднее / P95</summary>
        <dl className="dialogue-metrics">
          {Object.entries(labels).map(([key, label]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>{metric(timing.stages[key])}</dd>
            </div>
          ))}
        </dl>
        <Help />
      </details>
    </section>
  );
}
