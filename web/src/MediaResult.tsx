import { type MediaDetail } from "./api";
const states: Record<string, string> = {
  queued: "В очереди",
  preparing: "Загрузка и подготовка",
  transcribing: "Распознавание речи",
  analyzing: "Анализ кадров",
  completed: "Завершено",
  partial: "Частичный результат",
  unavailable: "Недоступно",
  unknown: "Результат неизвестен",
  cancelled: "Отменено",
  rejected: "Отклонено",
  started: "Выполняется",
  invalid: "Некорректный результат",
};
const time = (s: number) =>
  `${Math.floor(s / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(s % 60)
    .toString()
    .padStart(2, "0")}`;
const money = (n: number | null | undefined) =>
  n == null ? "Неизвестно" : `$${n.toFixed(6)}`;
const limitations: Record<string, string> = {
  file_size: "Файл превышает 20 МиБ",
  duration_limit: "Ролик превышает установленную длительность",
  request_media_limit: "В одном запросе обрабатывается до трёх вложений",
  tools_unavailable: "FFmpeg или ffprobe недоступен",
  not_configured: "Модель не подключена",
  preparation_failed: "Не удалось подготовить файл для распознавания",
  interrupted:
    "Обработка прервана; платный запрос автоматически не повторяется",
  processing_uncertain: "Результат обработки неизвестен",
  source_changed: "Источник изменён или доступ отозван",
  low_audio_signal: "Очень тихая дорожка; речь не распознавалась",
  missing_audio: "Аудиодорожки нет",
  asr_unknown: "Результат распознавания речи неизвестен",
  vision_unknown: "Результат анализа кадров неизвестен",
  asr_rejected: "Распознавание речи отклонено",
  vision_rejected: "Анализ кадров отклонён",
  invalid_observation: "Не удалось прочитать результат анализа кадров",
  analysis_context_changed: "Вопрос изменён или удалён; прежний анализ скрыт",
};

export function MediaResult({ detail }: { detail: MediaDetail }) {
  const limitText = (code: string) =>
    code
      .split(",")
      .map((c) => limitations[c] ?? c)
      .join("; ");
  return (
    <>
      <p className="muted">
        {detail.scope === "private" ? "Личка" : "Группа"} · {detail.chat_id} ·{" "}
        {states[detail.state] ?? detail.state}
      </p>
      {detail.scope === "private" && (
        <p className="notice">
          Личный разговор. Сведения не переносятся в группы без разрешения
          собеседника.
        </p>
      )}
      <h3>Что обработано</h3>
      <p>
        Длительность:{" "}
        {detail.duration == null ? "Неизвестно" : time(detail.duration)} ·
        кадров: {detail.frames.length}
      </p>
      <p>
        Речь:{" "}
        {detail.coverage.audio_intervals?.length
          ? detail.coverage.audio_intervals
              .map(([a, b]) => `${time(a)}–${time(b)}`)
              .join(", ")
          : "Покрытие речи пока недоступно"}
      </p>
      {detail.coverage.speech === "empty_transcript" && (
        <p className="muted">Модель вернула пустую расшифровку.</p>
      )}
      {detail.coverage.audio_signal === "missing" && (
        <p className="muted">Аудиодорожки нет.</p>
      )}
      {detail.coverage.note && <p className="muted">{detail.coverage.note}</p>}
      {!detail.available && (
        <p className="notice">
          Источник недоступен или разбор выключен. Содержимое скрыто.
        </p>
      )}
      {detail.error && <p className="notice">{limitText(detail.error)}</p>}
      {detail.transcript != null && (
        <>
          <h3>Расшифровка речи</h3>
          <p className="research-text">
            {detail.transcript || "Речь не распознана."}
          </p>
        </>
      )}
      {detail.frames.length > 0 && (
        <details>
          <summary>Выбранные кадры · {detail.frames.length}</summary>
          <div
            className={`photo-grid ${detail.frames.length === 1 ? "media-single-frame" : ""}`}
          >
            {detail.frames.map((frame, i) => (
              <figure key={frame.url}>
                <img
                  className="photo-image"
                  src={frame.url}
                  alt={`Кадр ${i + 1}, отметка ${time(frame.seconds)}`}
                />
                <figcaption>
                  <a
                    className="media-frame-link"
                    href={frame.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Открыть кадр {i + 1} · {time(frame.seconds)} ↗
                  </a>
                </figcaption>
              </figure>
            ))}
          </div>
        </details>
      )}
      {detail.result && (
        <>
          {(
            [
              ["Наблюдения", detail.result.observations],
              ["Текст в кадрах", detail.result.visible_text],
              ["Интерпретация", detail.result.interpretation],
              ["Неопределённость", detail.result.uncertainty],
              ["Краткое содержание речи", detail.result.speech_summary],
              ["Ответ на исходный вопрос", detail.result.question_answer],
            ] as const
          ).map(
            ([title, value]) =>
              value && (
                <div key={title}>
                  <h3>{title}</h3>
                  <p className="research-text">{value}</p>
                </div>
              ),
          )}
          {!!detail.result.timeline?.length && (
            <details>
              <summary>
                События по выбранным кадрам · {detail.result.timeline.length}
              </summary>
              <ol>
                {detail.result.timeline.map((event, i) => (
                  <li key={i}>
                    <strong>{time(event.seconds)}</strong>
                    <p className="research-text">{event.visual}</p>
                  </li>
                ))}
              </ol>
              <p className="muted">
                Между выбранными кадрами могут быть события, которые не попали в
                анализ.
              </p>
            </details>
          )}
        </>
      )}
      <details className="operation-details">
        <summary>Операции и расход</summary>
        {["asr", "vision"].map((operation) => {
          const call = detail.calls.find((c) => c.operation === operation);
          return (
            <article className="research-source" key={operation}>
              <h4>
                {operation === "asr" ? "Распознавание речи" : "Анализ кадров"}
              </h4>
              {call ? (
                <>
                  <p>
                    {call.model} · {states[call.state] ?? call.state}
                  </p>
                  <p>
                    Токены: {call.usage?.input_tokens ?? "Неизвестно"} вход /{" "}
                    {call.usage?.output_tokens ?? "Неизвестно"} выход
                  </p>
                  <p>
                    Длительность:{" "}
                    {call.latency_ms == null
                      ? "Неизвестно"
                      : `${call.latency_ms} мс`}{" "}
                    · оценка: {money(call.cost_usd)}
                  </p>
                  {call.error && <p className="muted">Код: {call.error}</p>}
                </>
              ) : (
                <p className="muted">
                  {[
                    "queued",
                    "preparing",
                    "transcribing",
                    "analyzing",
                  ].includes(detail.state)
                    ? "Вызов пока не зарегистрирован."
                    : "Не выполнялось."}
                </p>
              )}
            </article>
          );
        })}
        <p className="muted">
          Оценка по возвращённому usage, не подтверждённое списание. Финальный
          ответ учитывается отдельно в «Диалогах». Повторного платного разбора
          при обновлении страницы нет.
        </p>
      </details>
    </>
  );
}
