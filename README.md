# 家庭储能时段调度 API

基于 **Python + FastAPI + Pydantic** 的家庭储能调度服务。输入等长的半小时电价、基础负荷、光伏预测序列以及电池参数，用**动态规划（SOC 离散到 0.1 kWh）**求出满足功率、SOC 和末端 SOC 约束的**最低总成本**充放电计划；另提供 `/telemetry/audit` 对半小时实测记录做能量守恒与购电量**审计**（只核对标记，不修正读数、不生成调度计划）；`/dispatch/settlement` 对一份已下发计划做**执行结算**：携带原始 `/dispatch` 输入与计划快照，服务重算校验快照未被篡改、复用遥测审计逐条核对，并只对首个异常 / SOC 断点之前的连续有效区间按实测购电量与吞吐量计费；`/dispatch/replan` 支持计划**执行到中途**时依据连续实测前缀**续排**：核对原计划与前缀审计、保留实测原记录并从实测末态重排余段，同时返回可直接交给结算接口的余段调度输入与计划快照。`/dispatch/sessions` 进一步提供**可持久接续的执行会话**：实测记录可分多批到达，创建会话时提交原 `/dispatch` 输入、完整计划快照与审计容差（先重算确认快照一致），之后按时段下标成批追加记录，首批首态接原初始 SOC、后续批次首态接已保存末态，沿用现有遥测审计逐段核对且**整批原子入库**；同一起点原样重试幂等、内容不同的并发上报只有一份生效；重试去重只认**已确认整批**（起点、长度、记录内容三者一致），且幂等回执冻结在该批首次成功时；会话与记录落在 **SQLite（命名卷）**，批次边界、首次回执与逐段记录在同一写事务内确认，进程重启、跨进程并发、后续批次入库后重试均遵守同一规则，旧库逐段记录保留查询与 `next_index` 续报能力且不按前缀猜测去重，进程重启后仍可查询、续报，未完成前缀可直接续排、满期记录可直接结算。

## 一条命令启动

需要 Docker 20.10+（带 Compose v2）：

```bash
docker compose up --build
```

服务启动后：

- 健康检查：<http://localhost:8000/healthz>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：`Ctrl+C`（后台运行用 `docker compose up -d --build`，停止用 `docker compose down`）。

本地裸机运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

### `GET /healthz`

```json
{"status": "ok"}
```

### `POST /dispatch`

请求体（每个时段固定 **0.5 小时**，三个数组必须等长，长度 1–200）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `prices` | 元/kWh | 各时段购电电价（非负） |
| `base_load_kw` | kW | 各时段基础负荷 |
| `pv_kw` | kW | 各时段光伏预测功率 |
| `capacity_kwh` | kWh | 电池容量，必须为 0.1 的整数倍 |
| `max_charge_kw` / `max_discharge_kw` | kW | 最大充 / 放电**交流侧**功率 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `initial_soc_kwh` | kWh | 初始 SOC（0.1 整数倍，≤ 容量） |
| `min_terminal_soc_kwh` | kWh | 最低末端 SOC（0.1 整数倍，≤ 容量） |
| `degradation_cost_per_kwh` | 元/kWh | 按电芯吞吐量计的循环衰减成本 |

示例：

```bash
curl -s -X POST http://localhost:8000/dispatch \
  -H 'Content-Type: application/json' \
  -d '{
    "prices": [1.0, 0.0, 1.0],
    "base_load_kw": [0.2, 0.2, 0.2],
    "pv_kw": [0.0, 0.0, 0.0],
    "capacity_kwh": 0.2,
    "max_charge_kw": 1.0,
    "max_discharge_kw": 1.0,
    "charge_efficiency": 1.0,
    "discharge_efficiency": 1.0,
    "initial_soc_kwh": 0.0,
    "min_terminal_soc_kwh": 0.0,
    "degradation_cost_per_kwh": 0.0
  }'
```

响应：`periods` 为逐时段计划，`summary` 为汇总。

```json
{
  "periods": [
    {
      "index": 0,
      "action": "idle",                 // charge / discharge / idle
      "action_energy_kwh": 0.0,         // 电芯侧能量变化：正=充，负=放，0.1 整数倍
      "charge_ac_energy_kwh": 0.0,      // 该时段电池吸收的交流电量
      "discharge_ac_energy_kwh": 0.0,   // 该时段电池放出的交流电量
      "battery_power_kw": 0.0,          // 正=充电功率，负=放电功率
      "grid_purchase_energy_kwh": 0.1,  // 购电量
      "pv_curtailed_energy_kwh": 0.0,   // 弃光量
      "soc_start_kwh": 0.0,
      "soc_end_kwh": 0.0,
      "energy_cost_yuan": 0.1,          // 该时段能量费
      "degradation_cost_yuan": 0.0      // 该时段衰减费
    }
  ],
  "summary": {
    "num_periods": 3,
    "period_hours": 0.5,
    "soc_resolution_kwh": 0.1,
    "total_energy_cost_yuan": 0.1,
    "total_degradation_cost_yuan": 0.0,
    "total_cost_yuan": 0.1,
    "total_grid_purchase_kwh": 0.2,
    "peak_grid_purchase_energy_kwh": 0.2,
    "peak_grid_purchase_power_kw": 0.4,
    "battery_throughput_kwh": 0.1,
    "terminal_soc_kwh": 0.0
  }
}
```

### `POST /telemetry/audit`

对按时间顺序排列的半小时**实测记录**逐条做台账核对：电芯侧能量守恒、购电量功率平衡、相邻记录 SOC 衔接。只审计并标记异常，**不修正读数、不生成调度计划**。

请求体：

| 字段 | 单位 | 说明 |
|---|---|---|
| `capacity_kwh` | kWh | 电池容量，须为正 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `energy_tolerance_kwh` | kWh | 能量容差（非负）；任一分项残差绝对值**严格大于**它即判该条异常 |
| `records` | – | 按序排列的半小时实测记录，长度 1–2000 |

