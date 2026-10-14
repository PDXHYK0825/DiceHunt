# 对战地图寻宝策略分析平台

二维栅格地图上的寻宝对抗仿真平台：人工玩家与机器 AI 轮流投骰子（1–6，得到本回合最大**有效**移动步数 N），
输入方向序列移动，撞障碍物不消耗步数，踏入宝藏/陷阱立刻结算；**回合移动全部结束后**终点坐标重合才触发对战，
对战有胜负时，赢家留在原地，输家等概率随机传送到全图当前空白格（排除赢家位置、墙、宝藏和陷阱）；
平局或全图没有可用空白格时位置不变。地图宝藏被拾取完即终局。
新生成地图的出生点固定为 A 左上角 `(0,0)`、B 右下角 `(高-1,宽-1)`，两个格子预留为空地。
支持三种模式、三种对局形态，并附带批量随机地图仿真与算法对比。

随机生成或手动添加宝藏时，每枚宝藏独立抽取分值：**1–10 分各 9.9%，15 分和 20 分各 0.5%**。
分值生成后固定并显示在地图上，拾取时按该分值加分；保存/读取地图不会重新抽取，同 seed 可复现。

| 模式 | 机器 AI | 陷阱可见性（人工视角） |
| --- | --- | --- |
| 简单 EASY | 贪心 AI（BFS 性价比选宝，无博弈推演） | 完全可见 |
| 普通 NORMAL | OPT Expecti-Max（α-β 剪枝 + 记忆化 DP） | 完全可见 |
| 困难 HARD | OPT Expecti-Max（同上） | 初始隐藏（伪装成空地），踩中后永久暴露；机器始终读真值 |

规格来源：[`Project/docs/spec.md`](docs/spec.md)；接口冻结契约：[`Project/docs/06-interface-freeze.md`](docs/06-interface-freeze.md)。

## 1. 环境要求

| 项 | 值 |
| --- | --- |
| Python | 3.11+（本机验证版本 3.14.7） |
| 控制台版本依赖 | **零第三方依赖**（纯标准库，见 `requirements.txt`） |
| 测试 | pytest（仓库自带 `.pylibs/pytest` 9.1.1，无需全局安装） |
| 源码编码 | UTF-8；控制台输出中文 |
| 平台 | Windows / macOS / Linux 均可（`run_tests.cmd` 为 Windows 入口，其他平台用等价 pytest 命令） |

## 2. 目录结构

```
Project/
├─ docs/                      # spec、框架、接口契约、算法、测试计划、报告大纲
├─ src/treasure_game/         # 产品代码（包根 = Project/src）
│  ├─ __init__.py             # 包边界：零副作用、不导入子模块/gui
│  ├─ models.py               # 冻结数据模型、异常、GameIO 协议、冻结文案
│  ├─ path_module.py          # PathModule：唯一移动语义核 + BFS + 相邻空地
│  ├─ map_module.py           # MapModule：txt 读写、随机生成、视图生成
│  ├─ battle_module.py        # BattleModule：回合终点对战结算
│  ├─ greedy_ai.py / human_policy.py / opt_ai.py / divide_conquer_module.py / ai_module.py
│  ├─ simulator.py            # Simulator + SeatPolicy：回合驱动
│  ├─ console_io.py           # ConsoleIO：控制台表现层
│  ├─ __main__.py             # python -m treasure_game 入口
│  └─ gui.py                  # 选做 PyQt6 可视化（延迟导入）
├─ tests/                     # pytest 用例（conftest.py 注入 Project/src）
├─ experiments/run_batch.py   # 批量仿真与 CSV/JSON 导出
├─ logs/  reports/            # 生成的日志与报告（不入库）
├─ requirements.txt           # 运行时：空（纯标准库）
└─ requirements-dev.txt       # 测试：pytest>=8
```

## 3. 安装

```bat
REM 运行时：无需安装任何第三方包
python -m pip install -r Project\requirements.txt      REM 空操作

REM 开发/测试：本地环境想独立安装 pytest 时
python -m pip install -r Project\requirements-dev.txt
```

> 仓库根的 `.pylibs/` 已内置 pytest，`run_tests.cmd` 会自动注入，因此**通常不需要**执行上面第二条。

## 4. 运行

### 4.1 控制台主菜单（必做）

```bat
cd Project
set PYTHONPATH=src
python -m treasure_game
```

PowerShell：

```powershell
cd Project
$env:PYTHONPATH = "src"
python -m treasure_game
```

菜单逐字复刻 spec 第三节 A（`==== 寻宝博弈对战平台 ====` 七项功能）。人工输入 `w/a/s/d`（空格分隔）、`end` 结束本回合。

