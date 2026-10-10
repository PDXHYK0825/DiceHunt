# 04｜测试、实验与验收计划

> 依据 [spec 第七节](spec.md)；实现签名以 [01](01-module-contracts.md) 为准，默认值见 [00 D1—D11](00-framework.md)。**本阶段不创建测试代码**；以下 pytest 函数名和夹具是 Wave 1—4 工作指令。命令均从 `Project/` 执行，测试环境安装 `pytest`；控制台运行时依旧零依赖。

## 将 spec 第七节 5 例转为 pytest

| 原用例/文件/测试函数 | 可复现夹具（未列格均为空，初始点不可放资源） | 核心断言与边界 |
| --- | --- | --- |
| 1 简单/`tests/test_greedy_ai.py::test_spec_case_1_easy_greedy_ratio` | 8×8；AI B=(0,0)，人工 A=(0,2)；宝藏 (0,3)=+6、(2,0)=+4，其余可为空；`dice_N=3` | 两目标距离 3/2、比率均 2，按分值破平局选 (0,3)；返回 `RIGHT×3`，Path 重放终点 (0,3)、加 6；把人工移到其他位置不改变贪心输出；EASY 的视图原样显示陷阱（另加 -2 于不在路径格） |
| 2 普通/`tests/test_opt_ai.py::test_spec_case_2_normal_opt_tradeoffs` | 固定 8×8、NORMAL、深度 1、骰 1；AI B=(3,3)、人工 A=(3,4)，左邻宝藏 (3,2)=+2、另有 (7,7)=+2；三局 AI/人工分数依次 10/0、0/10、10/0（第三局左邻宝藏改 +20） | 前局终点重合使 AI +5/人工 -5，U 增 10，优于捡 +2，选择主动对战；后局重合使 U 减 10，选择 +2 或安全停步但不重合；第三局选 +20 宝藏；NORMAL 的 ViewMap 暴露尚未触发陷阱；根 U 与无剪枝枚举一致 |
| 3 困难/`tests/test_simulator.py::test_spec_case_3_hard_hidden_trap_reveal` | 8×8；人工 A=(1,1)，陷阱 (1,2)=-2，AI B=(6,6)，远处宝藏 +2；HARD、注入骰=1 和人工输入 `RIGHT`，截取前后视图 | 前图 (1,2)=0；Path 成功移动、分数 -2、`TRAP` 事件一次、后图 (1,2)=-2 且 `spent_traps` 标记；第二次踏入不再扣分；人工 UI/日志在踩中前不含该陷阱坐标/负值，AI 所持 TrueMap 始终有 -2；同图 NORMAL 一开始可见 -2 |
| 4 撞障碍/`tests/test_path.py::test_spec_case_4_blocked_attempt_does_not_spend_die` | 8×8；起点 (2,1)；墙 (2,3)；`max_valid_step=3`，序列 `RIGHT,RIGHT,UP,LEFT` | 尝试数 4、有效步 3、终点 (1,1)、分数增量 0；第 2 条事件 `BLOCKED` 且含不消耗步数文案；随后的 UP/LEFT 仍执行，越界同规则 |
| 5 对战/`tests/test_battle.py::test_spec_case_5_boxed_loser_still_loses_points` | 3×3，中心 (1,1) 空且四邻为墙；双方回合终点均中心，A=10、B=5 | `triggered=True`、A=15、B=0，败方 B 仍中心；同分 5/5 不变、未重合不结算；Simulator 同回合最多触发一次，不能因仍重合递归触发 |

用例 1 的 A=(0,2) 只位于**路径中间**：途经不会对战，AI 最终落在 (0,3)；用例 2 的远处宝藏保证深度 1 仍有局面价值，避免对局提前结束掩盖战斗比较。自动夹具必须直接构造 `TrueMap` 和 `GameState`，不依赖随机生成器；若具体参数不满足预期，先打印状态/U 并修正夹具，不放宽断言。

## 测试层次、运行频度与失败定位

