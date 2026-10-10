# 08｜GUI 可视化版本（spec 第三节 B 选做加分项）使用说明与验收对照

> Owner：`gui-dev`（任务 `task-6` / W5-01）。交付物：
> `Project/src/treasure_game/gui.py`、`Project/tests/test_gui.py`、本文件。
> 状态：**已完成**（PyQt6 6.11 / Qt 6.11.0 实测通过，offscreen 与真实显示两条路径均可用）。

---

## 0. 一句话说明

`treasure_game.gui` 是**选做加分项**：在控制台版本（必做）之外提供一个 PyQt6 图形界面，包含
地图编辑面板、参数化一键随机生成、简单/普通/困难三模式下拉、骰子投点与玩家逐格移动动画、
撞障碍物/踩陷阱/对战胜负弹窗、双方分数与剩余有效步数展示、对局报告导出。

**它绝不拖累必做项**：`gui.py` 顶层不 `import PyQt6`（延迟到首次使用），PyQt6 缺失时
`import treasure_game` / `python -m treasure_game` 完全照常工作，GUI 入口给出受控提示并返回退出码 2。

---

## 1. 运行方式

### 1.1 前置条件

| 项 | 值 |
| --- | --- |
| PyQt6 | 已装在 `<repo>\.pylibs`（实测 `PyQt6.QtCore.QT_VERSION_STR == 6.11.0`） |
| 包根 | `<repo>\Project\src`（`import treasure_game.gui`） |
| 运行依赖 | GUI 额外依赖 PyQt6；`gui.py` 的非 Qt 部分仍只用标准库 |

### 1.2 启动 GUI（真实显示）

仓库根执行：

```powershell
$env:PYTHONPATH = "E:\Comprehensive Experiment on Algorithm Design\.pylibs;E:\Comprehensive Experiment on Algorithm Design\Project\src"
python -m treasure_game.gui                 # 打开窗口（默认普通模式 + 8×8 随机地图）
python -m treasure_game.gui --mode HARD     # 直接以困难模式启动
python -m treasure_game.gui --load "<任意 txt 地图路径>"   # 启动即读取本地地图
python -m treasure_game.gui --no-animation  # 关闭动画（立即模式，适合远程桌面/录屏）
python -m treasure_game.gui --help          # 完整参数说明
```

也可以从 `Project/` 目录用 `PYTHONPATH=src;..\.pylibs` 运行；`gui.py` 末尾有
`if __name__ == "__main__": raise SystemExit(main())`，所以 `python -m treasure_game.gui` 与
`python Project/src/treasure_game/gui.py`（需自行注入包路径）等价。

### 1.3 无显示环境冒烟自检（验收用）

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -c "from treasure_game import gui; print(gui.is_available()); print(gui.main(['--smoke']))"
python -m treasure_game.gui --smoke --seed 11 --mode HARD --report "$env:TEMP\gui_smoke.txt"
```

`--smoke` 会：offscreen 构造 `QApplication` + 主窗口 → 按小地图参数（4×4 / 墙 3 / 宝藏 1 / 陷阱 1）
随机生成地图 → 用脚本化人工座位真实推进对局（含 `Simulator` + AI）→ 导出对局报告 → 打印一行摘要并
返回 0（`--report` 指定报告路径，缺省落到 `Project/reports/`）。真实输出示例：

```
True
GUI 冒烟通过：模式=NORMAL  回合数=1  比分 A=0 B=2  胜者=B  帧数=3  弹窗=3  报告=<repo>\Project\reports\gui_report_<时间戳>_NORMAL.txt
0
```

### 1.4 截图

界面在无字体目录的 offscreen 插件下会把文字画成占位方块（Qt 环境限制），因此仓库不内置 PNG。
需要截图时执行（会把 PNG 写到指定路径，**不改动仓库**）：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -c @"
from treasure_game import gui
from treasure_game.models import GameMode
app = gui.create_app()
w = gui.MainWindow(mode=GameMode.NORMAL); w.resize(1080, 720); w.show(); app.processEvents()
w.on_generate(seed=20260928); app.processEvents()
w.grab().save(r'<out>\gui.png')
"@
```

