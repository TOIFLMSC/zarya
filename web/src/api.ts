export type Settings = {
  display_name: string;
  owner_telegram_id: string;
  owner_username: string;
  owner_name: string;
  persona: string;
  tone: "balanced" | "warm" | "reserved";
  dialogue_enabled: boolean;
  model: "gpt-6-luna" | "gpt-6.1-sol";
  reasoning: "low" | "medium" | "high";
  max_output_tokens: number;
  photo_enabled: boolean;
  photo_model: "gpt-6-luna" | "gpt-6.1-sol";
  memory_enabled: boolean;
  memory_model: "gpt-6-luna" | "gpt-6.1-sol";
  memory_retrieval: "legacy" | "shadow" | "graph" | "e5";
  participant_context_messages: number;
  topic_context_chat_id: string;
  participant_summaries_enabled: boolean;
  participant_summary_prompt: string;
  research_enabled: boolean;
  research_model: "gpt-6-luna" | "gpt-6.1-sol";
  research_reasoning: "low" | "medium" | "high";
  media_enabled: boolean;
  asr_model: "gpt-4o-mini-transcribe" | "gpt-4o-transcribe";
  media_model: "gpt-6-luna" | "gpt-6.1-sol";
  media_reasoning: "low" | "medium" | "high";
  voice_max_seconds: number;
  video_max_seconds: number;
  video_max_frames: number;
  behavior_enabled: boolean;
  reactions_enabled: boolean;
  expressiveness: "restrained" | "balanced" | "expressive";
  emotional_max_parts: number;
};
export type SummaryCoverage = {
  enabled: boolean;
  block_size: number;
  covered_ids: number[];
  original_ids: number[];
  fresh_ids: number[];
  raw_reasons: Record<string, string>;
  blocks: {
    id: number;
    state: string;
    summary: string | null;
    error_code: string | null;
    created_at: string;
    full_window: boolean;
    used_ids: number[];
    manifest: { message_id: number; event_id: number; received_at: number }[];
    model: string | null;
    settings_version: number | null;
    call_state: string | null;
    cost_usd: number | null;
    latency_ms: number | null;
    usage: Record<string, unknown> | null;
  }[];
};
export type GroupPolicy = {
  mode: "off" | "shadow" | "live";
  chance_percent: number;
  version: number;
};
export type BehaviorData = {
  settings: Settings;
  settings_version: number;
  peers: {
    chat_id: string;
    title: string;
    scope: string;
    access: string;
    policy: GroupPolicy;
  }[];
  moods: {
    chat_id: string;
    thread_id: number;
    title: string;
    scope: string;
    tone: string;
    intensity: number;
    version: number;
    updated_at: number;
    reason: string;
  }[];
  decisions: {
    run_id: number;
    chat_id: string;
    thread_id: number | null;
    mode: string;
    action: string;
    plan: { text?: string; reaction?: string };
    reason: string;
    applied: boolean;
    state: string;
    created_at: number;
    cost_usd: number | null;
    call_state: string | null;
    delivery: string | null;
  }[];
  page: number;
  total: number;
};
export type MediaItem = {
  id: number;
  chat_id: string;
  scope: string;
  kind: string;
  state: string;
  duration: number | null;
  created_at: string;
  error: string | null;
};
export type MediaList = {
  items: MediaItem[];
  total: number;
  page: number;
  enabled: boolean;
  configured: boolean;
  tools_available: boolean;
  asr_model: string;
  model: string;
  limits: {
    voice: number;
    video: number;
    frames: number;
    bytes: number;
    items: number;
  };
};
export type MediaLibrary = Omit<MediaList, "items" | "limits"> & {
  items: (MediaItem & {
    source: "photo" | "media";
    count: number;
    chat_title: string | null;
  })[];
  page_size: number;
  chats: { id: string; scope: string; title: string | null }[];
  photo_enabled: boolean;
  photo_configured: boolean;
  photo_model: string;
  known_cost_usd: number | null;
  unknown_cost_calls: number;
  limits: { voice: number; video: number; frames: number; items: number };
};
export type MediaDetail = MediaItem & {
  available: boolean;
  transcript: string | null;
  result: {
    observations: string;
    visible_text: string;
    interpretation: string;
    uncertainty: string;
    timeline?: { seconds: number; visual: string }[];
    speech_summary?: string;
    question_answer?: string;
  } | null;
  coverage: {
    audio_signal?: string;
    audio_intervals?: number[][];
    speech?: string;
    note?: string;
  };
  frames: { seconds: number; url: string }[];
  calls: {
    operation: string;
    model: string;
    state: string;
    usage: { input_tokens?: number; output_tokens?: number } | null;
    latency_ms: number | null;
    cost_usd: number | null;
    error: string | null;
  }[];
};
export type DialogItem = {
  id: number;
  chat_id: string;
  thread_id: number | null;
  trigger: string;
  state: string;
  created_at: string;
  mode: string;
  input: string;
  invalidated?: boolean;
};
export type DialogList = {
  items: DialogItem[];
  total: number;
  page: number;
  configured: boolean;
  known_cost_usd: number | null;
  unknown_cost_calls: number;
};
export type DialogDetail = DialogItem & {
  timing: import("./TimingDiagnostics").Timing;
  typing: { state: string; attempts: number; at: number; error: string | null };
  response: string | null;
  error: string | null;
  replay_of: number | null;
  snapshot: {
    invalidated?: boolean;
    verbatim_quote?: boolean;
    context_budget?: ContextBudget;
    task_state?: {
      fields: { kind: string; message_id: number; quote: string }[];
    };
    topic_context?: {
      message_id: number;
      event_id: number;
      name: string;
      text: string;
      forwarded: boolean;
      received_at: number;
    }[];
    current_messages?: { message_id: number; event_id: number; text: string }[];
    participant_context?: {
      algorithm: string;
      limit: number;
      current_included: boolean;
      characters: number;
      reply_chain_truncated: boolean;
      participants: {
        sender_id: string;
        name: string;
        reason: "speaker" | "reply_chain";
        available: number;
        human_messages: number;
        assistant_messages: number;
        summarized_messages?: number;
        original_messages?: number;
        summaries?: SummaryCoverage;
      }[];
    };
    retrieval?: {
      algorithm: string;
      mode: Settings["memory_retrieval"];
      epoch: number;
      load_ms: number;
      legacy_ms: number;
      graph_ms?: number;
      semantic_ms?: number;
      candidates: { facts: number; episodes: number };
      fallback: string | null;
      capped: boolean;
      legacy: RetrievalSelection;
      graph?: RetrievalSelection;
      semantic?: RetrievalSelection;
    };
    avatar_profile?: {
      user_id: string;
      name: string;
      available_count: number | null;
      analysed_indices: number[];
      has_more: boolean;
      list_changed: boolean;
      listing_truncated: boolean;
      state: string;
      error: string | null;
      observations: { descriptions: string[]; uncertainty: string } | null;
    } | null;
    incoming: { text: string; sender_id: string; message_id: number };
    context: {
      text: string;
      name: string;
      sender_id: string;
      role: string;
      block_id?: number;
      source_ids?: number[];
      message_id: number;
      thread_id: number | null;
    }[];
    owner: boolean;
    settings_version: number;
    settings: Settings;
  };
  call: {
    model: string;
    state: string;
    usage: { input_tokens: number; output_tokens: number } | null;
    latency_ms: number | null;
    cost_usd: number | null;
    pricing: string;
    error: string | null;
  } | null;
  parts: {
    reply_id?: number | null;
    index: number;
    state: string;
    text: string;
    message_id: number | null;
    error: string | null;
  }[];
};

