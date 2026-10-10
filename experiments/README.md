# W4-01 批量配对定量仿真实验驱动（`experiments/run_batch.py`）

Owner: **experiments-dev**（task-3）。写范围：`experiments/run_batch.py`、`experiments/README.md`、
`tests/test_experiments.py`、`experiments/results/.gitkeep`。

契约来源：[04-test-plan.md §批量定量实验](../docs/04-test-plan.md)、
[06-interface-freeze.md §3/§5.5](../docs/06-interface-freeze.md)、[spec.md 第六节](../docs/spec.md)。

---

## 1. 快速开始

在 `Project/` 目录下执行：

```bat
REM 12 行 smoke：2 地图 seed × 2 骰序 seed = 4 配对单元 × 3 模式
python experiments\run_batch.py --seed 42 --maps 2 --dice-seeds 2 --output experiments\results\smoke.csv

REM 主批次（默认 arm=main，12×12）：30 地图 seed × 3 骰序 seed = 90 配对单元 = 270 局
python experiments\run_batch.py --arm main --seed 42 --maps 30 --dice-seeds 3 ^
    --output experiments\results\batch_main_seed42_maps30_dice3_12x12.csv

REM 小图基线（arm=baseline，10×10，partition_enabled 恒 False = 单体 OPT）
python experiments\run_batch.py --arm baseline --seed 42 --maps 30 --dice-seeds 3 ^
    --output experiments\results\batch_baseline_seed42_maps30_dice3_10x10.csv
```

不写 `--output` 时按参数派生文件名（可反查 seed/地图/骰序/网格，避免覆盖历史结果）：

```
experiments/results/batch_<arm>_seed<S>_maps<M>_dice<D>_<H>x<W>_w<walls>t<treasures>p<traps>.csv
```

单测（仓库根，两种入口等价）：

```bat
python run_tests.py Project\tests\test_experiments.py
run_tests.cmd Project\tests\test_experiments.py
```

退出码：`0` 全部用例完成；`1` 有用例异常（异常类型保留在 JSON，不做静默丢弃）；
`2` CLI/配置非法；`4` `treasure_game` 运行模块尚未落地（阶段 1 直接跑 CLI 的预期结果）。

---

## 2. 实验设计（04 预注册口径 + Lead 裁决）

| 项 | 本驱动的实现 |
| --- | --- |
| 实验单位 | 配对单元 `(map_seed, dice_seed)`；每单元 EASY/NORMAL/HARD 各 1 局 |
| 样本量 | 主批次 30 地图 seed × 3 骰序 seed = 90 单元 = **270 局**；smoke = 2×2 = **12 局** |
| 地图臂 | `--arm main`（默认）= **12×12（144 格 = 分治阈值 144）**，NORMAL/HARD `partition_enabled=True`；`--arm baseline` = 10×10（100 格 < 阈值），三模式 `partition_enabled=False`，即单体 OPT 基线（同图同骰比较单体 vs 分治） |
| 墙/宝/陷 | 两组臂共用同一绝对数量 16 墙 / 8 宝 / 6 陷（只变网格尺寸，保证可比）；`walls+treasures+traps+2 ≤ 格数` 由配置校验强制 |
| 种子推导 | `map_seed = seed + i`、`dice_seed = seed + 100003 + 7*j`（两区间不相交；`--maps` 增大不改变已有种子，前缀稳定） |
| 对照与自变量 | A 座位 = 视图启发式人类代理（只读 `ViewMap`，`human_model_id=VIEW_HEURISTIC:GATHER:v1`）；B 座位 = EASY 贪心 / NORMAL、HARD 用 OPT。EASY vs NORMAL 隔离算法差异，NORMAL vs HARD 隔离可见性差异 |
| 控制条件 | 起始分数 0:0、A 先手、深度 3、K=24、分治阈值 144、`max_turns=500`、固定人类策略、固定种子清单 |
| RNG 独立性 | 地图 `random.Random(map_seed)`；骰子 `random.Random(dice_seed)`；人类策略 `random.Random(dice_seed)`；**搜索不消耗 RNG** |
| 骰序 | 每单元预生成 `2*max_turns` 个骰值，按 `(turn_index, seat)` 索引 = `turn_index*2 + (0=A, 1=B)`，取模读取；同单元三模式共用同一张表 |
| 同图保证 | 同一 `map_seed` 的地图在所有骰序单元与三模式下复用同一 `TrueMap`（同 `map_id`、同出生点）；若检测到同 `map_seed` 出现不同 `map_id`，记 `MapIdMismatch` 异常 |
| 分治开关 | `--partition auto`（默认）：EASY 恒 `False`（贪心不做搜索）；NORMAL/HARD 按 `地图格数 >= 阈值(144)`。可用 `--partition on/off` 强制 |

