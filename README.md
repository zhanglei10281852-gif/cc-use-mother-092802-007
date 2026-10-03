# 电网韧性投资规划

山地冰崩同时损毁道路与输电走廊后，年度投资排序不能只看设备年龄。本后端基于
**资产供电依赖、风险情景、服务人口、修复/替代线路工期、施工资源与工程前置条件**，
为候选改造组合计算可解释的优先级，并对已审批年度计划实施版本冻结。

## 排序模型

- **级联断电**：避险点由「直接供电资产 + 其全部上游」供电，链上任一资产失效即失电。
  一个变电站失效连带切断的避险点，沿供电拓扑向下游传播计算。
- **情景风险**：`年期望受影响人口 = Σ情景 年发生频率 × Σ避险点 人口×重要度 × 失电概率`。
- **恢复时间**：避险点恢复取供电链上最慢资产；资产恢复取「完全修复工期」与
  「替代线路（旁路）工期」二者较短值——替代线路修复时间因此直接进入效益。
- **改造效益**：加固可降低残存失效概率、新增旁路、缩短修复工期。候选内在优先级是
  风险下降、停电人口日下降、连带暴露人口三个归一化维度的加权和（0–100 分）。
- **组合选择**：在年度预算、施工队并行能力、工期窗口与工程前置约束下，
  按边际效益费用比贪心选择，每轮重算边际效益（上游已加固会稀释下游项目效益）。

## 版本治理

- 计划版本状态：`draft → approved`，审批后可 `withdrawn`（回退）。
- **审批即冻结**：数据快照、风险权重、入选组合、排程与内容指纹（SHA-256）一并固化。
- **新增资产关系不能倒改旧计划**：灾后补勘的新拓扑只能在已审批版本之后开新版本
  （`parent_version` 指向上一版），旧版本排序与指纹不变。
- **审批回退**：撤回旧版（标记 withdrawn），以其冻结快照为父本开出可编辑新草稿。
- **施工延期重评**：延期不触碰冻结组合与权重，而是在审批版本下生成 revision，
  重新排程并标出因上游延期而改期、超出工期窗口的受影响下游项目；多次延期累积生效。

## 模块结构

```
src/grid_resilience/
  contracts.py    输入数据结构（资产、服务点、情景、修复资源、候选、预算）
  topology.py     供电拓扑：祖先/下游传播、级联范围、环检测
  scoring.py      风险与恢复计算、候选打分、贪心组合、多队排程
  validation.py   引用完整性、取值范围、环校验
  planning.py     计划版本：草稿/审批/回退/延期重评、权重与情景比较
  serialization.py 确定性 JSON 快照、内容指纹、结果序列化
  codec.py        HTTP JSON 编解码
  app.py          纯标准库 WSGI 服务（无第三方依赖）
  cli.py          命令行：serve / rank
```

## 运行

```bash
# 测试
python -m unittest discover -s tests -v

# 编译检查
python -m compileall -q src tests

# 一次性试算
PYTHONPATH=src python -m grid_resilience.cli rank --input examples/sample_plan.json

# 启动 HTTP 服务
PYTHONPATH=src python -m grid_resilience.cli serve --host 127.0.0.1 --port 8000
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/evaluate` | 无状态试算，返回排序依据、阻塞与排程 |
| POST | `/compare-scenarios` | 同一数据比较不同情景组合（不落库） |
| GET | `/plans` | 计划列表 |
| POST | `/plans/{id}` | 建立计划草稿 v1 |
| GET | `/plans/{id}` | 版本列表 |
| GET | `/plans/{id}/versions/{n}` | 版本详情（`?snapshot=1` 附带冻结快照） |
| POST | `/plans/{id}/versions` | 审批后用新数据/拓扑开下一版草稿 |
| PUT | `/plans/{id}/draft/data` | 更新草稿数据（已审批返回 409） |
| POST | `/plans/{id}/draft/weights` | 调整草稿风险权重 |
| POST | `/plans/{id}/compare/weights` | 多套权重并排比较（不落库） |
| POST | `/plans/{id}/approve` | 审批冻结 |
| POST | `/plans/{id}/rollback` | 审批回退（withdrawn + 新草稿） |
| POST | `/plans/{id}/delays` | 登记施工延期，重评受影响下游 |

错误以 `404 not_found / 409 invalid_transition / 422 invalid_data` 区分。

排序响应中每个候选都给出 `priority_score`、`benefit_cost_ratio`、三个维度的
`contributions`、未满足的 `blockers`、`not_selected_reason` 与排程起止日/施工队，
便于规划人员复核「为什么排这个、为什么没选那个」。

## 测试覆盖

- 网络拓扑变化（改接独立走廊后级联范围变化）、环与悬空引用检测
- 资源冲突（单施工队 + 紧窗口导致落选告警）、预算约束、前置阻塞
- 审批冻结、新增关系不倒改旧版、审批回退、再审批
- 施工延期只重排下游、超窗告警、多次延期累积、冻结组合不被重选
- 权重与情景比较不落库、排序跨进程可重复
- 完整 HTTP 生命周期（WSGI 端到端）