在真实桌面（`QT_QPA_PLATFORM` 不设置）运行 `python -m treasure_game.gui` 即为正常文字界面。
实测 offscreen 截图确认：三模式下拉、五个参数微调框、七个按钮、2×2 勾选框、彩色棋盘
（灰=墙、金=宝藏、红=陷阱、蓝=A、绿=B）、状态面板、输入行、日志面板均按预期排布；
同一张地图 NORMAL 与 HARD 的棋盘文本分别为：

```
NORMAL：A -2 · +3        HARD：A ·  · +3
        ·  ·  #  ·               ·  ·  #  ·
        ·  ·  ·  B               ·  ·  ·  B
```

（HARD 未触发陷阱伪装为空地，切换模式立即生效 —— 这就是"视图-真值隔离"的可视化证据。）

---

## 2. 界面与操作流程

### 2.1 布局

```
┌─ 地图编辑面板 / 参数与模式 ───────────────────────────────────────────────┐
│ 宽(列)[6] 高(行)[5] 墙[5] 宝藏[2] 陷阱[1]                                 │
│ 游戏模式 [普通模式（NORMAL）▼]（简单/普通/困难三项）                      │
│ [一键随机生成地图][读取txt地图][保存txt地图][开始对局(人机)][机器对机器演示][中止对局][导出对局报告] │
│ ☑动画（骰子投点 + 逐格移动）      ☑弹窗提示（撞墙/陷阱/对战）             │
└───────────────────────────────────────────────────────────────────────────┘
┌─ 地图视图（只显示当前模式的脱敏视图）───┐ ┌─ 对局状态 ─────────────────────┐
│  彩色棋盘：单击格子循环编辑             │ │ 第 N 回合｜轮到 A（人类）｜骰子 N = 3 │
│  空地 → 墙 → 宝藏 → 陷阱 → 空地        │ │ 比分：A（人类）= 2｜B（OPT 最优 AI）= 0 │
│  A/B 为玩家令牌，图例在下方             │ │ 本回合剩余有效步数：2 / 3        │
│                                        │ │ 上一回合：A（人类） 有效移动 1/3，剩余有效步数 2，撞障碍物 1 次 │
│                                        │ │ 剩余宝藏：1｜模式：普通模式      │
│                                        │ └───────────────────────────────┘
│                                        │ ┌─ 人工指令（w/a/s/d 空格分隔，end 结束回合）─┐
│                                        │ │ 提示行 + [输入框][执行指令][结束回合(end)] │
│                                        │ └───────────────────────────────┘
│                                        │ ┌─ 对局日志（冻结文案逐字输出）──┐
└────────────────────────────────────────┘ └───────────────────────────────┘
```

### 2.2 操作流程

1. **造地图**（三选一）：填写「宽/高/墙/宝藏/陷阱」后点「一键随机生成地图」（走
   `MapModule.generate`：参数校验 + BFS 连通校验 + 出生点不重合）；或「读取 txt 地图」；
   或在「地图视图」上**单击任意格子**循环编辑格值（出生点不允许改成障碍物，被拒绝时状态栏给出原因）。
2. **选模式**：下拉切换简单/普通/困难；切换后棋盘预览立刻按新模式脱敏（HARD 隐藏未触发陷阱）。
   AI 路由由 `Simulator` 依据模式决定：简单 → 贪心 AI，普通/困难 → OPT 最优 AI。
3. **开始对局**：点「开始对局（人机）」。轮到人工座位（A）时输入行启用，键入 `w/a/s/d`
   （空格分隔）后回车或点「执行指令」；`end`（或「结束回合」按钮）放弃剩余步数。
   非法输入就地提示并继续等待，不会崩。对局中地图编辑与「开始对局」按钮自动禁用。
4. **看动画与弹窗**：骰子标签滚动后定格在本回合的 N；玩家令牌按事件流逐格移动；
   撞障碍物 / 踩陷阱（含 HARD「【踩到隐藏陷阱！扣除 X 分，陷阱已暴露】」）/ 对战胜负 / 终局
   分别弹窗（非模态，不阻塞对局），同时写入日志面板。
