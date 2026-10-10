# 06｜接口冻结（Lead 批准，实现期不得擅自更改）

> 本文件由 Team Lead 依据 [01 接口契约](01-module-contracts.md) 与 [00 框架](00-framework.md) 冻结，
> 用于多智能体并行实现时**防止接口漂移**。若 `01` 与本文件冲突，以本文件为准并立即向 Lead 报告。
> 修改本文件中的任何签名/字段 = 破坏性变更，必须先经 Lead 同意并同步所有在写队友。

## 0. 环境与测试入口（所有队友统一）

| 项 | 值 |
| --- | --- |
| Python | `C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\python.exe`（3.14.7，兼容 3.11+ 语法） |
| 包根 | `Project/src`（即 `import treasure_game.xxx`） |
| 运行控制台 | `python -m treasure_game`（cwd = `Project/`，且 `PYTHONPATH=src`） |
| 跑测试 | 仓库根执行 `run_tests.cmd`，等价于 `PYTHONPATH=<repo>\.pylibs;<repo>\Project\src; python -m pytest Project\tests -q` |
| pytest | 9.1.1，已装在 `<repo>\.pylibs`（禁止要求全局安装） |
| 运行时依赖 | 仅标准库；**禁止** `numpy`/`pytest` 进入 `treasure_game` 的运行时代码 |
| 编码 | 源码 UTF-8，控制台输出中文；日志文案与 spec 完全一致（见 §5） |

## 1. 冻结的模块路径与公开对象

| 文件 | 必须存在的公开对象 | Owner（任务） |
| --- | --- | --- |
| `src/treasure_game/__init__.py` | 包边界，可 `__all__`，无副作用、不导入 gui | A (W0-01) |
| `src/treasure_game/models.py` | 见 §2 全部类型 | A (W0-02) |
| `src/treasure_game/path_module.py` | `class PathModule` | A (W1-01) |
| `src/treasure_game/map_module.py` | `class MapModule` | B (W1-02) |
| `src/treasure_game/battle_module.py` | `class BattleModule` | B (W1-03) |
| `src/treasure_game/greedy_ai.py` | `class GreedyAI` | D (W2-01) |
| `src/treasure_game/human_policy.py` | `class HumanPolicy` | D (W2-02) |
| `src/treasure_game/opt_ai.py` | `class OptAI`、`class OptConfig` | D (W2-02) |
| `src/treasure_game/divide_conquer_module.py` | `class DivideConquerModule` | D (W2-03) |
| `src/treasure_game/ai_module.py` | `class AiModule` | D (W2-04) |
| `src/treasure_game/simulator.py` | `class Simulator`、`class SeatPolicy` | E (W3-01) |
| `src/treasure_game/console_io.py` | `class ConsoleIO` | E (W3-02) |
| `src/treasure_game/__main__.py` | `main(argv=None) -> int` | E (W3-02) |
| `src/treasure_game/gui.py` | 选做，延迟导入 PyQt6 | F (W5-01) |
| `experiments/run_batch.py` | `main(argv=None) -> int` | C (W4-01) |

> spec 第四节要求的"六模块"= `MapModule`/`PathModule`/`AiModule`/`BattleModule`/`Simulator`/`DivideConquerModule`，
> 名称必须逐字一致且保留这些类名；`greedy_ai.py`、`human_policy.py`、`opt_ai.py` 是 AI 模块的内部拆分。
> **`MapModule.generate` 必须是 `@staticmethod`**（spec 隐含"参数化随机生成"无需已有地图）。

## 2. `models.py` 冻结定义（Owner: A）

