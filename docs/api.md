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

## 命名复核阈值版本

阈值版本只用于实验室人工复核提示，不参与质量状态机，也不改变已发布报告数值。同名新版本会把同一通道集的旧同名版本置为 `superseded`；旧报告继续保存任务创建时冻结的规则快照。

### `POST /threshold-versions`

```json
{
  "name": "lab-rms-thd-202610",
  "channel_set_hash": "manifest.channel_set_hash",
  "change_note": "10 月实验室复核口径",
  "rules": [
    {
      "metric": "rms",
      "channels": ["Va", "Vb", "Vc"],
      "min_sample_rate_hz": 0,
      "max_sample_rate_hz": 6000,
      "lower_limit": 200,
      "upper_limit": 240,
      "unit": "V"
    },
    {
      "metric": "thd",
      "channels": ["Va", "Vb", "Vc"],
      "lower_limit": 0,
      "upper_limit": 5,
      "unit": "%"
    },
    {
      "metric": "negative_sequence_ratio",
      "channels": ["voltage"],
      "lower_limit": 0,
      "upper_limit": 2,
      "unit": "%"
    }
  ]
}
```

采样率段为闭区间；边界字段传 `null` 表示该侧不限制。`rms` 和 `thd` 的 `channels` 为具体通道；`negative_sequence_ratio` 使用 `voltage` 或 `current` 分量组。服务端不做单位换算，单位随规则保存并在提示中原样展示。

### `GET /threshold-versions?channel_set_hash=...&name=...`

按名称和通道集查询全部版本，包括已被替代版本。

## 任务与报告

### `POST /analysis-tasks`

```json
{
  "manifest_id": "...",
  "calibration_version_id": "...",
  "threshold_version_id": "...",
  "params": {"fundamental_hz": 50, "cycles_per_window": 6, "max_harmonic": 15},
  "idempotency_key": "lab-job-123"
}
```

创建时冻结清单、标定和参数。若选择阈值版本，也会完整冻结规则、版本 id、名称和状态；执行时不再读取可变阈值行。若使用 Celery，提交后自动发送 `run_analysis`；本地测试可调用 `/run`。

报告数值计算完成后生成 `result.threshold_review`：

- `advisory_only: true`：仅辅助人工复核，不写回 `quality_status`；
- `version_id/version_name/rules`：本报告当时冻结的版本；
- `violations[]`：逐项列出规则、上下限、单位、实测值、通道/分量和来源采样率段（负序比例还包含窗口）；
- 缺失指标（`null`）直接跳过，不会按 0 比较；无阈值版本时 `applied: false`。

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