5. **导出报告**：点「导出对局报告」，默认写到 `Project/reports/gui_report_<时间戳>_<模式>.txt`，
   也可在文件对话框里另选路径。
6. **机器对机器演示**：「机器对机器演示」按钮跑一局贪心 A VS OPT B（无需人工输入），
   用于演示动画与 AI 行为对比。

### 2.3 输入语义（与 `ConsoleIO.next_action` 逐条一致）

| 输入 | 行为 |
| --- | --- |
| `d d s` | 依次尝试右/右/下，只有成功踏入合法格才消耗有效步数 |
| `D W` | 大小写不敏感 |
| `d d end a w` | `end` 结束回合并**截断**其后输入 → 只执行 `d d` |
| 空输入 / `x` / `1 2 3` | 就地提示「非法输入：…」，**不结束等待**（可继续输入） |
| 步数多于骰子 N | 允许提交，提示"超出部分在完成 N 步后自动忽略"（由 `Simulator` 按规则忽略） |
| 对局中途关窗 / 点「中止对局」 | `request_human_action` 返回 `None` → `HumanAbortError` → 受控中止，**不产生假胜负** |

---

## 3. 与 spec 第三节 B 的逐条对照表

| spec 第三节 B 要求 | 实现位置（`Project/src/treasure_game/gui.py`） | 测试（`Project/tests/test_gui.py`） |
| --- | --- | --- |
| B1「地图编辑面板」 | `GameController.set_cell` / `cycle_cell`（`TrueMap.from_grid` 重建真值，出生点禁止改墙）；`MainWindow._on_cell_clicked` + `BoardWidget.cell_clicked`；预览走 `GameController.authoring_view()`（`ViewMap`） | `test_cell_edit_cycles_and_birth_point_wall_rejected`、`test_generate_from_form_matches_params` |
| B1「表单参数一键随机生成地图」 | `MainWindow.params()`（宽/高/墙/宝藏/陷阱 `QSpinBox`）+ `MainWindow.on_generate` → `GameController.generate_map` → `MapModule.generate` | `test_generate_from_form_matches_params`（格数与参数一致）、`test_generate_button_click_path`（真实点按钮） |
| B1「下拉选择游戏模式（简单/普通/困难）」 | `MainWindow.mode_combo`（3 项，`userData` = `EASY/NORMAL/HARD`）+ `_on_mode_changed` → `GameController.set_mode` | `test_main_window_constructs_offscreen`、`test_mode_switch_routes_simulator_ai`（参数化三模式）、`test_switch_to_hard_hides_traps_in_preview` |
| B2「动画：骰子投点」 | `MainWindow._build_animation_plan` 的 `DICE:*` 步骤 + `_play_animation`（`QTimer` 驱动嵌套事件循环，带安全超时）；`dice_rolls` 记录每次点数 | `test_animation_immediate_mode_is_synchronous`、`test_animation_timer_path_terminates` |
| B2「动画：玩家逐格移动」 | 以 `MOVE` 事件为步进源（不复制移动规则）：`_token_step` + `BoardWidget.set_tokens`；`SETTLE` 强制落到终态 | `test_animation_immediate_mode_is_synchronous`（`MOVE:A:0,1`）、`test_animation_timer_path_terminates` |
| B2「撞障碍物弹窗」 | `MainWindow._on_blocked(event)` → `_show_popup("BLOCKED", …)`，文案 = 06 §5 冻结文案逐字不加工 | `test_popup_methods_create_message_box`、`test_blocked_step_is_counted_without_consuming_steps` |
| B2「踩陷阱弹窗」 | `MainWindow._on_trap(event)`（`is_hidden_trap` 区分 HARD 隐藏陷阱标题） | `test_popup_methods_create_message_box`、`test_full_game_with_popups_enabled`、`test_hard_trap_reveal_end_to_end` |
| B2「对战胜负弹窗」 | `MainWindow._on_battle(result)` 同时接受 `GameEvent(BATTLE)` 与 `BattleResult` | `test_popup_methods_create_message_box` |
| B3「UI 展示：双方分数」 | `MainWindow.score_label`（`apply_frame` 用 `PublicStateView.score_a/score_b` 刷新） | `test_apply_frame_updates_labels`、`test_one_turn_updates_score_and_remaining_steps` |
| B3「UI 展示：本回合剩余有效步数」 | `MainWindow.steps_label`（帧起始 = 骰子 N）+ `last_turn_label`（按 `MOVE`/`BLOCKED` 事件流统计上一回合"有效移动 x/N、剩余 y、撞障碍物 z 次"） | 同上两条 + `test_blocked_step_is_counted_without_consuming_steps`（撞墙不耗步） |
| B3「导出对局报告」 | `MainWindow.on_export_report` → `GameController.export_report` → 纯函数 `build_report`（模式/双方分数/回合数/胜者/metrics/事件流），默认落 `Project/reports/` | `test_export_report_from_window`、`test_headless_controller_pipeline_and_report`、`test_main_smoke_entry_writes_report` |
| spec 第四节「表现层」 | `gui.py` 顶部 `GuiIO`（`GameIO` 实现）+ `GameController`（编排层）+ Qt 类（视图层）三层分工，与 `console_io.py` / `__main__.py` 一一对应 | `test_guiio_satisfies_gameio_protocol`、`test_guiio_rejects_truth_objects` |