export type ContextBudget = {
  version: number;
  model: string;
  profile: string;
  profile_source: string | null;
  context_limit: number | null;
  output_limit: number | null;
  output_reserve: number;
  safety_margin: number;
  input_budget: number | null;
  estimate: number;
  unknown_input: boolean;
  stage?: string;
  state: string;
  decision: string;
  provider_attempted: boolean;
  sections: {
    kind: string;
    count: number;
    bytes: number | null;
    estimate: number;
  }[];
  excluded: { kind: string; count: number; reason: string }[];
  api_count: {
    state: string;
    tokens: number | null;
    error: string | null;
    latency_ms: number | null;
    attempted?: boolean;
    cost_usd: number | null;
  };
};
export type Snapshot = {
  version: number;
  settings: Settings;
  updated_at: string;
};
export type RetrievalSelection = {
  facts: { id: number; version: number; reasons: string[] }[];
  episodes: { id: number; reasons: string[] }[];
  conflicts: number[];
  reply_messages: number[];
  chars: number;
};
export type PhotoItem = {
  id: number;
  chat_id: string;
  scope: string;
  state: string;
  created_at: string;
  model: string;
  count: number;
  caption: string;
  cache_source: number | null;
};
export type PhotoList = {
  items: PhotoItem[];
  total: number;
  page: number;
  enabled: boolean;
  configured: boolean;
  model: string;
  bot: { id: string; username: string } | null;
  known_cost_usd: number | null;
  unknown_cost_calls: number;
};
export type PhotoDetail = PhotoItem & {
  available: boolean;
  settings_version: number | null;
  error: string | null;
  result: {
    observations: string;
    visible_text: string;
    interpretation: string;
    uncertainty: string;
  } | null;
  items: {
    id: number;
    caption: string;
    width: number | null;
    height: number | null;
    byte_count: number | null;
    image_url: string | null;
  }[];
  call: {
    model: string;
    state: string;
    usage: { input_tokens: number; output_tokens: number } | null;
    latency_ms: number | null;
    cost_usd: number | null;
    pricing: string | null;
  } | null;
};
export type Session = {
  authenticated: boolean;
  setup_required: boolean;
  csrf: string | null;
};
export type ResearchItem = {
  id: number;
  chat_id: string;
  scope: string;
  state: string;
  mode: string;
  question: string;
  model: string;
  created_at: string;
  error: string | null;
};
export type ResearchList = {
  max_tool_calls: number;
  items: ResearchItem[];
  total: number;
  page: number;
  chats: { id: string; scope: string }[];
  enabled: boolean;
  configured: boolean;
  model: string;
  bot_id: string | null;
};
export type ResearchSource = {
  retrieved_at?: string;
  id: number;
  requested_url?: string;
  url: string | null;
  title: string;
  text: string;
  coverage: string;
  error: string | null;
  x_post?: {
    post_id: string;
    quote_status?: "none" | "available" | "unavailable" | "no_text";
    parent_ids?: string[];
    thread_status?: "none" | "available" | "partial";
    text_truncated?: boolean;
    has_media?: boolean;
    provider_error?: string;
  };
  youtube?: {
    video_id?: string;
    channel?: string;
    duration_seconds?: number | null;
    description_truncated?: boolean;
    chapters?: { seconds: number; title: string }[];
    transcript_status?: string;
    transcript_language?: string | null;
    transcript_automatic?: boolean;
    transcript_truncated?: boolean;
    fallback_used?: boolean;
    diagnostics?: { step: string; code: string }[];
  };
};
export type ResearchDetail = ResearchItem & {
  material: string;
  reasoning: string;
  sources: ResearchSource[];
  result: {
    summary: string;
    followup_summary?: string;
    followup?: "started" | "completed" | "incomplete";
    source_lookup?: {
      kind: "document" | "official_section" | "unconfirmed";
      source_id?: number;
      heading?: string;
    };
    claims: {
      text: string;
      kind: string;
      verdict: string;
      source_ids: number[];
    }[];
  } | null;
  actions: {
    type: string;
    queries?: string[];
    query?: string;
    url?: string;
    status?: string;
  }[];
  call: ResearchCall | null;
  calls?: ResearchCall[];
};
type ResearchCall = {
  phase?: "research_initial" | "research_gap";
  model: string;
  pricing: string | null;
  state: string;
  usage: { input_tokens: number; output_tokens: number } | null;
  latency_ms: number | null;
  cost_usd: number | null;
  search_cost_usd: number | null;
  error: string | null;
};

