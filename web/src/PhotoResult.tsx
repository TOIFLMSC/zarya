import { useState } from "react";
import { type PhotoDetail } from "./api";
const states: Record<string, string> = {
  queued: "В очереди",
  downloading: "Загружаем фото",
  analyzing: "Анализируем",
  completed: "Анализ готов",
  cache: "Готов · сохранённый результат",
  unknown: "Результат запроса неизвестен",
  error: "Ошибка обработки",
  cancelled: "Отменён",
  invalid: "Неполный анализ",
  rejected: "Запрос отклонён",
};
const money = (value: number | null | undefined) =>
  value == null ? "Неизвестно" : `$${value.toFixed(6)}`;

export function PhotoResult({ detail }: { detail: PhotoDetail }) {
  return (
    <>
      <p>
        {detail.scope === "private" ? "Личная переписка" : "Группа"} · ID{" "}
        {detail.chat_id}
      </p>
      {detail.scope === "private" && (
        <p className="muted">Личный контекст хранится отдельно от групп.</p>
      )}
      <p className="muted">
        {states[detail.state] ?? detail.state} · {detail.model}
        {detail.settings_version != null &&
          ` · настройки v${detail.settings_version}`}
      </p>
      {!detail.available && (
        <div className="notice">
          Исходники и результат скрыты: разрешение чата или сообщение
          изменились.
        </div>
      )}
      <div
        className={`photo-grid ${detail.items.length === 1 ? "photo-grid-single" : ""}`}
      >
        {detail.items.map((item, i) => (
          <figure key={item.id}>
            {item.image_url && (
              <PhotoImage
                url={item.image_url}
                label={`Фото ${i + 1} из ${detail.items.length}`}
              />
            )}
            <figcaption>
              Фото {i + 1} из {detail.items.length}
              {item.width && ` · ${item.width}×${item.height}`}
              {item.caption && <p>{item.caption}</p>}
            </figcaption>
          </figure>
        ))}
      </div>
      {detail.result && (
        <div className="photo-observation">
          <h3>Что видно</h3>
          <p>{detail.result.observations || "Наблюдений нет"}</p>
          <h3>Текст на фото</h3>
          <p>{detail.result.visible_text || "Текст не обнаружен"}</p>
          <h3>Возможная интерпретация</h3>
          <p>{detail.result.interpretation || "Не требуется"}</p>
          <h3>Что не удалось разобрать</h3>
          <p>{detail.result.uncertainty || "Явных сомнений не отмечено"}</p>
        </div>
      )}
      {detail.error && (
        <div className="notice error">
          Обработка не завершилась ({detail.error}). Автоматического повторного
          платного запроса нет.
        </div>
      )}
      <details className="operation-details">
        <summary>Операции и расход</summary>
        {detail.cache_source != null && (
          <p>
            В этой операции API не вызывался. Использован результат #
            {detail.cache_source} из этого же чата.
          </p>
        )}
        {detail.call && (
          <div className="photo-cost">
            <h3>Анализ фото</h3>
            <p>{states[detail.call.state] ?? detail.call.state}</p>
            <p>
              {detail.call.model} · {money(detail.call.cost_usd)} ·{" "}
              {detail.call.latency_ms == null
                ? "Время неизвестно"
                : `${(detail.call.latency_ms / 1000).toFixed(1)} с`}
            </p>
            <p className="muted">
              Вход: {detail.call.usage?.input_tokens ?? "неизвестно"} токенов ·
              выход: {detail.call.usage?.output_tokens ?? "неизвестно"}
              <br />
              Тариф: {detail.call.pricing ?? "неизвестно"}. Стоимость оценочная;
              изображения уже входят во входные токены.
            </p>
          </div>
        )}
      </details>
    </>
  );
}
function PhotoImage({ url, label }: { url: string; label: string }) {
  const [failed, setFailed] = useState(false);
  return failed ? (
    <p className="muted">
      Не удалось загрузить {label.toLowerCase()}. Обнови разбор.
    </p>
  ) : (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      aria-label={`Открыть ${label.toLowerCase()}`}
    >
      <img
        src={url}
        alt={label}
        loading="lazy"
        onError={() => setFailed(true)}
      />
    </a>
  );
}