---

## 4. 与 06 冻结契约的关系

| 约束（06 / 任务书） | 落实方式 |
| --- | --- |
| 顶层不得无条件 `import PyQt6` | 文件顶部只有标准库 + `treasure_game.*`；全部 Qt 类在 `_build_qt_classes()` 内定义，经 `qt_classes()` 缓存、`gui.MainWindow` 由 PEP 562 `__getattr__` 懒加载 | 
| PyQt6 缺失时包与控制台不受影响 | `is_available() -> bool`（`importlib.import_module` 失败即 False）；`main()` 返回 2 并打印提示；`python -m treasure_game` 完全独立 | 
| 视图-真值隔离 | `GuiIO.render` 强制 `isinstance(view, ViewMap)` / `PublicStateView`（真值对象在类型上就进不来）；`BoardWidget.set_frame` 同样只收 `ViewMap`；棋盘每个格子的文本/颜色只由 `view.cell_value(r, c)` 决定；`GameController` 是唯一持有 `TrueMap`/`GameState` 的编排层（与 `__main__._Session` 同职责） | 
| 复用而非重写 | 地图 = `MapModule.generate/load_txt/save_txt`；视图 = `MapModule.get_view_map`；对局 = `Simulator.run_human_vs_ai/run_machine_vs_machine`；AI = `AiModule`（`GameController._resolve_ai`）。GUI 不复制任何移动/对战/AI 逻辑，只把 `ViewMap`+`PublicStateView`+`GameEvent` 翻译成界面 | 
| 弹窗文案 | 全部直接使用 `GameEvent.message`（06 §5 冻结文案），GUI 不自行生成规则文案；HARD 隐藏陷阱文案由 `Simulator` 改写后原样展示 | 
| 无显示环境可测 | 测试顶部 `QT_QPA_PLATFORM=offscreen`（在导入 PyQt6 之前）；Qt 用例 `@requires_qt` 守卫；`animation_enabled=False`/`popups_enabled=False` 为立即模式；脚本化 `GuiIO(script=…, fallback=())` 让整局同步跑完，不进入事件循环 | 
| 不改他人文件 | 未改动 `simulator.py` / `console_io.py` / `__main__.py` / `models.py` / AI 模块 / 其他测试 | 

### 4.1 追加的公开 API（只追加，不改既有签名）

`gui.py` 属于 06 §1 表中"选做，延迟导入 PyQt6"的 `treasure_game/gui.py`，其内部公开对象为新增，
不影响任何冻结签名：

