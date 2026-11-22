import type { ContextBudget } from "./api";

const states: Record<string, string> = {
  preparing: "Подготовка изображений",
  fits_estimate: "Помещается по локальной оценке",
  fits_api: "Помещается по подсчёту API",
  needs_count: "Требуется уточняющий подсчёт",
  unverified: "Вместимость не подтверждена",
  overflow: "Контекст превышает доступный бюджет",
  provider_overflow: "API отклонил запрос: превышен лимит контекста",
};
const categories: Record<string, string> = {
  instructions: "Инструкции",
  current_message: "Текущее сообщение",
  current_messages: "Текущая серия",
  topic_context: "Хронология топика",
  task_state: "Состояние задачи",
  recent_originals: "Недавние реплики · оригиналы",
  recent_summaries: "Недавние реплики · сводки",
  assistant_replies: "Связанные ответы Зари",
  memory: "Долговременная память",
  material: "Материалы сообщения",
  research: "Результаты поиска",
  behavior: "Данные поведения",
  avatar: "Выбранный профиль",
  images: "Изображения",
  schema_tools: "Формат ответа и инструменты",
  other_input: "Дополнительный вход",
};
const countStates: Record<string, string> = {
  not_needed: "Не выполнялся",
  not_started: "Не выполнялся",
  started: "Выполняется",
  unknown: "Результат неизвестен",
  unavailable: "Недоступен",
  rejected: "Не выполнен",
  overflow: "API подтвердил переполнение",
  completed: "Завершён",
};
const number = (n: number | null | undefined) =>
  n == null ? "Неизвестно" : n.toLocaleString("ru-RU");

export function ContextBudgetDiagnostics({
  budget,
  recorded,
}: {
  budget: ContextBudget | undefined;
  recorded: boolean;
}) {
  if (!budget)
    return (
      <p className="muted">
        Проверка вместимости для этой операции не сохранена.
      </p>
    );
  const stopped = budget.decision === "blocked";
  const sections = new Map<
    string,
    { count: number; bytes: number | null; estimate: number }
  >();
  for (const s of budget.sections) {
    const previous = sections.get(s.kind);
    sections.set(s.kind, {
      count: (previous?.count ?? 0) + s.count,
      bytes: s.bytes == null ? null : (previous?.bytes ?? 0) + s.bytes,
      estimate: (previous?.estimate ?? 0) + s.estimate,
    });
  }
  return (
    <div>
      {stopped && (
        <p className="notice error">
          Запрос генерации не отправлен. Обязательный контекст не сокращался для
          подгонки под лимит.
        </p>
      )}
      {budget.state === "provider_overflow" && (
        <p className="notice error">
          Запрос генерации был отправлен, но API отклонил его. Автоматического
          платного повторения нет.
        </p>
      )}
      <details className="timing-diagnostics">
        <summary>
          Вместимость контекста · {states[budget.state] ?? budget.state}
        </summary>
        <p className="muted">
          {budget.model} · профиль {budget.profile}
          {budget.profile_source && (
            <>
              {" "}
              ·{" "}
              <a href={budget.profile_source} target="_blank" rel="noreferrer">
                Документация модели
              </a>
            </>
          )}
        </p>
        {recorded && (
          <p className="muted">
            Проверка исходного запроса. Сохранённый повтор не вызывает API.
          </p>
        )}
        {budget.stage === "assembled" && (
          <p className="muted">
            Предварительная сборка. Перед генерацией проверяется окончательный
            запрос, включая изображения.
          </p>
        )}
        <dl className="dialogue-metrics">
          <div>
            <dt>Окно модели</dt>
            <dd>{number(budget.context_limit)} токенов</dd>
          </div>
          <div>
            <dt>Резерв на ответ, включая рассуждения</dt>
            <dd>{number(budget.output_reserve)} токенов</dd>
          </div>
          <div>
            <dt>Служебный запас</dt>
            <dd>{number(budget.safety_margin)} токенов</dd>
          </div>
          <div>
            <dt>Доступно для входа</dt>
            <dd>{number(budget.input_budget)} токенов</dd>
          </div>
          <div>
            <dt>Локальная консервативная оценка</dt>
            <dd>≈ {number(budget.estimate)} токенов</dd>
          </div>
          <div>
            <dt>Подсчёт API</dt>
            <dd>
              {budget.api_count.tokens == null
                ? (countStates[budget.api_count.state] ??
                  budget.api_count.state)
                : `${number(budget.api_count.tokens)} токенов`}
            </dd>
          </div>
        </dl>
        <p className="muted">
          Приблизительная оценка собранного запроса; не точный токенизатор
          модели. Изображения учитываются с большим инженерным запасом, размер
          base64 не считается текстом. Уточняющий подсчёт нужен только для
          большого или неизвестного входа. Фактический usage показан отдельно в
          «Модель и расход».
        </p>
        {budget.api_count.attempted && (
          <p className="muted">
            Подсчёт API — отдельное обращение, без генерации ответа. Его
            стоимость не подтверждена. Время:{" "}
            {budget.api_count.latency_ms == null
              ? "неизвестно"
              : `${(budget.api_count.latency_ms / 1000).toFixed(2)} с`}
            .{budget.api_count.error && ` Код: ${budget.api_count.error}`}
          </p>
        )}
        <details>
          <summary>Состав входа</summary>
          {[...sections.entries()].map(([kind, s]) => (
            <div className="context-message" key={kind}>
              <strong>{categories[kind] ?? kind}</strong>
              <p className="muted">
                {kind.startsWith("recent_") || kind === "assistant_replies" || kind === "topic_context"
                  ? "Реплик"
                  : kind === "images"
                    ? "Изображений"
                    : "Элементов"}: {s.count}{" "}
                · ≈ {number(s.estimate)} токенов
                {s.bytes != null && ` · ${number(s.bytes)} байт текста`}
              </p>
            </div>
          ))}
        </details>
        {budget.excluded.length ? (
          <details>
            <summary>Исключённые элементы</summary>
            {budget.excluded.map((e, index) => (
              <p className="muted" key={index}>
                {categories[e.kind] ?? e.kind} · {e.count} ·{" "}
                {e.reason === "image_byte_limit"
                  ? "Лимит размера файлов; остальные изображения не переданы"
                  : e.reason}
              </p>
            ))}
          </details>
        ) : (
          <p className="muted">
            На этапе сборки элементы выбранного контекста не исключались. Подбор
            памяти и персональных окон показан в отдельных блоках.
          </p>
        )}
      </details>
    </div>
  );
}
