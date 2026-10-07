from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .thresholds import SEQUENCE_GROUPS


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


class ThresholdRule(BaseModel):
    metric: Literal["rms", "thd_percent", "negative_sequence_percent"]
    channels: list[str] = Field(min_length=1)
    sample_rate_min: float | None = Field(default=None, gt=0)
    sample_rate_max: float | None = Field(default=None, gt=0)
    lower: float | None = None
    upper: float | None = None
    unit: str = ""
    note: str | None = None

    @model_validator(mode="after")
    def check_rule(self) -> "ThresholdRule":
        if self.lower is None and self.upper is None:
            raise ValueError("at least one of lower/upper is required")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("lower must not exceed upper")
        if self.sample_rate_min is not None and self.sample_rate_max is not None:
            if self.sample_rate_min > self.sample_rate_max:
                raise ValueError("sample_rate_min must not exceed sample_rate_max")
        if "*" in self.channels and len(self.channels) > 1:
            raise ValueError("'*' must not be mixed with explicit channel names")
        if self.metric == "negative_sequence_percent":
            invalid = [c for c in self.channels if c != "*" and c not in SEQUENCE_GROUPS]
            if invalid:
                raise ValueError(f"negative_sequence_percent applies to {list(SEQUENCE_GROUPS)} groups, got {invalid}")
        return self


class ThresholdRuleSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    rules: list[ThresholdRule] = Field(min_length=1)
    change_note: str | None = None
    created_by: str = "lab"


class ThresholdRuleSetOut(BaseModel):
    id: str
    name: str
    version: int
    status: str
    rules: list[dict[str, Any]]
    change_note: str | None
    supersedes_id: str | None
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AnalysisCreate(BaseModel):
    manifest_id: str
    calibration_version_id: str | None = None
    threshold_rule_set_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class TaskOut(BaseModel):
    id: str
    manifest_id: str
    calibration_version_id: str
    threshold_rule_set_id: str | None
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
    threshold_rule_set_id: str | None
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