export type MemoryList = {
  chats: {
    chat_id: string;
    scope: string;
    title: string;
    participants: number;
    facts: number;
  }[];
  bot_id: string | null;
  enabled: boolean;
  configured: boolean;
  model: string;
  known_cost_usd: number | null;
  unknown_cost_calls: number;
  retention_days: number;
};
export type MemoryFact = {
  confidence?: {
    level: string;
    label: string;
    kind: string;
    messages: number;
    authors: number;
    episodes: number;
    first_seen: number | null;
    last_seen: number | null;
    eligible: boolean;
    source_ids: number[];
    reason: string;
  };
  id: number;
  sender_id: string;
  category: string;
  text: string;
  provenance: string;
  state: string;
  version: number;
  curated: boolean;
  created_at: string;
  updated_at: string;
  sources: {
    id: number;
    sender_id: string;
    name: string;
    message_id: number;
    thread_id: number | null;
    text: string;
    received_at: number;
  }[];
  share: {
    state: string;
    authority: string;
    actor_id: string;
    consent_event_id: number | null;
    fact_version: number;
  } | null;
};
export type MemoryProfile = {
  bot_id: string;
  chat_id: string;
  scope: string;
  title: string;
  facts: MemoryFact[];
  participants: { sender_id: string; name: string; last_seen: number }[];
  batches: {
    id: number;
    state: string;
    summary: string | null;
    created_at: string;
    finished_at: string | null;
    error: string | null;
    model: string | null;
    usage: string | null;
    cost_usd: number | null;
    latency_ms: number | null;
  }[];
};
export type Status = {
  version: string;
  mode: string;
  stage: number;
  uptime_seconds: number;
  database: {
    status: string;
    engine: string;
    schema_version: number;
    sqlite_version: string;
  };
  settings_version: number;
  integrations: { telegram: string; openai: string };
  workers: string;
  external_calls: number;
};
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
  csrf?: string | null,
): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(method !== "GET" ? { "Content-Type": "application/json" } : {}),
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      typeof data.detail === "string"
        ? data.detail
        : "Не удалось выполнить запрос",
      response.status,
    );
  return data as T;
}
