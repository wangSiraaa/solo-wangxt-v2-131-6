from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExpectedChunkIn(BaseModel):
    sequence: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    byte_offset: int = Field(ge=0)
    byte_length: int = Field(gt=0)
    sample_count: int = Field(gt=0)
    sample_rate: float = Field(gt=0)
    channels: list[str]
    start_time: str
    end_time: str
    encoding: str = "float32le-interleaved"


class ManifestCreate(BaseModel):
    name: str
    nominal_sample_rate: float | None = None
    expected_chunks: list[ExpectedChunkIn]
    start_time: str | None = None
    end_time: str | None = None


class ManifestOut(BaseModel):
    id: str
    name: str
    status: str
    expected_chunks: list[dict[str, Any]]
    channel_set: list[str]
    channel_set_hash: str
    nominal_sample_rate: float | None
    start_time: str | None
    end_time: str | None
    manifest_digest: str | None
    error: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class IssueOut(BaseModel):
    id: str
    severity: str
    code: str
    message: str
    details: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ChunkOut(BaseModel):
    id: str
    sequence: int
    object_key: str
    sha256: str
    byte_offset: int
    byte_length: int
    sample_count: int
    sample_rate: float
    channels: list[str]
    start_time: str
    end_time: str
    encoding: str
    received_at: datetime

    model_config = {"from_attributes": True}


class CalibrationCoefficient(BaseModel):
    gain: float = 1.0
    offset: float = 0.0
    phase_shift_rad: float = 0.0
    saturation_low: float | None = None
    saturation_high: float | None = None


class CalibrationCreate(BaseModel):
    channel_set_hash: str
    coefficients: dict[str, CalibrationCoefficient]
    change_note: str | None = None
    created_by: str = "lab"
    mark_previous_for_review: bool = True


class CalibrationOut(BaseModel):
    id: str
    channel_set_hash: str
    status: str
    coefficients: dict[str, Any]
    change_note: str | None
    supersedes_id: str | None
    created_by: str
    created_at: datetime
    activated_at: datetime

    model_config = {"from_attributes": True}


class ThresholdRuleIn(BaseModel):
    metric: Literal["rms", "thd", "negative_sequence_ratio"]
    channels: list[str]
    min_sample_rate_hz: float | None = Field(default=None, ge=0)
    max_sample_rate_hz: float | None = Field(default=None, ge=0)
    lower_limit: float | None = None
    upper_limit: float | None = None
    unit: str = Field(min_length=1)

    def model_post_init(self, __context: Any) -> None:
        channels = [channel.strip() for channel in self.channels if channel.strip()]
        if not channels:
            raise ValueError("threshold rule channels must not be empty")
        if len(channels) != len(set(channels)):
            raise ValueError("threshold rule channels must not contain duplicates")
        self.channels = channels
        self.unit = self.unit.strip()
        if not self.unit:
            raise ValueError("threshold unit must not be empty")
        if self.metric == "negative_sequence_ratio" and not set(channels) <= {"voltage", "current"}:
            raise ValueError("negative_sequence_ratio channels must be voltage and/or current")
        if self.lower_limit is None and self.upper_limit is None:
            raise ValueError("threshold rule must define at least one limit")
        if self.min_sample_rate_hz is not None and self.max_sample_rate_hz is not None:
            if self.min_sample_rate_hz > self.max_sample_rate_hz:
                raise ValueError("min_sample_rate_hz cannot exceed max_sample_rate_hz")
        if self.lower_limit is not None and self.upper_limit is not None:
            if self.lower_limit > self.upper_limit:
                raise ValueError("lower_limit cannot exceed upper_limit")


class ThresholdVersionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    channel_set_hash: str = Field(min_length=64, max_length=64)
    rules: list[ThresholdRuleIn]
    change_note: str | None = None
    created_by: str = "lab"


class ThresholdVersionOut(BaseModel):
    id: str
    name: str
    channel_set_hash: str
    status: str
    rules: list[dict[str, Any]]
    change_note: str | None
    supersedes_id: str | None
    created_by: str
    created_at: datetime
    activated_at: datetime

    model_config = {"from_attributes": True}


class AnalysisCreate(BaseModel):
    manifest_id: str
    calibration_version_id: str | None = None
    threshold_version_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class TaskOut(BaseModel):
    id: str
    manifest_id: str
    calibration_version_id: str
    threshold_version_id: str | None
    status: str
    params: dict[str, Any]
    manifest_snapshot: dict[str, Any]
    stage_results: dict[str, Any]
    attempts: int
    lease_owner: str | None
    lease_until: datetime | None
    heartbeat_at: datetime | None
    error_code: str | None
    error_message: str | None
    cancellation_requested: bool
    idempotency_key: str | None
    requested_at: datetime
    started_at: datetime | None
    ended_at: datetime | None

    model_config = {"from_attributes": True}


class ReportOut(BaseModel):
    id: str
    task_id: str
    manifest_id: str
    calibration_version_id: str
    threshold_version_id: str | None
    status: str
    result: dict[str, Any]
    snapshot_digest: str
    published_at: datetime | None
    review_reason: str | None
    superseded_by_calibration_id: str | None

    model_config = {"from_attributes": True}


class RetryOut(BaseModel):
    task_id: str
    status: str
    attempts: int


QualitySeverity = Literal["ok", "warning", "error"]
