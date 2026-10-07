from __future__ import annotations

import hashlib
from datetime import timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import AnalysisTask, CalibrationVersion, Manifest, Report, ThresholdVersion
from .validation import canonical_json


def coefficients_to_json(coefficients: dict) -> dict:
    return {
        channel: value.model_dump(exclude_none=False) if hasattr(value, "model_dump") else dict(value)
        for channel, value in coefficients.items()
    }


def create_calibration(
    db: Session,
    *,
    channel_set_hash: str,
    coefficients: dict,
    change_note: str | None,
    created_by: str,
    mark_previous_for_review: bool = True,
) -> CalibrationVersion:
    if not coefficients:
        raise ValueError("calibration coefficients must not be empty")
    previous = db.scalar(
        select(CalibrationVersion)
        .where(CalibrationVersion.channel_set_hash == channel_set_hash, CalibrationVersion.status == "active")
        .order_by(CalibrationVersion.activated_at.desc(), CalibrationVersion.created_at.desc())
        .limit(1)
    )
    version = CalibrationVersion(
        channel_set_hash=channel_set_hash,
        status="active",
        coefficients=coefficients_to_json(coefficients),
        change_note=change_note,
        supersedes_id=previous.id if previous else None,
        created_by=created_by,
    )
    db.add(version)
    if previous is not None:
        previous.status = "superseded"
    db.flush()

    if previous is not None and mark_previous_for_review:
        db.execute(
            update(Report)
            .where(
                Report.calibration_version_id == previous.id,
                Report.status == "published",
            )
            .values(
                status="needs_review",
                review_reason="calibration coefficients were revised after publication",
                superseded_by_calibration_id=version.id,
            )
        )
    db.flush()
    return version


def get_active_calibration(db: Session, channel_set_hash: str) -> CalibrationVersion | None:
    return db.scalar(
        select(CalibrationVersion)
        .where(CalibrationVersion.channel_set_hash == channel_set_hash, CalibrationVersion.status == "active")
        .order_by(CalibrationVersion.activated_at.desc(), CalibrationVersion.created_at.desc())
        .limit(1)
    )


def threshold_rules_to_json(rules: list) -> list[dict]:
    normalized: list[dict] = []
    for index, rule in enumerate(rules, start=1):
        value = rule.model_dump() if hasattr(rule, "model_dump") else dict(rule)
        value = {"id": f"rule-{index}", **value}
        channels = list(dict.fromkeys(str(channel) for channel in value["channels"]))
        if not channels:
            raise ValueError("threshold rule channels must not be empty")
        value["channels"] = channels
        normalized.append(value)
    return normalized


def create_threshold_version(
    db: Session,
    *,
    name: str,
    channel_set_hash: str,
    rules: list,
    change_note: str | None,
    created_by: str,
) -> ThresholdVersion:
    normalized_rules = threshold_rules_to_json(rules)
    if not normalized_rules:
        raise ValueError("threshold rules must not be empty")
    previous = db.scalar(
        select(ThresholdVersion)
        .where(
            ThresholdVersion.channel_set_hash == channel_set_hash,
            ThresholdVersion.name == name,
            ThresholdVersion.status == "active",
        )
        .order_by(ThresholdVersion.activated_at.desc(), ThresholdVersion.created_at.desc())
        .limit(1)
    )
    version = ThresholdVersion(
        name=name,
        channel_set_hash=channel_set_hash,
        status="active",
        rules=normalized_rules,
        change_note=change_note,
        supersedes_id=previous.id if previous else None,
        created_by=created_by,
    )
    db.add(version)
    # Unlike calibration revisions, advisory threshold changes must not alter
    # the quality/review state or values of an already published report.
    if previous is not None:
        previous.status = "superseded"
    db.flush()
    return version


def build_threshold_snapshot(threshold: ThresholdVersion) -> dict:
    return {
        "id": threshold.id,
        "name": threshold.name,
        "channel_set_hash": threshold.channel_set_hash,
        "status": threshold.status,
        "rules": [dict(rule) for rule in threshold.rules],
        "change_note": threshold.change_note,
        "supersedes_id": threshold.supersedes_id,
        "created_by": threshold.created_by,
        "activated_at": threshold.activated_at.astimezone(timezone.utc).isoformat(),
    }


def build_manifest_snapshot(
    manifest: Manifest,
    calibration: CalibrationVersion,
    params: dict,
    threshold: ThresholdVersion | None = None,
) -> dict:
    expected = sorted(manifest.expected_chunks, key=lambda item: item["sequence"])
    return {
        "manifest_id": manifest.id,
        "manifest_digest": manifest.manifest_digest,
        "channel_set": sorted(manifest.channel_set),
        "channel_set_hash": manifest.channel_set_hash,
        "nominal_sample_rate": manifest.nominal_sample_rate,
        "expected_chunks": expected,
        "calibration_version_id": calibration.id,
        "calibration_coefficients": calibration.coefficients,
        "threshold_version_id": threshold.id if threshold else None,
        "threshold_rules_snapshot": build_threshold_snapshot(threshold) if threshold else None,
        "params": dict(params),
        "fixed_at": manifest.completed_at.astimezone(timezone.utc).isoformat()
        if manifest.completed_at
        else None,
    }


def snapshot_digest(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot)).hexdigest()


def create_analysis_task(
    db: Session,
    *,
    manifest: Manifest,
    calibration: CalibrationVersion,
    params: dict,
    idempotency_key: str | None,
    threshold: ThresholdVersion | None = None,
) -> AnalysisTask:
    if idempotency_key:
        existing = db.scalar(
            select(AnalysisTask).where(AnalysisTask.idempotency_key == idempotency_key).limit(1)
        )
        if existing is not None:
            return existing

    snapshot = build_manifest_snapshot(manifest, calibration, params, threshold)
    task = AnalysisTask(
        manifest_id=manifest.id,
        calibration_version_id=calibration.id,
        threshold_version_id=threshold.id if threshold else None,
        status="queued",
        params=dict(params),
        manifest_snapshot=snapshot,
        idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    return task
