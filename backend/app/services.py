from __future__ import annotations

import hashlib
from datetime import timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import AnalysisTask, CalibrationVersion, Manifest, Report, ThresholdRuleSet
from .thresholds import validate_rules
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


def create_threshold_rule_set(
    db: Session,
    *,
    name: str,
    rules: list[dict],
    change_note: str | None,
    created_by: str,
) -> ThresholdRuleSet:
    """Append a new version of a named rule set.

    Versions are server-assigned per name (latest + 1) so a (name, version)
    pair is immutable once created. The previous active version is marked
    superseded, but existing reports are deliberately left untouched: their
    findings keep the rule version frozen at task creation time.
    """

    normalized = validate_rules(rules)
    previous = db.scalar(
        select(ThresholdRuleSet)
        .where(ThresholdRuleSet.name == name)
        .order_by(ThresholdRuleSet.version.desc())
        .limit(1)
    )
    rule_set = ThresholdRuleSet(
        name=name,
        version=(previous.version + 1) if previous else 1,
        status="active",
        rules=normalized,
        change_note=change_note,
        supersedes_id=previous.id if previous else None,
        created_by=created_by,
    )
    db.add(rule_set)
    if previous is not None:
        previous.status = "superseded"
    db.flush()
    return rule_set


def build_manifest_snapshot(
    manifest: Manifest,
    calibration: CalibrationVersion,
    params: dict,
    threshold_rule_set: ThresholdRuleSet | None = None,
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
        "threshold_rule_set": {
            "id": threshold_rule_set.id,
            "name": threshold_rule_set.name,
            "version": threshold_rule_set.version,
            "rules": threshold_rule_set.rules,
        }
        if threshold_rule_set is not None
        else None,
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
    threshold_rule_set: ThresholdRuleSet | None = None,
) -> AnalysisTask:
    if idempotency_key:
        existing = db.scalar(
            select(AnalysisTask).where(AnalysisTask.idempotency_key == idempotency_key).limit(1)
        )
        if existing is not None:
            return existing

    snapshot = build_manifest_snapshot(manifest, calibration, params, threshold_rule_set)
    task = AnalysisTask(
        manifest_id=manifest.id,
        calibration_version_id=calibration.id,
        threshold_rule_set_id=threshold_rule_set.id if threshold_rule_set is not None else None,
        status="queued",
        params=dict(params),
        manifest_snapshot=snapshot,
        idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    return task
