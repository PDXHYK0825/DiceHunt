# 03｜下游实现智能体执行计划

> 项目还没有实现文件。先冻结 [01 接口](01-module-contracts.md)，按 [02 算法](02-algorithm-design.md) 和 [04 测试](04-test-plan.md) 执行。写范围为 `Project/` 下独占路径；不得改 `spec.md`、`Sources/`、`Test/`。需求变更交集成负责人排队，同步六份文档。

## 执行约定

每任务先读相关契约及前置产物，只改自己的文件；附测试结果、限制、实际工时。Python 3.11+；运行时 `requirements.txt` 零依赖、开发 `requirements-dev.txt` 含 pytest。命令从 `Project/` 执行；W0-01 README 固定 `PYTHONPATH=src` 或等价安装方式。实验固定地图指纹、出生点、种子、骰子按 `(turn,seat)` 调度、模型 ID、深度/K。

## WBS（1 点≈1–2 人时）

| 波次/ID | 执行内容 | 独占写范围（`Project/` 下） | 前置 | 验收（`Project/` 下执行） | 点 |
| --- | --- | --- | --- | --- | ---: |
| 0/W0-01 | README、依赖/忽略清单、包占位 | `README.md`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `src/treasure_game/__init__.py` | 无 | `python --version` 显示 3.11+；`Test-Path src/treasure_game/__init__.py` 为 True（PowerShell） | 3 |
| 0/W0-02 | 冻结值对象、枚举、异常、GameIO 协议 | `src/treasure_game/models.py` | W0-01 | `python -m compileall -q src/treasure_game/models.py`；TrueMap/ViewMap 不同类且不可变 | 5 |
| 1/W1-01 | Path 唯一移动内核、BFS、邻空地 | `src/treasure_game/path_module.py`, `tests/test_path.py` | W0-02 | `python -m pytest -q tests/test_path.py`；spec 用例 4、即时资源通过 | 10 |
| 1/W1-02 | Map txt/随机生成/三视图/连通 | `src/treasure_game/map_module.py`, `tests/test_map.py` | W1-01 | `python -m pytest -q tests/test_map.py`；复现、残局拒导、隐藏揭示通过 | 11 |
| 1/W1-03 | Battle 终点结算、推开/围死 | `src/treasure_game/battle_module.py`, `tests/test_battle.py` | W1-01 | `python -m pytest -q tests/test_battle.py`；spec 用例 5、同分/未重合通过 | 6 |
| 2/W2-01 | 贪心 BFS 比率、平局及方向前缀 | `src/treasure_game/greedy_ai.py`, `tests/test_greedy_ai.py` | W1-02 | `python -m pytest -q tests/test_greedy_ai.py`；spec 用例 1 | 8 |
| 2/W2-02 | OPT 核、人类视图策略、chance/缓存/安全界 | `src/treasure_game/opt_ai.py`, `src/treasure_game/human_policy.py`, `tests/test_opt_ai.py` | W1-02、W1-03 | `python -m pytest -q tests/test_opt_ai.py`；六面等权、符号、剪枝=无剪枝 | 20 |
| 2/W2-03 | 分治跨块 BFS、子块回调、合并 | `src/treasure_game/divide_conquer_module.py`, `tests/test_divide_conquer.py` | W2-02 | `python -m pytest -q tests/test_divide_conquer.py`；跨块可重放 | 10 |
| 2/W2-04 | AI facade、A/B 座位规范化、诊断和阈值路由 | `src/treasure_game/ai_module.py`, `tests/test_ai_module.py` | W2-01/02/03 | `python -m pytest -q tests/test_ai_module.py tests/test_opt_ai.py`；预测落点=重放落点 | 5 |
| 3/W3-01 | Simulator 三入口共享轮循环、注入骰子、终局/超时、黄金局 | `src/treasure_game/simulator.py`, `tests/test_simulator.py`, `tests/test_golden_games.py` | W2-04 | `python -m pytest -q tests/test_simulator.py tests/test_golden_games.py`；spec 用例 2/3、仅终点对战 | 10 |
| 3/W3-02 | 控制台菜单、输入、视图渲染/日志及入口 | `src/treasure_game/console_io.py`, `src/treasure_game/__main__.py`, `tests/test_cli.py` | W3-01 | `python -m pytest -q tests/test_cli.py`；`python -m treasure_game` 七菜单项可选 | 6 |
| 4/W4-01 | 批量配对实验驱动、CSV/JSON 指标 | `experiments/run_batch.py`, `tests/test_experiments.py` | W3-02 | `python -m pytest -q tests/test_experiments.py`；`python experiments/run_batch.py --seed 42 --maps 2 --dice-seeds 2 --output experiments/results/smoke.csv` 产生 12 行（4 对×3 模式） | 7 |
| 4/W4-02 | 跨层性质、手动性能基准及全套回归 | `tests/test_properties.py`, `tests/test_performance.py` | W4-01 | `python -m pytest -q tests --ignore=tests/test_performance.py`；性能机跑 `python -m pytest -q tests/test_performance.py` | 4 |
| 5/W5-01 | **选做** PyQt6 GameIO 适配器 | `src/treasure_game/gui.py`, `tests/test_gui.py` | W4 gate、PyQt6 可用 | `python -m pytest -q tests/test_gui.py`；未装 PyQt6 的控制台测试仍通过 | 8 |
| 6/W6-01 | 按 05 写报告、附样本路径及统计口径 | `reports/report.md` | W4 gate、W5 PASS/SKIP | `Select-String -Path reports/report.md -Pattern 方法,结果,局限,复现`（PowerShell）均命中；数据可反查 CSV | 6 |