`records` 每条记录的字段（均须有限且非负，SOC 不得超过容量）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `base_load_kw` | kW | 基础负荷功率 |
| `pv_kw` | kW | 光伏功率 |
| `charge_ac_energy_kwh` / `discharge_ac_energy_kwh` | kWh | 交流侧充 / 放电量 |
| `grid_purchase_energy_kwh` | kWh | 电网购电量 |
| `soc_start_kwh` / `soc_end_kwh` | kWh | 时段首 / 末 SOC |

逐条核算规则（与 `/dispatch` 的模型假设一致）：

```
期望末态 SOC = 首态 SOC + 交流充电量×充电效率 − 交流放电量÷放电效率
期望购电量   = max(0, (基础负荷 − 光伏)×0.5 + 交流充电量 − 交流放电量)
SOC 衔接差   = 首态 SOC − 上一条记录的末态 SOC（首条记 0）
```

响应中每条记录给出三个分项残差（实测 − 核算）：`soc_residual_kwh`、`grid_residual_kwh`、`soc_continuity_gap_kwh`，任一绝对值超容差则该条 `anomalous=true` 并在 `reasons` 列出原因；顶层 `anomalous_records` 汇总异常下标。审计结果正常返回 **HTTP 200**（异常是核对结论，不是请求错误）。

示例（第二条记录购电量实测 0.7，核算仅 0.55，残差 0.15 超容差）：

```bash
curl -s -X POST http://localhost:8000/telemetry/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity_kwh": 10.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
    "energy_tolerance_kwh": 0.05,
    "records": [
      {"base_load_kw": 1.0, "pv_kw": 0.4, "charge_ac_energy_kwh": 0.5,
       "discharge_ac_energy_kwh": 0.0, "grid_purchase_energy_kwh": 0.8,
       "soc_start_kwh": 2.0, "soc_end_kwh": 2.45},
      {"base_load_kw": 2.0, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.0,
       "discharge_ac_energy_kwh": 0.45, "grid_purchase_energy_kwh": 0.7,
       "soc_start_kwh": 2.45, "soc_end_kwh": 1.95}
    ]
  }'
```

```json
{
  "num_records": 2,
  "period_hours": 0.5,
  "energy_tolerance_kwh": 0.05,
  "anomalous": true,
  "anomalous_records": [1],
  "records": [
    {
      "index": 0,
      "expected_soc_end_kwh": 2.45,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.8,
      "grid_residual_kwh": 0.0,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": false,
      "reasons": []
    },
    {
      "index": 1,
      "expected_soc_end_kwh": 1.95,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.55,
      "grid_residual_kwh": 0.15,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": true,
      "reasons": ["购电量残差 +0.150000kWh 超容差 0.05kWh（实测 0.7kWh，核算值 0.55kWh）"]
    }
  ]
}
```

### `POST /dispatch/settlement`

对已下发的调度计划做执行结算。请求携带四样东西：

| 字段 | 说明 |
|---|---|
| `dispatch` | 一次 `/dispatch` 的**完整输入**（电价/负荷/光伏序列 + 全部电池参数） |
| `plan_snapshot` | 当初 `/dispatch` 返回的计划快照（`periods` + `summary` 原样回传） |
| `settlement_tolerance_kwh` | 结算容差（kWh，非负）；遥测三类残差与 SOC 轨迹偏离均按「绝对值严格大于容差」判定 |
| `records` | 与计划**等长**的半小时实测记录（字段同 `/telemetry/audit`） |

服务端处理顺序：

1. **重算防篡改**：用现有求解器对 `dispatch` 重算，逐字段（动作、各能量、SOC、逐时段费用、summary）比对 `plan_snapshot`，任一处不一致整体拒绝（`plan_snapshot_mismatch`）；
2. **遥测审计**：复用 `/telemetry/audit` 的同一套核算，逐条检查电芯能量守恒、购电量平衡、相邻记录 SOC 衔接；
3. **SOC 断点**：额外比对每条记录的首 / 末 SOC 与计划 SOC 轨迹，偏差超容差即为断点（实际执行偏离了计划状态轨迹，后续时段的计划口径不再可比）；
4. **冻结待结算**：从**首条异常或 SOC 断点**（含该条）起，后续记录一律标为 `pending`（待结算），即使后面数据恢复自洽也不继续计费；
5. **有效区间计费**：对此前连续有效前缀（下标 0 起），逐时段按**实测购电量**算能量费、按**实测电芯吞吐量**（交流充电×ηc + 交流放电÷ηd）算衰减费，并给出与计划费用的差额及逐时段偏差。

`status=settled` 表示全部记录连续有效、整体结算；`status=partially_settled` 表示存在异常 / 断点，仅前缀结算。实测与预测的偏差（例如实际负荷高于预测导致多购电，但 SOC 轨迹不变）不属于异常，照常结算并计入费用差额。

整体拒绝（HTTP 422）的情形：

- `plan_snapshot_mismatch`：快照与重算结果不一致（被篡改或缺字段）；重算本身无解时为 `infeasible`；
- `invalid_input`：记录长度与计划时段数不符、容差为负 / 非有限、测量值为负 / 非有限、SOC 超容量等；
- `no_valid_interval`：**首条**记录即异常或 SOC 断点，不存在任何有效区间。

示例（全部记录有效；t1 购电表读数 0.015 比计划多 0.005kWh，在 0.01kWh 容差内，照常结算并计 0.005 元能量费差额）：

```bash
curl -s -X POST http://localhost:8000/dispatch/settlement \
  -H 'Content-Type: application/json' \
  -d '{
    "dispatch": {
      "prices": [0.0, 1.0, 1.0, 0.0],
      "base_load_kw": [0.2, 0.2, 0.2, 0.2],
      "pv_kw": [0.0, 0.0, 0.0, 0.0],
      "capacity_kwh": 0.4,
      "max_charge_kw": 1.0,
      "max_discharge_kw": 1.0,
      "charge_efficiency": 0.9,
      "discharge_efficiency": 0.9,
      "initial_soc_kwh": 0.0,
      "min_terminal_soc_kwh": 0.0,
      "degradation_cost_per_kwh": 0.2
    },
    "plan_snapshot": { "...": "原样回传 /dispatch 的完整响应（periods + summary）" },
    "settlement_tolerance_kwh": 0.01,
    "records": [
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.015, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0}
    ]
  }'
```

