"""Named threshold rule versions evaluated after numeric analysis.

Findings are advisory review aids only:
- they are computed from the frozen rule set stored in the task snapshot;
- they never change ``quality_status`` and never alter computed values;
- missing metrics (``None``) are skipped and are never treated as zero.
"""

from __future__ import annotations

from typing import Any, Iterable

METRICS = ("rms", "thd_percent", "negative_sequence_percent")
SEQUENCE_GROUPS = ("voltage", "current")
ALL = "*"

_METRIC_LABELS = {
    "rms": "RMS",
    "thd_percent": "THD%",
    "negative_sequence_percent": "负序比例%",
}


def validate_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and validate raw rule dicts. Raises ValueError on any problem."""

    if not rules:
        raise ValueError("threshold rule set must contain at least one rule")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rules):
        if not isinstance(raw, dict):
            raise ValueError(f"rule {index} must be an object")
        metric = raw.get("metric")
        if metric not in METRICS:
            raise ValueError(f"rule {index}: metric must be one of {list(METRICS)}")
        channels = raw.get("channels")
        if not isinstance(channels, list) or not channels or not all(isinstance(c, str) and c for c in channels):
            raise ValueError(f"rule {index}: channels must be a non-empty list of channel names or ['*']")
        if ALL in channels and len(channels) > 1:
            raise ValueError(f"rule {index}: '*' must not be mixed with explicit channel names")
        if metric == "negative_sequence_percent":
            invalid = [c for c in channels if c != ALL and c not in SEQUENCE_GROUPS]
            if invalid:
                raise ValueError(
                    f"rule {index}: negative_sequence_percent applies to {list(SEQUENCE_GROUPS)} groups, got {invalid}"
                )
        lower = raw.get("lower")
        upper = raw.get("upper")
        if lower is None and upper is None:
            raise ValueError(f"rule {index}: at least one of lower/upper is required")
        if lower is not None and upper is not None and float(lower) > float(upper):
            raise ValueError(f"rule {index}: lower must not exceed upper")
        rate_min = raw.get("sample_rate_min")
        rate_max = raw.get("sample_rate_max")
        for label, value in (("sample_rate_min", rate_min), ("sample_rate_max", rate_max)):
            if value is not None and float(value) <= 0:
                raise ValueError(f"rule {index}: {label} must be positive")
        if rate_min is not None and rate_max is not None and float(rate_min) > float(rate_max):
            raise ValueError(f"rule {index}: sample_rate_min must not exceed sample_rate_max")
        normalized.append(
            {
                "metric": metric,
                "channels": list(channels),
                "sample_rate_min": float(rate_min) if rate_min is not None else None,
                "sample_rate_max": float(rate_max) if rate_max is not None else None,
                "lower": float(lower) if lower is not None else None,
                "upper": float(upper) if upper is not None else None,
                "unit": str(raw.get("unit") or ""),
                "note": raw.get("note"),
            }
        )
    return normalized


def _rate_matches(rule: dict[str, Any], sample_rate: float | None) -> bool:
    if sample_rate is None:
        return False
    rate_min = rule.get("sample_rate_min")
    rate_max = rule.get("sample_rate_max")
    if rate_min is not None and sample_rate < rate_min:
        return False
    if rate_max is not None and sample_rate > rate_max:
        return False
    return True


def _channel_values(rule: dict[str, Any], segment: dict[str, Any]) -> Iterable[tuple[str, float | None]]:
    metric = rule["metric"]
    wanted = rule["channels"]
    channel_results = segment.get("channels") or {}
    for channel, channel_result in channel_results.items():
        if wanted != [ALL] and channel not in wanted:
            continue
        yield channel, channel_result.get(metric)


def _sequence_values(rule: dict[str, Any], segment: dict[str, Any]) -> Iterable[tuple[str, float | None]]:
    wanted = set(rule["channels"])
    groups = segment.get("symmetrical_components") or {}
    for kind in SEQUENCE_GROUPS:
        if wanted != {ALL} and kind not in wanted:
            continue
        windows = groups.get(kind) or []
        values = [
            float(window["unbalance_percent"])
            for window in windows
            if window.get("status") == "ok" and window.get("unbalance_percent") is not None
        ]
        # Missing in every window stays missing; it is never averaged as zero.
        yield kind, (sum(values) / len(values) if values else None)


def evaluate_thresholds(result: dict[str, Any], frozen_rule_set: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Compare computed metrics against the frozen rules of one rule set version.

    Pure function over the analysis result; returns one finding per
    (rule, segment, channel/group) violation.
    """

    if not frozen_rule_set:
        return []
    findings: list[dict[str, Any]] = []
    rules = frozen_rule_set.get("rules") or []
    segments = result.get("segments") or []
    for rule_index, rule in enumerate(rules):
        metric = rule["metric"]
        lower = rule.get("lower")
        upper = rule.get("upper")
        unit = rule.get("unit") or ""
        for segment_index, segment in enumerate(segments):
            if not _rate_matches(rule, segment.get("sample_rate")):
                continue
            if metric == "negative_sequence_percent":
                candidates = _sequence_values(rule, segment)
            else:
                candidates = _channel_values(rule, segment)
            for target, measured in candidates:
                if measured is None:
                    # Missing metric: skip, never compare as zero.
                    continue
                violation = None
                if lower is not None and measured < lower:
                    violation = "below_lower"
                elif upper is not None and measured > upper:
                    violation = "above_upper"
                if violation is None:
                    continue
                findings.append(
                    {
                        "rule_index": rule_index,
                        "metric": metric,
                        "channel": target,
                        "measured": float(measured),
                        "unit": unit,
                        "lower": lower,
                        "upper": upper,
                        "violation": violation,
                        "segment_index": segment_index,
                        "sample_rate": segment.get("sample_rate"),
                        "source": {
                            "start_sequence": segment.get("start_sequence"),
                            "end_sequence": segment.get("end_sequence"),
                        },
                        "message": _format_message(metric, target, measured, unit, lower, upper, violation, segment_index),
                    }
                )
    return findings


def _format_message(
    metric: str,
    target: str,
    measured: float,
    unit: str,
    lower: float | None,
    upper: float | None,
    violation: str,
    segment_index: int,
) -> str:
    label = _METRIC_LABELS.get(metric, metric)
    suffix = f" {unit}" if unit else ""
    if violation == "below_lower":
        bound = f"低于下限 {lower}{suffix}"
    else:
        bound = f"高于上限 {upper}{suffix}"
    return f"{label} {target} = {measured:.6g}{suffix} {bound}（段 {segment_index}）"