---

## 3. CSV schema（20 列，顺序冻结）

```
map_seed,dice_seed,map_id,mode,depth,K,partition_enabled,human_model_id,score_a,score_b,winner,draw_timeout,turns,trap_steps_a,trap_steps_b,battles,ai_calls,ai_elapsed_ns,cache_hits,cache_misses
```

| 列 | 语义 |
| --- | --- |
| `map_seed` / `dice_seed` | 配对单元键；地图与骰序由两个独立 RNG 生成 |
| `map_id` | `TrueMap.map_id`（静态内容指纹，不含动态状态） |
| `mode` | `EASY` / `NORMAL` / `HARD` |
| `depth` / `K` | OPT 搜索深度 / 候选上限（默认 3 / 24） |
| `partition_enabled` | 该局**是否满足启用分治的配置条件**（EASY 恒 `False`）；实际是否走分治路径见 JSON `per_game[].partition_used` 与 `checks.partition_used_matches_plan` |
| `human_model_id` | 固定人类代理标识，默认 `VIEW_HEURISTIC:GATHER:v1` |
| `score_a` / `score_b` | 终局分数（A=人类座位，B=机器座位） |
| `winner` | `A` / `B` / `DRAW`（`DRAW` = 非超时平局，即 `GameResult.winner is None`） |
| `draw_timeout` | `True` / `False`；`True` 时 `winner=DRAW` 且**不进正常胜率分母** |
| `turns` / `trap_steps_a` / `trap_steps_b` / `battles` | 回合数、分座位踩陷阱次数、触发对战次数 |
| `ai_calls` / `ai_elapsed_ns` | 机器决策次数 / 累计机器决策耗时（`perf_counter_ns`，HUMAN 座位不计） |
| `cache_hits` / `cache_misses` | DP 缓存命中/未命中 |

编码约定：UTF-8、`\n` 行尾、布尔写 `True/False`、**缺失值写空串**（尤其 `ai_elapsed_ns`，绝不填 0）。

---

## 4. JSON 摘要（同目录 `<stem>.summary.json`）

| 字段 | 内容 |
| --- | --- |
| `environment` | Python 版本/实现/可执行路径、平台、CPU 数、`pytest_required: false`（与 pytest 无关） |
| `source` | git commit/branch/dirty、`run_batch/map_gen/dice_table/stats` 版本串、各 `src/treasure_game/*.py` 的 `sha1[:16]` 指纹 |
| `config` | 全部控制条件（含 depth/K/阈值/地图参数/起手座位/骰子范围） |
| `seeds` | 根 seed、地图 seed 清单、骰序 seed 清单、两套 RNG 的独立性声明 |
| `dice_tables` | 每个骰序 seed 的长度、前 8 个值、`sha1[:16]`、索引规则 |
| `matrix` | 单元数、模式序、单元内共享项、控制条件、自变量说明、`partition_mode`、`map_arm`（本批网格/面积/阈值/各模式 partition 标志/角色）与 `arms`（**两组臂各自的网格、墙宝陷、阈值、partition 标志、复现命令、是否本批**） |
| `timing` | 墙钟、每局墙钟的分位数、地图生成累计耗时与次数、**全量 270 局预计耗时**（`estimated_full_scale_ns/human`，粗估 = 每局均值 × 270） |
| `output` | CSV/JSON 路径、派生文件命名规则、`overwrote_existing`（是否覆盖了已有文件）、是否显式 `--output` |
| `csv` | 列清单、行数、`winner`/布尔/缺失编码说明 |
| `counts` | 计划/实跑/异常/被停止组数、异常类型计数、整批墙钟 |
| `aggregate.per_mode` | 各模式：`games/normal_games/timeouts`、`wins_b/losses_b/ties`、`win_rate`、`win_rate_denominator`、`draw_rate`、`timeout_rate`、超时保守区间、`score_a/b_mean`、`score_diff_mean/std(n−1)/median`、`trap_steps_a/b_mean`、`treasure_steps_a/b_mean`、`battles_mean`、`ai_calls_mean`、`nodes_mean`、`cache_hit_rate`、耗时 (`ai_elapsed_ns_mean/median/p95`、`ai_ns_per_call_median/p95`、`wall_ns_median/p95`)、`ai_elapsed_ns_present/missing` |
| `aggregate.paired` | `NORMAL-EASY` 与 `HARD-NORMAL` 的单元级配对差值（`score_diff`、`trap_steps_a/b`、`battles`、`win_b`）+ **按地图 seed 分簇**的 95% bootstrap 区间（默认 2000 次重抽样，RNG 固定可复现） |
| `aggregate.timeouts` | 超时总数/分模式/比例 + 「不进正常胜率分母」标记 |
| `checks` | `dice_sequence_matches_table`、`dice_sequence_covers_turns`、`timed_out_consistent`、`partition_used_matches_config_condition`、`map_id_consistent` 的 ok/failed/unknown 计数 + `notes`（语义说明）；**`failed` 是「观测与预期不符」的诊断，不代表实验异常**——异常只计入 `errors` |
| `per_game` | 每局明细（含 `wall_ns` 每局总耗时、`score_diff`、`nodes`、`partition_used`、`checks`） |
| `errors` | 异常类型 + 消息 + 定位（`map_seed/dice_seed/mode`），含 `SkippedAfterGroupError` |

