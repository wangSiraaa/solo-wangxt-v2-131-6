from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .dsp import identify_phase

METRIC_LABELS = {
    "rms": "RMS",
    "thd": "THD",
    "negative_sequence_ratio": "负序比例",
}


def _rate_matches(rule: dict[str, Any], sample_rate: float) -> bool:
    minimum = rule.get("min_sample_rate_hz")
    maximum = rule.get("max_sample_rate_hz")
    if minimum is not None and sample_rate < float(minimum):
        return False
    if maximum is not None and sample_rate > float(maximum):
        return False
    return True


def _rule_channels(rule: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(str(channel) for channel in rule.get("channels", [])))


def _is_group_name(channel: str) -> bool:
    return channel.strip().lower() in {"voltage", "current"}


def _channel_kind(channel: str) -> str | None:
    normalized = channel.strip().lower().replace("-", "_")
    if normalized in {"voltage", "current"}:
        return normalized
    if normalized.startswith(("i", "current")) or normalized.endswith(("_ia", "_ib", "_ic")):
        return "current"
    if identify_phase(channel) is not None:
        return "voltage"
    return None


def _selected_actual_channels(rule_channels: Iterable[str], available_channels: Iterable[str]) -> list[str]:
    selected: list[str] = []
    for channel in rule_channels:
        if not _is_group_name(channel):
            if channel in available_channels:
                selected.append(channel)
            continue
        kind = channel.strip().lower()
        for actual in available_channels:
            if _channel_kind(actual) == kind and actual not in selected:
                selected.append(actual)
    return selected


def _limit_violation(value: float, rule: dict[str, Any]) -> str | None:
    lower = rule.get("lower_limit")
    upper = rule.get("upper_limit")
    if lower is not None and value < float(lower):
        return "below_lower_limit"
    if upper is not None and value > float(upper):
        return "above_upper_limit"
    return None


def _source_segment(segment: dict[str, Any], segment_index: int) -> dict[str, Any]:
    return {
        "segment_index": segment_index,
        "sample_rate_hz": segment["sample_rate"],
        "start_sequence": segment.get("start_sequence", segment_index),
        "end_sequence": segment.get("end_sequence", segment_index),
    }


def _make_violation(
    *,
    rule: dict[str, Any],
    metric: str,
    channel: str,
    measured_value: float,
    source: dict[str, Any],
    violation: str,
) -> dict[str, Any]:
    return {
        "rule_id": rule["id"],
        "metric": metric,
        "metric_label": METRIC_LABELS[metric],
        "channel": channel,
        "phase": identify_phase(channel) if metric != "negative_sequence_ratio" else None,
        "sample_rate_segment": {
            "min_sample_rate_hz": rule.get("min_sample_rate_hz"),
            "max_sample_rate_hz": rule.get("max_sample_rate_hz"),
        },
        "lower_limit": rule.get("lower_limit"),
        "upper_limit": rule.get("upper_limit"),
        "unit": rule["unit"],
        "measured_value": measured_value,
        "violation": violation,
        "source": source,
    }


def evaluate_thresholds(result: dict[str, Any], threshold_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Check calculated values against the frozen, named threshold rules.

    This result is advisory only: it is separate from ``quality_status``. A
    missing metric is represented by ``None`` and is deliberately skipped.
    """

    rules = [dict(rule) for rule in threshold_snapshot.get("rules", [])]
    violations: list[dict[str, Any]] = []
    evaluated_count = 0

    for segment_index, segment in enumerate(result.get("segments", [])):
        sample_rate = float(segment["sample_rate"])
        channels = segment.get("channels", {})
        available_channels = list(channels.keys())
        source_segment = _source_segment(segment, segment_index)

        for rule_index, rule in enumerate(rules):
            rule.setdefault("id", f"rule-{rule_index + 1}")
            metric = rule["metric"]
            if not _rate_matches(rule, sample_rate):
                continue

            if metric in {"rms", "thd"}:
                value_key = "rms" if metric == "rms" else "thd_percent"
                for channel in _selected_actual_channels(_rule_channels(rule), available_channels):
                    value = channels[channel].get(value_key)
                    if value is None:
                        continue
                    evaluated_count += 1
                    violation = _limit_violation(float(value), rule)
                    if violation:
                        violations.append(
                            _make_violation(
                                rule=rule,
                                metric=metric,
                                channel=channel,
                                measured_value=float(value),
                                source=source_segment,
                                violation=violation,
                            )
                        )
            elif metric == "negative_sequence_ratio":
                for group in _rule_channels(rule):
                    kind = group.strip().lower()
                    if kind not in {"voltage", "current"}:
                        continue
                    group_windows = segment.get("symmetrical_components", {}).get(kind, [])
                    for window in group_windows:
                        if window.get("status") != "ok":
                            continue
                        value = window.get("unbalance_percent")
                        if value is None:
                            continue
                        evaluated_count += 1
                        violation = _limit_violation(float(value), rule)
                        if violation:
                            source = {
                                **source_segment,
                                "window_index": window.get("window_index"),
                                "start_sample": window.get("start_sample"),
                                "end_sample": window.get("end_sample"),
                            }
                            violations.append(
                                _make_violation(
                                    rule=rule,
                                    metric=metric,
                                    channel=kind,
                                    measured_value=float(value),
                                    source=source,
                                    violation=violation,
                                )
                            )

    return {
        "advisory_only": True,
        "applied": True,
        "version_id": threshold_snapshot["id"],
        "version_name": threshold_snapshot["name"],
        "status": threshold_snapshot["status"],
        "rules": rules,
        "evaluated_count": evaluated_count,
        "violation_count": len(violations),
        "violations": violations,
    }


def empty_threshold_review() -> dict[str, Any]:
    return {
        "advisory_only": True,
        "applied": False,
        "version_id": None,
        "version_name": None,
        "status": None,
        "rules": [],
        "evaluated_count": 0,
        "violation_count": 0,
        "violations": [],
    }
