"""Acceptance tests for named threshold rule set versions.

Covers:
- two rule versions on the same recording produce distinguishable findings;
- old reports keep the rule version frozen at task creation after rule edits;
- missing metrics are skipped, never compared as zero;
- findings are advisory: quality status and published values are untouched;
- sample-rate scopes restrict findings to matching segments.
"""

from fastapi.testclient import TestClient

from tests.synthetic import (
    CHANNELS,
    create_manifest,
    make_calibration,
    make_chunks,
    make_custom_chunks,
    make_rate_change_chunks,
    upload_chunks,
)


def completed_recording(client, **wave_kwargs):
    chunks = make_chunks(sample_chunks=(720,), **wave_kwargs)
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    return manifest, calibration


def make_rule_set(client, rules, name="lab-review", note=None):
    response = client.post(
        "/threshold-rule-sets",
        json={"name": name, "rules": rules, "change_note": note},
    )
    assert response.status_code == 201, response.text
    return response.json()


def run_task(client, manifest_id, calibration_id, rule_set_id=None):
    payload = {"manifest_id": manifest_id, "calibration_version_id": calibration_id}
    if rule_set_id:
        payload["threshold_rule_set_id"] = rule_set_id
    response = client.post("/analysis-tasks", json=payload)
    assert response.status_code == 201, response.text
    task = response.json()
    response = client.post(f"/analysis-tasks/{task['id']}/run")
    assert response.status_code == 200, response.text
    reports = client.get(f"/reports?manifest_id={manifest_id}").json()
    return task, next(item for item in reports if item["task_id"] == task["id"])


def test_two_rule_versions_produce_distinguishable_findings(client: TestClient):
    manifest, calibration = completed_recording(client)
    v1 = make_rule_set(
        client,
        [{"metric": "rms", "channels": ["*"], "upper": 250.0, "unit": "V"}],
        note="tight review limits",
    )
    _, report1 = run_task(client, manifest["id"], calibration["id"], v1["id"])
    findings1 = report1["result"]["threshold_findings"]
    assert report1["result"]["threshold_rule_set"]["version"] == 1
    assert {item["channel"] for item in findings1} == set(CHANNELS)
    assert all(item["violation"] == "above_upper" for item in findings1)
    assert all(item["upper"] == 250.0 and item["unit"] == "V" for item in findings1)

    v2 = make_rule_set(
        client,
        [{"metric": "rms", "channels": ["*"], "upper": 400.0, "unit": "V"}],
        note="relaxed review limits",
    )
    assert v2["version"] == 2
    assert v2["supersedes_id"] == v1["id"]
    _, report2 = run_task(client, manifest["id"], calibration["id"], v2["id"])
    assert report2["result"]["threshold_rule_set"]["version"] == 2
    assert report2["result"]["threshold_findings"] == []
    # The two reports are distinguishable by their frozen rule set version.
    assert report1["threshold_rule_set_id"] == v1["id"]
    assert report2["threshold_rule_set_id"] == v2["id"]
    assert report1["result"]["threshold_rule_set"]["id"] != report2["result"]["threshold_rule_set"]["id"]


def test_old_report_keeps_frozen_rule_version_after_rule_update(client: TestClient):
    manifest, calibration = completed_recording(client)
    v1 = make_rule_set(
        client,
        [{"metric": "rms", "channels": ["Va"], "upper": 250.0, "unit": "V"}],
    )
    _, report1 = run_task(client, manifest["id"], calibration["id"], v1["id"])
    assert len(report1["result"]["threshold_findings"]) == 1

    v2 = make_rule_set(
        client,
        [{"metric": "rms", "channels": ["Va"], "upper": 400.0, "unit": "V"}],
        note="limit raised after interlab comparison",
    )
    refreshed_v1 = client.get(f"/threshold-rule-sets/{v1['id']}").json()
    assert refreshed_v1["status"] == "superseded"
    assert v2["status"] == "active"

    reloaded = client.get(f"/reports/{report1['id']}").json()
    # Old report still shows the version selected when its task was created.
    assert reloaded["threshold_rule_set_id"] == v1["id"]
    assert reloaded["result"]["threshold_rule_set"]["version"] == 1
    assert len(reloaded["result"]["threshold_findings"]) == 1
    frozen_rules = reloaded["result"]["fixed_snapshot"]["threshold_rule_set"]["rules"]
    assert frozen_rules[0]["upper"] == 250.0
    # Advisory findings never flip the report to needs_review by themselves.
    assert reloaded["status"] == "published"


def test_missing_metrics_are_never_compared_as_zero(client: TestClient):
    # DC-only signal: the fundamental is absent, so THD is None (not 0.0).
    import numpy as np

    chunks = make_custom_chunks(np.full((720, len(CHANNELS)), 100.0, dtype=np.float64))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    rule_set = make_rule_set(
        client,
        [
            {"metric": "thd_percent", "channels": ["*"], "lower": 0.0001, "unit": "%"},
            {"metric": "rms", "channels": ["*"], "lower": 50.0, "upper": 90.0, "unit": "V"},
        ],
    )
    _, report = run_task(client, manifest["id"], calibration["id"], rule_set["id"])
    segment = report["result"]["segments"][0]
    assert all(channel["thd_percent"] is None for channel in segment["channels"].values())
    findings = report["result"]["threshold_findings"]
    # THD is missing and must not be treated as 0.0 below the lower limit.
    assert not [item for item in findings if item["metric"] == "thd_percent"]
    # RMS is present (= 100 V DC > 90 V upper) and still evaluated normally.
    assert {item["channel"] for item in findings if item["metric"] == "rms"} == set(CHANNELS)
    assert report["result"]["quality_status"] == "error"  # missing_fundamental, unchanged by findings