合计 **119 点**，必做 111 点、选做 GUI 8 点。`experiments/results/`、`logs/` 仅运行时生成，不属源码写范围；批量数据仅 W4-01 单进程写。`__init__.py` 归 W0-01，`__main__.py` 归 W3-02。

## 并行队列与交接协议

| 时点 | 可并行分派 | 串行/冲突处理 |
| --- | --- | --- |
| W0-01 完成 | W0-02 | W0-01 先验收；不并改 `models.py` |
| W1-01 完成 | W1-02 与 W1-03 | 两者只读 `path_module.py`，分别写 Map/Battle |
| Wave 1 gate 完成 | W2-01 与 W2-02 | 可见人类策略归 W2-02；W2-01 不改 OPT |
| W2-02 完成 | W2-03 与尚未完成的 W2-01 | W2-04 等待前三任务，独占 `ai_module.py` |
| W3-01 完成 | W3-02 | W4-01 必须等 Wave 3 gate，不能提前覆盖集成产物 |
| Wave 4 gate 完成 | W5-01 选做，或直接 W6-01 | GUI/报告无共同写路径；报告只读封存原始样本 |

集成时检查 `git diff --name-only` 是否完全落在该任务写范围；若前波文件要改，暂停并行、返给原 owner，重跑其波 gate。上述仅是将来分派策略，本阶段不派生实现智能体。

## 波次 Gate（PASS 才进入下一波）

| 波次 | 进入条件 | 离开条件 |
| --- | --- | --- |
| Wave 0 脚手架 | 六份设计文档冻结；D1—D11 定稿 | Python 3.11+ 可导入 models；运行时零依赖；冻结类型、签名符合 01 |
| Wave 1 核心模块 | Wave 0 PASS | `python -m pytest -q tests/test_path.py tests/test_map.py tests/test_battle.py` 全过；可达、撞墙步数、终点结算 |
| Wave 2 AI | Wave 1 PASS | 四组 AI 测试全过；剪枝=无剪枝、缓存命中、根预测终点一致、分治跨块重放 |
| Wave 3 模拟器/IO | Wave 2 PASS | Simulator/黄金局/CLI 全过；三入口七菜单；无 PyQt 可运行；HARD 不向人工泄露真值 |
| Wave 4 仿真实验 | Wave 3 PASS | 12 行 smoke、CSV/JSON schema、性质与完整非性能测试全过；正式 30×3 配对样本封存 |
| Wave 5 GUI 选做 | Wave 4 PASS 或明确 SKIP | 执行则 GUI 冒烟且无 PyQt 仍能启动控制台；SKIP 不阻断 Wave 6 |
| Wave 6 报告 | Wave 4 PASS、Wave 5 PASS/SKIP | 报告图表可反查原始样本；[DoD](04-test-plan.md) 达成，附命令与局限 |