| 层 | 文件 / 命令 | 必测范围 |
| --- | --- | --- |
| 单元 | `python -m pytest -q tests/test_path.py tests/test_map.py tests/test_battle.py tests/test_greedy_ai.py tests/test_opt_ai.py tests/test_divide_conquer.py` | 5 个 spec 例中的路径/结算/AI、生成连通、视图、骰子六分支、剪枝值、DP key 变化、分块边界 |
| 集成 | `python -m pytest -q tests/test_ai_module.py tests/test_simulator.py tests/test_cli.py tests/test_experiments.py` | 三模式路由、三对局入口、骰序、终局/超时、中文提示、CSV schema |
| 黄金局 | `python -m pytest -q tests/test_golden_games.py` | 2 张手工图×3 模式×固定种子；比较事件种类/座位/位置/分数序列及终局，不把耗时硬编码进快照；更新快照必须附规则变更原因 |
| 性质/负例 | `python -m pytest -q tests/test_properties.py` | 100 个固定种子小图循环；地图类型隔离、移动、资源单次性、状态不可变、回放一致性 |
| 性能 | `python -m pytest -q tests/test_performance.py` | 单独机器/环境，8×8 深度 3、12×12/14×14 分治阈值；记 CPU/Python、节点、耗时中位数/p95 与缓存命中率，不与稳定单元测试混合 |

必须覆盖的失败路径：无效/非矩形 txt、重合/占墙出生点、生成无解、序列空/比 N 长/连续撞墙、负/零骰值、所有宝藏初始为空、资源触发后回踩、同分/四面围死、失败导出残局、ViewMap 误入真值 API 抛 `TypeError`、人工 EOF 受控中止。

## 不变量 / 性质测试（stdlib 定种子生成，不强制 Hypothesis）

| 性质 / 建议函数 | 自动断言 |
| --- | --- |
| `test_ai_projection_matches_path_replay` | 对 100 个定种子合法状态：`AiModule.get_opt_move(state,dice_N)` 后再次调用同一 Path 核；`final_pos==get_diagnostics().root_final_pos` 且 `score_delta==root_score_delta`；贪心同理 |
| `test_successful_steps_are_bounded_and_blocked_are_free` | 每条轨迹 `0<=valid_steps<=N`、`valid_steps<=attempted_steps`；加入墙碰撞不改变后续有效位移及所得分；达到 N 后追加动作不改变结果 |
| `test_resources_are_consumed_once_and_inputs_frozen` | 同一宝/陷阱重复踏入时分数只变一次；原 `TrueMap` 不变；HARD 已暴露标记单调不减 |
| `test_visibility_is_noninterfering` | 两张真图仅改变隐藏未触发陷阱坐标，人类可见视图相同，`human_policy` 动作集合/顺序相同；AI 真值输入可以不同；GameIO 永不持有真图 |
| `test_opt_cache_and_pruning_are_exact` | 小图、相同候选集/深度/模型下：开/关精确缓存、开/关安全剪枝的根 U 与确定 tie-break 动作一致；`cache_hits>0` 的重复子树场景 |
| `test_battle_only_at_turn_end` | 中间一步经过对手但终点分离无 BATTLE；终点重合只触发一次；四邻无空位也不重复结算；最后宝藏与对手同格则先对战后结算终局 |
| `test_generated_maps_are_connected_and_reproducible` | 同 seed/参数静态地图指纹与两出生点相同，所有非墙格 BFS 可达；不同 seed 不要求一定不同 |
| `test_partition_actions_replay_on_full_map` | 每个分块输出在完整图中没有越墙成功步；跨块宝/陷阱实际触发与预测一致 |

**类型限制不是秘密计算的绝对保证**：Python 类型注解与构造守卫配合测试保证 API 边界；安全审计还需确认 `console_io.py`/`gui.py` 没有访问 `.true_map` 或导入内部真图。人工可见事件应只包含已发生的隐藏陷阱事件，快照不能序列化未揭示陷阱坐标。

## 批量定量实验（Wave 4）

