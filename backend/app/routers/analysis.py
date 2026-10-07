from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..dsp import default_params
from ..models import AnalysisTask, CalibrationVersion, Manifest, Report, ThresholdVersion
from ..pipeline import execute_task, recover_stale_tasks, request_cancel, request_retry
from ..schemas import (
    AnalysisCreate,
    CalibrationCreate,
    CalibrationOut,
    ReportOut,
    RetryOut,
    TaskOut,
    ThresholdVersionCreate,
    ThresholdVersionOut,
)
from ..services import (
    create_analysis_task,
    create_calibration,
    create_threshold_version,
    get_active_calibration,
)

router = APIRouter(tags=["analysis"])


@router.post("/calibrations", response_model=CalibrationOut, status_code=201)
def post_calibration(payload: CalibrationCreate, db: Session = Depends(get_db)):
    try:
        version = create_calibration(
            db,
            channel_set_hash=payload.channel_set_hash,
            coefficients=payload.coefficients,
            change_note=payload.change_note,
            created_by=payload.created_by,
            mark_previous_for_review=payload.mark_previous_for_review,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return version


@router.get("/calibrations", response_model=list[CalibrationOut])
def list_calibrations(channel_set_hash: str | None = None, db: Session = Depends(get_db)):
    stmt = select(CalibrationVersion).order_by(CalibrationVersion.created_at.desc())
    if channel_set_hash:
        stmt = stmt.where(CalibrationVersion.channel_set_hash == channel_set_hash)
    return db.scalars(stmt).all()


@router.get("/calibrations/{version_id}", response_model=CalibrationOut)
def get_calibration(version_id: str, db: Session = Depends(get_db)):
    version = db.get(CalibrationVersion, version_id)
    if version is None:
        raise HTTPException(404, "calibration version not found")
    return version


@router.post("/threshold-versions", response_model=ThresholdVersionOut, status_code=201)
def post_threshold_version(payload: ThresholdVersionCreate, db: Session = Depends(get_db)):
    try:
        version = create_threshold_version(
            db,
            name=payload.name,
            channel_set_hash=payload.channel_set_hash,
            rules=payload.rules,
            change_note=payload.change_note,
            created_by=payload.created_by,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    db.refresh(version)
    return version


@router.get("/threshold-versions", response_model=list[ThresholdVersionOut])
def list_threshold_versions(
    channel_set_hash: str | None = None,
    name: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(ThresholdVersion).order_by(
        ThresholdVersion.name.asc(), ThresholdVersion.created_at.desc()
    )
    if channel_set_hash:
        stmt = stmt.where(ThresholdVersion.channel_set_hash == channel_set_hash)
    if name:
        stmt = stmt.where(ThresholdVersion.name == name)
    return db.scalars(stmt).all()


@router.get("/threshold-versions/{version_id}", response_model=ThresholdVersionOut)
def get_threshold_version(version_id: str, db: Session = Depends(get_db)):
    version = db.get(ThresholdVersion, version_id)
    if version is None:
        raise HTTPException(404, "threshold version not found")
    return version


@router.post("/analysis-tasks", response_model=TaskOut, status_code=201)
def post_analysis(payload: AnalysisCreate, db: Session = Depends(get_db)):
    manifest = db.get(Manifest, payload.manifest_id)
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    if manifest.status != "completed":
        raise HTTPException(422, f"manifest is {manifest.status}; complete immutable upload validation first")

    if payload.calibration_version_id:
        calibration = db.get(CalibrationVersion, payload.calibration_version_id)
    else:
        calibration = get_active_calibration(db, manifest.channel_set_hash)
    if calibration is None:
        raise HTTPException(422, "no calibration version specified or active for channel set")
    if calibration.channel_set_hash != manifest.channel_set_hash:
        raise HTTPException(422, "selected calibration does not belong to this channel set")

    threshold = None
    if payload.threshold_version_id:
        threshold = db.get(ThresholdVersion, payload.threshold_version_id)
        if threshold is None:
            raise HTTPException(422, "selected threshold version does not exist")
        if threshold.channel_set_hash != manifest.channel_set_hash:
            raise HTTPException(422, "selected threshold version does not belong to this channel set")

    params = {**default_params(), **payload.params}
    task = create_analysis_task(
        db,
        manifest=manifest,
        calibration=calibration,
        params=params,
        idempotency_key=payload.idempotency_key,
        threshold=threshold,
    )
    db.commit()
    db.refresh(task)
    # The Celery send is deliberately after commit. In local/test mode a missing
    # broker leaves the task queued; /run or a worker can execute it later. The
    # DB lease remains the final duplicate-execution boundary.
    try:
        from ..celery_app import run_analysis

        run_analysis.delay(task.id)
    except Exception:
        pass
    return task


@router.get("/analysis-tasks", response_model=list[TaskOut])
def list_tasks(manifest_id: str | None = None, db: Session = Depends(get_db)):
    stmt = select(AnalysisTask).order_by(AnalysisTask.requested_at.desc())
    if manifest_id:
        stmt = stmt.where(AnalysisTask.manifest_id == manifest_id)
    return db.scalars(stmt).all()


@router.get("/analysis-tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@router.post("/analysis-tasks/{task_id}/run", response_model=dict)
def run_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    db.commit()
    return execute_task(task_id)


@router.post("/analysis-tasks/{task_id}/retry", response_model=RetryOut)
def retry_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if task.status not in {"failed", "retry_wait", "running", "cancelled"}:
        raise HTTPException(409, f"cannot retry task in status {task.status}")
    if task.report is not None and task.report.status == "published":
        raise HTTPException(409, "published report cannot be replaced by a retry")
    request_retry(db, task)
    db.commit()
    return {"task_id": task.id, "status": task.status, "attempts": task.attempts}


@router.post("/analysis-tasks/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    request_cancel(db, task)
    db.commit()
    db.refresh(task)
    return task


@router.post("/maintenance/recover-stale-tasks")
def recover_tasks(db: Session = Depends(get_db)):
    return {"recovered": recover_stale_tasks(db)}


@router.get("/reports", response_model=list[ReportOut])
def list_reports(manifest_id: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Report).order_by(Report.published_at.desc().nullslast())
    if manifest_id:
        stmt = stmt.where(Report.manifest_id == manifest_id)
    return db.scalars(stmt).all()


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: str, db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "report not found")
    return report