```json
{
  "num_records": 4,
  "period_hours": 0.5,
  "settlement_tolerance_kwh": 0.01,
  "status": "settled",
  "settled_periods": 4,
  "pending_periods": 0,
  "first_anomaly_index": null,
  "anomalous_records": [],
  "soc_breakpoint_records": [],
  "periods": [
    {
      "index": 1,
      "status": "settled",
      "plan": {
        "action": "discharge",
        "grid_purchase_energy_kwh": 0.01,
        "battery_throughput_kwh": 0.1,
        "energy_cost_yuan": 0.01,
        "degradation_cost_yuan": 0.02,
        "soc_start_kwh": 0.2,
        "soc_end_kwh": 0.1
      },
      "soc_gap_start_kwh": 0.0,
      "soc_gap_end_kwh": 0.0,
      "actual_grid_purchase_energy_kwh": 0.015,
      "actual_battery_throughput_kwh": 0.1,
      "actual_energy_cost_yuan": 0.015,
      "actual_degradation_cost_yuan": 0.02,
      "actual_total_cost_yuan": 0.035,
      "planned_energy_cost_yuan": 0.01,
      "planned_degradation_cost_yuan": 0.02,
      "planned_total_cost_yuan": 0.03,
      "energy_cost_delta_yuan": 0.005,
      "degradation_cost_delta_yuan": 0.0,
      "total_cost_delta_yuan": 0.005,
      "grid_purchase_delta_kwh": 0.005,
      "throughput_delta_kwh": 0.0,
      "audit": {
        "index": 1,
        "expected_soc_end_kwh": 0.1,
        "soc_residual_kwh": 0.0,
        "expected_grid_purchase_energy_kwh": 0.015,
        "grid_residual_kwh": 0.0,
        "soc_continuity_gap_kwh": 0.0,
        "anomalous": false,
        "reasons": [],
        "soc_breakpoint": false,
        "soc_breakpoint_reasons": []
      },
      "reasons": []
    }
  ],
  "summary": {
    "settled_periods": 4,
    "actual_grid_purchase_kwh": 0.447222,
    "planned_grid_purchase_kwh": 0.442222,
    "grid_purchase_delta_kwh": 0.005,
    "actual_battery_throughput_kwh": 0.4,
    "planned_battery_throughput_kwh": 0.4,
    "throughput_delta_kwh": 0.0,
    "actual_energy_cost_yuan": 0.025,
    "planned_energy_cost_yuan": 0.02,
    "energy_cost_delta_yuan": 0.005,
    "actual_degradation_cost_yuan": 0.08,
    "planned_degradation_cost_yuan": 0.08,
    "degradation_cost_delta_yuan": 0.0,
    "actual_total_cost_yuan": 0.105,
    "planned_total_cost_yuan": 0.1,
    "total_cost_delta_yuan": 0.005
  },
  "audit_summary": {"anomalous": false, "anomalous_records": []}
}
```

若 t2 实测购电量为 0.11（核算值 0.01，残差 0.1 超容差），则 t0/t1 正常结算、t2 起冻结：

```json
{
  "status": "partially_settled",
  "settled_periods": 2,
  "pending_periods": 2,
  "first_anomaly_index": 2,
  "anomalous_records": [2],
  "soc_breakpoint_records": [],
  "periods": [
    {"index": 0, "status": "settled", "...": "..."},
    {"index": 1, "status": "settled", "...": "..."},
    {
      "index": 2,
      "status": "pending",
      "actual_grid_purchase_energy_kwh": null,
      "actual_energy_cost_yuan": null,
      "total_cost_delta_yuan": null,
      "planned_total_cost_yuan": 0.03,
      "reasons": ["购电量残差 +0.100000kWh 超容差 0.01kWh（实测 0.11kWh，核算值 0.01kWh）"],
      "...": "审计分项残差与 SOC 偏差照常返回"
    },
    {
      "index": 3,
      "status": "pending",
      "actual_total_cost_yuan": null,
      "reasons": ["前序第 2 条记录已出现异常/SOC 断点，后续记录冻结为待结算"],
      "...": "..."
    }
  ],
  "summary": {
    "settled_periods": 2,
    "actual_total_cost_yuan": 0.07,
    "planned_total_cost_yuan": 0.07,
    "total_cost_delta_yuan": 0.0,
    "...": "汇总只覆盖已结算前缀；购电量、吞吐量、能量费、衰减费均给出实际/计划/差额三列"
  }
}
```

待结算记录的各 `actual_*` 与差额字段为 `null`，但计划口径（`planned_*`）、审计残差、SOC 偏差（`soc_gap_start_kwh` / `soc_gap_end_kwh`）与冻结原因照常返回，便于人工复核。

### `POST /dispatch/replan`

计划执行到**中途**时，依据连续实测状态续排余段。请求包含六样东西：

| 字段 | 说明 |
|---|---|
| `dispatch` | 生成原计划时使用的**完整 `/dispatch` 输入** |
| `plan_snapshot` | 当初 `/dispatch` 返回的计划快照（`periods` + `summary` 原样回传） |
| `audit_tolerance_kwh` | 审计容差（kWh，非负），规则与 `/telemetry/audit` 完全一致：残差绝对值**严格大于**它才判异常 |
| `executed_records` | 自原时间轴起点**连续已执行**的半小时实测记录（字段同 `/telemetry/audit`），至少一段、且必须**留有待执行时段**（段数 < 原计划时段数） |
| `remaining_prices` | 剩余时段的**新电价**预测，长度 = 原时段数 − 已执行段数 |
| `remaining_base_load_kw` / `remaining_pv_kw` | 剩余时段的**新负荷 / 光伏**预测，长度同上 |