---

## 5. 统计口径（与 04 一致）

* **AI 胜率** `win_rate = wins/(wins+losses+ties)`，`ties` 为**非超时平局**，**平局胜率贡献 0**；
  `draw_timeout=True` 的行**不计入该分母**（分母为 0 时 `win_rate = null`，不假装是 0）。
  口径字符串同时写入 JSON `aggregate.winner_rates_note`。
* **超时保守区间**：`win_rate_timeouts_as_losses`（超时算负）与 `win_rate_timeouts_as_wins`（超时算胜）
  同时给出，供报告写「含超时的保守范围」。
* **得分差**：主因变量 `scoreOpt − scoreHuman = score_b − score_a`；样本标准差用 **n−1**（`n<2` 返回 `null`）。
* **耗时**：中位数与 p95（线性插值分位数）；`ai_ns_per_call_*` 为「每局单次决策平均耗时」的分位数；
  **缺失耗时不计入、也不填 0**。
* **配对分析**：单元级差值按 **地图 seed 分簇**重抽样（不把 270 局当独立地图），
  百分位区间 `[2.5%, 97.5%]`，随机源固定（`bootstrap_seed` 缺省 = `seed ^ 0x5EED`）。
* **异常**：保留异常类型与消息，**停止该配对单元**剩下的局（记为 `SkippedAfterGroupError`），
  不做静默丢弃、不自动剔除样本；`--fail-fast` 可改为整批立即停止。

---

## 6. 消融 / 分治与其它用法

```bat
REM 两组地图臂（Lead 裁决：主批次 12×12 + 基线 10×10）
python experiments\run_batch.py --arm main     --maps 30 --dice-seeds 3
python experiments\run_batch.py --arm baseline --maps 30 --dice-seeds 3

REM 同图同骰的分治 vs 单体对照（同为 12×12、同 map_seed/骰序 seed）：
REM   AiModule 的分治门槛是 area >= partition_threshold，
REM   故把阈值设成 9999 即在同图上关闭分治（CSV 的 partition_enabled 同步为 False）
python experiments\run_batch.py --arm main --partition-threshold 9999 --maps 30 --dice-seeds 3
REM 文件名自动区分（默认阈值 144 无后缀，非默认加 _thr9999 / _poff），不会互相覆盖

REM 阈值以上更大网格（14×14 = 196 格，仍走分治）
python experiments\run_batch.py --arm main --height 14 --width 14 --maps 3 --dice-seeds 1

REM 强制关闭分治（分治开关层面的显式关闭，文件名加 _poff）
python experiments\run_batch.py --maps 2 --dice-seeds 1 --partition off

REM 换人类代理策略（v2 可见性臂：CAUTIOUS 会利用视图上可见的负值格避让；
REM   GATHER 为 v1 默认策略，两者结果并存，文件名自动带 _cautious 后缀）
python experiments\run_batch.py --arm main     --maps 30 --dice-seeds 3 --human-strategy CAUTIOUS
python experiments\run_batch.py --arm baseline --maps 30 --dice-seeds 3 --human-strategy CAUTIOUS
python experiments\run_batch.py --arm main --partition-threshold 9999 --maps 30 --dice-seeds 3 ^
    --human-strategy CAUTIOUS

REM 陷阱数量剂量扫描（可见性机制的趋势证据：陷阱越多，NORMAL/HARD 差异应越大）
python experiments\run_batch.py --arm main --maps 10 --dice-seeds 3 --traps 2
python experiments\run_batch.py --arm main --maps 10 --dice-seeds 3 --traps 6
python experiments\run_batch.py --arm main --maps 10 --dice-seeds 3 --traps 12
```

> 同图同骰对照的两份 CSV 可直接按 `(map_seed, dice_seed, mode)` 配对求 `ΔU = U_partition − U_mono`
> （`U = score_b − score_a`）、胜率差与加速比（`ai_elapsed_ns` 或 `ai_calls` 归一化）。
> **EASY 行在两次运行中应当逐字一致**（贪心不经过 OPT/分治），这是一条很好的内部一致性自检。
> 逐步动作一致率不在本驱动的口径内：06 §5.5 冻结的 `metrics` 不导出动作序列，由 Lead 侧用
> `dice_seed` 确定性重放单独测量；本驱动可提供的是 `GameResult.events` 的 MOVE 轨迹
> （「已实现轨迹一致率 / 最长公共前缀占比」），以及 CSV 级别的分数/胜率/耗时对照。

