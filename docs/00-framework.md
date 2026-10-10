# 00｜项目框架总览

> 依据：[原始需求](spec.md)。本文件规定目录、模型和架构边界；签名以 [01 接口契约](01-module-contracts.md) 为唯一准则，算法、WBS、测试分别见 [02](02-algorithm-design.md)、[03](03-execution-plan.md)、[04](04-test-plan.md)。下列树是**最终目标**，并非本阶段已经实现；本阶段仅创建六份 Markdown。

## 目标与交付边界

| 类别 | 必须交付并验证 | 不在本阶段实现 |
| --- | --- | --- |
| 必做运行时 | Python 3.11+ 控制台零第三方依赖；三模式、三种对局入口、txt/随机地图、日志及确定性种子；六模块及两套 AI | 本阶段只设计，不写 `.py` |
| 必做算法 | 贪心 BFS；限深 OPT Expecti-Max + 安全 α-β + 精确状态缓存；大地图分块近似；统一移动语义 | 不以“全局最优”包装限深/裁剪结果 |
| 必做实验 | 小图规则与策略测试、批量地图定量对照、胜率/得分/踩陷阱/耗时/缓存命中与分治质量 | 测试代码、实验数据在后续波次生成 |
| 必做报告 | 实验设计、复杂度、正确性、误差与局限；[05 大纲](05-report-outline.md) | 正式报告在 Wave 6 写 |
| 选做加分 | PyQt6 GUI；可调奖惩/骰子/深度、日志回放、双向未知陷阱 POMDP | 不能阻塞控制台必做验收 |

运行时仅 `dataclasses`、`enum`、`collections`、`random`、`time`、`typing`、`json`、`pathlib` 等标准库；测试使用**开发依赖** `pytest`（目标 `requirements-dev.txt`），不装测试依赖也能运行 `python -m treasure_game`；GUI 的 PyQt6 只在可选入口导入。CI/评分运行环境固定 Python 3.11，控制台 UTF-8，跨 Windows/Linux 使用 `pathlib`；种子通过独立 `random.Random(seed)` 注入而非全局随机状态。

## 最终目标目录（逐项职责）

```text
Project/
├─ README.md                         # 安装、运行、种子与验收指令
├─ requirements.txt                  # 控制台运行时空依赖清单
├─ requirements-dev.txt              # pytest 开发依赖
├─ .gitignore                        # 忽略缓存、生成日志和实验结果
├─ Sources/
│  └─ 对战地图寻宝策略分析平台（修订‑三模式版本）(1).pdf  # 原始只读材料
├─ docs/
│  ├─ spec.md                        # PDF 提取的原始规格，不擅改
│  ├─ 00-framework.md                # 架构与决策
│  ├─ 01-module-contracts.md         # 唯一公开接口定义
│  ├─ 02-algorithm-design.md         # 算法与复杂度
│  ├─ 03-execution-plan.md           # 波次、独占写范围和 gate
│  ├─ 04-test-plan.md                # 测试用例和完成定义
│  └─ 05-report-outline.md           # 报告章节及证据表
├─ src/
│  └─ treasure_game/
│     ├─ __init__.py                  # 包边界，无副作用
│     ├─ __main__.py                  # python -m treasure_game 入口
│     ├─ models.py                    # 模式、地图、状态、事件、结果、IO 协议
│     ├─ map_module.py                # MapModule：解析/生成/导出/视图
│     ├─ path_module.py               # PathModule：唯一移动内核、BFS、邻空地
│     ├─ battle_module.py             # BattleModule：终点对战
│     ├─ greedy_ai.py                 # 纯贪心候选与 BFS 选路
│     ├─ human_policy.py              # 只依据 ViewMap 生成自动人工动作
│     ├─ opt_ai.py                    # 限深博弈核、chance、界与缓存
│     ├─ divide_conquer_module.py     # DivideConquerModule：分块与合并
│     ├─ ai_module.py                 # AiModule：两种 AI 外部入口/统计
│     ├─ simulator.py                 # Simulator：轮次、结束、批量驱动
│     ├─ console_io.py                # 人工指令解析、视图渲染、文本日志
│     └─ gui.py                       # 可选 PyQt6 适配器，仅 Wave 5
├─ tests/
│  ├─ test_map.py                     # 加载/生成/可见性
│  ├─ test_path.py                    # 指令与移动内核
│  ├─ test_battle.py                  # 对战边界
│  ├─ test_greedy_ai.py               # 贪心策略
│  ├─ test_opt_ai.py                  # chance/剪枝/缓存/效用
│  ├─ test_divide_conquer.py          # 跨块与近似质量
│  ├─ test_ai_module.py               # AI 路由/一致性
│  ├─ test_simulator.py               # 回合与模式路由
│  ├─ test_golden_games.py            # 固定种子黄金对局
│  ├─ test_cli.py                     # 控制台菜单与输入
│  ├─ test_experiments.py             # 批量结果统计
│  ├─ test_properties.py              # 有界随机性质测试
│  ├─ test_performance.py             # 手动性能基准
│  └─ test_gui.py                     # 可选 GUI 冒烟
├─ experiments/
│  ├─ run_batch.py                    # 固定种子矩阵及 CSV/JSON 导出
│  └─ results/                        # 生成的原始观测（不入库）
├─ logs/                               # 生成的文本对局日志（不入库）
└─ reports/
   └─ report.md                       # Wave 6 正式报告
```

