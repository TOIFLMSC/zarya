"""Joint video evidence: ordered frames, speech and the addressed question."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VideoEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(ge=0, allow_inf_nan=False)
    visual: str = Field(max_length=500)


class VideoObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: str = Field(max_length=1500)
    visible_text: str = Field(max_length=2000)
    timeline: list[VideoEvent] = Field(max_length=32)
    speech_summary: str = Field(max_length=1000)
    interpretation: str = Field(max_length=1200)
    question_answer: str = Field(max_length=1200)
    uncertainty: str = Field(max_length=1000)
    evidence_basis: Literal["sampled_frames_and_transcript"]