服务端处理顺序：

1. **重算核对原计划**：用现有求解器对 `dispatch` 重算，逐字段比对 `plan_snapshot`（与结算同一套防篡改核对），不一致即拒绝；
2. **审计实测前缀**：按现有遥测审计规则核对连续前缀（电芯能量守恒、购电量平衡、相邻记录 SOC 衔接），并额外要求**首条记录的首态 SOC 在审计容差内接原初值**；
3. **保留实测、从末态重排**：已执行动作即使偏离旧计划，只要实测自洽就**保留原记录**（不做 SOC 断点判定），并从前缀末态重新安排余段；前缀末态 SOC 必须落在 **0.1kWh 网格**上，否则离散求解无法精确承接；
4. **末端约束核对**：原 `min_terminal_soc_kwh` 约束从实测末态起、在余段新预测下仍须可达，否则按无解拒绝；
5. **输出**：原时间轴上已执行（`executed`）与待执行（`planned`）的逐时段明细；按**现有结算口径**计算的实测前缀费用（实测购电量×电价 + 实测电芯吞吐量×衰减单价）加余段预测费用；以及可**直接交给 `/dispatch/settlement`** 的余段调度输入（`remaining_dispatch`，初始 SOC 已设为前缀末态）与余段计划快照（`remaining_plan_snapshot`）。

示例（前 2 段按原计划执行，余段 t2 电价从 1 元更新为 5 元）：

```bash
curl -s -X POST http://localhost:8000/dispatch/replan \
  -H 'Content-Type: application/json' \
  -d '{
    "dispatch": {
      "prices": [0.0, 1.0, 1.0, 0.0],
      "base_load_kw": [0.2, 0.2, 0.2, 0.2],
      "pv_kw": [0.0, 0.0, 0.0, 0.0],
      "capacity_kwh": 0.4,
      "max_charge_kw": 1.0,
      "max_discharge_kw": 1.0,
      "charge_efficiency": 0.9,
      "discharge_efficiency": 0.9,
      "initial_soc_kwh": 0.0,
      "min_terminal_soc_kwh": 0.0,
      "degradation_cost_per_kwh": 0.2
    },
    "plan_snapshot": { "...": "原样回传 /dispatch 的完整响应（periods + summary）" },
    "audit_tolerance_kwh": 0.01,
    "executed_records": [
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}
    ],
    "remaining_prices": [5.0, 0.0],
    "remaining_base_load_kw": [0.2, 0.2],
    "remaining_pv_kw": [0.0, 0.0]
  }'
```

```json
{
  "num_periods": 4,
  "period_hours": 0.5,
  "audit_tolerance_kwh": 0.01,
  "executed_periods": 2,
  "remaining_periods": 2,
  "periods": [
    {
      "index": 0,
      "status": "executed",
      "record": {"...": "原始实测记录原样保留"},
      "plan": {
        "action": "charge",
        "grid_purchase_energy_kwh": 0.322222,
        "battery_throughput_kwh": 0.2,
        "energy_cost_yuan": 0.0,
        "degradation_cost_yuan": 0.04,
        "soc_start_kwh": 0.0,
        "soc_end_kwh": 0.2
      },
      "soc_gap_start_kwh": 0.0,
      "soc_gap_end_kwh": 0.0,
      "actual_grid_purchase_energy_kwh": 0.322222,
      "actual_battery_throughput_kwh": 0.2,
      "actual_energy_cost_yuan": 0.0,
      "actual_degradation_cost_yuan": 0.04,
      "actual_total_cost_yuan": 0.04,
      "planned_energy_cost_yuan": 0.0,
      "planned_degradation_cost_yuan": 0.04,
      "planned_total_cost_yuan": 0.04,
      "energy_cost_delta_yuan": 0.0,
      "degradation_cost_delta_yuan": 0.0,
      "total_cost_delta_yuan": 0.0,
      "grid_purchase_delta_kwh": 0.0,
      "throughput_delta_kwh": 0.0,
      "audit": {"index": 0, "anomalous": false, "reasons": [], "...": "三类分项残差同 /telemetry/audit"},
      "reasons": []
    },
    {"index": 1, "status": "executed", "...": "同结构；实测自洽即通过，对旧计划的偏离只体现在 soc_gap_* 与各 delta 中"},
    {
      "index": 2,
      "status": "planned",
      "remaining_index": 0,
      "action": "discharge",
      "action_energy_kwh": -0.1,
      "charge_ac_energy_kwh": 0.0,
      "discharge_ac_energy_kwh": 0.09,
      "battery_power_kw": -0.18,
      "grid_purchase_energy_kwh": 0.01,
      "pv_curtailed_energy_kwh": 0.0,
      "soc_start_kwh": 0.1,
      "soc_end_kwh": 0.0,
      "energy_cost_yuan": 0.05,
      "degradation_cost_yuan": 0.02
    },
    {"index": 3, "status": "planned", "remaining_index": 1, "action": "idle", "...": "..."}
  ],
  "executed_summary": {
    "executed_periods": 2,
    "final_soc_kwh": 0.1,
    "actual_grid_purchase_kwh": 0.332222,
    "planned_grid_purchase_kwh": 0.332222,
    "grid_purchase_delta_kwh": 0.0,
    "actual_battery_throughput_kwh": 0.3,
    "planned_battery_throughput_kwh": 0.3,
    "throughput_delta_kwh": 0.0,
    "actual_energy_cost_yuan": 0.01,
    "planned_energy_cost_yuan": 0.01,
    "energy_cost_delta_yuan": 0.0,
    "actual_degradation_cost_yuan": 0.06,
    "planned_degradation_cost_yuan": 0.06,
    "degradation_cost_delta_yuan": 0.0,
    "actual_total_cost_yuan": 0.07,
    "planned_total_cost_yuan": 0.07,
    "total_cost_delta_yuan": 0.0
  },
  "remaining_summary": { "...": "余段计划的 summary，口径同 /dispatch（num_periods=2）" },
  "actual_prefix_total_cost_yuan": 0.07,
  "remaining_forecast_total_cost_yuan": 0.07,
  "total_cost_yuan": 0.14,
  "remaining_dispatch": {
    "prices": [5.0, 0.0],
    "base_load_kw": [0.2, 0.2],
    "pv_kw": [0.0, 0.0],
    "capacity_kwh": 0.4,
    "max_charge_kw": 1.0,
    "max_discharge_kw": 1.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
    "initial_soc_kwh": 0.1,
    "min_terminal_soc_kwh": 0.0,
    "degradation_cost_per_kwh": 0.2
  },
  "remaining_plan_snapshot": { "...": "余段计划（periods + summary），与 remaining_dispatch 配套" }
}
```

