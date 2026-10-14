# 01｜六大模块接口契约

> [总览与 D1—D11](00-framework.md)；[算法](02-algorithm-design.md)；[测试](04-test-plan.md)。以下 `def` 行是后续实现的规范签名，类名与函数名保持 `spec.md` 第四节写法；`get_adjacent_empty()` 在原规格只给名字，本文件补全必要参数。公开结果类型/异常统一定义在 `src/treasure_game/models.py`，不可用裸字典替代。

## 公共类型、阶段与错误约定

| 记号 | 精确定义 / 生命周期 |
| --- | --- |
| `Position` | 冻结值对象 `(row: int, col: int)`，零基；两出生点不能相同且均为空地 |
| `MoveSequence` | `tuple[Direction, ...]`；`Direction` 只含 `UP, LEFT, DOWN, RIGHT`；`end` 是 IO 控制符而非方向 |
| `TrueMap` / `ViewMap` | 两个独立冻结类；真值的静态底图+资源原值+动态剩余/已揭示集合；展示对象仅有格子与已耗尽陷阱标志；无反向转换 |
| `GameState` | 真值图、A/B 坐标分数、模式、`turn: Seat`；人机标准映射 A=Human、B=Opt；机机/人人保留字段名仅作固定座位编号；`SeatPolicy` 取值为 `HUMAN/GREEDY/OPT/OPT_PARTITION`（**以 [06](06-interface-freeze.md) 为准**） |
| `MoveResult` | `true_map, final_pos, score_delta, valid_steps, attempted_steps, events`；移动阶段的增量，**不**直接更新 `GameState` 分数/对手位置；Path 产生的 `MOVE/BLOCKED/TREASURE/TRAP` 事件**自带** `actor`（由 `sim_player_multi_step(..., actor=Seat.X)` 传入，默认 `Seat.A`），Simulator 直接发布不再改写（**以 [06](06-interface-freeze.md) 为准**） |
| `BattleResult` | `triggered, pos_a, pos_b, score_a, score_b, events`；仅对战后的最终结果，图不变 |
| `GameResult` | `final_state, winner, turns, events, metrics, draw_timeout`；超时不计胜负 |
| `SearchDiagnostics` | `nodes, cache_hits, cache_misses, elapsed_ns, root_final_pos, root_score_delta, approximate`；单次决策快照 |
| 错误 | `MapFormatError(ValueError)`、`MapGenerationError(ValueError)`、`IllegalStateError(ValueError)` 由 `models.py` 定义；IO 语法错误统一 `ValueError` 后由控制台重新输入 |

通用约束：地图与状态输入不可变，返回新值而不修改调用者；`Position` 越界/墙上、负步数、非枚举模式/方向、无效地图均抛 `ValueError` 或表列专门异常；用 `ViewMap` 冒充 `TrueMap` 抛 `TypeError`，不能静默修正。所有**合法**撞墙/越界方向都不抛异常，而产生日志事件。`score_delta` 使用宝藏 `+value`、陷阱原负值累加；`dice_N` 必须在 1..6。搜索期的事件保留在内存，不能输出到实际 UI。

## 1. MapModule｜`src/treasure_game/map_module.py`

| 项目 | 契约 |
| --- | --- |
| 职责 | txt 初始地图解析/导出、参数化随机布局与 BFS 有效性、按模式构造人工视图；不执行移动或对战 |
| 不变量 | 长宽正、矩形、底图只含 0/1、资源格不重叠/不占墙/出生点；所有非墙格与 A 连通；B≠A；静态 `map_id` 由布局确定 |

```python
def MapModule.__init__(self, true_map: TrueMap) -> None
def MapModule.get_view_map(self, mode: GameMode) -> ViewMap
def MapModule.load_txt(path: Path) -> tuple[TrueMap, Position, Position]  # 静态方法
def MapModule.save_txt(path: Path, true_map: TrueMap, pos_a: Position, pos_b: Position) -> None  # 静态方法，仅初始图
def MapModule.generate(height: int, width: int, walls: int, treasures: int, traps: int, rng: random.Random) -> tuple[TrueMap, Position, Position]  # 静态方法
```

`get_view_map` 的 EASY/NORMAL 暴露全部当前资源与陷阱，HARD 对 `active_traps` 中尚未 `revealed_traps` 的格显示 0；`revealed_traps` 始终显示其原始负值，但 `spent_traps` 标注无剩余伤害。对外不得返回底图/陷阱原值的引用。`load_txt` 接受 D3 首行维度、整数矩阵与两行坐标（`+1` 表示 1 分宝藏，`1` 表示墙），语法/连通错误抛 `MapFormatError`，找不到/无法写文件透传 `OSError`；`save_txt` 对已游玩地图抛 `IllegalStateError`，确保不把动态视图误作原图。`generate` 先验证 `height*width >= 2+walls+treasures+traps`、宝藏默认独立抽取（1–10 分各 9.9%，15/20 分各 0.5%）、陷阱默认 `-2`、数量非负、连通且出生点空而异；1000 次无成功抛 `MapGenerationError`，参数错误抛 `ValueError`。狭小/不连通 txt 一律拒绝，不能让胜负条件陷入不可达。