任何 gate FAIL 交回原 owner 修复，不能以缩小测试范围绕过。性能记录 CPU/Python 版本，未达目标如实报告。

## 风险登记（高/中/低为主观先验）

| ID | 风险 | 影响 | 概率 | 缓解措施 | 触发信号 |
| --- | --- | --- | --- | --- | --- |
| R01 | 墙 `1`/宝藏编码冲突 | 载入误判 | 高 | D1 解析约定和回写回读测试 | `1` 被计入宝藏 |
| R02 | 陷阱消失后不再暴露 | HARD 规则错误 | 高 | 原值/active/revealed 分离 | 第二次扣分或重新隐藏 |
| R03 | AI 复制移动规则 | 估值漂移 | 高 | 唯一 Path 核与性质重放 | 预测终点≠执行终点 |
| R04 | `eval=H−O` 直接取 MAX | 反向决策 | 高 | 内部 U=−eval、小图反例 | 有利对战被主动放弃 |
| R05 | chance 像 MIN/MAX 剪枝 | 期望值错误 | 高 | 保守区间与无剪枝基线 | 剪枝前后根值不同 |
| R06 | DP key 漏揭示/骰/深度 | 缓存污染 | 高 | 完整 key 检查、变状态对比 | 缓存开关改变结果 |
| R07 | HARD 人类策略偷看真图 | 对照失真 | 中 | human_policy 只读 ViewMap | 未揭示陷阱改变人工动作 |
| R08 | 出生点/宝藏不连通 | 不终局 | 中 | 全非墙连通、500 回合超时 | timeout 比率上升 |
| R09 | 分治把块边当墙 | 漏跨块路径 | 中 | 全图 BFS 与全图重放 | 穿墙或漏目标 |
| R10 | 深度 4 指数爆炸 | 卡顿 | 高 | 默认 3、节点计数/基准 | p95 耗时暴涨 |
| R11 | 模式比较不配对 | 伪提升 | 中 | 封存地图/骰表与相同对手 | 两组图指纹不同 |
| R12 | GUI 依赖溢出核心 | 控制台运行失败 | 中 | 延迟导入、可选测试 | 无 PyQt 启动失败 |
| R13 | 近似称全局最优 | 报告不可辩护 | 高 | 图注写深度/K/超时 | 结论无边界条件 |
| R14 | 围死重合重复结算 | 分数失控 | 中 | 每回合一次结算 | 同回合多次 +5/-5 |

## 时间预算与首批顺序

| 波次 | ID 与点数 | 小计 | 累计必做 |
| --- | --- | ---: | ---: |
| 0 | W0-01 3、W0-02 5 | 8 | 8 |
| 1 | W1-01 10、W1-02 11、W1-03 6 | 27 | 35 |
| 2 | W2-01 8、W2-02 20、W2-03 10、W2-04 5 | 43 | 78 |
| 3 | W3-01 10、W3-02 6 | 16 | 94 |
| 4 | W4-01 7、W4-02 4 | 11 | 105 |
| 5 | W5-01 8（选做） | 8 | 105 |
| 6 | W6-01 6 | 6 | 111 |

**第一批**：W0-01 → W0-02 → W1-01；之后 W1-02 与 W1-03 可并行。不可先写 AI 再倒推修改移动语义。
