from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Settings(StrictModel):
    display_name: str = Field(default="Заря", min_length=1, max_length=40)
    owner_telegram_id: str = Field(default="", pattern=r"^(|[1-9][0-9]{0,15})$")
    owner_username: str = Field(default="", pattern=r"^(@?[A-Za-z0-9_]{1,32})?$")
    owner_name: str = Field(default="", max_length=80)
    persona: str = Field(
        default="Вежливый ИИ-собеседник. Отвечает по существу и признаёт неопределённость.",
        min_length=1,
        max_length=4000,
    )
    tone: Literal["balanced", "warm", "reserved"] = "balanced"
    dialogue_enabled: bool = True
    model: Literal["gpt-6-luna", "gpt-6.1-sol"] = "gpt-6-luna"
    reasoning: Literal["low", "medium", "high"] = "low"
    max_output_tokens: int = Field(default=2500, ge=1000, le=6000)
    photo_enabled: bool = True
    photo_model: Literal["gpt-6-luna", "gpt-6.1-sol"] = "gpt-6-luna"
    memory_enabled: bool = True
    memory_model: Literal["gpt-6-luna", "gpt-6.1-sol"] = "gpt-6-luna"
    research_enabled: bool = True
    research_model: Literal["gpt-6-luna", "gpt-6.1-sol"] = "gpt-6-luna"
    research_reasoning: Literal["low", "medium", "high"] = "medium"
    media_enabled: bool = True
    asr_model: Literal["gpt-4o-mini-transcribe", "gpt-4o-transcribe"] = "gpt-4o-mini-transcribe"
    media_model: Literal["gpt-6-luna", "gpt-6.1-sol"] = "gpt-6-luna"
    media_reasoning: Literal["low", "medium", "high"] = "medium"
    voice_max_seconds: int = Field(default=300, ge=10, le=300)
    video_max_seconds: int = Field(default=120, ge=5, le=120)
    video_max_frames: int = Field(default=24, ge=1, le=32)
    behavior_enabled: bool = False
    reactions_enabled: bool = True
    expressiveness: Literal["restrained", "balanced", "expressive"] = "balanced"
    emotional_max_parts: int = Field(default=4, ge=1, le=4)


class GroupBehaviorUpdate(StrictModel):
    expected_version: int = Field(ge=0)
    mode: Literal["off", "shadow", "live"]
    chance_percent: float = Field(default=5, ge=0, le=100)


class PilotPreferences(StrictModel):
    expected_version: int = Field(ge=1)
    warning_usd: float | None = Field(default=None, gt=0, le=1000000, allow_inf_nan=False)


class PilotReview(StrictModel):
    expected_version: int = Field(ge=0)
    status: Literal["pending", "passed", "failed"]
    note: str = Field(default="", max_length=1000)


class MoodReset(StrictModel):
    expected_version: int = Field(ge=1)
    thread_id: int = Field(default=0, ge=0)


class SettingsUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    settings: Settings


class SettingsSnapshot(StrictModel):
    version: int
    settings: Settings
    updated_at: str


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=12, max_length=128)


class SetupInput(LoginInput):
    token: SecretStr = Field(min_length=20, max_length=128)


class AccessDecision(StrictModel):
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,15}$")
    scope: Literal["group", "private"]
    subject_id: str = Field(pattern=r"^-?[1-9][0-9]{0,15}$")
    state: Literal["approved", "rejected", "revoked"]
    expected_version: int = Field(ge=1)


class DeliveryDismiss(StrictModel):
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,15}$")
    id: int = Field(ge=1)


class ReplayInput(StrictModel):
    run_id: int = Field(ge=1)
    mode: Literal["recorded", "paid"] = "recorded"
    expected_settings_version: int | None = Field(default=None, ge=1)


class MemoryMutation(StrictModel):
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,15}$")
    expected_version: int = Field(ge=1)
    action: Literal["accept", "edit", "delete", "share", "unshare"]
    text: str | None = Field(default=None, min_length=1, max_length=500)
