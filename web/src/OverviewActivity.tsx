import { useEffect, useId, useState, type PointerEvent } from "react";
import { api, ApiError } from "./api";
import "./overview-activity.css";

const periods = [
  { minutes: 30, label: "30 мин" },
  { minutes: 60, label: "1 ч" },
  { minutes: 180, label: "3 ч" },
  { minutes: 720, label: "12 ч" },
  { minutes: 1440, label: "24 ч" },
] as const;

const attachmentTypes = [
  { kind: "photo", label: "Фото" },
  { kind: "voice", label: "Голосовые" },
  { kind: "video", label: "Видео" },
  { kind: "video_note", label: "Кружки" },
  { kind: "animation", label: "GIF" },
  { kind: "sticker", label: "Стикеры" },
  { kind: "other", label: "Другие" },
] as const;

type AttachmentType = (typeof attachmentTypes)[number]["kind"];
type Point = {
  bucket_time: number;
  time: number;
  end: number;
  requests: number;
  attachments: number;
  attachment_types: Partial<Record<AttachmentType, number>>;
};
type Activity = {
  period_minutes: number;
  bucket_minutes: number;
  minutes: Point[];
  leader: {
    username: string | null;
    name: string | null;
    requests: number;
    cost_usd: number | null;
    unknown_cost_calls: number;
    unassigned_calls: number;
  } | null;
};

const count = new Intl.NumberFormat("ru-RU");
const clock = new Intl.DateTimeFormat("ru-RU", {
  hour: "2-digit",
  minute: "2-digit",
});
const minuteLabel = (time: number) => clock.format(time * 1000);
const day = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
});
const dayLabel = (time: number) => day.format(time * 1000);
const usd = (amount: number) =>
  amount > 0 && amount < 0.000001 ? "< $0.000001" : `$${amount.toFixed(6)}`;