def test_missing_negative_sequence_is_skipped_on_diagnostic_report(client: TestClient):
    channels = ["Va", "Vb"]
    chunks = make_chunks(sample_chunks=(720,), channels=channels)
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = client.post(
        "/calibrations",
        json={
            "channel_set_hash": manifest["channel_set_hash"],
            "coefficients": {channel: {"gain": 1, "offset": 0, "phase_shift_rad": 0} for channel in channels},
        },
    ).json()
    rule_set = make_rule_set(
        client,
        [
            {"metric": "negative_sequence_percent", "channels": ["voltage"], "upper": 5.0, "unit": "%"},
            {"metric": "rms", "channels": ["*"], "upper": 250.0, "unit": "V"},
        ],
    )
    _, report = run_task(client, manifest["id"], calibration["id"], rule_set["id"])
    assert report["status"] == "diagnostic_failed"
    findings = report["result"]["threshold_findings"]
    # Unbalance is unavailable without all three phases; it is not zero.
    assert not [item for item in findings if item["metric"] == "negative_sequence_percent"]
    assert {item["channel"] for item in findings if item["metric"] == "rms"} == set(channels)


def test_findings_are_advisory_and_do_not_change_quality_or_values(client: TestClient):
    manifest, calibration = completed_recording(client)
    _, plain_report = run_task(client, manifest["id"], calibration["id"])

    rule_set = make_rule_set(
        client,
        [
            {"metric": "rms", "channels": ["*"], "upper": 250.0, "unit": "V"},
            {"metric": "thd_percent", "channels": ["Va"], "upper": 5.0, "unit": "%"},
        ],
    )
    _, report = run_task(client, manifest["id"], calibration["id"], rule_set["id"])
    assert report["result"]["threshold_findings"]
    assert report["result"]["quality_status"] == "ok"
    assert report["status"] == "published"
    # Computed values are identical to the run without any rule set.
    plain_va = plain_report["result"]["segments"][0]["channels"]["Va"]
    checked_va = report["result"]["segments"][0]["channels"]["Va"]
    assert checked_va["rms"] == plain_va["rms"]
    assert checked_va["thd_percent"] == plain_va["thd_percent"]
    assert plain_report["result"]["threshold_findings"] == []
    assert plain_report["result"]["threshold_rule_set"] is None


def test_negative_sequence_finding_carries_measured_value_and_source(client: TestClient):
    manifest, calibration = completed_recording(client, reversed_a=True)
    rule_set = make_rule_set(
        client,
        [{"metric": "negative_sequence_percent", "channels": ["voltage"], "upper": 5.0, "unit": "%"}],
    )
    _, report = run_task(client, manifest["id"], calibration["id"], rule_set["id"])
    findings = report["result"]["threshold_findings"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["channel"] == "voltage"
    assert finding["violation"] == "above_upper"
    assert abs(finding["measured"] - 200.0) < 1e-3
    assert finding["segment_index"] == 0
    assert finding["sample_rate"] == 6000.0
    assert finding["source"] == {"start_sequence": 0, "end_sequence": 0}


def test_sample_rate_scope_restricts_findings_to_matching_segments(client: TestClient):
    chunks = make_rate_change_chunks()
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    rule_set = make_rule_set(
        client,
        [
            {
                "metric": "rms",
                "channels": ["*"],
                "upper": 250.0,
                "unit": "V",
                "sample_rate_min": 6000.0,
                "sample_rate_max": 6000.0,
            }
        ],
    )
    _, report = run_task(client, manifest["id"], calibration["id"], rule_set["id"])
    findings = report["result"]["threshold_findings"]
    assert findings
    assert {item["segment_index"] for item in findings} == {0}
    assert all(item["sample_rate"] == 6000.0 for item in findings)
    assert all(item["source"] == {"start_sequence": 0, "end_sequence": 0} for item in findings)


def test_invalid_rule_set_is_rejected(client: TestClient):
    response = client.post(
        "/threshold-rule-sets",
        json={"name": "bad", "rules": [{"metric": "rms", "channels": ["Va"], "unit": "V"}]},
    )
    assert response.status_code == 422
    response = client.post(
        "/threshold-rule-sets",
        json={
            "name": "bad",
            "rules": [{"metric": "negative_sequence_percent", "channels": ["Va"], "upper": 5.0}],
        },
    )
    assert response.status_code == 422
    response = client.post(
        "/threshold-rule-sets",
        json={"name": "bad", "rules": [{"metric": "rms", "channels": ["*"], "lower": 10.0, "upper": 5.0}]},
    )
    assert response.status_code == 422


def test_task_with_unknown_rule_set_is_rejected(client: TestClient):
    manifest, calibration = completed_recording(client)
    response = client.post(
        "/analysis-tasks",
        json={
            "manifest_id": manifest["id"],
            "calibration_version_id": calibration["id"],
            "threshold_rule_set_id": "does-not-exist",
        },
    )
    assert response.status_code == 422
