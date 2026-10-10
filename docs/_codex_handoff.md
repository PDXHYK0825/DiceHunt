**给下游实现智能体的交接摘要**

本阶段已完成设计，**尚未编写或运行 Python 实现**。控制台目标环境为 Python 3.11+、运行时零第三方依赖；pytest 仅用于开发测试，PyQt6 仅用于选做 GUI。

**交付文件**
- `Project/docs/00-framework.md:1`：范围、目标目录、数据模型、依赖与 11 项待定决策。
- `Project/docs/01-module-contracts.md:1`：六大模块的精确签名、边界、不变量与异常。
- `Project/docs/02-algorithm-design.md:1`：贪心、OPT、分治的算法、复杂度及正确性边界。
- `Project/docs/03-execution-plan.md:1`：15 项独占写范围任务、波次 gate、风险和工作量。
- `Project/docs/04-test-plan.md:1`：规格五例的 pytest 断言、性质测试与 270 局配对实验。
- `Project/docs/05-report-outline.md:1`：报告章节、证据要求和局限性。

**最终目标目录**（以下为后续实施目标；目前只创建了上列六份文档）
```text
Project/
├─ README.md
├─ requirements.txt
├─ requirements-dev.txt
├─ .gitignore
├─ Sources/
│  └─ 对战地图寻宝策略分析平台（修订‑三模式版本）(1).pdf
├─ docs/
│  ├─ spec.md
│  ├─ 00-framework.md
│  ├─ 01-module-contracts.md
│  ├─ 02-algorithm-design.md
│  ├─ 03-execution-plan.md
│  ├─ 04-test-plan.md
│  └─ 05-report-outline.md
├─ src/treasure_game/
│  ├─ __init__.py
│  ├─ __main__.py
│  ├─ models.py
│  ├─ map_module.py
│  ├─ path_module.py
│  ├─ battle_module.py
│  ├─ greedy_ai.py
│  ├─ human_policy.py
│  ├─ opt_ai.py
│  ├─ divide_conquer_module.py
│  ├─ ai_module.py
│  ├─ simulator.py
│  ├─ console_io.py
│  └─ gui.py                 # 选做
├─ tests/
│  ├─ test_map.py
│  ├─ test_path.py
│  ├─ test_battle.py
│  ├─ test_greedy_ai.py
│  ├─ test_opt_ai.py
│  ├─ test_divide_conquer.py
│  ├─ test_ai_module.py
│  ├─ test_simulator.py
│  ├─ test_golden_games.py
│  ├─ test_cli.py
│  ├─ test_experiments.py
│  ├─ test_properties.py
│  ├─ test_performance.py
│  └─ test_gui.py            # 选做
├─ experiments/
│  ├─ run_batch.py
│  └─ results/
├─ logs/
└─ reports/
   └─ report.md
```
既有的 `Project/docs/_codex_prompt.md`、`_codex_run.log` 及仓库其他未跟踪文件未改动；目标树不把工作材料算作产品文件。

**波次与 gate**
| 波次 | 放行条件 |
| --- | --- |
| Wave 0 脚手架 | 冻结 D1—D11；模型类型、签名和零运行依赖符合契约。 |
| Wave 1 核心 | Path、Map、Battle 测试通过；撞墙不耗步、地图可达、终点结算成立。 |
| Wave 2 AI | 贪心/OPT/分治测试通过；剪枝与无剪枝同值，预测落点可重放。 |
| Wave 3 模拟器/IO | 三入口、七菜单、黄金局及 HARD 视图隔离通过。 |
| Wave 4 仿真实验 | 12 行 smoke、性质与集成测试通过；封存正式 30×3 配对样本。 |
| Wave 5 GUI 选做 | 通过独立 GUI 冒烟，或明确记为 SKIP；不得阻塞控制台。 |
| Wave 6 报告 | 每项结论能追溯到原始结果、参数和复现命令。 |

**全部待定决策及推荐默认值**
| ID | 推荐默认值 |
| --- | --- |
| D1 | `1` 只作墙；txt 宝藏分值 `>=2`。 |
| D2 | 陷阱扣分仅一次；已踩陷阱永久可见，标记为已耗尽。 |
| D3 | txt 用尺寸、整数矩阵、A/B 坐标；所有非墙格连通；随机生成最多重试 1000 次。 |
| D4 | 败方只推往从未放资源的空格，按上、左、下、右选；同分或无空位不推。 |
| D5 | 人类候选只由 `ViewMap` 生成；MIN 在有限策略集中选对 AI 最不利结果。 |
| D6 | “OPT”仅指给定深度与候选集内最优；默认深度 3、候选上限 24，近似须标记。 |
| D7 | 模式与策略由构造/入口确定；默认机机为贪心 A 对 OPT B。 |
| D8 | `end` 在 IO 层截断；核心接受空序列和撞墙尝试，成功走满 N 步后忽略剩余指令。 |
| D9 | 单局上限 500 回合；未结束记 `draw_timeout`，不计正常胜负。 |
| D10 | 随机图默认宝藏 `+2`、陷阱 `-2`；A 先行、A/B 交替。 |
| D11 | 拾取最后宝藏的回合，先做一次终点对战，再判定终局。 |

**最易做错的五点**
1. 把 `ViewMap` 交给 AI，或让人工策略、渲染层接触 `TrueMap`。
2. 在 AI 中另写移动规则，导致撞墙步数、途中取物或预测终点与 Path 不一致。
3. 将 `scoreHuman−scoreOpt` 直接取 MAX；AI 内部应优化其相反数。
4. 对 chance 节点套用普通 α-β，或把未算完的界缓存为精确期望值。
5. 分块时把边界当墙，或把“限深、裁剪、分治”的结果宣称为全局最优。

**下一批任务**：按 `W0-01 → W0-02 → W1-01` 顺序启动；Wave 0 gate 通过且 `W1-01` 完成后，再并行 `W1-02` 与 `W1-03`。设计检查结果：六份文档链接均有效、15 项任务写范围无重复、未创建 `.py` 文件；实现测试须留待后续波次运行。