常用参数：`--seed --maps --dice-seeds --arm {auto,main,baseline} --height --width --walls --treasures
--traps --depth --candidate-limit --partition-threshold --max-turns --human-strategy --human-model-id
--partition {auto,on,off} --bootstrap --bootstrap-seed --fail-fast --quiet --output --summary`。
`--height/--width` 显式给出时覆盖 `--arm` 预设；`--arm` 显式给出时 JSON 的 `config.arm` 以它为准
（否则按网格推导：12×12→main、10×10→baseline、其他→custom-<H>x<W>）。

---

## 7. 目录与依赖

```
experiments/
  run_batch.py          驱动（本文件说明的对象）
  README.md             本文件
  results/              运行时输出（smoke.csv / *.summary.json 等）
tests/
  test_experiments.py   单测：schema/矩阵/骰表/统计/截尾/异常 + 12 行 smoke 包装
```

* `run_batch.py` **只用标准库**，不导入 `pytest`；导入 `treasure_game` 是延迟且在
  `SimulatorCaseRunner` 内部完成的，阶段 1（模块未落地）也能完整跑矩阵/统计/写出与单测。
* 唯一接通点：`SimulatorCaseRunner.run_case`（构造 `GameState` → `AiModule` → `HumanPolicy` +
  `ViewHeuristicIO` → `Simulator.run_human_vs_ai`）；契约漂移只需改这一处。
* 座位 A 的 `GameIO` 适配器 `ViewHeuristicIO` 只接收 `ViewMap` 与 `PublicStateView`，
  按字段做鸭子类型识别并**显式排除任何持有 `true_map` 的对象**，不接触真图。
* 人类代理用 `HumanPolicy(strategy=..., rng=random.Random(dice_seed))` 构造：同一个配对单元的
  三模式拿到**同一条随机流**（若该策略含随机 tie-break 也保持配对与可复现）；若策略本身是确定性
  贪心（04 要求），该注入不影响结果。

### 运行注意

* 控制台输出全为 ASCII（避免重定向/代码页导致的乱码）；中文口径说明只出现在 UTF-8 的
  README 与 JSON 摘要里。
* `Simulator` 必须每回合**先** `GameIO.render(view, state_view, events)`、**再**
  `GameIO.next_action(seat, dice_N, view, state_view)`（06 §2 修订签名）；
  `HumanPolicy.choose` 需要 `PublicStateView`。`ViewHeuristicIO` **同时兼容**三种形态：
  新四参 `next_action(..., state_view)`、旧三参 `next_action(seat, dice_N, view)`
  （此时用 `render` 缓存的状态视图）、以及带关键字的调用；若两者都没有则抛
  `RuntimeError: Simulator must pass state_view to GameIO.next_action(...)` 并记入 JSON `errors`，
  不会静默产出垃圾数据。
* `SeatPolicy` 以 06 §3 为准，只有 `HUMAN/GREEDY/OPT/OPT_PARTITION` 四值；`VIEW_HEURISTIC` 是
  座位 A 的**策略名**（`human_model_id`），不是 `SeatPolicy` 取值——本驱动用
  `policy_a=SeatPolicy.HUMAN` + 注入 `ViewHeuristicIO` 实现。
* `--human-strategy` 原样传给 `treasure_game.human_policy.HumanPolicy(strategy=...)`（由该模块校验
  取值），并**自动派生** `human_model_id = VIEW_HEURISTIC:<strategy>:v1`（可用 `--human-model-id` 覆盖），
  派生文件名同时加 `_<strategy 小写>` 后缀，因此 `GATHER`（v1）与 `CAUTIOUS`（v2 可见性臂）的结果并存不覆盖。
* 已知局限（报告需写明）：A 座位是启发式视图代理而非真人；座位优势未对调（恒 A=人 / B=机）；
  「逐步动作一致率」由 Lead 侧确定性重放单独测量，不在本驱动口径内。
* 阶段 2 门禁：`treasure_game.{models,map_module,human_policy,ai_module,simulator}` 五个模块
  都存在且可导入时，`tests/test_experiments.py` 中的 `skipif` 守卫用例自动生效
  （冻结签名守卫 + 3 行真实 smoke）。适配层调用约定另有**假模块集成用例**（真实
  `models/map_module` + 严格按 06 §3 的假 AI/人工/Simulator），阶段 1 即可验证。