### 4.2 测试

在**仓库根**执行（脚本自动注入 `.pylibs`、`Project/src`、`Project`）：

```bat
run_tests.cmd                                  REM 全部用例
run_tests.cmd Project\tests\test_path.py       REM 单个文件
run_tests.cmd -k battle                        REM 关键字过滤
```

等价的手工命令（`Project/tests/conftest.py` 会自动把 `Project/src` 注入 `sys.path`，无需再管 `src`；
但本机 pytest 装在仓库自带 `.pylibs` 里，需要把它加进 `PYTHONPATH`，或自行安装 `pytest>=8`）：

```bat
set PYTHONPATH=%CD%\.pylibs
python -m pytest -q Project\tests\test_path.py
```

模块级导入自检：

```bat
python -c "import sys; sys.path.insert(0,'Project/src'); import treasure_game.models, treasure_game.path_module; print('import ok')"
```

### 4.3 批量实验与 GUI（选做）

```bat
python Project\experiments\run_batch.py        REM 固定种子矩阵 → experiments/results/*.csv|json
python -m treasure_game.gui                    REM 选做 PyQt6，需自行安装 PyQt6
```

GUI 人机对战中，人类每回合先点击骰子，骰面旋转后显示点数，再输入移动指令。
机器回合与机器对机器演示自动播放骰子动画；取消勾选动画可立即显示结果。

### 4.4 Web 前端

在当前 DiceHunt 项目目录双击 `run_web.cmd`，或在 PowerShell 中执行：

```powershell
$env:PYTHONPATH = "src"
python -B -m treasure_game.web
```

然后在浏览器打开 **http://127.0.0.1:8765**。可用 `--port 8766` 指定其他本地端口。
Web 版无需安装前端依赖或 PyQt6，复用现有 Python 游戏与 AI 模块。
支持参数化地图生成、点击格子编辑、txt 地图读取与保存、三种模式、人机/机机对战、
点击投骰与自动骰子旋转、逐格移动、事件弹窗（陷阱提示说明触发玩家）、分数与步数、
中止对局、记录与报告下载。人类投骰前隐藏点数，动画完成后才允许提交移动。
困难模式只向页面发送脱敏地图，未触发陷阱的坐标与分值不会出现在对局接口中。
本地服务共用一局状态，建议只用一个浏览器窗口操作；机器思考期间的中止在下一个回合边界生效。
在终端按 `Ctrl+C` 关闭服务。

## 5. txt 地图格式（D3 固化）

```
5 5                 <- 首行：height width（空格分隔）
0 0 0 0 0           <- 之后 height 行，每行 width 个整数
0 1 +1 0 0          <- 0 空地 / 1 障碍物 / +1 为1分宝藏 / >=2 宝藏分值 / <=-1 陷阱扣分
0 0 0 -2 0
0 0 0 0 2
0 0 0 0 0
A 0 0               <- 座位 A 出生点（零基 row col）
B 4 4               <- 座位 B 出生点
```

规则与校验：

* `1` 表示墙，`+1` 表示 1 分宝藏，`2` 及以上表示对应分值的宝藏，陷阱为负值；旧地图仍可读取；
* 两个出生点必须落在空地且互不相同；**所有非墙格必须与 A 连通**（因此 B 与全部宝藏可达）；
* 语法/结构非法抛 `MapFormatError`（`ValueError` 子类），文件不存在等 IO 问题透传 `OSError`；
* 加载后地图拆成"静态底图 + 资源原值 + 动态剩余/已揭示集合"两层；
  `save_txt` 只导出**初始布局**（对已游玩地图抛 `IllegalStateError`），残局回放由事件/状态快照承担。

## 6. 种子与可复现性

* **所有随机性都走注入的 `random.Random`**，禁止调用全局 `random`（ADR-08），因此同一 seed 必然复现同一局：
  * 地图生成：`MapModule.generate(h, w, walls, treasures, traps, rng=random.Random(seed))`，最多重试 1000 次；
  * 骰子：`Simulator(..., rng=random.Random(seed))` 内部 `rng.randint(1, 6)`；
  * AI 搜索中的随机（若有）使用同一个注入实例，不额外消耗骰子 RNG。
* **骰序可注入**：`Simulator(..., dice_seed_table=[...])` 时按
  `table[(turn_index * 2 + (0 for A else 1)) % len(table)]` 取骰值，搜索过程不消耗 RNG，
  保证"同骰序对照实验"（贪心 vs OPT、普通 vs 困难）变量可控。
