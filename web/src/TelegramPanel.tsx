import { useEffect, useState } from "react";
import { api, ApiError } from "./api";

type Peer = {
  id: string;
  title: string;
  username: string | null;
  state: string;
  version: number;
  member_state: string;
};
type Peers = {
  items: Peer[];
  total: number;
  page: number;
  bot_id: string | null;
};
type TelegramState = {
  state: string;
  error: string | null;
  healthy: boolean;
  last_poll: number | null;
  bot: { id: string; username: string; privacy_disabled: boolean } | null;
  workers: string;
  api_calls: number;
  diagnostics: {
    jobs: Record<string, number>;
    outbox: Record<string, number>;
    events: Record<string, number>;
    unknown: {
      id: number;
      chat_id: string;
      created_at: string;
      code: string;
    }[];
  } | null;
};
const states: Record<string, string> = {
  not_configured: "Токен не настроен",
  dev_disabled: "Telegram выключен в режиме разработки",
  waiting_admin: "Ожидает настройки администратора",
  connecting: "Подключаемся…",
  connected: "Подключён",
  retrying: "Восстанавливаем связь",
  blocked: "Подключение остановлено",
  failed: "Обработчик остановлен",
  pending: "Ожидает решения",
  approved: "Разрешён",
  rejected: "Отклонён",
  revoked: "Отозван",
  running: "Выполняется",
  done: "Обработано",
  cancelled: "Отменено",
  sent: "Отправлено",
  sending: "Отправляется",
  unknown: "Доставка неизвестна",
  dismissed: "Закрыто вручную",
  accepted: "Принято",
  denied: "Пропущено без содержимого",
  request: "Заявки",
  membership: "Изменение участия",
  migration: "Миграция группы",
  ignored: "Пропущено",
};
const errors: Record<string, string> = {
  invalid_token: "Проверь файл токена и перезапусти приложение.",
  unauthorized:
    "Telegram отклонил токен. Проверь его и перезапусти приложение.",
  webhook_configured:
    "У бота настроен webhook. Отключи его в прежнем приложении перед запуском здесь.",
  unauthorized_or_other_receiver:
    "Токен отклонён или этого бота уже получает другое приложение. Останови второй экземпляр и перезапусти Зарю.",
  network:
    "Нет связи с Telegram. Повторное подключение выполняется автоматически.",
  rate_limit: "Telegram попросил подождать. Повтор запланирован автоматически.",
  receiver_failed:
    "Приём остановлен до подтверждения событий. Перезапусти приложение после проверки диска и базы.",
  worker_failed:
    "Обработка остановлена. После перезапуска сохранённые задания будут восстановлены.",
};
function Counts({
  title,
  values,
}: {
  title: string;
  values: Record<string, number>;
}) {
  return (
    <section className="card">
      <h3>{title}</h3>
      {Object.keys(values).length ? (
        <dl className="counts">
          {Object.entries(values).map(([key, value]) => (
            <div key={key}>
              <dt>{states[key] ?? key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p>Событий пока нет</p>
      )}
    </section>
  );
}
export function TelegramPanel({
  csrf,
  onUnauthorized,
}: {
  csrf: string | null;
  onUnauthorized: () => void;
}) {
  const [scope, setScope] = useState<"group" | "private">("private");
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<TelegramState | null>(null);
  const [peers, setPeers] = useState<Peers | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    let active = true,
      fetching = false;
    setLoading(true);
    setPeers(null);
    async function load() {
      if (fetching) return;
      fetching = true;
      try {
        const [status, list] = await Promise.all([
          api<TelegramState>("telegram"),
          api<Peers>(`telegram/access?scope=${scope}&page=${page}`),
        ]);
        if (active) {
          setState(status);
          setPeers(list);
          setConnectionError("");
        }
      } catch (e) {
        if (active) {
          setConnectionError(
            e instanceof Error ? e.message : "Нет связи с сервером",
          );
          if (e instanceof ApiError && e.status === 401) onUnauthorized();
        }
      } finally {
        fetching = false;
        if (active) setLoading(false);
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [scope, page, revision]);
  async function decide(peer: Peer, next: string) {
    if (!peers?.bot_id) return;
    if (
      next === "revoked" &&
      !window.confirm(
        "Отозвать доступ и отменить ожидающие ответы? Уже начатая отправка может завершиться.",
      )
    )
      return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api(
        "telegram/access",
        "POST",
        {
          bot_id: peers.bot_id,
          scope,
          subject_id: peer.id,
          state: next,
          expected_version: peer.version,
        },
        csrf,
      );
      setMessage("Решение сохранено. Старые ожидающие задания отменены.");
      setRevision((v) => v + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось сохранить решение");
      if (e instanceof ApiError && e.status === 401) onUnauthorized();
    } finally {
      setBusy(false);
    }
  }
  async function dismiss(id: number) {
    if (
      !state?.bot ||
      !window.confirm(
        "Закрыть предупреждение без повторной отправки? Это не подтверждает доставку сообщения.",
      )
    )
      return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api("telegram/dismiss", "POST", { bot_id: state.bot.id, id }, csrf);
      setRevision((v) => v + 1);
      setMessage("Предупреждение закрыто. Повторной отправки не было.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка");
      if (e instanceof ApiError && e.status === 401) onUnauthorized();
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="telegram-panel">
      {(error || connectionError) && (
        <div className="notice error" role="alert">
          {error || connectionError}{" "}
          <button
            className="text-button"
            disabled={busy}
            onClick={() => {
              setError("");
              setRevision((v) => v + 1);
            }}
          >
            Обновить
          </button>
        </div>
      )}
      {busy && <p role="status">Сохраняем решение…</p>}
      {message && (
        <div className="notice success" role="status">
          {message}
        </div>
      )}
      <section className="card connection-card">
        <div>
          <p className="eyebrow">ПОДКЛЮЧЕНИЕ</p>
          <h2>
            {state
              ? (states[state.state] ?? state.state)
              : "Загружаем состояние…"}
          </h2>
          {state?.bot && (
            <p>
              @{state.bot.username} · ID {state.bot.id}
            </p>
          )}
          {state?.error && (
            <p role="status">
              {errors[state.error] ??
                "Ошибка подключения. Перезапусти приложение."}
            </p>
          )}
          <p>
            После подключения Заря сможет принимать разрешённые сообщения.
            Ответы Зари включаются в настройках и доступны в разрешённых чатах.
          </p>
        </div>
        <button
          disabled={busy || loading}
          onClick={() => setRevision((v) => v + 1)}
        >
          Обновить состояние
        </button>
        <details>
          <summary>Как подключить бота и проверить доступ</summary>
          <ol>
            <li>
              Сохрани токен одной строкой в <code>data/telegram-token.txt</code>{" "}
              или задай переменную <code>ZARYA_TELEGRAM_TOKEN</code>.
              Перезапусти приложение.
            </li>
            <li>
              Напиши боту <code>/start</code> в личке или добавь его в тестовую
              группу. Заявка появится здесь.
            </li>
            <li>
              Разреши нужную личку или группу. После одобрения повторный{" "}
              <code>/start</code> в личке получит сервисный ответ.
            </li>
          </ol>
          <p>
            Для обычных сообщений группы выключи Privacy Mode через BotFather и
            заново добавь бота либо назначь его администратором. Токен здесь
            никогда не отображается.
          </p>
        </details>
        {state?.bot && (
          <p className="muted">
            {state.bot.privacy_disabled
              ? "Privacy Mode выключен: бот может получать обычные сообщения групп."
              : "Privacy Mode включён: обычные сообщения доступны только в группах, где бот администратор."}{" "}
            Последний успешный приём:{" "}
            {state.last_poll
              ? new Date(state.last_poll * 1000).toLocaleTimeString("ru-RU")
              : "ещё не было"}
            .
          </p>
        )}
      </section>
      <section className="access-section" aria-labelledby="access-title">
        <div className="access-heading">
          <h2 id="access-title">Кому можно общаться с Зарёй</h2>
          <span className="muted">Записей: {peers?.total ?? 0}</span>
        </div>
        <div
          className="scope-buttons"
          role="group"
          aria-label="Область доступа"
        >
          <button
            disabled={busy}
            aria-pressed={scope === "private"}
            className={scope === "private" ? "primary" : ""}
            onClick={() => {
              setScope("private");
              setPage(0);
              setMessage("");
              setError("");
            }}
          >
            Личные заявки
          </button>
          <button
            disabled={busy}
            aria-pressed={scope === "group"}
            className={scope === "group" ? "primary" : ""}
            onClick={() => {
              setScope("group");
              setPage(0);
              setMessage("");
              setError("");
            }}
          >
            Группы
          </button>
        </div>
        <p className="muted">
          Доступ к группе не разрешает личную переписку. Имя и ник не определяют
          владельца — используется Telegram ID.
        </p>
        {loading ? (
          <p role="status">Обновляем список…</p>
        ) : connectionError ? (
          <p>Список не загружен. Обнови данные после восстановления связи.</p>
        ) : !peers?.items.length ? (
          <div className="card empty-state">
            <h3>
              {scope === "private" ? "Заявок пока нет" : "Групп пока нет"}
            </h3>
            <p>
              {scope === "private"
                ? "Пользователь должен отправить /start подключённому боту."
                : "Добавь подключённого бота в группу. Доступ выдаётся отдельно здесь."}
            </p>
          </div>
        ) : (
          <div className="peer-list">
            {peers.items.map((peer) => (
              <article className="card peer" key={peer.id}>
                <div className="peer-info">
                  <h3>{peer.title}</h3>
                  <p>
                    {peer.username ? `@${peer.username} · ` : ""}ID {peer.id}
                  </p>
                  <span className="badge">
                    {states[peer.state] ?? peer.state}
                  </span>
                  {["left", "kicked", "migrated"].includes(
                    peer.member_state,
                  ) && <p>Бот удалён, заблокирован или группа перенесена.</p>}
                </div>
                <div className="peer-actions">
                  {peer.state !== "approved" && (
                    <button
                      className="primary"
                      aria-label={`Разрешить: ${peer.title}, ID ${peer.id}`}
                      disabled={
                        busy ||
                        !!connectionError ||
                        ["left", "kicked", "migrated"].includes(
                          peer.member_state,
                        )
                      }
                      onClick={() => void decide(peer, "approved")}
                    >
                      Разрешить
                    </button>
                  )}
                  {peer.state === "pending" && (
                    <button
                      disabled={busy || !!connectionError}
                      onClick={() => void decide(peer, "rejected")}
                      aria-label={`Отклонить: ${peer.title}, ID ${peer.id}`}
                    >
                      Отклонить
                    </button>
                  )}
                  {peer.state === "approved" && (
                    <button
                      disabled={busy || !!connectionError}
                      onClick={() => void decide(peer, "revoked")}
                      aria-label={`Отозвать доступ: ${peer.title}, ID ${peer.id}`}
                    >
                      Отозвать доступ
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
        <div className="pagination">
          <button
            disabled={busy || loading || page === 0}
            onClick={() => setPage((v) => v - 1)}
          >
            Назад
          </button>
          <span>Страница {page + 1}</span>
          <button
            disabled={busy || loading || (page + 1) * 20 >= (peers?.total ?? 0)}
            onClick={() => setPage((v) => v + 1)}
          >
            Далее
          </button>
        </div>
      </section>
      {state?.diagnostics && (
        <>
          <h2 className="queue-heading">Приём и обработка</h2>
          <p className="muted">
            Технические состояния без текста переписки. Обработчик:{" "}
            {state.workers === "running" ? "работает" : "остановлен"}.
          </p>
          <div className="cards">
            <Counts title="События" values={state.diagnostics.events} />
            <Counts title="Задания" values={state.diagnostics.jobs} />
            <Counts title="Отправки" values={state.diagnostics.outbox} />
          </div>
          {state.diagnostics.unknown.length > 0 && (
            <section className="card uncertain">
              <h3>Результат доставки неизвестен</h3>
              <p>
                Telegram мог принять сообщение до обрыва связи. Такие сообщения
                автоматически не повторяются. Показаны последние 20.
              </p>
              {state.diagnostics.unknown.map((item) => (
                <div className="unknown-row" key={item.id}>
                  <span>
                    Отправка №{item.id} · чат {item.chat_id}
                  </span>
                  <button disabled={busy} onClick={() => void dismiss(item.id)}>
                    Закрыть без повтора
                  </button>
                </div>
              ))}
            </section>
          )}
        </>
      )}
    </div>
  );
}