权威语义见 [00 数据模型表](00-framework.md#数据模型与不变量)。以下为**必须逐字一致**的字段名与顺序；
允许增加带默认值的可选字段（追加在末尾），不得重命名、不得改变语义。

```python
class GameMode(enum.Enum):
    EASY = "EASY"; NORMAL = "NORMAL"; HARD = "HARD"

class Direction(enum.Enum):
    UP = "w"; LEFT = "a"; DOWN = "s"; RIGHT = "d"
    @property
    def delta(self) -> tuple[int, int]        # UP=(-1,0) LEFT=(0,-1) DOWN=(1,0) RIGHT=(0,1)
    def __str__(self) -> str                  # "w"/"a"/"s"/"d"

@dataclasses.dataclass(frozen=True, slots=True)
class Position:
    row: int
    col: int
    def moved(self, direction: Direction) -> "Position"

@dataclasses.dataclass(frozen=True, slots=True)
class TrueMap:
    height: int
    width: int
    terrain: tuple[tuple[int, ...], ...]            # 0 空地 / 1 墙，只读矩形
    treasure_values: collections.abc.Mapping[Position, int]   # 静态原始布局，值 >= 2
    trap_values: collections.abc.Mapping[Position, int]       # 静态原始布局，值 <= -1
    remaining_treasures: frozenset[Position]
    active_traps: frozenset[Position]               # 尚未触发的陷阱
    revealed_traps: frozenset[Position]             # 曾经触发过的陷阱（永久保留）
    map_id: str                                     # 静态内容指纹（sha1[:16] of terrain+层），不含动态状态
    def in_bounds(self, pos: Position) -> bool
    def is_wall(self, pos: Position) -> bool
    def cell_value(self, pos: Position) -> int      # 动态视图值：墙=1、剩余宝藏=+v、已触发陷阱=-v（暴露用）、
                                                    # 未触发陷阱=-v、其他=0；仅供内部/真值使用
    def treasure_total(self) -> int
    def is_finished(self) -> bool                   # not remaining_treasures

@dataclasses.dataclass(frozen=True, slots=True)
class ViewMap:
    height: int
    width: int
    cells: tuple[tuple[int, ...], ...]              # 0/1/正宝藏/负陷阱（按模式脱敏）
    spent_traps: frozenset[Position]
    mode: GameMode
    def cell_value(self, row: int, col: int) -> int

class Seat(enum.Enum):
    A = "A"; B = "B"
    @property
    def opponent(self) -> "Seat"

@dataclasses.dataclass(frozen=True, slots=True)
class GameState:
    true_map: TrueMap
    pos_a: Position
    pos_b: Position
    score_a: int
    score_b: int
    turn: Seat
    mode: GameMode
    turn_index: int = 0

@dataclasses.dataclass(frozen=True, slots=True)
class GameEvent:
    kind: str          # MOVE / BLOCKED / TREASURE / TRAP / BATTLE / GAME_OVER / INFO
    actor: Seat | None
    position: Position | None
    score_delta: int
    message: str

@dataclasses.dataclass(frozen=True, slots=True)
class MoveResult:
    true_map: TrueMap
    final_pos: Position
    score_delta: int
    valid_steps: int
    attempted_steps: int
    events: tuple[GameEvent, ...]
    blocked_steps: int = 0

@dataclasses.dataclass(frozen=True, slots=True)
class BattleResult:
    triggered: bool
    pos_a: Position
    pos_b: Position
    score_a: int
    score_b: int
    events: tuple[GameEvent, ...]

@dataclasses.dataclass(frozen=True, slots=True)
class SearchDiagnostics:
    algorithm: str            # 取值："greedy" / "opt" / "opt+partition"
    nodes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    elapsed_ns: int = 0
    root_move_seq: tuple[Direction, ...] = ()
    root_final_pos: Position | None = None
    root_score_delta: int = 0
    approximate: bool = False
    depth: int = 0

@dataclasses.dataclass(frozen=True, slots=True)
class GameResult:
    mode: GameMode
    final_state: GameState
    winner: str | None          # "A" / "B" / None（平局或超时）
    score_a: int
    score_b: int
    turns: int
    events: tuple[GameEvent, ...]
    draw_timeout: bool = False
    metrics: collections.abc.Mapping[str, object] = dataclasses.field(default_factory=dict)

class MapFormatError(ValueError): ...
class MapGenerationError(ValueError): ...
class IllegalStateError(ValueError): ...

class GameIO(typing.Protocol):
    def render(self, view: ViewMap, state_view: "PublicStateView", events: tuple[GameEvent, ...]) -> None: ...
    def next_action(self, seat: Seat, dice_N: int, view: ViewMap,
                    state_view: "PublicStateView") -> tuple[Direction, ...]: ...
    def log(self, message: str) -> None: ...
```

为仿真器/IO 再定义（同样冻结）：

```python
@dataclasses.dataclass(frozen=True, slots=True)
class PublicStateView:
    """人工界面唯一可见信息：绝不含 TrueMap、active_traps 未揭示坐标。"""
    mode: GameMode
    pos_a: Position
    pos_b: Position
    score_a: int
    score_b: int
    turn: Seat
    turn_index: int
    dice_N: int
    remaining_treasure_count: int
```

`SeatPolicy`（定义在 `simulator.py`，不由 models 定义）：`HUMAN` / `GREEDY` / `OPT` / `OPT_PARTITION` 四值枚举。
> ⚠ 与 `01-module-contracts.md` 的差异处理（Lead 裁决 2026-09-28）：`01` 中出现的 `VIEW_HEURISTIC` **不是** `SeatPolicy` 取值，
> 而是"座位 A 的视图启发式人类代理策略名"（`human_model_id`），由 `experiments/run_batch.py` 侧用
> `policy_a=SeatPolicy.HUMAN` + 注入视图代理 `GameIO` 实现。`SeatPolicy` 只有上列四值。

## 3. 冻结的公开签名

```python
# path_module.py
class PathModule:
    @staticmethod
    def sim_player_multi_step(true_map: TrueMap, start_pos: Position, move_seq: tuple[Direction, ...],
                              max_valid_step: int, actor: Seat = Seat.A) -> MoveResult
    @staticmethod
    def get_adjacent_empty(true_map: TrueMap, origin: Position) -> Position | None
    @staticmethod
    def bfs_distances(true_map: TrueMap, start: Position) -> dict[Position, int]
    @staticmethod
    def bfs_path(true_map: TrueMap, start: Position, target: Position) -> tuple[Direction, ...] | None

# map_module.py
class MapModule:
    def __init__(self, true_map: TrueMap) -> None
    def get_view_map(self, mode: GameMode) -> ViewMap
    @staticmethod
    def load_txt(path: pathlib.Path) -> tuple[TrueMap, Position, Position]      # (true_map, pos_a, pos_b)
    @staticmethod
    def save_txt(path: pathlib.Path, true_map: TrueMap, pos_a: Position, pos_b: Position) -> None
    @staticmethod
    def generate(height: int, width: int, walls: int, treasures: int, traps: int,
                 rng: random.Random, treasure_value: int = 2, trap_value: int = -2,
                 max_attempts: int = 1000) -> tuple[TrueMap, Position, Position]

# battle_module.py
class BattleModule:
    @staticmethod
    def trigger_battle(true_map: TrueMap, pos_a: Position, pos_b: Position,
                       score_a: int, score_b: int, actor_a: Seat = Seat.A,
                       actor_b: Seat = Seat.B, win_score: int = 5) -> BattleResult

# greedy_ai.py
class GreedyAI:
    def decide(self, state: GameState, dice_N: int, seat: Seat = Seat.B) -> tuple[Direction, ...]

# human_policy.py
class HumanPolicy:
    """只接受 ViewMap + PublicStateView，禁止接收 TrueMap/GameState。"""
    def __init__(self, strategy: str = "GATHER", rng: random.Random | None = None) -> None
    def candidate_actions(self, view: ViewMap, seat: Seat, dice_N: int, public: PublicStateView
                          ) -> list[tuple[Direction, ...]]
    def choose(self, view: ViewMap, seat: Seat, dice_N: int, public: PublicStateView) -> tuple[Direction, ...]

# opt_ai.py
class OptConfig:
    depth: int = 3; candidate_limit: int = 24; use_cache: bool = True; use_pruning: bool = True
class OptAI:
    def __init__(self, config: OptConfig | None = None) -> None
    def decide(self, state: GameState, dice_N: int, seat: Seat = Seat.B) -> tuple[Direction, ...]
    def diagnostics(self) -> SearchDiagnostics

# divide_conquer_module.py
class DivideConquerModule:
    def __init__(self, partition_threshold: int = 144, block_rows: int = 4, block_cols: int = 4) -> None
    def solve(self, state: GameState, dice_N: int, seat: Seat,
              solve_local: collections.abc.Callable[..., tuple[Direction, ...]]) -> tuple[Direction, ...]

# ai_module.py
class AiModule:
    def __init__(self, depth: int = 3, candidate_limit: int = 24, partition_threshold: int = 144,
                 use_cache: bool = True, use_pruning: bool = True, rng: random.Random | None = None) -> None
    def get_greedy_move(self, state: GameState, dice_N: int, seat: Seat = Seat.B) -> tuple[Direction, ...]
    def get_opt_move(self, state: GameState, dice_N: int, seat: Seat = Seat.B) -> tuple[Direction, ...]
    def get_diagnostics(self) -> SearchDiagnostics

# simulator.py
class SeatPolicy(enum.Enum):
    HUMAN = "HUMAN"; GREEDY = "GREEDY"; OPT = "OPT"; OPT_PARTITION = "OPT_PARTITION"
class Simulator:
    def __init__(self, initial_state: GameState, io: GameIO | None = None,
                 rng: random.Random | None = None,
                 policy_a: SeatPolicy = SeatPolicy.HUMAN, policy_b: SeatPolicy = SeatPolicy.OPT,
                 max_turns: int = 500, ai: AiModule | None = None,
                 dice_seed_table: collections.abc.Sequence[int] | None = None) -> None
    def run_human_vs_ai(self, game_mode: GameMode) -> GameResult
    def run_machine_vs_machine(self) -> GameResult
    def run_human_vs_human(self) -> GameResult
```

`dice_seed_table`：可选注入的预生成骰值表（W4 实验用于"骰序按 `(turn,seat)` 索引、搜索不消耗 RNG"）；
若提供则按 `turn_index*2 + (0 for A else 1)` 取模索引，缺省用 `rng.randint(1,6)`。

## 4. 语义不变量（会被测试直接断言）

1. **撞墙不耗步**：命中越界或墙 → 坐标/图/分数不变、`valid_steps` 不增、产生 `BLOCKED` 事件（`actor` = 当前座位）。
2. **资源单次触发**：宝藏/陷阱在首次踏入时立即结算并从 `remaining_treasures` / `active_traps` 移除；
   陷阱同时加入 `revealed_traps` 且**永久保留**（再踩不扣分、`spent_traps` 标记）。
3. **`TrueMap` 不可变**：任何操作返回新 `TrueMap`，入参对象内容不变（`frozenset`/`tuple`/`MappingProxyType`）。
4. **步数语义**：`0 <= valid_steps <= max_valid_step`、`valid_steps <= attempted_steps`；成功走满 N 后忽略剩余指令。
5. **唯一移动真相源**：AI/分治内部推演**必须**调用 `PathModule.sim_player_multi_step`（或其等价纯内核），
   不得另写第二份逐格结算。AI 预测的 `root_final_pos`/`root_score_delta` 必须等于把返回序列交给 Path 重放的结果。
6. **视图-真值隔离**：`ViewMap` 绝不传入 `PathModule`/`AiModule`/`BattleModule`；HARD 下未触发陷阱视图为 `0`，
   已触发陷阱显示原始负值；AI 永远读 `TrueMap`。
7. **对战只在回合终点**：`Simulator` 每回合移动全部结束后调用一次 `BattleModule.trigger_battle`；
   途中经过对手不触发；同回合不得重复结算。
8. **对战结算**：`pos_a != pos_b` → `triggered=False` 且分数位置全不变；相等且同分 → 平局不推开；
   分数不等 → 胜方 `+win_score`、败方 `-win_score`，败方推往上/左/下/右第一个"底图 0 且从未布置资源"的空格，
   无候选则原地不动但分数照改。
9. **终局**：`remaining_treasures` 为空即终局；拾取最后一枚宝藏的回合**先做终点对战再判定终局**；
   `turn_index >= max_turns` 仍有宝藏 → `draw_timeout=True` 且 `winner=None`。
10. **座位规范化**：机机对局中当前 AI 在 A 座位时，搜索内部以"当前 AI = MAX/OPT、对手 = MIN/HUMAN"规范化，
    输出动作再映射回真实座位；不得交换 `TrueMap` 中的资源归属。

## 5. 必须逐字一致的文案

| 场景 | 文案（`GameEvent.message` 或日志输出） |
| --- | --- |
| 撞墙/越界 | `该方向为障碍物，无法通过，不消耗步数` |
| HARD 踩到隐藏陷阱 | `【踩到隐藏陷阱！扣除 {X} 分，陷阱已暴露】`（X = 扣分数绝对值） |
| 拾取宝藏 | `拾取宝藏 +{v} 分` |
| 踩陷阱（非隐藏） | `踩到陷阱 {v} 分`（`{v}` = 陷阱**原始负值**，例如 `踩到陷阱 -2 分`） |
| 对战 | `触发对战：{胜者} +5 分，{败者} -5 分` / 平局 `触发对战：分数相同，平局` |
| 终局 | `游戏结束：宝藏已全部拾取` |

**隐藏陷阱文案的产生责任（Lead 裁决）**：`PathModule.sim_player_multi_step` 是**模式无关**的纯内核，
它只产生通用 `TRAP` 事件（`踩到陷阱 -2 分`）。HARD 模式下"人类首次踩中尚未揭示的陷阱"额外需要的
`【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】` 提示，由 **Simulator/ConsoleIO** 依据
"`mode == HARD` 且该陷阱在此次移动前不在 `revealed_traps` 中"判定后，追加一条 `kind="TRAP"`、
`GameEvent.trap(..., hidden=True)` 生成的事件（或等价地改写该条事件文案）。判定所需信息可从
`MoveResult.events` 中 `TRAP` 事件的位置与该次移动**之前**的 `TrueMap.revealed_traps` 推出。
交付要求：HARD 真人脚下踩中隐藏陷阱时，控制台必须打印该提示；且提示中不得泄露其他未触发陷阱坐标。

主菜单必须**逐字**复刻 spec 第三节 A 的七项（含 `==== 寻宝博弈对战平台 ====` 与 `请选择功能：`）。

## 5.5 `GameResult.metrics` 冻结键（W4 实验直接依赖）

`Simulator` 必须填充以下键（`object` 值类型见括号），缺键 = 实验任务直接失败：

| 键 | 类型 | 语义 |
| --- | --- | --- |
| `battles` | `int` | 本局触发对战的次数（含平局） |
| `trap_steps_a` / `trap_steps_b` | `int` | 各座位踩中陷阱的次数（含重复踩已暴露陷阱，按"踏入且扣分"计） |
| `treasure_steps_a` / `treasure_steps_b` | `int` | 各座位拾取宝藏次数 |
| `ai_calls` | `int` | 机器决策调用次数（HUMAN 座位不计） |
| `ai_elapsed_ns` | `int` | 机器决策累计耗时（`time.perf_counter_ns`，仅机器座位） |
| `cache_hits` / `cache_misses` | `int` | 本局累计 DP 缓存命中/未命中 |
| `nodes` | `int` | 本局累计搜索节点数 |
| `partition_used` | `bool` | 是否走了分治近似路径 |
| `dice_sequence` | `list[int]` | 实际使用的骰值序列（按 `(turn_index, seat)` 顺序），供复现 |
| `timed_out` | `bool` | 与 `draw_timeout` 一致（冗余但便于 CSV） |

## 6. 冲突处理

- 发现本文件与 `01`/`02` 冲突 → 立即 `send_message` 给 Lead，不要自行改契约。
- 需要新增公开 API 时，只能**追加**（不改既有签名/字段），并在自己的任务完成说明里写清。
- 队友之间接口调用一律按本文件；若对端文件尚未落地，可先按本文件编写并在对端落地后重跑测试。
