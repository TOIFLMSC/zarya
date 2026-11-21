import { useEffect, useState } from "react";
import { api, ApiError, type SummaryCoverage } from "./api";
import { PersonalSummaries } from "./PersonalSummaries";

type WindowData = {
  sender_id: string;
  limit: number;
  available: number;
  human_messages: number;
  assistant_messages: number;
  summaries: SummaryCoverage;
  messages: {
    message_id: number;
    sender_id: string;
    name: string;
    role: string;
    text: string;
    thread_id: number | null;
    received_at: number;
    forwarded?: boolean;
    in_reply_to?: number;
  }[];
};

export function ParticipantWindow({
  chat,
  sender,
  onExpired,
}: {
  chat: string;
  sender: string;
  onExpired: () => void;
}) {
  const [data, setData] = useState<WindowData | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true,
      loading = false;
    async function load() {
      if (loading) return;
      loading = true;
      try {
        const result = await api<WindowData>(
          `memory/chats/${encodeURIComponent(chat)}/context?sender_id=${encodeURIComponent(sender)}`,
        );
        if (active) {
          setData(result);
          setError("");
        }
      } catch (e) {
        if (active) {
          setData(null);
          if (e instanceof ApiError && e.status === 401) onExpired();
          setError(
            e instanceof Error ? e.message : "Не удалось загрузить контекст",
          );
        }
      } finally {
        loading = false;
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [chat, sender, onExpired]);
  return (
    <section
      className="participant-window timing-diagnostics"
      aria-label="Недавний контекст собеседника"
    >
      <h3>Недавний контекст собеседника</h3>
      {error ? (
        <p role="alert" className="notice error">
          {error}
        </p>
      ) : !data ? (
        <p role="status">Загружаем контекст…</p>
      ) : (
        <>
          <dl className="dialogue-metrics">
            <div>
              <dt>Человеческих в окне / доступно</dt>
              <dd>
                {data.human_messages} / {data.available}
              </dd>
            </div>
            <div>
              <dt>Предел окна</dt>
              <dd>{data.limit}</dd>
            </div>
            <div>
              <dt>Связанных ответов Зари</dt>
              <dd>{data.assistant_messages}</dd>
            </div>
          </dl>
          <p className="muted">
            Доступность на момент загрузки. Состав конкретного запроса показан в
            «Диалогах».
          </p>
          <PersonalSummaries
            data={data.summaries}
            messages={data.messages.filter((m) => m.role === "user")}
          />
          {!data.human_messages ? (
            <p>Доступных сообщений этого собеседника пока нет.</p>
          ) : (
            <details>
              <summary>Все оригиналы окна</summary>
              {data.messages.map((m) => (
                <div className="context-message" key={m.message_id}>
                  <small>
                    {m.role === "assistant" ? "Заря" : m.name} · {m.sender_id} ·
                    сообщение {m.message_id}
                    {m.thread_id ? ` · топик ${m.thread_id}` : ""} ·{" "}
                    {new Date(m.received_at * 1000).toLocaleString("ru-RU")}
                    {m.forwarded ? " · пересланный материал" : ""}
                    {m.in_reply_to ? ` · ответ на ${m.in_reply_to}` : ""}
                  </small>
                  <p className="dialogue-text">{m.text}</p>
                </div>
              ))}
            </details>
          )}
        </>
      )}
    </section>
  );
}