`docs/_codex_prompt.md` 与 `_codex_run.log` 属于现有工作材料，不纳入目标产品树；不清理或改动。`Test/` 与本项目无关。空目录在实际落地时由脚手架创建，不用占位实现文件冒充已完工。

## 数据模型与不变量

| 类型（定义于 `models.py`） | 字段/语义 | 约束 |
| --- | --- | --- |
| `GameMode` / `SeatPolicy` | `EASY/NORMAL/HARD`；`HUMAN/GREEDY/OPT/OPT_PARTITION`（**以 [06](06-interface-freeze.md) 为准**；`VIEW_HEURISTIC` 是座位 A 的视图启发式人类代理策略名，不是 `SeatPolicy` 取值） | 人机 AI 路由 EASY→贪心，其余→OPT；机器对局可注入仅基于视图的自动人工代理 |
| `Direction`、`Position`、`Seat` | `UP/LEFT/DOWN/RIGHT`；`Position(row, col)`；`Seat.A/B` | 零基行列；控制台 `w/a/s/d` 只在 IO 转换；禁止斜向 |
| `TrueMap` | `terrain: tuple[tuple[int,...],...]`（0 空地/1 墙）；`treasure_values/trap_values: tuple[tuple[Position,int],...]`（排序后的静态原值）；`remaining_treasures/active_traps/revealed_traps: frozenset[Position]`；`map_id: str` 为静态内容指纹 | 资源只能触发一次；`revealed_traps` 包含已触发陷阱；迭代与状态键稳定 |
| `ViewMap` | 不可变展示格 `cells`（0、1、正宝藏、负陷阱），另有 `spent_traps` 标志集合 | 从 `TrueMap` 派生；HARD 未触发陷阱呈 0；历史暴露陷阱展示负数但无再扣分 |
| `GameState` | `true_map`、`pos_human`、`pos_opt`、`score_human`、`score_opt`、`turn`、`mode` | 仅机器/规则层可持有；人人/机机用 seat A/B 映射到相同两座位；不含 IO 对象 |
| `GameEvent` | `kind`、`actor: Seat | None`、`position`、`score_delta`、`message` | Path 纯核不知座位时 `actor=None`，Simulator 在发布前补为当前座位；事件有序、只读；隐藏陷阱信息仅在踩中后发布 |
| `MoveResult` / `BattleResult` / `GameResult` | 移动返回新图与分数增量；对战返回两人新坐标/分数；整局返回分数、胜方、事件、统计 | 无原地修改；事件记录可复现实验；不保存未触发陷阱到人工日志 |
| `SearchDiagnostics` | `nodes`、`cache_hits`、`cache_misses`、`elapsed_ns`、`root_final_pos`、`root_score_delta`、`approximate` | 为实验和移动一致性断言提供可观察证据，不包含未触发陷阱展示 |