* 先手与交替：**A 先行**，随后 A/B 严格交替；`turn_index` 从 0 起。
* 超时保护：单局默认 `max_turns=500`，超时则 `draw_timeout=True`、`winner=None`，不计正常胜负。
* 复现一局所需信息：`seed`、`mode`、两座位策略、`dice_sequence`（`GameResult.metrics["dice_sequence"]`）、
  初始地图 `map_id`。批量实验的 CSV 应至少记录这几列。
* `TrueMap.map_id` 是**静态内容指纹**（sha1 前 16 位，覆盖尺寸/底图/资源层，不含动态状态），整局恒定，
  适合作为"同一张地图"的稳定标识。

## 7. 冻结文案（唯一来源：`models.py` 常量 + `GameEvent` 工厂）

| 场景 | 文案 |
| --- | --- |
| 撞墙/越界 | `该方向为障碍物，无法通过，不消耗步数` |
| HARD 首次踩中隐藏陷阱 | `【踩到隐藏陷阱！扣除 {X} 分，陷阱已暴露】`（X = 扣分绝对值） |
| 拾取宝藏 | `拾取宝藏 +{v} 分` |
| 踩陷阱（非隐藏） | `踩到陷阱 {v} 分`（`v` 为陷阱原始负值，如 `-2`） |
| 对战 | `触发对战：{胜者} +{win_score} 分，{败者} -{win_score} 分`；平局 `触发对战：分数相同，平局` |
| 终局 | `游戏结束：宝藏已全部拾取` |

下游模块请直接调用 `GameEvent.blocked() / .treasure() / .trap() / .battle() / .battle_draw() / .game_over()`
或复用 `models` 的模板常量，不要各写一份字面量。

> 困难模式"隐藏陷阱"文案由**知道模式的一方**（`Simulator`/`ConsoleIO`）用
> `GameEvent.trap(..., hidden=True)` 生成——移动内核只认识地图，不感知游戏模式。

## 8. 公开 API 速查（`models.py` / `path_module.py`）

```python
from treasure_game.models import (
    GameMode, Direction, Position, TrueMap, ViewMap, Seat, GameState, GameEvent,
    MoveResult, BattleResult, SearchDiagnostics, GameResult, PublicStateView, GameIO,
    MapFormatError, MapGenerationError, IllegalStateError,
    BLOCKED_MOVE_MESSAGE, require_true_map, compute_map_id,
)
from treasure_game.path_module import PathModule
```

* `TrueMap.create(height, width, terrain, treasures, traps, *, remaining_treasures=None,
  active_traps=None, revealed_traps=None, map_id="")` —— 初始局面构造；省略 `map_id` 时自动算静态指纹。
* `TrueMap.from_grid(grid)` —— 从整数矩阵构造（0/1/≥2/≤-1），非矩形抛 `MapFormatError`；
  1 分宝藏用 `TrueMap.create(..., treasures={pos: 1})` 构造，txt 中使用 `+1`。
* `TrueMap` 约定：`terrain` 为 `tuple[tuple[int, ...], ...]`；`treasure_values`/`trap_values` 为
  `MappingProxyType`，键是 `Position`；三个集合为 `frozenset`；全部字段冻结不可变。
  构造期做廉价校验（形状、边界、分值符号、两层不重叠、剩余/激活集合为子集）。
* `require_true_map(obj)` —— 入口守卫，`ViewMap` 冒充真值抛 `TypeError`（视图-真值隔离）。
* `GameState.position_of(seat)` / `score_of(seat)` —— 座位归一化常用小工具。
* `MoveResult.events` 语义：每条**成功**移动产生一条 `MOVE`；同一步内若触发资源，紧跟 `TREASURE`/`TRAP`；
  撞墙产生 `BLOCKED`（`position` = 未移动的当前坐标，恒在界内，便于 UI 直接渲染）。
* `PathModule` 五个静态方法：

| 方法 | 语义 |
| --- | --- |
| `sim_player_multi_step(true_map, start_pos, move_seq, max_valid_step, actor=Seat.A) -> MoveResult` | 唯一移动内核：撞墙不耗步、资源立刻结算、走满 N 即停 |
| `get_adjacent_empty(true_map, origin) -> Position \| None` | 邻序 上/左/下/右 的第一个"底图 0 且从未布置资源"的格子 |
| `get_any_empty(true_map, exclude=()) -> Position \| None` | 从全图当前空白格中等概率随机选择落点，排除指定坐标 |
| `bfs_distances(true_map, start) -> dict[Position, int]` | 只以底图墙为障碍；起点 0；不可达**缺键** |
| `bfs_path(true_map, start, target) -> tuple[Direction, ...] \| None` | 同权最短路；`start==target` 返回 `()`；不可达 `None` |

