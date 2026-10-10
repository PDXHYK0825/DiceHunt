你是资深软件架构师 + 算法竞赛教练。工作目录：`E:\Comprehensive Experiment on Algorithm Design`。

# 背景
`Project/docs/spec.md` 是《对战地图寻宝策略分析平台（修订-三模式版本）》的完整需求规格（由 PDF 提取）。
`Project/Sources/` 下有原始 PDF。`Test/` 下有一些与本项目无关的 C 语言小题，忽略它们。

这是一个"算法设计与分析"课程的综合实验项目，需要：三模式（简单/普通/困难）、六大核心模块
（MapModule / PathModule / AiModule / BattleModule / Simulator / DivideConquerModule）、
两套 AI（贪心 BFS AI、OPT Expecti-Max + α-β 剪枝 + 记忆化 DP AI）、控制台版本（必做）、
GUI（选做加分）、批量仿真定量实验、实验报告。

# 你的任务（只做第一阶段：完善框架 + 详细执行计划，不要写实现代码）
请完整阅读 `Project/docs/spec.md`，然后**在仓库里创建/完善以下设计文档**（用中文书写，Markdown）：

1. `Project/docs/00-framework.md` —— 项目框架总览
   - 目标与范围（必做 / 选做加分 明确分开）
   - 技术栈与运行环境（Python 版本、仅标准库还是允许第三方；控制台必做零依赖，GUI 可选 PyQt）
   - 最终目录结构（逐文件、逐目录说明职责，给出完整树）
   - 数据模型（地图表示、状态对象、模式枚举、事件/日志模型）
   - 模块依赖图（谁调用谁，禁止反向依赖；说明为何 AI 层不能依赖 IO 层）
   - 关键设计决策与理由（ADR 风格，至少 8 条，含被否决的备选方案）

2. `Project/docs/01-module-contracts.md` —— 六大模块接口契约（这是本阶段最重要的产物）
   - 每个模块：职责、公开 API 的**精确签名**（含参数类型、返回值、异常）、不变量、边界条件
   - 必须逐字对齐 spec 中出现的这些接口名：
     `MapModule.get_view_map(mode)`、`PathModule.sim_player_multi_step(true_map, start_pos, move_seq, max_valid_step)`、
     `PathModule.get_adjacent_empty()`、`AiModule.get_opt_move(state, dice_N)`、
     `BattleModule.trigger_battle(true_map, posA, posB, scoreA, scoreB)`、
     `Simulator.run_human_vs_ai(game_mode)`、`run_machine_vs_machine()`、`run_human_vs_human()`
   - 明确"视图-真值隔离"如何用类型/接口强制（例如 AI 只能拿到 true_map，渲染只能拿到 view_map）
   - 明确 `sim_player_multi_step` 作为**唯一**移动语义真相源，AI 内部推演必须复用它或其纯函数内核

3. `Project/docs/02-algorithm-design.md` —— 算法设计
   - 贪心 AI：BFS 距离、性价比定义（含平局打破规则）、路径→方向序列生成
   - OPT Expecti-Max：节点类型（MAX=AI / MIN=人类 / CHANCE=骰子1-6）、
     估值函数 `eval = scoreHuman − scoreOpt`、状态 key 归一化（treasure/trap 用 frozenset 或位图）、
     α-β 在 chance node 下的正确剪枝边界条件、记忆化 DP 的 key 与命中统计、
     人类行为的建模方式（最坏情况 / 启发式策略集 / 有限理性），并说明搜索深度 3-4 的复杂度推导
   - 分治分块：切分策略、块边界跨块路径处理、子块求解 + 合并、精度-速度权衡的量化方式
   - 每个算法给出复杂度分析（时间/空间）与正确性论证要点

4. `Project/docs/03-execution-plan.md` —— 详细执行计划（**必须可被另一个 AI 智能体直接照做**）
   - WBS 任务分解：每个任务给出 ID、名称、**独占写文件范围（write scope）**、前置依赖、
     验收标准（可执行的命令 / 断言）、预估工作量
   - 严格划分波次（Wave 0 脚手架 → Wave 1 核心模块 → Wave 2 AI → Wave 3 模拟器/IO → Wave 4 仿真实验 → Wave 5 GUI 选做 → Wave 6 报告）
   - 并行策略：哪些任务可以并行给不同智能体，如何保证写范围不冲突
   - 里程碑与每波次的 gate（什么条件下才允许进入下一波）
   - 风险登记表（风险 / 影响 / 概率 / 缓解措施 / 触发信号），至少 10 条
   - 时间预算表（按任务给出相对工作量权重）

5. `Project/docs/04-test-plan.md` —— 测试与验收计划
   - 把 spec 第七节的 5 个关键测试用例逐条转成**可自动化的 pytest 用例**（给出测试函数名与断言要点）
   - 单元测试 / 集成测试 / 黄金对局回归测试 / 性能基准 的分层
   - 不变量与性质测试（property-based 思路）：例如"AI 返回的移动序列在 sim 中执行的最终落点 == 其内部估值落点"
   - 批量仿真实验设计：对照组、自变量、因变量、样本量、统计口径（均值/标准差/胜率/耗时/踩陷阱频次）
   - 明确的 Definition of Done

6. `Project/docs/05-report-outline.md` —— 实验报告大纲（对应 spec 第八、九节亮点与局限）

# 硬性要求
- **不要写 Python 实现代码**（不要创建 `.py` 文件）。本阶段只产出设计与计划文档。可以写少量伪代码/签名。
- 文档必须自洽、相互引用一致（模块名、函数签名、文件名在所有文档中完全一致）。
- 中文书写。表格优先。避免空话，所有结论要具体、可执行、可验收。
- 允许你先创建/调整目录结构（例如 `Project/docs/`、`Project/src/`、`Project/tests/`、`Project/experiments/`、
  `Project/logs/`、`Project/reports/`）以及 `.gitignore`、`README.md` 骨架、`requirements.txt`（占位），
  但**不要**写实现逻辑。
- 如果 spec 有歧义或缺口，在文档中用 `> ⚠ 待定决策` 明确标出，并给出**你的推荐默认值**与理由，不要留空。

# 交付
完成后，你的最终回复必须是一份**给下游实现智能体的交接摘要**，包含：
1. 你创建/修改的文件清单（相对路径）
2. 最终目录结构树
3. 波次划分与每波 gate 的浓缩版
4. 所有 `⚠ 待定决策` 的清单及推荐默认值
5. 你认为下游最容易做错的 5 个点（陷阱/易错点清单）
6. 下一步建议的第一批任务 ID（按依赖顺序）