| 项 | 预注册口径 |
| --- | --- |
| 实验单位/样本量 | 30 个固定地图 seed × 每图 3 个固定骰序 seed = 90 个**配对单元**；每单元 EASY/NORMAL/HARD 各 1 局，共 270 局。先用 `--maps 2 --dice-seeds 2` 做 12 行 smoke。地图同布局同出生点，骰序预生成按 `(turn,seat)` 索引，搜索不消耗 RNG |
| 对照与自变量 | 同一 `VIEW_HEURISTIC` 座位 A（只读 ViewMap）对机器 B；EASY=贪心完全可见，NORMAL=OPT 完全可见，HARD=OPT 隐藏未触发陷阱。EASY vs NORMAL 隔离算法差异；NORMAL vs HARD 隔离可见性差异。B 始终读取真值，不能将隐藏模式误解为 AI POMDP |
| 控制条件 | Python/CPU、地图尺寸/墙宝陷数量、起始分数/座位、固定人类代理策略、深度 3/K 24、分治阈值 144、500 回合上限、种子清单一致；地图生成与骰子各自独立 RNG；报告出座位优势未对调的限制 |
| 主因变量 | AI 胜率 `wins/(wins+losses+ties)`（平局胜率贡献 0，平局率单列）；平均 `scoreOpt−scoreHuman` 和样本标准差（`n−1`）；机器每次决策耗时中位数/p95 与每局总耗时；每局机器/人工踩陷阱次数、对战次数、DP 命中率 |
| 截尾/异常 | `draw_timeout=True` 单列数量/比例，不纳入**正常胜率**分母；同时报告含超时的保守范围；决策耗时缺失不是 0；实验异常保留错误类型并停止该组，不自动丢样本 |
| 配对分析 | 每单元差值 `NORMAL−EASY` 与 `HARD−NORMAL` 的得分差/陷阱频次、95% bootstrap（按地图 seed 分簇重抽样）区间；不把 270 局当独立地图；结论不越过置信区间与人类代理的适用范围 |
| 消融/分治 | 固定较小地图同候选树比较 OPT 全树/关剪枝/关缓存的 U、节点、时间；阈值附近 10×10、12×12、14×14 同图同骰比较单体/分治的 `|ΔU|`、动作一致率、加速比、胜率差；超时记 censored |
| 记录 schema | 每行 `map_seed,dice_seed,map_id,mode,depth,K,partition_enabled,human_model_id,score_a,score_b,winner,draw_timeout,turns,trap_steps_a,trap_steps_b,battles,ai_calls,ai_elapsed_ns,cache_hits,cache_misses`；另保存地图/骰表版本、源码 commit 和环境元数据；不得仅留汇总均值 |

`VIEW_HEURISTIC` 的生成逻辑与 OPT 的 `MIN` 人类候选共用 `human_policy.py` 的视图入口，实验至少固定其中一种 deterministic 贪心视图策略作为 A 的实际行动；搜索 MIN 可以考虑多种策略，**预测策略集**与**实战 A 单一策略**的不同需在报告明确。贪心 AI 与 OPT 在 EASY/NORMAL 对比时完全信息相同；NORMAL/HARD 对比时只能变化人工视图。

## Definition of Done（不可用文档替代运行证据）

1. Wave 0—4 所有 gate PASS；六大模块及指定公开签名存在，三模式/三入口/七菜单可运行；无 PyQt/pytest 仍可启动控制台。
2. spec 第七节五例及负例、100 种子性质测试、固定黄金局全通过；AI 预测终点与实际终点一致；机会节点优化与无剪枝同值；无未揭示真值泄漏到人工 IO。
3. `python -m pytest -q tests --ignore=tests/test_performance.py --ignore=tests/test_gui.py` 返回 0；性能基准另跑并在报告记录环境、p50/p95、节点/命中，不做虚假跨机绝对性能承诺。
4. 270 局正式配对观测及 schema/seed 表可复现；异常/超时按上述口径明确计数；分治精度与速度均有图表、原始样本和局限。报告依 [05](05-report-outline.md) 完成。
5. GUI 若做必须独立可选且不影响零依赖验收；若未做标 `SKIP`，不是必做失败。当前设计阶段的 DoD 则是六份文档完整、互引一致、无 `.py` 新文件，不能声称上述运行测试已经通过。
