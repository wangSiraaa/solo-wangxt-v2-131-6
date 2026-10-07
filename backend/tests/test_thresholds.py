from fastapi.testclient import TestClient

from tests.synthetic import (
    create_manifest,
    make_calibration,
    make_chunks,
    make_rate_change_chunks,
    upload_chunks,
)


def completed_recording(client: TestClient):
    chunks = make_chunks(sample_chunks=(720,))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    return manifest, calibration


def make_threshold(client: TestClient, channel_set_hash: str, version_note: str, *, rms_upper: float, thd_upper: float):
    response = client.post(
        "/threshold-versions",
        json={
            "name": "lab-review-rules",
            "channel_set_hash": channel_set_hash,
            "change_note": version_note,
            "rules": [
                {
                    "metric": "rms",
                    "channels": ["Va"],
                    "min_sample_rate_hz": 0,
                    "max_sample_rate_hz": 6000,
                    "lower_limit": 0,
                    "upper_limit": rms_upper,
                    "unit": "V",
                },
                {
                    "metric": "thd",
                    "channels": ["Va"],
                    "min_sample_rate_hz": 0,
                    "max_sample_rate_hz": 6000,
                    "lower_limit": 0,
                    "upper_limit": thd_upper,
                    "unit": "%",
                },
                {
                    "metric": "negative_sequence_ratio",
                    "channels": ["voltage"],
                    "lower_limit": 0,
                    "upper_limit": 1,
                    "unit": "%",
                },
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_and_run_with_threshold(client: TestClient, manifest_id: str, calibration_id: str, threshold_id: str):
    response = client.post(
        "/analysis-tasks",
        json={
            "manifest_id": manifest_id,
            "calibration_version_id": calibration_id,
            "threshold_version_id": threshold_id,
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    response = client.post(f"/analysis-tasks/{task['id']}/run")
    assert response.status_code == 200, response.text
    return task["id"]


def report_for_task(client: TestClient, manifest_id: str, task_id: str):
    reports = client.get(f"/reports?manifest_id={manifest_id}").json()
    return next(report for report in reports if report["task_id"] == task_id)


def test_two_frozen_threshold_versions_produce_distinguishable_advisory_violations(client: TestClient):
    manifest, calibration = completed_recording(client)
    strict = make_threshold(
        client,
        manifest["channel_set_hash"],
        "strict initial limits",
        rms_upper=300,
        thd_upper=5,
    )
    strict_task = create_and_run_with_threshold(client, manifest["id"], calibration["id"], strict["id"])
    strict_report = report_for_task(client, manifest["id"], strict_task)
    strict_review = strict_report["result"]["threshold_review"]

    assert strict_report["threshold_version_id"] == strict["id"]
    assert strict_review["version_id"] == strict["id"]
    assert strict_review["version_name"] == "lab-review-rules"
    assert strict_review["advisory_only"] is True
    assert strict_review["violation_count"] == 2
    assert {item["metric"] for item in strict_review["violations"]} == {"rms", "thd"}
    assert strict_report["result"]["quality_status"] == "ok"
    rms_violation = next(item for item in strict_review["violations"] if item["metric"] == "rms")
    assert rms_violation["measured_value"] > 300
    assert rms_violation["source"] == {
        "segment_index": 0,
        "sample_rate_hz": 6000.0,
        "start_sequence": 0,
        "end_sequence": 0,
    }

    relaxed = make_threshold(
        client,
        manifest["channel_set_hash"],
        "relaxed reviewed limits",
        rms_upper=310,
        thd_upper=10,
    )
    relaxed_task = create_and_run_with_threshold(client, manifest["id"], calibration["id"], relaxed["id"])
    relaxed_report = report_for_task(client, manifest["id"], relaxed_task)
    relaxed_review = relaxed_report["result"]["threshold_review"]

    versions = {item["id"]: item for item in client.get("/threshold-versions").json()}
    assert versions[strict["id"]]["status"] == "superseded"
    assert versions[relaxed["id"]]["status"] == "active"
    assert relaxed_review["version_id"] == relaxed["id"]
    assert relaxed_review["violation_count"] == 0
    # Same measured values are retained; only frozen rule selection differs.
    assert relaxed_report["result"]["segments"][0]["channels"]["Va"]["rms"] == rms_violation["measured_value"]
    assert strict_report["result"]["quality_status"] == "ok"


def test_changing_named_threshold_does_not_change_old_report_snapshot_or_status(client: TestClient):
    manifest, calibration = completed_recording(client)
    initial = make_threshold(
        client,
        manifest["channel_set_hash"],
        "old limits",
        rms_upper=300,
        thd_upper=5,
    )
    old_task = create_and_run_with_threshold(client, manifest["id"], calibration["id"], initial["id"])
    old_report_before = report_for_task(client, manifest["id"], old_task)
    assert old_report_before["status"] == "published"

    revised = make_threshold(
        client,
        manifest["channel_set_hash"],
        "new limits",
        rms_upper=310,
        thd_upper=10,
    )
    old_report_after = client.get(f"/reports/{old_report_before['id']}").json()
    old_review = old_report_after["result"]["threshold_review"]

    assert old_report_after["status"] == "published"
    assert old_report_after["threshold_version_id"] == initial["id"]
    assert old_review["version_id"] == initial["id"]
    assert old_review["status"] == "active"
    assert old_review["rules"][0]["upper_limit"] == 300
    assert old_review["rules"][1]["upper_limit"] == 5
    assert revised["supersedes_id"] == initial["id"]


def test_missing_metrics_are_skipped_instead_of_compared_as_zero(client: TestClient):
    chunks = make_chunks(sample_chunks=(720,), zero=True)
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    threshold = client.post(
        "/threshold-versions",
        json={
            "name": "missing-metric-rules",
            "channel_set_hash": manifest["channel_set_hash"],
            "rules": [
                {
                    "metric": "thd",
                    "channels": ["Va"],
                    "lower_limit": 5,
                    "upper_limit": 10,
                    "unit": "%",
                },
                {
                    "metric": "negative_sequence_ratio",
                    "channels": ["voltage"],
                    "lower_limit": 1,
                    "upper_limit": 10,
                    "unit": "%",
                },
            ],
        },
    ).json()

    task_id = create_and_run_with_threshold(client, manifest["id"], calibration["id"], threshold["id"])
    report = report_for_task(client, manifest["id"], task_id)
    review = report["result"]["threshold_review"]

    assert report["result"]["segments"][0]["channels"]["Va"]["thd_percent"] is None
    assert report["result"]["segments"][0]["symmetrical_components"]["voltage"][0]["unbalance_percent"] is None
    assert review["violations"] == []
    assert review["evaluated_count"] == 0


def test_threshold_rule_records_applicable_sample_rate_segment(client: TestClient):
    chunks = make_rate_change_chunks()
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    threshold = client.post(
        "/threshold-versions",
        json={
            "name": "segment-specific-rms",
            "channel_set_hash": manifest["channel_set_hash"],
            "rules": [
                {
                    "metric": "rms",
                    "channels": ["Va"],
                    "min_sample_rate_hz": 6900,
                    "max_sample_rate_hz": 7100,
                    "upper_limit": 300,
                    "unit": "V",
                }
            ],
        },
    ).json()

    task_id = create_and_run_with_threshold(client, manifest["id"], calibration["id"], threshold["id"])
    report = report_for_task(client, manifest["id"], task_id)
    violations = report["result"]["threshold_review"]["violations"]

    assert len(violations) == 1
    assert violations[0]["source"]["segment_index"] == 1
    assert violations[0]["source"]["sample_rate_hz"] == 7000.0
    assert violations[0]["source"]["start_sequence"] == 1
    assert violations[0]["source"]["end_sequence"] == 1


def test_task_without_threshold_keeps_review_advisory_empty(client: TestClient):
    manifest, calibration = completed_recording(client)
    response = client.post(
        "/analysis-tasks",
        json={"manifest_id": manifest["id"], "calibration_version_id": calibration["id"]},
    )
    task_id = response.json()["id"]
    client.post(f"/analysis-tasks/{task_id}/run")
    report = report_for_task(client, manifest["id"], task_id)
    review = report["result"]["threshold_review"]

    assert report["threshold_version_id"] is None
    assert review["applied"] is False
    assert review["advisory_only"] is True
    assert review["violations"] == []