## 2. PathModule｜`src/treasure_game/path_module.py`

| 项目 | 契约 |
| --- | --- |
| 职责 | **唯一**移动语义核、BFS 连通/路径距离、败方相邻空地查询；不读骰子 RNG，不判定对战 |
| 不变量 | 成功步数 `0..max_valid_step`；每个成功步目标非墙且在界内；资源最多触发一次；失败尝试不改坐标/图/分数/步数 |

```python
def PathModule.sim_player_multi_step(true_map: TrueMap, start_pos: Position, move_seq: MoveSequence, max_valid_step: int) -> MoveResult  # 静态方法
def PathModule.get_adjacent_empty(true_map: TrueMap, origin: Position) -> Position | None  # 静态方法
def PathModule.bfs_distances(true_map: TrueMap, start: Position) -> dict[Position, int]  # 静态方法
def PathModule.bfs_path(true_map: TrueMap, start: Position, target: Position) -> MoveSequence | None  # 静态方法
```

逐条读取 `move_seq`，一旦成功次数达到 `max_valid_step` 即停止（含 `0`）；越界/墙发 `BLOCKED` 事件，文案 `该方向为障碍物，无法通过，不消耗步数`；成功入格立刻取宝/触陷并更新**返回的新 TrueMap**，事件依序记录。序列可空/可长于 N；无论连续撞墙多少次，不自动补骰步；非法方向抛 `ValueError`。`bfs_distances` 只以底图墙为障碍，返回起点距离 0，不可达目标缺键；`bfs_path` 同权最短、邻序上/左/下/右，`start==target` 返回空，`None` 表示不可达。`get_adjacent_empty` 同邻序，只看**静态从未布置资源的底图空格**，无候选返 `None`；非法起点抛 `ValueError`。BFS 不读 `ViewMap`、不触发宝物。

**禁止**在 `greedy_ai.py`、`opt_ai.py`、`simulator.py` 复制逐格结算：后续若优化出私有纯函数内核，`sim_player_multi_step` 与 AI 只能共同委托这同一个内核，并保留相同事件顺序与中途资源处理。任何优化都要先过 [04 的移动一致性测试](04-test-plan.md)。

## 3. AiModule｜`src/treasure_game/ai_module.py`

| 项目 | 契约 |
| --- | --- |
| 职责 | 对外统一本轮动作与诊断；EASY 调 `greedy_ai.py`；NORMAL/HARD 调 `opt_ai.py`，超过分块阈值经 `DivideConquerModule`；不输出/读取 IO |
| 不变量 | 每次返回只含 `Direction`，可少于 N；每条候选由 PathModule 在真图上检验；搜索不改变外部 `state` 与 RNG |

```python
def AiModule.__init__(self, depth: int = 3, candidate_limit: int = 24, partition_threshold: int = 144) -> None
def AiModule.get_greedy_move(self, state: GameState, dice_N: int) -> MoveSequence
def AiModule.get_opt_move(self, state: GameState, dice_N: int) -> MoveSequence
def AiModule.get_diagnostics(self) -> SearchDiagnostics
```

`get_opt_move(state, dice_N)` 和 `get_greedy_move` 均代表**当前 `state.turn` 的机器座位**，支持机机 A/B；搜索把行动座位规范成 Opt（MAX）、对手规范成 Human（MIN），返回坐标再映射回原座位；人机标准模式 B 为 Opt。无宝藏/终局返回空序列；`dice_N∉1..6`、非法 `state` 抛 `ValueError`/`IllegalStateError`。`get_opt_move` 无需凭 mode 决定是否贪心，它**始终**是 OPT；模式路由由 `Simulator.run_human_vs_ai(game_mode)` 决定。`get_diagnostics` 仅在一次决策之后有效，否则抛 `IllegalStateError`；包含根动作通过 Path 内核计算的 `root_final_pos`/`root_score_delta`、每次运行清零的缓存命中/未命中及近似标志。分治入口详见第 6 模块；默认限深和裁剪的“最优”仅对被搜索的动作集合成立。

## 4. BattleModule｜`src/treasure_game/battle_module.py`

```python
def BattleModule.trigger_battle(true_map: TrueMap, posA: Position, posB: Position, scoreA: int, scoreB: int) -> BattleResult  # 静态方法
```