原始 txt 的单格约定 `0` 空、`1` 墙、`>=2` 宝藏得分、`<0` 陷阱扣分；加载后拆成底图与资源层，导出时仅输出**初始布局**，残局回放改由事件/状态快照承担。位置与资源集合/值均不可变，分叉搜索共享底图；游戏结束仅判断 `remaining_treasures` 为空，起始即空则零回合结算。`TrueMap` 和 `ViewMap` **不同名义类型**；AI/Path/Battle 的入参必须是 `TrueMap`，`GameIO.render` 只接受 `ViewMap`、经脱敏的分数/事件，构造适配器时运行时校验类型。Python 没有绝对的信息流安全：不把 `GameState`/`TrueMap` 传给人工 IO 是调用图责任。

## 依赖图与单向调用

```text
__main__ → console_io → (models 的 GameIO 协议)
__main__ → simulator → map_module → path_module → models
                    → ai_module → greedy_ai → path_module
                                → opt_ai → human_policy、path_module、battle_module
                                → divide_conquer_module → opt_ai 的纯求解回调
                    → human_policy、path_module、battle_module → models
gui（可选） → simulator、models 的 GameIO 协议
experiments/run_batch → simulator、map_module、models
```

`MapModule` 的连通校验调用 `PathModule.bfs_distances`，`PathModule` **不能**反向依赖 `MapModule`；`BattleModule` 调 `PathModule.get_adjacent_empty`，Path 不能反向调 Battle。`opt_ai.py` 不导入 `DivideConquerModule`；分块通过注入局部求解回调调用纯核，由 `ai_module.py` 单向协调，避免环。`Simulator` 只认 `GameIO` 协议，由 `__main__.py` 绑定 `console_io.py`；AI 只处理不可变状态与动作，绝不读取 stdin、打印、知道 PyQt 类型或接收 `ViewMap`。否则搜索复制 IO 副作用、隐藏真值泄露到人工 UI，且无法注入确定性对手/复现测试。

## ADR（约束性决策）

| ID | 采用及理由 | 否决方案与原因 |
| --- | --- | --- |
| ADR-01 | Python 3.11+，控制台纯标准库；便于评分机器直接运行 | 全部依赖 PyQt/Pygame：增大部署门槛 |
| ADR-02 | `TrueMap`/`ViewMap` 两种不可变类型，展示端仅接收视图 | 单一可变二维数组：历史暴露和 AI 预测容易串扰 |
| ADR-03 | `sim_player_multi_step` 为唯一移动规则；搜索复用其纯内核 | AI 手写第二份移动：撞墙步数和中途资源容易漂移 |
| ADR-04 | 零基 `(row,col)`，静态底图与动态资源分离 | 原地覆写负值为 0：丢失永久暴露历史 |
| ADR-05 | `scoreHuman-scoreOpt` 为报告评估值；搜索 AI 效用取负值 | 直接以该值做 MAX：优化方向反了 |
| ADR-06 | chance 等权六支；仅在有可证上下界时剪枝，精确项才入 DP | 把机会节点当普通 MIN/MAX 做 `alpha>=beta`：不正确 |
| ADR-07 | 限深、候选裁剪明确标“近似”，小地图无裁剪穷举作基准 | 声称所有图都全局最优：状态空间不可承受 |
| ADR-08 | seeded RNG 注入，游戏与 AI 搜索不共享全局随机数 | 调用全局 `random`：结果随诊断/搜索顺序漂移 |
| ADR-09 | GUI/控制台只作 `GameIO` 适配器，Simulator 管回合 | 将规则埋进界面回调：无法自动批量测试 |
| ADR-10 | 分治只作用于大图 OPT；跨块可达与路径必须在全图核验 | 独立块内求解后拼接直线路径：可能穿墙或漏资源 |

