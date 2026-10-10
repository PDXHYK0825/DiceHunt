# 各波次完成状态

> 本文件由 Team Lead 维护，是"当前实际进度"的唯一权威快照（设计文档描述目标，本文件描述事实）。
> 最后更新：**全部波次完成**（Wave 0—6）；报告已交付 `reports/report.md`，所有数字经 Lead 独立审计（57 OK / 0 FAIL）。

## 总览

| 波次 | 内容 | 状态 | 证据 |
| --- | --- | --- | --- |
| 阶段一（设计） | Codex(gpt-6-sol, reasoning=high) 产出 6 份设计与计划文档 | ✅ DONE | `docs/00`—`docs/05`，交接摘要 `docs/_codex_handoff.md` |
| 契约冻结 | Lead 冻结接口契约（含 11 项待定决策裁决） | ✅ DONE | `docs/06-interface-freeze.md` |
| Wave 0 脚手架 | README/依赖清单/包边界 | ✅ DONE | `task-1`（core-dev） |
| Wave 1 核心 | `models.py` / `path_module.py` / `map_module.py` / `battle_module.py` | ✅ DONE | `task-1`、`task-2` |
| Wave 2 AI | `greedy_ai` / `human_policy` / `opt_ai` / `divide_conquer_module` / `ai_module` | ✅ DONE | `task-4`（ai-dev） |
| Wave 3 模拟器/IO | `simulator.py` / `console_io.py` / `__main__.py` + 黄金对局 | ✅ DONE | `task-5`（simulator-dev） |
| Wave 4 仿真实验 | 驱动 + v1/v2 各 270 局 × 多臂 + 分治消融 + 陷阱密度扫描 | ✅ DONE | `task-3`（experiments-dev）+ Lead 独立复核脚本 |
| Wave 5 GUI（选做） | PyQt6 可视化 | ✅ DONE | `task-6`（gui-dev），48 项 GUI 测试；Lead 加 `--gui` 入口转发 |
| Wave 6 报告 | `reports/report.md` | ✅ DONE | 9 章 + 3 附录；引用数字由 `tools/lead_audit_report.py` 全量回查原始 CSV |

## 测试基线（Lead 亲自执行，非队友自述）

| 时点 | 命令 | 结果 |
| --- | --- | --- |
| Wave 1 完成 | `python run_tests.py` | 172 passed, 2 skipped, exit 0 |
| Wave 2/3 完成 | `python run_tests.py` | 318 passed, 2 skipped, exit 0 |
| AI + 实验 + GUI 全部落地 | `python run_tests.py` | **374 passed, 2 skipped, exit 0** |
| 补完选做项 2（可配置参数） | `python run_tests.py` | **396 passed, 2 skipped, exit 0** |
| Lead 独立验收（含选做核查） | `python Project/tools/lead_acceptance.py` | **40 PASS / 0 FAIL** |
| Lead 重放保真度 | `python Project/tools/lead_replay.py verify` | 12/12 与实验 CSV 逐字段一致 |
| **Lead 报告数字审计** | `python Project/tools/lead_audit_report.py` | **57 OK / 0 FAIL, exit 0** |

## 选做加分项完成情况（spec 第六节 ✨）

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 1 GUI 可视化 | ✅ 完成 | 48 项 GUI 测试；offscreen 冒烟通过；AST 检查确认顶层零 PyQt 导入；PyQt6 缺失时受控降级 |
| 2 可配置参数 | ✅ 完成 | `--win-score` / `--dice-range` / `--depth` + `Simulator` 三个默认值参数；22 项专项测试 + 验收 8 项断言 |
| 3 日志保存与回放 | 🟡 部分 | 保存已实现并实测落盘；回放所需事件流与骰序齐备且经确定性重放验证，但无面向用户的回放命令 |
| 4 POMDP 双向未知 | ⛔ 未做 | 高难度扩展，已列入后续工作 |

## Lead 独立实测的性能数据（不是队友自述）

| 场景 | 配置 | 实测 |
| --- | --- | --- |
| 移动内核 | `sim_player_multi_step`（60×60, N=3） | ≈9 µs/次 |
| BFS | `bfs_distances`（60×60） | ≈3.7 ms/次 |
| OPT 决策 8×8 | depth=3, K=24 | 中位 61.9 ms，max 62.2 ms，nodes=642，hits=1317，misses=642，`algorithm=opt` |
| OPT 决策 12×12 | depth=3, K=24, threshold=144 | 中位 59.7 ms，max 60.4 ms，nodes=792，hits=305，misses=792，`algorithm=opt+partition`，`approximate=True` |

## 端到端跑通证据（Lead 亲自执行）

- `python -m treasure_game`（cwd=`Project`, `PYTHONPATH=src`）：七项主菜单逐字一致；EASY 人机对局 7 回合结束 `A=0 B=6 胜者=B`，骰序与性能统计正常输出，日志落盘 `Project/logs/`。
- HARD 模式人工踩到隐藏陷阱，控制台真实打印 `【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】`。
- 非法输入重提示、`end` 结束回合、`0` 退出均正常，进程 exit 0，无裸 traceback。

## 已知限制与说明（诚实记录）

1. `MapModule.generate` 在自由格占比 <62% 时因"打乱分配 + 1000 次重试"可能抛 `MapGenerationError`（D3 语义，非缺陷）；实验用小/中图固定低墙密度。
2. "OPT 最优"的准确口径是**给定深度与候选集内的最优 + 安全剪枝下与原树同值**，不是信息不完全博弈的全局最优。
3. 分治输出为近似解（`SearchDiagnostics.approximate=True`），存在误差；精度-速度权衡见实验数据。
4. 困难模式仅对**人工**隐藏陷阱，机器 AI 始终读取底层真值；双向未知需 POMDP，属选做。
5. GUI（Wave 5）为选做加分项，已实现并验证；PyQt6 缺失时受控回退，不阻塞必做验收。
6. **可见性实验的代理前提**：NORMAL vs HARD 的对照只有在人工代理会利用可见陷阱信息时才非空。陷阱盲代理（`GATHER`）下二者逐位相同；使用 `CAUTIOUS` 后差异显著。做此类消融必须先验证"代理具备使用被操纵信息的能力"。
7. **`CAUTIOUS` 的病理行为**：当陷阱挡住剩余宝藏的必经之路时，该确定性代理会拒绝踩陷阱而无限绕行，与 AI 在瓶颈格每回合相遇导致对战分数无界堆积（v2 NORMAL 2/90 局、超时 3/90 局、10×10 基线 7/90 局）。规则本身正确（`max_turns` 正常截断），但聚合均值会被污染，故报告以中位数 + 病态局占比为主口径。
8. **胜率口径**：主口径为 `wins / (wins + losses + ties)` 且**平局计 0**（与驱动 `winner_rates_note` 一致）；报告同时列出"平局计 0.5"的宽松口径，避免混用。
9. **α-β 剪枝在本问题近似无效**：机会节点恒返回精确期望使窗口无法收紧，开关剪枝节点数完全相同；真正的加速器是精确值 DP 缓存。属真实零结果，已如实记录。