职责仅是**移动阶段结束之后**由 Simulator 调用；`posA != posB` 返回 `triggered=False` 且完全不改位置/分数；相等且同分返回 `triggered=True` 的平局、不推开；分数不等，在原始分数比较后胜者 `+5`、败者 `-5`，再用 `PathModule.get_adjacent_empty(true_map, overlap_pos)` 推开败者；无空格保持重合，但分数照改。对战只允许 `GameState` 两座位有效坐标，坏坐标/墙上抛 `ValueError`；允许任意整数分数（含负）。同一回合中途经过对手**不得**调用；其他模块不得自行按重合坐标加减分。该函数不消耗宝藏/陷阱，不能递归触发第二次对战。

## 5. Simulator｜`src/treasure_game/simulator.py`

```python
def Simulator.__init__(self, initial_state: GameState, io: GameIO | None, rng: random.Random, policy_a: SeatPolicy | None = None, policy_b: SeatPolicy | None = None, max_turns: int = 500) -> None
def Simulator.run_human_vs_ai(self, game_mode: GameMode) -> GameResult
def Simulator.run_machine_vs_machine(self) -> GameResult
def Simulator.run_human_vs_human(self) -> GameResult
```

`GameIO` 为 `models.py` 中的协议，公开方法签名为 `render(self, view_map: ViewMap, score_a: int, score_b: int, dice_N: int, events: tuple[GameEvent, ...]) -> None` 和 `next_action(self, view_map: ViewMap, dice_N: int, seat: Seat) -> MoveSequence`；只接当前可见事件/分数/骰子，不把 `GameState` 或 `TrueMap` 交给人工界面。`VIEW_HEURISTIC` 是同样只读 `ViewMap` 的自动人工代理。`run_human_vs_ai(game_mode)` 强制 A=HUMAN、B=GREEDY（EASY）/OPT（NORMAL/HARD），以每轮的 `MapModule(state.true_map).get_view_map(game_mode)` 显示，人工无 IO 抛 `IllegalStateError`；`run_machine_vs_machine()` 强制两机，`None` 解析为 A=GREEDY、B=OPT，显式策略不得是 HUMAN；`run_human_vs_human()` 强制两人工按构造 mode（至少有 IO）。构造的完整对局初态必须 `turn=Seat.A`，无参入口按构造模式，三入口共享一个私有回合循环。每轮**先**以注入 RNG 掷均匀 1..6，取动作，按 Path 返回的新图/得分结算，再仅在终点调用 Battle，**然后**检查最后宝藏是否耗尽；初态空则不掷骰也不触发对战。到 `max_turns` 仍有宝藏返回 `draw_timeout=True`。每次运行从 `initial_state` 复制启动，事件/指标隔离；不在 AI 搜索内实际掷骰。`max_turns<=0`、不合法策略/状态抛 `ValueError`；人工 EOF/取消转换为受控中止而非假胜负。

## 6. DivideConquerModule｜`src/treasure_game/divide_conquer_module.py`

```python
def DivideConquerModule.solve(self, state: GameState, dice_N: int, solve_local: Callable[[GameState, int, frozenset[Position]], MoveSequence]) -> MoveSequence
```

职责：仅对 OPT 大图切块，选择局部关注块集合并合并候选；`solve_local` 是 `opt_ai.py` 的纯求解回调（第三参为**允许关注的资源位置**），本模块不得导入 `ai_module.py` 或改动地图规则。返回的完整方向序列必须由 `PathModule.sim_player_multi_step` 在**全图**执行校验；跨块路线通过全图 BFS 求出，不得把子图边界作墙；不可达块剔除，无候选返空。`state`/骰子不合法同 AI 异常；阈值与近似标志由 AiModule 管理；复杂度与精度口径见 [02](02-algorithm-design.md)。

## 视图-真值隔离的验收边界

| 调用 | 允许类型 | 强制方式 / 断言 |
| --- | --- | --- |
| `MapModule.get_view_map(mode)` | `TrueMap → ViewMap` | 实例只由受信任编排层构造，返回新冻结视图 |
| `PathModule.sim_player_multi_step(...)`、`AiModule.get_opt_move(...)`、`BattleModule.trigger_battle(...)` | 只接 `TrueMap`/`GameState` 真值 | 类型标注 + 入口 `isinstance` 守卫；把 `ViewMap` 传入必须报 `TypeError` |
| `GameIO.render(...)`、`GameIO.next_action(...)` | 只接 `ViewMap`、脱敏事件 | 注入协议与运行时守卫；HARD 的开局渲染/日志不含未触发负值 |
| 测试夹具 | 同一静态底图上同时构造两类地图 | 断言 HARD 隐藏而 AI 按真值避险；不能从显示对象反推未揭示陷阱 |

> 约定：以上为**设计契约**，本阶段没有创建这些 `.py` 文件。D1—D11 的推荐值记录在 [00](00-framework.md)，更改任一项需同步本契约、算法、测试和报告。