## 规格缺口：推荐默认值（与 01—05 保持一致）

> ⚠ 待定决策 D1｜`1` 同时被描述为墙与“正数宝藏”。推荐 txt 中 `1` 唯一表示墙、宝藏值从 `2` 起；无损遵循墙优先，文件加载拒绝其他不合规编码。

> ⚠ 待定决策 D2｜“踩后陷阱消失”与“永久暴露负值”同时出现。推荐陷阱**效果**一次性消失，历史 `revealed_traps` 永久保存并在视图显示原负值，标为 spent、再踩不扣分；避免秘密重置。

> ⚠ 待定决策 D3｜txt 具体格式、出生点、地图生成重试上限未给。推荐首行 `height width`，后接整数矩阵与 `A row col`/`B row col`；出生点在空地且不同，所有非墙格从 A 连通（因此 B、宝藏可达）；随机生成最多重试 1000 次，失败报 `MapGenerationError`；具体格式通过 README 固化。

> ⚠ 待定决策 D4｜败方“相邻合法空地”、候选并列与双方相等未给。推荐仅选择底图 0 且**从未布置**宝藏/陷阱的位置；顺序上、左、下、右；同分不推开、不改分；无空位则留在原格。

> ⚠ 待定决策 D5｜OPT 的 MIN 人类策略与困难模式知识边界未给。推荐人类动作由**当前 ViewMap** 上的有限启发式策略集产生，MIN 从该集合选对 AI 最不利结果；执行仍在 TrueMap 上，禁止人类候选窥探隐藏陷阱；与最坏全动作模型单列敏感性实验。

> ⚠ 待定决策 D6｜“OPT 最优”与深度 3–4、分治近似相冲突。推荐“给定深度/候选集内最优”；小图全候选枚举作正确性对照，大图输出 `approximate=True` 与耗时/误差，绝不宣称全局最优。

> ⚠ 待定决策 D7｜三种对局入口的地图/模式传参及机器对机器阵容未指定。推荐 `Simulator` 构造时注入初始状态、模式、两座位策略与 RNG；`run_human_vs_ai(game_mode)` 显式覆盖模式；无参机机默认贪心 A vs OPT B（地图视图按构造模式）；无参人人使用构造模式；对照实验可构造两 OPT 或两贪心以控制变量。

> ⚠ 待定决策 D8｜路径输入长度、`end` 与非法指令边界未给。推荐 IO 把 `end` 截成短序列；核心 `move_seq` 仅 `Direction`，允许空序列及撞墙尝试，不强行截断原始指令数，达到 N 次成功后忽略剩余指令；非方向立即抛 `ValueError`。

> ⚠ 待定决策 D9｜对局可能因所有宝藏不可达/消极策略永不终止。推荐生成/导入保证宝藏连通，批量仿真单局 `max_turns=500` 超时标记 `draw_timeout`，不将其算作正常胜负；交互运行同样提示可中止。

> ⚠ 待定决策 D10｜随机地图宝藏/陷阱的分值分布和先手未指定。推荐默认宝藏 `+2`、陷阱 `-2`，A 先行，随后 A/B 严格交替；显式 txt 可用任何 `>=2` 宝藏、`<0` 陷阱，实验记录分值配置；这样生成器无需隐含新参数且小图可手工验算。

> ⚠ 待定决策 D11｜最后一个宝藏拾取与同回合终点对战谁先判定未写明。推荐先完成移动、**只结算一次终点对战**，再检测宝藏耗尽并结算最终胜负；初态没有宝藏则直接零回合结束，不触发凭空对战。