* 纯 Python（无 PyQt6 也可用）：`is_available`、`GuiUnavailableError`、`GuiIO`、`GameController`、
  `Frame`、`Popup`、`parse_action_text`、`cell_text`、`is_hidden_trap`、`build_report`、
  `default_report_path`、`create_app`、`create_window`、`qt_classes`、`main(argv=None, *, app=None) -> int`；
  常量 `MODE_ORDER` / `MODE_LABELS` / `DEFAULT_MAP_PARAMS` / `SMOKE_MAP_PARAMS` / `EDIT_CYCLE` /
  `REPORT_DIR` / `USAGE` / `HIDDEN_TRAP_PREFIX` / `CELL_WALL` / `CELL_EMPTY`。
* `GuiIO` 额外提供 `set_seat_labels`（与 `ConsoleIO.set_seat_labels` 同口径）与只读 `frames` /
  `requests` / `transcript` / `render_calls`，便于测试与报告导出。
* 未修改 `GameIO` 协议、`PublicStateView`、`GameEvent`、`Simulator` 的任何签名。

### 4.2 与冻结契约的偏离

**无。** 现有冻结签名/字段/文案一律未改；GUI 只读取公开结果字段
（`GameResult` 的 `mode/winner/score_a/score_b/turns/draw_timeout/metrics/events`）与
`ViewMap`/`PublicStateView`/`GameEvent`。报告里的 `map_id` 由编排层显式传入 `build_report`，
避免纯函数触碰真值对象。

---

## 5. 测试与验收（真实执行输出）

### 5.1 单文件验收

```powershell
$env:PYTHONPATH="E:\Comprehensive Experiment on Algorithm Design\.pylibs;E:\Comprehensive Experiment on Algorithm Design\Project\src"
python run_tests.py Project/tests/test_gui.py
```

```
...............................................+                         [100%]
48 passed in 0.71s          （exit 0）
```

无 PyQt6 路径（用环境开关复现：`TREASURE_GUI_TEST_FORCE_NO_QT=1` 会在导入 PyQt6 前装一个
`meta_path` 拦截器）：

```
......................ssssssssssssssssssssssss.s                         [100%]
23 passed, 25 skipped in 0.32s      （exit 0）
```

即：**PyQt6 缺失时所有 Qt 用例 skip，纯逻辑用例（`GuiIO`/`GameController`/报告/输入解析）照常通过，
退出码仍为 0。** 另有两条子进程用例与一条进程内 `sys.meta_path` monkeypatch 用例在不依赖环境开关的
情况下独立验证该契约（`test_is_available_false_when_pyqt6_blocked_inprocess`、
`test_import_and_console_survive_without_pyqt6_subprocess`、`test_console_entry_runs_without_pyqt6_on_path`）。

### 5.2 全量回归（不得破坏基线）

```powershell
python run_tests.py
```

```
373 passed, 2 skipped in 5.38s      （exit 0；GUI 交付时点的快照，连续两次一致）
```

* 我接手时的基线是 `318 passed, 2 skipped`；本次新增 **48** 条 GUI 用例，**原有用例 0 失败、
  跳过项仍为原有 2 条**（这 2 条是与 GUI 无关的既有跳过项）。
* 总数里其余增量为同期其他队友新增的用例（多队友并行写同一个 `Project/tests/`），因此
  "全量总数"会随队友进度变化；GUI 的稳定口径是 `test_gui.py` 的 **48 passed**。
* 我在交付窗口内观察到 `test_ai_module.py` 因队友正在写文件而出现一次瞬时 `NameError`
  （与 GUI 无关）；队友完成写入后 `test_ai_module.py` 单跑 34 passed、全量 373 passed 连续两次绿。

### 5.3 冒烟与入口

| 命令 | 结果 |
| --- | --- |
| `python -c "…; print(gui.main(['--smoke']))"`（offscreen） | `True` / `GUI 冒烟通过：模式=NORMAL 回合数=1 比分 A=0 B=2 胜者=B 帧数=3 弹窗=3 报告=…` / `0`，exit 0 |
| `python -m treasure_game.gui --smoke --seed 11 --mode HARD --report <tmp>` | `GUI 冒烟通过：模式=HARD 回合数=2 比分 A=0 B=2 胜者=B 帧数=5 弹窗=3`，exit 0 |
| `python -m treasure_game.gui --help` | 打印 `USAGE`，exit 0 |
| `PYTHONPATH=Project\src` + `python -m treasure_game --no-log`（**无 PyQt6**） | 主菜单正常打印，exit 0 |

