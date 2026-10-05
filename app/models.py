from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brief: str = Field(min_length=10, max_length=4000)
    title: str | None = Field(default=None, max_length=100)
    duration_seconds: int = Field(default=24, ge=12, le=60)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    style: Literal["cinematic", "editorial", "playful"] = "cinematic"
    provider_mode: Literal["demo", "free", "live"] = "demo"
    review_required: bool = True
    transition: Literal["fade", "cut"] = "fade"

    @model_validator(mode="after")
    def free_studio_requires_review(self):
        if self.provider_mode == "free":
            self.review_required = True
        return self

    @field_validator("brief")
    @classmethod
    def strip_brief(cls, value):
        value = value.strip()
        if len(value) < 10:
            raise ValueError("Describe your video in at least 10 characters")
        return value


class Scene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    narration: str = Field(min_length=1, max_length=1600)
    visual_prompt: str = Field(min_length=5, max_length=2000)
    duration_seconds: float = Field(ge=1, le=60)

    @field_validator("narration", "visual_prompt")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Scene content cannot be blank")
        return value.strip()


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str | None = Field(default=None, min_length=1, max_length=12000)
    scenes: list[Scene] | None = Field(default=None, min_length=1, max_length=12)
    notes: str | None = Field(default=None, max_length=2000)


def validate_scenes(scenes, duration):
    result = [Scene.model_validate(scene).model_dump() for scene in scenes]
    if not 1 <= len(result) <= 12:
        raise ValueError("Scene plan must have 1 to 12 scenes")
    if len({scene["id"] for scene in result}) != len(result):
        raise ValueError("Scene IDs must be unique")
    if abs(sum(scene["duration_seconds"] for scene in result) - duration) > 0.5:
        raise ValueError("Scene durations must add up to the requested video duration")
    return result
