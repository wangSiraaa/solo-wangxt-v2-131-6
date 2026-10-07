# 主要 HTTP 接口

所有时间使用 ISO 8601。原始块编码为 `float32le-interleaved`。

## 清单与上传

### `POST /manifests`

创建不可变分块清单，请求示例：

```json
{
  "name": "line-20261001",
  "nominal_sample_rate": 6000,
  "expected_chunks": [
    {
      "sequence": 0,
      "sha256": "...64 hex...",
      "byte_offset": 0,
      "byte_length": 3600,
      "sample_count": 300,
      "sample_rate": 6000,
      "channels": ["Va", "Vb", "Vc"],
      "start_time": "2026-10-01T00:00:00",
      "end_time": "2026-10-01T00:00:00.049833333",
      "encoding": "float32le-interleaved"
    }
  ]
}
```

创建失败返回 422，`detail.issues` 中列出重叠、空洞、序号、时间或通道集合错误。

### `PUT /manifests/{manifest_id}/chunks/{sequence}/raw`

`multipart/form-data` 字段名：`chunk_file`。可乱序、重传。重复摘要返回已有块；摘要/长度错误返回 422。

### `POST /manifests/{manifest_id}/finalize`

执行最终核对。成功：

```json
{"completed": true, "already_completed": false, "warnings": []}
```

失败返回 422，issues 不做静默合并。并发时只有一个请求能完成首次转换。

### `GET /manifests/{id}/preview?calibration_version_id=...`

返回分段、采样率、降采样波形和缺口/质量 issue，供 Vue/ECharts 展示。后端使用磁盘 memmap 和降采样，避免把数 GB 原始波形全部放入响应内存。

## 标定

### `POST /calibrations`

```json
{
  "channel_set_hash": "manifest.channel_set_hash",
  "change_note": "CT 二次接线复核后修订",
  "coefficients": {
    "Va": {"gain": 1, "offset": 0, "phase_shift_rad": 0, "saturation_low": -450, "saturation_high": 450}
  }
}
```

新 active 版本会让使用旧 active 版本发布的报告进入 `needs_review`。

## 阈值规则集（命名版本）

实验室对 RMS、THD 和负序比例的复核阈值以命名规则集保存，同名重复创建自动递增版本。

### `POST /threshold-rule-sets`

```json
{
  "name": "lab-voltage",
  "change_note": "2026Q4 复核限值",
  "rules": [
    {"metric": "rms", "channels": ["*"], "lower": 280, "upper": 320, "unit": "V"},
    {"metric": "thd_percent", "channels": ["Va", "Vb", "Vc"], "upper": 8, "unit": "%"},
    {"metric": "negative_sequence_percent", "channels": ["voltage"], "upper": 2, "unit": "%",
     "sample_rate_min": 6000, "sample_rate_max": 6000}
  ]
}
```

- `metric`：`rms` / `thd_percent` / `negative_sequence_percent`。
- `channels`：具体通道名或 `["*"]`；负序比例只接受 `voltage` / `current` / `*`。
- `sample_rate_min/max`：可选，限定规则适用的固定采样率段。
- `lower/upper`：至少给一个；`unit` 仅作展示。
- 同名再次创建生成 `version+1`，旧版本置为 `superseded`；`(name, version)` 创建后不可变。
- 非法规则（无上下限、上下限倒置、采样率段倒置、负序通道非法）返回 422。

### `GET /threshold-rule-sets` / `GET /threshold-rule-sets/{id}`

按名称与版本列出或读取单个版本。

## 任务与报告

### `POST /analysis-tasks`

```json
{
  "manifest_id": "...",
  "calibration_version_id": "...",
  "threshold_rule_set_id": "...",
  "params": {"fundamental_hz": 50, "cycles_per_window": 6, "max_harmonic": 15},
  "idempotency_key": "lab-job-123"
}
```

创建时冻结清单、标定、参数和选定的阈值规则版本（`threshold_rule_set_id` 可空，空则不评估）。若使用 Celery，提交后自动发送 `run_analysis`；本地测试可调用 `/run`。

### 执行、重试、取消

- `POST /analysis-tasks/{id}/run`
- `POST /analysis-tasks/{id}/retry`
- `POST /analysis-tasks/{id}/cancel`
- `POST /maintenance/recover-stale-tasks`

### `GET /reports/{id}`

报告状态：

- `published`：正常发布；
- `needs_review`：标定后来更新，需要复核；
- `diagnostic_failed`：分析有 error 阶段，保留诊断但不是完成报告。

报告体中的阈值越界提示：

- `result.threshold_rule_set`：任务创建时冻结的规则集 `{id, name, version}`；完整规则文本在 `result.fixed_snapshot.threshold_rule_set.rules`。
- `result.threshold_findings`：逐项越界提示，每项含 `rule_index`、`metric`、`channel`、`measured`、`unit`、`lower/upper`、`violation`（`above_upper`/`below_lower`）及来源段（`segment_index`、`sample_rate`、`source.start_sequence/end_sequence`）。
- 提示仅辅助人工复核：不改变 `quality_status`，不修改已发布数值；缺失指标（如缺相时的负序比例、基波缺失时的 THD）跳过比较，绝不按 0 处理。规则集后续更新不影响旧报告，旧报告仍显示当时冻结的版本。