要点：

- `periods` 按**原时间轴下标**升序混合返回：已执行段 `status=executed`（含原始记录、原计划回显、实测费用、与原计划的差额、审计残差；偏离旧计划不是异常），待执行段 `status=planned`（字段同 `/dispatch` 的逐时段计划，另带余段内相对下标 `remaining_index`）；
- `remaining_dispatch.initial_soc_kwh` 等于前缀末态 SOC（`executed_summary.final_soc_kwh`），余段首时段 `soc_start_kwh` 与之**严格衔接**；末端 SOC 约束、容量、效率、功率、衰减单价均沿用原输入；
- 费用三段并列：`actual_prefix_total_cost_yuan`（前缀实测）、`remaining_forecast_total_cost_yuan`（余段按新电价/负荷/光伏的预测）、`total_cost_yuan`（两者之和）。

余段执行完后，把 `remaining_dispatch`、`remaining_plan_snapshot` 与余段实测记录一起 POST 给 `/dispatch/settlement` 即可完成余段结算（快照可通过重算核对）。

整体拒绝（HTTP 422）的情形：

- `plan_snapshot_mismatch`：原计划快照与重算不一致；原输入重算无解时为 `infeasible`；
- `prefix_audit_failed`：首条记录首态 SOC 未在容差内接原初值，或前缀中任一条未通过遥测审计；
- `prefix_soc_off_grid`：前缀末态 SOC 不落在 0.1kWh 网格（或超出容量）；
- `infeasible`：从实测末态出发，原末端 SOC 约束在余段新预测下已无法满足；
- `invalid_input`：前缀为空 / 覆盖全周期、余段预测长度与剩余时段数不符、容差或测量值非法等。

### `POST /dispatch/sessions` — 创建执行会话

实测记录常**分批到达**（每批覆盖若干连续的半小时时段）。先创建会话锁定计划基线：

| 字段 | 说明 |
|---|---|
| `dispatch` | 生成原计划时使用的**完整 `/dispatch` 输入** |
| `plan_snapshot` | 当初 `/dispatch` 返回的完整计划快照（`periods` + `summary`） |
| `audit_tolerance_kwh` | 审计容差（kWh，非负），之后每批追加都沿用它；残差绝对值**严格大于**它即判异常 |

服务端**先重算并逐字段核对快照**（与结算、续排同一套防篡改核对），不一致返回 `plan_snapshot_mismatch`、原输入重算无解返回 `infeasible`，均不创建会话；核对通过后把原输入、快照与容差持久化，返回 `session_id`（UUID）、`num_periods`、`next_index=0`、`status=awaiting_records`。

### `POST /dispatch/sessions/{session_id}/batches` — 成批追加实测记录

请求体形如 `{"start_index": 0, "records": [ …半小时实测记录… ]}`，记录字段同 `/telemetry/audit`，每批 1–200 条：

1. **下标衔接**：`start_index` 必须等于会话当前 `next_index`（首批为 0），下标跳跃（`batch_index_gap`）或区间超出 `[0, num_periods)`（`batch_out_of_range`）都**整批不入库**；
2. **首态衔接**：首批首态 SOC 须在容差内接**原初始 SOC**；后续批次首态 SOC 须在容差内接**已保存最后一条的末态 SOC**（`batch_boundary_gap` 整批不入库）；
3. **逐段审计**：沿用现有 `audit_telemetry` 核对批内每条记录的电芯能量守恒、购电量平衡与相邻记录 SOC 衔接；实测动作**偏离计划但自洽时仍接受**（会话不做计划 SOC 轨迹核对，偏离不是异常）；任一条异常（`batch_audit_failed`）都**整批不入库**，原因中点名首个异常下标与残差；
4. **原子入库**：上述检查全部通过后整批写入同一 SQLite 事务，返回 `result=accepted`、更新后的 `confirmed_periods` / `next_index` / `status`（`awaiting_records` → `in_progress` → `complete`）、本批逐条 `batch_audit`、`first_record_soc_gap_kwh` 与累计 `final_soc_kwh`；
5. **重试与冲突**：只有请求批次的**起点、长度、记录内容**与某个**已确认整批**完全一致的原样重试才返回 HTTP 200、`result=deduped`：不重复入库，且除 `result` 外响应的其余字段（`confirmed_periods` / `next_index` / `status` / `batch_audit` / `first_record_soc_gap_kwh` / `final_soc_kwh`）**保持该批首次成功时的值**——即使之后又确认了后续批次，重发早期批次仍返回该批当时的进度与末态，而不是最新进度；同起点**短批 / 长批、任一条内容变更、跨批拼接**（如已确认 `[0,2)` 与 `[2,4)` 后重发 `[1,3)`）一律 **HTTP 409 `conflict`**，已保存记录与进度完全不变，响应原因给出当前 `next_index`。批次边界（起点+长度）、本批规范化记录与首次回执和逐段记录在**同一 SQLite 写事务**内确认，跨进程并发、进程重启、后续批次入库后的重试都按上述规则判定。旧版库只有逐段记录（无批次边界）时，会话查询与按 `next_index` 续报照常保留，但对历史区间的任何重发都因**无法确认历史批次边界**而返回 409，不按记录前缀猜测去重。