### 5.4 测试覆盖清单（48 条 = 37 个用例：34 个普通 + 3 个参数化共 14 个实例）

| 分组 | 条数 | 覆盖点 |
| --- | --- | --- |
| 延迟导入/降级 | 5 | 源码级"顶层无 `import PyQt6`"、`is_available` 类型、进程内 `sys.meta_path` 屏蔽 PyQt6 后 `is_available()==False` 且包导入照常、子进程屏蔽 PyQt6 后包导入成功且 `main` 返回 2、无 PyQt6 时控制台入口 exit 0 |
| 纯逻辑·输入解析 | 12 | `parse_action_text` 五个正例（含 `end` 截断、大小写）、六个反例（空/非法字符/数字）、非法 `dice_N` |
| 纯逻辑·GuiIO | 3 | 满足 `GameIO` 协议、拒绝 `TrueMap`/非 `PublicStateView`/非法骰值、`log` 写入 transcript |
| 纯逻辑·Controller/报告 | 2 | 无 Qt 主链（读地图 → 脚本人工 → `Simulator` → 报告导出并校验字段）、无对局结果时报错 |
| 入口参数校验（无 Qt） | 1 | `--help`/`-h`/未知参数/非法 `--mode`/`--seed`/`--params` 的退出码 |
| 窗口构造 | 2 | offscreen 构造 + 模式下拉三项 + 输入区初始禁用 + 懒属性访问；`GuiIO.render` 驱动标签与棋盘 |
| 地图编辑 | 4 | 表单参数与格数/墙/宝/陷阱数量一致、真实点击按钮、单击循环编辑 + 出生点拒绝、txt 保存/读取闭环 |
| 模式与路由 | 4 | 三模式 `Simulator` → 贪心/OPT 路由（spy AI 计数）、切 HARD 隐藏陷阱 |
| 一回合标签 | 3 | `apply_frame` 标签、真实推进一回合（逐帧快照断言比分/剩余步数/上一回合统计）、撞墙不耗步计数 |
| 报告（窗口） | 2 | 导出含模式/比分/回合数/事件流、无对局时受控提示 |
| HARD 不泄露 | 2 | 开局棋盘与视图逐格一致且无负值、踩陷阱弹窗 + 暴露负值 |
| 弹窗 | 4 | `_on_blocked/_on_trap/_on_battle` 创建 `QMessageBox`（含 `BattleResult`）、关闭弹窗时只记录、整局开启弹窗、隐藏陷阱标题 |
| 动画 | 2 | 立即模式同步完成并落到终态、`QTimer` 路径必须终止 |
| 入口/交互 | 2 | `--smoke` 写报告、交互式嵌套事件循环（`QTimer` 模拟点击）+ 提交/结束回合回调 |

其中 **23 条不依赖 PyQt6**（延迟导入、输入解析、`GuiIO`、`Controller`、报告、参数校验），
因此在"没有 PyQt6"的环境里依然有实质覆盖，而不是全盘跳过。

---

## 6. 对局报告格式（`build_report` 输出样例）