`PathModule.sim_player_multi_step` 的返回字段与语义（下游 AI/仿真必须对齐）：

> `MoveResult(true_map, final_pos, score_delta, valid_steps, attempted_steps, events, blocked_steps)`：
> `true_map` 是结算资源后的**新** `TrueMap`（入参永不修改，静态 `map_id` 不变）；
> `final_pos` 是本回合停留坐标（全部指令被撞墙/越界挡下时等于起点）；
> `score_delta` 是宝藏 `+v` 与陷阱原负值的累加；
> `valid_steps` 是成功移动次数（`0 <= valid_steps <= max_valid_step`，达到上限立即停止并忽略剩余指令，`max_valid_step=0` 时一个指令都不处理）；
> `attempted_steps` 是实际处理过的指令数（含撞墙/越界失败的那些，`valid_steps <= attempted_steps <= len(move_seq)`）；
> `blocked_steps` 是失败次数；`events` 是按序的 `MOVE`/`BLOCKED`/`TREASURE`/`TRAP` 事件元组（`actor` = 传入座位）。
> 非法输入（`max_valid_step < 0`、起点越界/是墙、序列含非 `Direction`）抛 `ValueError`；传 `ViewMap` 抛 `TypeError`。
> **AI/分治内部推演必须调用这同一个内核**，预测的 `root_final_pos`/`root_score_delta` 必须等于把返回序列重放的结果（ADR-03）。

邻序遍历固定 **上、左、下、右**，决定 BFS 并列最短路的取舍。对战败方通过
`get_any_empty(true_map, exclude=(胜方位置,))` 从全图当前空白格中随机选择落点，不受邻序限制。

## 9. 关键不变量

1. **撞墙不耗步**：越界/墙 → 坐标、地图、分数、`valid_steps` 全不变，只追加 `BLOCKED` 事件。
2. **资源单次触发**：宝藏/陷阱首次踏入立即结算并移出动态集合；陷阱同时进入 `revealed_traps` **永久保留**，
   再踩不扣分（视图以 `spent_traps` 标记），困难模式据此还原"历史暴露"。
3. **真值图不可变**：任何操作返回新 `TrueMap`，内部为 `tuple`/`frozenset`/`MappingProxyType`。
4. **视图-真值隔离**：`ViewMap` 绝不进入 `PathModule`/`AiModule`/`BattleModule`；HARD 下未触发陷阱在视图里是 `0`。
5. **对战只在回合终点**：中途路过对手不触发；同回合只结算一次；拾取最后一枚宝藏的回合先做终点对战再判终局。
6. **唯一移动真相源**：全项目只有 `PathModule.sim_player_multi_step` 一份逐格结算（ADR-03）。

## 10. 测试索引（节选）

| 用例 | 位置 | 断言要点 |
| --- | --- | --- |
| spec 用例 4（撞障碍不耗步） | `tests/test_path.py::test_spec_case_4_blocked_attempt_does_not_spend_die` | 起点 (2,1)、墙 (2,3)、N=3、序列 右右上左 → 有效步 3 / 尝试 4 / 终点 (1,1) / 分数 0 / 第 2 条事件 `BLOCKED` 且文案逐字一致 |
| 越界与墙同规则 | `test_out_of_bounds_and_wall_share_rule_and_never_refill_steps` | 连续 5 次失败不耗步、不补步 |
| 走满 N 忽略剩余指令 | `test_stop_at_max_valid_step_ignores_remaining_commands`、`test_zero_max_valid_step_processes_nothing` | 长序列 == 截断序列；N=0 零处理 |
| 资源单次触发/陷阱永久暴露 | `test_resources_trigger_once_and_trap_stays_revealed` | `TREASURE`/`TRAP` 各一次，重复踏入不加分 |
| 真值图不可变 | `test_true_map_is_deeply_immutable_and_result_returns_new_object` | `MappingProxyType` 写入报错、入参内容不变 |
| BFS 与邻序 | `test_bfs_distances_...`、`test_bfs_path_shortest_with_frozen_neighbor_order` | 不可达缺键/返回 `None`、并列最短路按冻结邻序 |
| 败方随机传送 | `test_loser_b_teleported_to_random_empty_with_winner_unchanged` | 全图空白格均可被选中，赢家不动 |
| 性质测试 | `test_property_random_walks_match_reference_model` | 固定种子 300 局随机走位与独立参考模型逐项互证 |

完整测试计划见 [`Project/docs/04-test-plan.md`](docs/04-test-plan.md)。