### `GET /dispatch/sessions/{session_id}` 与 `GET /dispatch/sessions`

单会话查询返回：原计划信息（`dispatch` + `plan_snapshot` + `audit_tolerance_kwh`）、自下标 0 起**连续已确认记录** `confirmed_records`（每条带全局 `index`、原始 `record` 与 `stored_at`）、对全部确认记录重算的 `confirmed_audit`、`next_index`、`status`、`initial_soc_kwh` 与 `final_soc_kwh`。未完成的前缀可直接取 `confirmed_records[*].record` 作为 `executed_records` 交给 `/dispatch/replan`；`status=complete` 的满期记录可直接作为 `records` 交给 `/dispatch/settlement`。列表接口返回全部会话的概览（确认段数、下一下标、状态、首末 SOC、时间戳）。未知会话返回 **404 `session_not_found`**。

### 持久化

会话与记录存放在容器内 `/data/sessions.db`（SQLite，环境变量 `DISPATCH_SESSION_DB` 可覆盖路径），Compose 已把命名卷 `dispatch-session-data` 挂到 `/data`：容器重建、镜像升级或进程重启后，会话与已确认记录**原样保留**，可继续查询、幂等重试与续报。批次边界（起点、长度）、本批规范化记录与首次成功回执与逐段记录在**同一 SQLite 写事务**内确认（`BEGIN IMMEDIATE` + 文件锁），跨进程并发与重启后的重试均按整批规则判定。打开旧版库（只有 `sessions` 与 `session_records`）时会自动补建批次表：逐段记录原样保留，查询与续报不受影响，只是历史区间因缺少批次边界而无法去重。裸机运行时若 `/data` 不可写，自动落到工作目录 `./data/sessions.db`。

### 可执行示例（创建、分批追加、重试、查询、续排/结算）

以下示例在仓库目录直接可跑（需要 `jq`；先 `docker compose up -d --build`）。所用 4 段计划为「t0 谷时充 0.2kWh、t1/t2 各放 0.1kWh、t3 静置」，效率 0.9、容差 0.01kWh。

```bash
BASE=http://localhost:8000

# 0) 生成计划并保存输入与快照（jq -c 压成单行 JSON）
cat > /tmp/dispatch.json <<'JSON'
{
  "prices": [0.0, 1.0, 1.0, 0.0],
  "base_load_kw": [0.2, 0.2, 0.2, 0.2],
  "pv_kw": [0.0, 0.0, 0.0, 0.0],
  "capacity_kwh": 0.4,
  "max_charge_kw": 1.0,
  "max_discharge_kw": 1.0,
  "charge_efficiency": 0.9,
  "discharge_efficiency": 0.9,
  "initial_soc_kwh": 0.0,
  "min_terminal_soc_kwh": 0.0,
  "degradation_cost_per_kwh": 0.2
}
JSON
curl -s -X POST $BASE/dispatch -H 'Content-Type: application/json' \
  -d @/tmp/dispatch.json | jq -c . > /tmp/plan.json

# 1) 创建执行会话（服务端重算确认快照一致）
curl -s -X POST $BASE/dispatch/sessions -H 'Content-Type: application/json' \
  -d "$(jq -n --slurpfile d /tmp/dispatch.json --slurpfile p /tmp/plan.json \
        '{dispatch:$d[0], plan_snapshot:$p[0], audit_tolerance_kwh:0.01}')" \
  | tee /tmp/session.json | jq .
SID=$(jq -r .session_id /tmp/session.json)

# 2) 首批追加 t0、t1 两段（start_index=0，首态接原初始 SOC 0.0）
cat > /tmp/batch0.json <<'JSON'
{"start_index": 0, "records": [
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
   "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
   "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch0.json | jq '{result, confirmed_periods, next_index, status, first_record_soc_gap_kwh}'

# 3) 网络超时后原样重试同一起点同一批 → result=deduped，不重复入库
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch0.json | jq '{result, confirmed_periods, next_index}'

#    只重发第一条（同起点短批）→ HTTP 409 conflict，绝不误判 deduped，记录/进度不变
jq -c '.records |= .[0:1]' /tmp/batch0.json \
  | curl -s -o /tmp/short.json -w '%{http_code}\n' \
    -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' -d @-
jq '{error, reason}' /tmp/short.json   # 409，reason 中给出 next_index=2
curl -s $BASE/dispatch/sessions/$SID | jq '.confirmed_periods'   # 仍为 2

#    同起点但内容不同（哪怕只改一个读数）→ HTTP 409 conflict
sed 's/"grid_purchase_energy_kwh": 0.01/"grid_purchase_energy_kwh": 0.012/' /tmp/batch0.json \
  | curl -s -o /tmp/conflict.json -w '%{http_code}\n' \
    -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' -d @-
jq .error /tmp/conflict.json   # "conflict"

# 4) 第二批追加 t2、t3（start_index 必须接上批的 next_index=2，首态接已保存末态 0.1）
cat > /tmp/batch2.json <<'JSON'
{"start_index": 2, "records": [
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
   "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0},
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0,
   "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch2.json | jq '{result, confirmed_periods, next_index, status}'
# 若下标跳跃（如 start_index=3）或记录异常，会返回 422 且整批不入库，可修正后按 next_index 重发

# 4b) 后续批次入库后，重发「完整首批」仍 deduped，且回执冻结在首批首次成功时：
#     confirmed_periods/next_index/status/final_soc_kwh 不反映最新进度（最新已是 4/complete/0.0）
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch0.json | jq '{result, confirmed_periods, next_index, status, final_soc_kwh}'
# => {"result":"deduped","confirmed_periods":2,"next_index":2,"status":"in_progress","final_soc_kwh":0.1}

#     跨批拼接（取首批的 t1 与第二批的 t2 拼成 [1,3)）→ HTTP 409 conflict，不按前缀猜测去重
curl -s -o /tmp/splice.json -w '%{http_code}\n' \
  -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d "$(jq -s -c '.[0].records[1] as $a | .[1].records[0] as $b | {start_index:1, records:[$a,$b]}' /tmp/batch0.json /tmp/batch2.json)"
jq .error /tmp/splice.json   # "conflict"，会话仍为 4 条已确认、next_index=4

# 5) 查询会话：原计划 + 连续已确认记录 + 下一下标（重启后同样可查）
curl -s $BASE/dispatch/sessions/$SID | tee /tmp/detail.json | jq \
  '{status, confirmed_periods, next_index, initial_soc_kwh, final_soc_kwh}'
curl -s $BASE/dispatch/sessions | jq '.sessions | length'

# 6) 满期（status=complete）后，直接用已确认记录结算
jq -c '{dispatch: .dispatch, plan_snapshot: .plan_snapshot,
        settlement_tolerance_kwh: 0.01,
        records: [.confirmed_records[].record]}' /tmp/detail.json \
  | curl -s -X POST $BASE/dispatch/settlement -H 'Content-Type: application/json' -d @- \
  | jq '{status, settled_periods, actual_total_cost: .summary.actual_total_cost_yuan}'

# 7) 若只确认了部分前缀（下例取已确认的前 2 段），可用连续前缀续排余段
jq -c '{dispatch, plan_snapshot, audit_tolerance_kwh,
            executed_records: [.confirmed_records[0:2][].record],
            remaining_prices: [5.0, 0.0],
            remaining_base_load_kw: [0.2, 0.2],
            remaining_pv_kw: [0.0, 0.0]}' /tmp/detail.json \
  | curl -s -X POST $BASE/dispatch/replan -H 'Content-Type: application/json' -d @- \
  | jq '{executed_periods, remaining_periods, total_cost_yuan}'
```