function ActivityChart({
  activity,
  metric,
  title,
  summary,
  empty,
}: {
  activity: Activity;
  metric: "requests" | "attachments";
  title: string;
  summary: string;
  empty: string;
}) {
  const id = useId();
  const [selectedTime, setSelectedTime] = useState<number | null>(null);
  const points = activity.minutes;
  const first = points[0];
  const last = points[points.length - 1];
  const bucketMinutes = activity.bucket_minutes;
  const periodLabel = periods.find(
    ({ minutes }) => minutes === activity.period_minutes,
  )?.label;
  const values = points.map((point) => point[metric]);
  const total = values.reduce((sum, value) => sum + value, 0);
  const peak = Math.max(...values);
  const unit = Math.ceil(Math.max(1, peak) / 3);
  const limit = peak <= 3 ? Math.max(1, peak) : unit * 3;
  const ticks = Array.from(
    { length: limit / unit + 1 },
    (_, i) => limit - i * unit,
  );
  const active = points.findIndex(
    (point) => point.bucket_time === selectedTime,
  );
  const selected = points[active];
  const series =
    metric === "attachments"
      ? attachmentTypes
          .map((type) => ({
            ...type,
            total: points.reduce(
              (sum, point) => sum + (point.attachment_types[type.kind] ?? 0),
              0,
            ),
          }))
          .filter((type) => type.total > 0)
      : [];
  const partial = (point: Point) => point.end - point.time < bucketMinutes * 60;
  const axisTimes = [0, 1, 2, 3].map(
    (part) => first.time + ((last.end - first.time) * part) / 3,
  );
  const crossesDate = dayLabel(first.time) !== dayLabel(last.end);
  const chosenLabel =
    selected &&
    (bucketMinutes === 1
      ? minuteLabel(selected.time)
      : `${activity.period_minutes >= 720 ? `${dayLabel(selected.time)}, ` : ""}${minuteLabel(selected.time)}–${
          dayLabel(selected.time) !== dayLabel(selected.end)
            ? `${dayLabel(selected.end)}, `
            : ""
        }${minuteLabel(selected.end)}`);

  function selectPoint(event: PointerEvent<HTMLDivElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    const ratio = Math.max(
      0,
      Math.min(1, (event.clientX - box.left) / box.width),
    );
    const time = first.time + (last.end - first.time) * ratio;
    setSelectedTime(
      (points.find((point) => point.end > time) ?? last).bucket_time,
    );
  }

  return (
    <section
      className={`activity-card activity-chart activity-chart--${metric}`}
      aria-labelledby={id}
    >
      <h3 id={id}>{title}</h3>
      <div className="activity-chart-total">
        <strong>{count.format(total)}</strong>
        <span>за {periodLabel}</span>
        <span className="activity-chart-resolution">
          {bucketMinutes === 1 ? "По минутам" : `По ${bucketMinutes} мин`}
        </span>
      </div>
      <div className="activity-chart-plot">
        <div className="activity-chart-scale" aria-hidden="true">
          {ticks.map((tick) => (
            <span key={tick} style={{ top: `${(1 - tick / limit) * 100}%` }}>
              {tick}
            </span>
          ))}
        </div>
        <div
          className="activity-chart-canvas"
          role="group"
          tabIndex={0}
          aria-label={`${title}. Всего ${count.format(total)}. Клавиши со стрелками меняют интервал.`}
          onPointerMove={(event) => {
            if (event.pointerType !== "touch") selectPoint(event);
          }}
          onPointerDown={selectPoint}
          onPointerLeave={(event) => {
            if (
              event.pointerType !== "touch" &&
              document.activeElement !== event.currentTarget
            )
              setSelectedTime(null);
          }}
          onFocus={() =>
            setSelectedTime(selected?.bucket_time ?? last.bucket_time)
          }
          onBlur={() => setSelectedTime(null)}
          onKeyDown={(event) => {
            const lastIndex = points.length - 1;
            const previous = active >= 0 ? active : lastIndex;
            const navigation: Record<string, number | null> = {
              ArrowLeft: Math.max(0, previous - 1),
              ArrowRight: Math.min(lastIndex, previous + 1),
              Home: 0,
              End: lastIndex,
              Escape: null,
            };
            const next = navigation[event.key];
            if (next === undefined) return;
            event.preventDefault();
            setSelectedTime(next === null ? null : points[next].bucket_time);
          }}
        >
          {ticks.map((tick) => (
            <i
              key={tick}
              className="activity-chart-gridline"
              style={{ bottom: `${(tick / limit) * 100}%` }}
              aria-hidden="true"
            />
          ))}
          <div
            className="activity-chart-bars"
            aria-hidden="true"
            style={{
              gridTemplateColumns: points
                .map((point) => `minmax(0, ${point.end - point.time}fr)`)
                .join(" "),
            }}
          >
            {values.map((value, index) => (
              <div
                key={points[index].bucket_time}
                className={`activity-chart-slot${partial(points[index]) ? " is-partial" : ""}${active === index ? " is-selected" : ""}`}
              >
                {metric === "requests" && value > 0 && (
                  <i style={{ height: `${(value / limit) * 100}%` }} />
                )}
                {series.map(({ kind }) => {
                  const amount = points[index].attachment_types[kind] ?? 0;
                  return (
                    amount > 0 && (
                      <i
                        key={kind}
                        data-attachment={kind}
                        style={{ height: `${(amount / limit) * 100}%` }}
                      />
                    )
                  );
                })}
              </div>
            ))}
          </div>
        </div>
        <div className="activity-chart-time" aria-hidden="true">
          {axisTimes.map((time, index) => (
            <span key={time}>
              {minuteLabel(time)}
              {crossesDate &&
                (index === 0 || index === axisTimes.length - 1) && (
                  <small>{dayLabel(time)}</small>
                )}
            </span>
          ))}
        </div>
      </div>
      <div role="status">
        <div className="activity-chart-caption">
          {selected ? (
            <>
              <strong>{chosenLabel}</strong>
              <span>
                {count.format(selected[metric])} за{" "}
                {bucketMinutes === 1 ? "минуту" : "интервал"}
              </span>
              {partial(selected) && <span>Неполный интервал</span>}
            </>
          ) : total ? (
            summary
          ) : (
            empty
          )}
        </div>
        {series.length > 0 && (
          <ul
            className="activity-legend"
            aria-label={
              selected ? "Типы вложений в интервале" : "Типы вложений за период"
            }
          >
            {series.map(({ kind, label, total: typeTotal }) => (
              <li key={kind}>
                <i data-attachment={kind} aria-hidden="true" />
                <span>{label}</span>
                <strong>
                  {count.format(
                    selected
                      ? (selected.attachment_types[kind] ?? 0)
                      : typeTotal,
                  )}
                </strong>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

export function OverviewActivity({
  onUnauthorized,
}: {
  onUnauthorized: () => void;
}) {
  const [activity, setActivity] = useState<Activity | null>(null);
  const [period, setPeriod] = useState(60);
  const [failedPeriod, setFailedPeriod] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  const periodName = useId();
  useEffect(() => {
    let mounted = true;
    let timer: number | undefined;
    async function refresh() {
      try {
        const loaded = await api<Activity>(
          `overview/activity?period=${period}`,
        );
        if (!mounted) return;
        setActivity(loaded);
        setFailedPeriod(null);
      } catch (cause) {
        if (!mounted) return;
        if (cause instanceof ApiError && cause.status === 401) {
          onUnauthorized();
          return;
        }
        setFailedPeriod(period);
      }
      timer = window.setTimeout(
        () => void refresh(),
        61_000 - (Date.now() % 60_000),
      );
    }
    void refresh();
    return () => {
      mounted = false;
      window.clearTimeout(timer);
    };
  }, [onUnauthorized, period, revision]);

  const charts = activity?.period_minutes === period ? activity : null;
  const error = failedPeriod === period;
  const timeframeId = `activity-period-${periodName}`;
  const leader = activity?.leader;
  const username = leader?.username ? `@${leader.username}` : null;
  return (
    <section
      className="overview-activity"
      aria-labelledby="overview-activity-heading"
    >
      <div className="activity-heading">
        <h2 id="overview-activity-heading">Активность Зари</h2>
        <fieldset className="activity-period">
          <legend className="activity-visually-hidden">Период графиков</legend>
          {periods.map(({ minutes, label }) => (
            <label key={minutes} className="activity-period-option">
              <input
                className="activity-visually-hidden"
                type="radio"
                name={timeframeId}
                checked={period === minutes}
                onChange={() => setPeriod(minutes)}
              />
              <span>{label}</span>
            </label>
          ))}
        </fieldset>
      </div>
      {error && (
        <div className="notice error activity-error" role="alert">
          {charts
            ? "Не удалось обновить активность. Показаны последние данные."
            : "Не удалось загрузить активность."}{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => setRevision((n) => n + 1)}
          >
            Повторить
          </button>
        </div>
      )}
      {!charts && !error && (
        <p className="muted" role="status">
          Загружаем активность…
        </p>
      )}
      {charts && (
        <div className="activity-chart-grid">
          <ActivityChart
            key={`requests-${period}`}
            activity={charts}
            metric="requests"
            title="Обращения к Заре"
            summary="По времени поступления"
            empty="За выбранный период обращений не было"
          />
          <ActivityChart
            key={`attachments-${period}`}
            activity={charts}
            metric="attachments"
            title="Обработанные вложения"
            summary="По времени завершения обработки"
            empty="За выбранный период вложений не обработано"
          />
        </div>
      )}
      {activity && (
        <section
          className="activity-card activity-leader"
          aria-labelledby="activity-leader-heading"
        >
          <div className="activity-leader-heading">
            <h3 id="activity-leader-heading">Самый активный собеседник</h3>
            <span>За последние 24 часа</span>
          </div>
          {leader ? (
            <div className="activity-leader-row">
              <div className="activity-leader-person">
                <strong>{username ?? leader.name ?? "Участник"}</strong>
                {!username && (
                  <small>Имя пользователя в Telegram не указано</small>
                )}
              </div>
              <dl className="activity-leader-metrics">
                <div>
                  <dt>Обращения</dt>
                  <dd>{count.format(leader.requests)}</dd>
                </div>
                <div>
                  <dt>Оценка расходов API</dt>
                  <dd>
                    {leader.cost_usd === null
                      ? "Нет оценки"
                      : usd(leader.cost_usd)}
                  </dd>
                  {leader.unknown_cost_calls > 0 && (
                    <dd className="activity-cost-note">
                      Вызовов без полной оценки —{" "}
                      {count.format(leader.unknown_cost_calls)}
                    </dd>
                  )}
                  {leader.unassigned_calls > 0 && (
                    <dd className="activity-cost-note">
                      Вызовов с неясной привязкой —{" "}
                      {count.format(leader.unassigned_calls)}
                    </dd>
                  )}
                </div>
              </dl>
            </div>
          ) : (
            <p className="muted activity-empty">
              За последние 24 часа обращений не было.
            </p>
          )}
        </section>
      )}
    </section>
  );
}