```
===== 对战地图寻宝策略分析平台 · 对局报告（GUI）=====
生成时间：2026-09-27 18:49:43
模式：普通模式（NORMAL）
地图来源：随机生成 4x4（墙=3 宝藏=1 陷阱=1）
地图指纹：fe94c46a1176f160
座位：A = 人类｜B = OPT 最优 AI

【对局结果】
比分：A（人类）= -5    B（OPT 最优 AI）= 7
回合数：2
胜者：B

【对局统计】
触发对战：1 次
拾取宝藏：A=0 B=1
踩中陷阱：A=0 B=0
机器决策：2 次｜累计思考=18.490 ms｜平均=9.245 ms｜DP 缓存命中/未命中=685/313（命中率 68.6%）｜搜索节点=313｜分治近似=否
骰序：[3, 1, 4, 6]

【对局日志（事件流）】
0001 [BLOCKED][A] 该方向为障碍物，无法通过，不消耗步数
0002 [MOVE][B] 移动到 (2, 0)
0003 [MOVE][B] 移动到 (2, 1)
0004 [TREASURE][B] 拾取宝藏 +2 分
0005 [MOVE][B] 移动到 (2, 2)
0006 [MOVE][B] 移动到 (2, 3)
0007 [BATTLE][-] 触发对战：B +5 分，A -5 分
0008 [GAME_OVER][-] 游戏结束：宝藏已全部拾取

【界面日志（每帧公开状态快照）】
第 1 回合｜A｜骰子 N=3｜比分 A=0 B=0｜剩余宝藏=1｜模式=NORMAL
第 1 回合｜B｜骰子 N=1｜比分 A=0 B=0｜剩余宝藏=1｜模式=NORMAL
...
第 1 回合｜A｜骰子 N=6｜比分 A=-5 B=7｜剩余宝藏=0｜模式=NORMAL

===== 报告结束 =====
```

* **【对局日志（事件流）】** 来自 `GameResult.events`（权威事件序列，含 `kind` 与 `actor`）。
* **【界面日志】** 来自 `GuiIO.transcript`，是界面**每帧**看到的 `PublicStateView` 快照
  （回合/座位/骰子/比分/剩余宝藏/模式）——只含公开信息，天然不含未触发陷阱坐标。

---

## 7. 限制与已知问题

1. **动画需要真实事件循环/显示**：`QTimer` 帧与逐格移动在真实桌面才有视觉效果；offscreen 下
   文字会渲染成占位方块（Qt 找不到字体目录的告警），且不进入事件循环就直接以"立即模式"完成。
2. **弹窗为非模态**：`QMessageBox.show()`（不 `exec()`）保证对局不被阻塞、测试不会卡死；
   因此连续事件会累积多个弹窗（实现里最多保留最近 8 个并自动关闭更早的）。
3. **「中止对局」只在等待人工输入时生效**：机器思考（OPT 搜索同步执行）期间界面不响应；
   该按钮此时只给状态栏提示，不能强行打断 `Simulator`。
4. **动画期间机器思考会冻结界面**：`Simulator` 在本线程同步跑，AI 决策时 GUI 不刷新（与
   `__main__` 控制台一致）。大地图 + 深搜索会感到卡顿，可用 `--no-animation` 或 EASY 模式缓解。
5. **地图编辑是"授权期"操作**：仅在对局未开始时可编辑；编辑改的是**真值**（编辑面板按当前模式
   脱敏预览，因此 HARD 下看不见未触发陷阱，但编辑仍作用于真值）。
6. **未实现**（超出本次加分项范围）：对局日志回放（spec 六✨3）、可配置奖惩分数/骰子范围/搜索深度
   （spec 六✨2）、HARD 的 POMDP 双向未知（spec 六✨4）。参数化入口目前是"参数 → 随机地图"，
   未把 `treasure_value` / `trap_value` / `win_score` / OPT `depth` 暴露到界面。
7. **报告为纯文本**（`.txt`，UTF-8），未生成 CSV/JSON；批量实验的 CSV 由 W4 的
   `experiments/run_batch.py` 负责，两者互不依赖。
8. `Project/reports/` 是 GUI 冒烟与导出报告的默认落点（首次运行时自动创建）。

---

## 8. 给 Lead 的建议（未实施，需 Lead 决定）

* **`__main__.py` 增加 `--gui` 开关**：在控制台七项菜单之外，允许
  `python -m treasure_game --gui` 直接转发到 `treasure_game.gui.main`，无 PyQt6 时打印提示并回退菜单。
  本次**未修改** `__main__.py`（属 simulator-dev 的写范围），如需请由 Lead 指派。
  备选（零侵入）：在 `docs`/README 中并列给出 `python -m treasure_game` 与
  `python -m treasure_game.gui` 两条命令。
* 若后续要做"日志回放"，建议复用 `GuiIO.frames`（只含 `ViewMap` + `PublicStateView` + `GameEvent`，
  天然不含真值），即可在不泄露 HARD 隐藏陷阱的前提下重放整局。