进程重启验证：`docker compose restart`（或 `docker compose down && docker compose up -d`，命名卷保留）后再次执行第 5 步，会话与已确认记录仍在；对已保存批次执行第 3 步仍返回 `deduped`，且即使第 4 步已追加后续批次，重发完整首批仍返回首批首次成功时冻结的进度与末态。**旧版数据库**（仅逐段记录、无批次边界）升级后：会话查询与按 `next_index` 续报照常，新批次正常入库并支持整批幂等；但对历史区间的重发返回 409 `conflict`（无法确认历史批次边界，不按前缀猜测去重）。

## 模型与假设

- **时段**：固定 0.5 h；功率（kW）× 0.5 即该时段能量（kWh）。
- **动作与 SOC 网格**：动作即一个时段内电芯能量变化，离散为 0.1 kWh 的整数倍：正数充电、负数放电、0 静置；SOC 全程位于 `[0, capacity]` 的 0.1 kWh 网格上。容量、初始 SOC、最低末端 SOC 必须精确落在该网格上，否则报参数错误（不做静默取整）。
- **功率平衡**（每时段）：

  ```
  购电量 = max(0, 基础负荷 − 光伏 + 充电交流电量 − 放电交流电量)
  ```

  光伏优先供负荷，剩余可给电池充电；**不允许向电网倒送电**，无法消纳的光伏盈余计为弃光（`pv_curtailed_energy_kwh`），放电量因此不会超过同时段净负荷。
- **效率**：充电交流电量 = 电芯电量 / ηc；放电交流电量 = 电芯电量 × ηd。功率上限按交流侧考核（充电：电芯电量 ≤ P充·Δt·ηc；放电：电芯电量 ≤ P放·Δt/ηd）。
- **费用**：
  - 能量费 = 电价 × 购电量；
  - 衰减费 = `degradation_cost_per_kwh` × 电芯侧吞吐量，吞吐量 = Σ|充入电芯能量| + Σ|电芯放出能量|（充、放各计一次）；
  - 总成本 = 能量费 + 衰减费。
- **末端约束**：最后一个时段结束时 SOC ≥ `min_terminal_soc_kwh`。

### 动态规划

状态 `(t, j)`：第 `t` 个时段结束、SOC 处于第 `j` 格（j × 0.1 kWh）时的最优前缀。每格最多转移到「静置 / 充电 1..k 格 / 放电 1..k 格」，转移范围同时受功率上限、SOC 上下界和"禁止倒送"约束，转移成本即时算出能量费与衰减费。时间复杂度 O(N · C · P)，其中 C = 容量/0.1，P = 单时段最多动作格数。

### 破平局（保证结果确定、可复现）

当不同计划在数值容差内无法区分时，依次按：

1. **总成本**低者优先（能量费 + 衰减费）；
2. 总成本相同 → **峰值购电量**（各时段购电量的最大值，kWh/半小时）小者优先，即偏好削峰；
3. 仍相同 → **动作序列字典序**小者优先，动作为整数（0.1 kWh 格数，正充负放），越靠前的时段差异权重越大——因此"先静置、后动作"会胜过"先动作、后静置"。

## 错误处理

非法输入或无解统一返回 **HTTP 422**，JSON 说明原因：

```json
{"error": "invalid_input", "reason": "电价、基础负荷、光伏预测必须等长：当前分别为 3、2、3"}
```

```json
{"error": "infeasible", "reason": "无满足末端 SOC 约束的计划：调度期末最高可达 SOC 约为 0.0kWh，低于最低末端 SOC 0.1kWh；原因是受时段数、最大充电功率或充电效率限制，无法在期末充到要求电量"}
```

`invalid_input` 覆盖：数组不等长、负值/非有限值、效率不在 (0,1]、容量/SOC 非 0.1 整数倍、初始或末端 SOC 超过容量、数组长度超出 1–200 等；`infeasible` 表示输入合法但约束可行域为空，并给出最高可达末端 SOC 等诊断信息。`/telemetry/audit` 的非法输入（容量非正、效率越界、测量值或容差为负/非有限、SOC 超容量、记录数超出 1–2000 等）同样以 `invalid_input` 返回。

`/dispatch/settlement` 除沿用上述非法输入拦截（记录数与计划时段数不符等，`invalid_input`）外，另有两类整体拒绝：

```json
{"error": "plan_snapshot_mismatch", "reason": "计划快照第 0 个时段的 grid_purchase_energy_kwh=0.99 与重算结果 0.322222 不一致，快照可能已被篡改"}
```

```json
{"error": "no_valid_interval", "reason": "首条实测记录（下标 0）即异常或 SOC 断点，不存在可结算的连续有效区间：电芯能量残差 -0.150000kWh 超容差 0.01kWh（实测末态 SOC 0.05kWh，核算值 0.2kWh）"}
```

- `plan_snapshot_mismatch`：计划快照与按 `dispatch` 重算的结果在任一字段上不一致（含动作、逐时段能量/SOC/费用、summary），或快照结构缺失；
- `no_valid_interval`：首条记录即未通过遥测审计或 SOC 轨迹核对，没有任何可结算前缀；
- 部分记录异常 / 断点**不是**错误：返回 HTTP 200 与 `status=partially_settled`，仅冻结异常点及后续记录。

`/dispatch/replan` 同样为整体拒绝返回 422，错误码包括：

- `plan_snapshot_mismatch`：原计划快照与按 `dispatch` 重算的结果不一致（原输入本身无解为 `infeasible`）；
- `prefix_audit_failed`：首条实测首态 SOC 未在容差内接原初值，或连续前缀中存在遥测审计异常（电芯能量 / 购电量 / SOC 衔接）；
- `prefix_soc_off_grid`：前缀末态 SOC 不落在 0.1kWh 网格上（离散调度只能从整格 SOC 起步）；
- `infeasible`：从实测末态出发，原末端 SOC 约束在余段新预测下已无法满足；
- `invalid_input`：前缀为空或覆盖全周期、余段预测长度不等于剩余时段数、容差为负 / 非有限、测量值为负 / 非有限、SOC 超容量等。

执行会话接口的错误码（除 `conflict` 为 **HTTP 409**、`session_not_found` 为 **HTTP 404** 外，其余为 422）：

- `plan_snapshot_mismatch` / `infeasible`：创建会话时快照核对不一致 / 原输入重算无解，均不创建会话；
- `batch_index_gap`：`start_index` 大于会话 `next_index` 的下标跳跃，整批不入库；
- `batch_out_of_range`：本批下标区间超出 `[0, num_periods)`，整批不入库；
- `batch_boundary_gap`：首批首态未在容差内接原初始 SOC，或后续批首态未接已保存末态，整批不入库；
- `batch_audit_failed`：批内任一条记录未通过遥测审计（原因点名首个异常下标），整批不入库；
- `conflict`（409）：重试未命中已确认整批——同起点已有确认批次但**长度不同**（短批 / 长批）或**记录内容不同**，或起点位于已确认区间内但不存在以该起点为边界的批次（**跨批拼接**、旧库**无历史批次边界**）；已保存记录与进度不变，只有起点、长度、内容三者完全一致的原样重试才返回 `deduped`；
- `session_not_found`（404）：会话 ID 不存在；
- `invalid_input`：容差为负 / 非有限、批为空或超长、测量值为负 / 非有限、SOC 超容量等。

注意：已执行动作**偏离旧计划**本身不构成拒绝——只要实测记录自洽（含相邻记录 SOC 衔接），偏离只作为 `soc_gap_*` 与各费用差额信息返回，续排一律以实测末态为准。

## 测试

```bash
pip install pytest httpx
pytest tests/ -q
```

覆盖谷充峰放套利、效率损耗与衰减费核算、末端 SOC 约束、禁止倒送、光伏免费充电、无解诊断、各类非法输入，以及峰值与字典序两级破平局；遥测审计部分覆盖自洽记录通过、三类残差（电芯能量 / 购电量 / SOC 衔接）越限标记、容差边界（等于容差不判异常）、光伏盈余购电截断，以及容量 / 效率 / 容差 / 测量值的非法输入拦截。结算部分覆盖：全部有效全额结算（含容差内购电偏差计入费用差额、实测吞吐量衰减费核算、预测偏差场景）、审计异常与 SOC 断点起的前缀冻结、断点与衔接异常的区分、计划快照（periods / summary / 动作）篡改拒绝、dispatch 输入与快照不匹配拒绝、长度不符与无有效区间拒绝、重算无解传播，以及既有三个接口行为不变。续排部分覆盖：原时间轴已执行/待执行明细与费用口径、余段电价变化后的最优重排、实测偏离旧计划但自洽时保留原记录并从末态重排、容差内表计偏差照实计费、余段输入与快照的结算接口端到端衔接（含余段仅一段）、首态 SOC 容差边界（等于容差通过、超出拒绝），以及前缀审计异常、首态未接原初值、末态非 0.1kWh 网格、余段末端约束不可达（`infeasible`）、快照篡改、长度不符、空前缀/全周期前缀、非法容差/测量值/余段预测等各类整体拒绝。执行会话部分覆盖：创建时快照重算核对（含篡改、原输入无解）、分批追加的首批/后续批首态衔接（含容差边界）、动作偏离计划但自洽仍接受、下标跳跃与越界整批不入库且无副作用、批内核电芯能量/购电量/SOC 衔接异常整批拒绝、同起点原样重试返回 deduped 且回执字段冻结在首次成功（含后续批次入库后重试不返回最新进度/末态）、同起点短批/长批/内容变更/跨批拼接均返回 409 且记录进度不变、同起点内容不同（含部分重叠）返回 409、并发同起点上报只有一份生效（多线程与两个独立存储对象模拟跨进程）、重启及后续批次入库后的整批重试仍返回冻结回执、旧版逐段记录库（无批次表）升级后查询与 next_index 续报照常但历史区间重发一律 409 且新批次仍可整批幂等、查询返回原计划与连续已确认记录及下一下标、未完成前缀端到端续排、满期记录端到端结算、按同一路径重建存储模拟进程重启后记录仍在且可续报、空批/负值/SOC 超容量等非法输入拦截，以及既有四个接口行为不变。
