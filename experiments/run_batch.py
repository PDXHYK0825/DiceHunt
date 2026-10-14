#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""W4-01 批量配对定量仿真实验驱动（Owner: experiments-dev）。

契约来源
--------
* ``Project/docs/04-test-plan.md`` §批量定量实验（实验单位/对照/控制条件/主因变量/截尾/配对分析/schema）
* ``Project/docs/06-interface-freeze.md`` §3 冻结签名、§5.5 ``GameResult.metrics`` 冻结键
* ``Project/docs/spec.md`` 第六节「批量随机地图仿真」

设计要点
--------
1. **配对单元** = ``(map_seed, dice_seed)``；同一单元内 EASY/NORMAL/HARD 三模式共用同一张地图
   （同 ``map_id``、同出生点）与同一张预生成骰值表。
2. **RNG 独立**：地图用 ``random.Random(map_seed)``，骰子用 ``random.Random(dice_seed)``，
   两者互不影响；骰值表预生成后按 ``(turn_index, seat)`` 索引（``turn_index*2 + 0/1``），
   搜索过程不消耗任何 RNG。
3. **单局上限** ``max_turns=500``；超时行 ``draw_timeout=True``、``winner`` 记为 ``DRAW``，
   **不计入正常胜率分母**。
4. **唯一接通点**：``SimulatorCaseRunner.run_case``（把 ``CaseSpec`` 接到冻结的
   ``MapModule/AiModule/HumanPolicy/Simulator``）。阶段 1 的矩阵/骰表/统计/写出与
   单测均通过注入的假 runner 验证，不依赖 ``simulator.py`` 落地。
5. 本文件只写 ``Project/experiments/results/``、以及调用方通过 ``--output`` 指定的路径。

CLI（cwd = ``Project/``）::

    python experiments/run_batch.py --seed 42 --maps 2 --dice-seeds 2 \\
        --output experiments/results/smoke.csv          # 4 配对单元 × 3 模式 = 12 行
    python experiments/run_batch.py --arm main --seed 42 --maps 30 --dice-seeds 3 \\
        --output experiments/results/batch_main.csv     # 90 单元 × 3 = 270 行（默认 arm=main，12x12）
    python experiments/run_batch.py --arm baseline --seed 42 --maps 30 --dice-seeds 3 \\
        --output experiments/results/batch_baseline.csv # 10x10 单体 OPT 基线（partition_enabled 恒 False）

Lead 裁决（2026-09-28）：主批次地图 **12×12（144 格 = 分治阈值）**，
NORMAL/HARD 的 `partition_enabled=True`；可选 10×10 基线 `--arm baseline`（100 格，`partition_enabled=False`）
用于同图同骰的单体 vs 分治对照。`SeatPolicy` 只有 `HUMAN/GREEDY/OPT/OPT_PARTITION` 四值
（`VIEW_HEURISTIC` 是座位 A 的视图代理策略名，不是 SeatPolicy 取值）。

退出码：0 = 全部用例完成；1 = 有用例异常（异常类型保留在 JSON，不做静默丢弃）；
2 = CLI/配置非法；4 = ``treasure_game`` 运行模块尚未落地（阶段 1 直接跑 CLI 时的预期结果）。
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import importlib
import importlib.util
import json
import math
import os
import pathlib
import platform
import random
import subprocess
import sys
import time
import types
import typing

__all__ = [
    "CSV_COLUMNS",
    "MODES",
    "METRIC_KEYS_REQUIRED",
    "WIN_RATE_CONVENTION",
    "BatchConfig",
    "CaseSpec",
    "PairedUnit",
    "CaseRecord",
    "BatchOutcome",
    "BatchConfigError",
    "MetricContractError",
    "RunnerUnavailable",
    "ViewHeuristicIO",
    "SimulatorCaseRunner",
    "build_case_matrix",
    "derive_dice_seeds",
    "derive_map_seeds",
    "make_dice_table",
    "dice_index",
    "dice_at",
    "partition_enabled_for",
    "mean",
    "median",
    "sample_std",
    "percentile",
    "bootstrap_cluster_ci",
    "aggregate_records",
    "paired_analysis",
    "run_batch",
    "write_csv",
    "write_summary",
    "build_parser",
    "main",
    "simulator_ready",
]

# --------------------------------------------------------------------------------------
# 路径引导：允许 `python experiments/run_batch.py`（cwd=Project/）与 `import experiments.run_batch`
# --------------------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve()
PROJECT_ROOT = _HERE.parent.parent                 # <repo>/Project
REPO_ROOT = PROJECT_ROOT.parent                    # <repo>
SRC_DIR = PROJECT_ROOT / "src"                     # 包根：import treasure_game.xxx
for _p in (str(SRC_DIR), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --------------------------------------------------------------------------------------
# 冻结常量
# --------------------------------------------------------------------------------------
RUN_BATCH_VERSION = "w4-01/1.0"
DICE_TABLE_VERSION = "dice-table/v1 (randint(1,6), index=turn_index*2+A?0:1)"
MAP_GEN_VERSION = "map-module.generate/v2 (treasure=1-10:9.9% each,15/20:0.5% each; trap_value=-2)"
STATS_VERSION = "stats/v1 (mean | sample-std n-1 | linear percentile | cluster bootstrap by map_seed)"

#: 严格等于 04-test-plan「记录 schema」的 20 列，顺序不可变。
CSV_COLUMNS: tuple[str, ...] = (
    "map_seed",
    "dice_seed",
    "map_id",
    "mode",
    "depth",
    "K",
    "partition_enabled",
    "human_model_id",
    "score_a",
    "score_b",
    "winner",
    "draw_timeout",
    "turns",
    "trap_steps_a",
    "trap_steps_b",
    "battles",
    "ai_calls",
    "ai_elapsed_ns",
    "cache_hits",
    "cache_misses",
)

MODES: tuple[str, ...] = ("EASY", "NORMAL", "HARD")

#: 模式 → 座位 B（机器）策略；EASY=贪心完全可见，NORMAL/HARD=OPT（HARD 仅视图层隐藏陷阱）。
MODE_MACHINE_POLICY: dict[str, str] = {"EASY": "GREEDY", "NORMAL": "OPT", "HARD": "OPT"}

WINNER_A = "A"
WINNER_B = "B"
WINNER_DRAW = "DRAW"

#: 04 明确：平局胜率贡献 0、平局率单列；超时行不纳入正常胜率分母。
WIN_RATE_CONVENTION = (
    "win_rate = wins/(wins+losses+ties)；平局（非超时 DRAW）胜率贡献 0；"
    "draw_timeout=True 的行不计入该分母（仅计入 timeout_rate 与超时保守区间）"
)

DICE_MIN = 1
DICE_MAX = 6
#: 骰序 seed 与地图 seed 使用互不相交的推导区间（保证两套 RNG 独立可审计）。
DICE_SEED_OFFSET = 100_003
DICE_SEED_STRIDE = 7

DEFAULT_HUMAN_STRATEGY = "GATHER"
#: 默认 human_model_id（= 由 DEFAULT_HUMAN_STRATEGY 派生；显式覆盖用 --human-model-id）
DEFAULT_HUMAN_MODEL_ID = f"VIEW_HEURISTIC:{DEFAULT_HUMAN_STRATEGY}:v1"

#: 地图臂（Lead 裁决 2026-09-28）：main = 12×12（= 分治阈值 144，NORMAL/HARD 启用分治）；
#: baseline = 10×10（单体 OPT，partition_enabled 恒 False），用于同图同骰的单体/分治对照。
ARM_PRESETS: dict[str, tuple[int, int]] = {"main": (12, 12), "baseline": (10, 10)}
DEFAULT_ARM = "main"
DEFAULT_PARTITION_THRESHOLD = 144
ARM_ROLES: dict[str, str] = {
    "main": "主批次：算法/可见性对照 + 分治证据（144 格 = 阈值）",
    "baseline": "单体 OPT 基线：同图同骰比较单体 vs 分治的 |ΔU|/加速比",
}
#: 04 预注册的正式规模（用于 JSON 里的全量耗时外推）
FULL_SCALE_CASES = 30 * 3 * 3
DEFAULT_OUTPUT_DIR = "experiments/results"
OUTPUT_FILENAME_CONVENTION = (
    "batch_<arm>_seed<S>_maps<M>_dice<D>_<H>x<W>_w<walls>t<treasures>p<traps>.csv"
)

#: 06 §5.5 冻结键；缺键 = 实验任务直接失败。
METRIC_KEYS_REQUIRED: tuple[str, ...] = (
    "battles",
    "trap_steps_a",
    "trap_steps_b",
    "treasure_steps_a",
    "treasure_steps_b",
    "ai_calls",
    "ai_elapsed_ns",
    "cache_hits",
    "cache_misses",
    "nodes",
    "partition_used",
    "dice_sequence",
    "timed_out",
)

#: 实验依赖的运行模块（06 §1）。缺失时进入「未落地」分支，而不是伪造数据。
TREASURE_GAME_MODULES: tuple[str, ...] = (
    "treasure_game.models",
    "treasure_game.map_module",
    "treasure_game.human_policy",
    "treasure_game.ai_module",
    "treasure_game.simulator",
)

#: 写入 JSON 的源码指纹清单（04：另保存地图/骰表版本与源码版本）。
FINGERPRINT_FILES: tuple[str, ...] = (
    "treasure_game/models.py",
    "treasure_game/map_module.py",
    "treasure_game/path_module.py",
    "treasure_game/battle_module.py",
    "treasure_game/human_policy.py",
    "treasure_game/greedy_ai.py",
    "treasure_game/opt_ai.py",
    "treasure_game/divide_conquer_module.py",
    "treasure_game/ai_module.py",
    "treasure_game/simulator.py",
)


class BatchConfigError(ValueError):
    """CLI / 实验配置非法。"""


class MetricContractError(RuntimeError):
    """``GameResult.metrics`` 缺少 06 §5.5 冻结键。"""


class RunnerUnavailable(RuntimeError):
    """``treasure_game`` 运行模块尚未落地（阶段 1 的预期状态）。"""


# --------------------------------------------------------------------------------------
# 配置与矩阵数据模型
# --------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True, slots=True)
class BatchConfig:
    """一次批量实验的全部控制条件（04「控制条件」行）。"""

    seed: int = 42
    maps: int = 30
    dice_seeds: int = 3
    height: int = 12               # Lead 裁决：主批次 12×12（= 分治阈值 144）
    width: int = 12
    walls: int = 16
    treasures: int = 8
    traps: int = 6
    depth: int = 3
    candidate_limit: int = 24
    partition_threshold: int = DEFAULT_PARTITION_THRESHOLD
    max_turns: int = 500
    human_strategy: str = DEFAULT_HUMAN_STRATEGY
    human_model_id: str = ""         # 空 = 由 strategy 派生（避免 CSV 记录的模型与实跑策略不一致）
    bootstrap_resamples: int = 2000
    bootstrap_seed: int | None = None
    partition: str = "auto"          # auto | on | off
    arm: str = "auto"                # auto | main | baseline
    fail_fast: bool = False
    quiet: bool = False

    # ---- 派生量 ----
    @property
    def map_area(self) -> int:
        return self.height * self.width

    @property
    def resolved_human_model_id(self) -> str:
        """``human_model_id`` 列取值；未显式指定时由策略派生，保证与实跑策略一致。"""
        return self.human_model_id or f"VIEW_HEURISTIC:{self.human_strategy}:v1"

    @property
    def arm_label(self) -> str:
        """地图臂标签：CLI 显式指定优先，否则按网格推导（12×12→main、10×10→baseline）。"""
        if self.arm != "auto":
            return self.arm
        for name, grid in ARM_PRESETS.items():
            if (self.height, self.width) == grid:
                return name
        return f"custom-{self.height}x{self.width}"

    @property
    def is_default_scale(self) -> bool:
        return (self.maps, self.dice_seeds) == (30, 3)

    def partition_flags(self) -> dict[str, bool]:
        return {mode: partition_enabled_for(mode, self) for mode in MODES}

    def validate(self) -> None:
        if self.maps < 1:
            raise BatchConfigError(f"--maps must be >= 1, got {self.maps}")
        if self.dice_seeds < 1:
            raise BatchConfigError(f"--dice-seeds must be >= 1, got {self.dice_seeds}")
        if self.height < 3 or self.width < 3:
            raise BatchConfigError(f"map grid too small: {self.height}x{self.width}")
        if self.walls < 0 or self.treasures < 1 or self.traps < 0:
            raise BatchConfigError("--walls/--traps must be >= 0 and --treasures must be >= 1")
        needed = self.walls + self.treasures + self.traps + 2  # +2 = two spawn points
        if needed > self.map_area:
            raise BatchConfigError(
                f"walls+treasures+traps+2 spawns = {needed} exceeds map cells {self.map_area}"
            )
        if self.depth < 1:
            raise BatchConfigError(f"--depth must be >= 1, got {self.depth}")
        if self.candidate_limit < 1:
            raise BatchConfigError(f"--candidate-limit must be >= 1, got {self.candidate_limit}")
        if self.partition_threshold < 1:
            raise BatchConfigError(
                f"--partition-threshold must be >= 1, got {self.partition_threshold}"
            )
        if self.max_turns < 1:
            raise BatchConfigError(f"--max-turns must be >= 1, got {self.max_turns}")
        if self.bootstrap_resamples < 0:
            raise BatchConfigError("--bootstrap must be >= 0")
        if self.partition not in ("auto", "on", "off"):
            raise BatchConfigError(f"--partition must be auto/on/off, got {self.partition!r}")
        if self.arm not in ("auto", *ARM_PRESETS):
            raise BatchConfigError(
                f"--arm must be auto/{'/'.join(ARM_PRESETS)}, got {self.arm!r}"
            )
        if self.human_model_id and not self.human_model_id.strip():
            raise BatchConfigError("human_model_id must not be blank (omit it to derive from strategy)")
        overlap = set(derive_map_seeds(self)) & set(derive_dice_seeds(self))
        if overlap:
            raise BatchConfigError(f"map seeds and dice seeds overlap: {sorted(overlap)}")

    def describe(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "maps": self.maps,
            "dice_seeds": self.dice_seeds,
            "arm": self.arm_label,
            "arm_source": "cli" if self.arm != "auto" else "derived from grid",
            "map_grid": f"{self.height}x{self.width}",
            "height": self.height,
            "width": self.width,
            "map_area": self.map_area,
            "walls": self.walls,
            "treasures": self.treasures,
            "traps": self.traps,
            "treasure_distribution": {
                **{str(value): 0.099 for value in range(1, 11)}, "15": 0.005, "20": 0.005
            },
            "trap_value": -2,
            "depth": self.depth,
            "K": self.candidate_limit,
            "partition_threshold": self.partition_threshold,
            "partition_mode": self.partition,
            "partition_enabled_by_mode": self.partition_flags(),
            "max_turns": self.max_turns,
            "max_valid_steps": DICE_MAX,
            "dice_range": [DICE_MIN, DICE_MAX],
            "human_model_id": self.resolved_human_model_id,
            "human_strategy": self.human_strategy,
            "human_rng": "random.Random(dice_seed)（同单元三模式同一随机流，保证配对与可复现）",
            "bootstrap_resamples": self.bootstrap_resamples,
            "start_scores": {"score_a": 0, "score_b": 0},
            "first_turn": "A",
            "seat_a_role": "HUMAN + ViewHeuristicIO (ViewMap only; VIEW_HEURISTIC is a policy name, not a SeatPolicy value)",
            "seat_b_role": "GREEDY(EASY) / OPT(NORMAL,HARD) (TrueMap)",
            "seat_policy_values": ["HUMAN", "GREEDY", "OPT", "OPT_PARTITION"],
        }


@dataclasses.dataclass(frozen=True, slots=True)
class CaseSpec:
    """一局游戏的全部输入（不可变、可哈希字段，便于复现与断言）。"""

    unit_index: int
    map_index: int
    dice_index: int
    map_seed: int
    dice_seed: int
    mode: str
    depth: int
    candidate_limit: int
    partition_threshold: int
    partition_enabled: bool
    human_model_id: str
    human_strategy: str
    height: int
    width: int
    walls: int
    treasures: int
    traps: int
    max_turns: int
    dice_table: tuple[int, ...]

    @property
    def K(self) -> int:
        return self.candidate_limit

    @property
    def unit_key(self) -> tuple[int, int]:
        return (self.map_seed, self.dice_seed)

    @property
    def row_key(self) -> tuple[int, int, str]:
        return (self.map_seed, self.dice_seed, self.mode)

    def describe(self) -> dict[str, object]:
        return {
            "unit_index": self.unit_index,
            "map_index": self.map_index,
            "dice_index": self.dice_index,
            "map_seed": self.map_seed,
            "dice_seed": self.dice_seed,
            "mode": self.mode,
            "machine_policy": MODE_MACHINE_POLICY[self.mode],
            "depth": self.depth,
            "K": self.candidate_limit,
            "partition_enabled": self.partition_enabled,
            "partition_threshold": self.partition_threshold,
            "human_model_id": self.human_model_id,
            "grid": f"{self.height}x{self.width}",
            "dice_table_length": len(self.dice_table),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class PairedUnit:
    """配对单元 = 同一地图 seed + 同一骰序 seed；三模式共用地图与骰表。"""

    index: int
    map_index: int
    dice_index: int
    map_seed: int
    dice_seed: int
    dice_table: tuple[int, ...]
    cases: tuple[CaseSpec, ...]


@dataclasses.dataclass(slots=True)
class CaseRecord:
    """一行实验数据（CSV 的 20 列 + 仅进 JSON 的辅助字段）。"""

    case: CaseSpec
    map_id: str = ""
    winner: str | None = None                 # "A" / "B" / None（非超时平局）
    draw_timeout: bool = False
    turns: int | None = None
    score_a: int | None = None
    score_b: int | None = None
    trap_steps_a: int | None = None
    trap_steps_b: int | None = None
    battles: int | None = None
    ai_calls: int | None = None
    ai_elapsed_ns: int | None = None           # None = 缺失；绝不填 0（04 截尾口径）
    cache_hits: int | None = None
    cache_misses: int | None = None
    wall_ns: int | None = None                 # 每局总耗时（仅 JSON）
    treasure_steps_a: int | None = None
    treasure_steps_b: int | None = None
    nodes: int | None = None
    partition_used: bool | None = None
    dice_sequence: tuple[int, ...] = ()
    checks: dict[str, object] = dataclasses.field(default_factory=dict)

    # ---- 便捷属性 ----
    @property
    def map_seed(self) -> int:
        return self.case.map_seed

    @property
    def dice_seed(self) -> int:
        return self.case.dice_seed

    @property
    def mode(self) -> str:
        return self.case.mode

    @property
    def score_diff(self) -> int | None:
        """主因变量 ``scoreOpt - scoreHuman`` = ``score_b - score_a``。"""
        if self.score_a is None or self.score_b is None:
            return None
        return self.score_b - self.score_a

    def csv_dict(self) -> dict[str, str]:
        """严格按 ``CSV_COLUMNS`` 输出字符串值；缺失 = 空串，布尔 = True/False。"""
        return {
            "map_seed": str(self.case.map_seed),
            "dice_seed": str(self.case.dice_seed),
            "map_id": self.map_id,
            "mode": self.case.mode,
            "depth": str(self.case.depth),
            "K": str(self.case.candidate_limit),
            "partition_enabled": _bool_text(self.case.partition_enabled),
            "human_model_id": self.case.human_model_id,
            "score_a": _num_text(self.score_a),
            "score_b": _num_text(self.score_b),
            "winner": self.winner if self.winner in (WINNER_A, WINNER_B) else WINNER_DRAW,
            "draw_timeout": _bool_text(self.draw_timeout),
            "turns": _num_text(self.turns),
            "trap_steps_a": _num_text(self.trap_steps_a),
            "trap_steps_b": _num_text(self.trap_steps_b),
            "battles": _num_text(self.battles),
            "ai_calls": _num_text(self.ai_calls),
            "ai_elapsed_ns": _num_text(self.ai_elapsed_ns),
            "cache_hits": _num_text(self.cache_hits),
            "cache_misses": _num_text(self.cache_misses),
        }

    def json_dict(self) -> dict[str, object]:
        """仅供 JSON 的每局明细（含 wall_ns 与一致性检查）。"""
        return {
            "map_seed": self.case.map_seed,
            "dice_seed": self.case.dice_seed,
            "map_id": self.map_id,
            "mode": self.case.mode,
            "winner": self.winner,
            "draw_timeout": self.draw_timeout,
            "turns": self.turns,
            "score_a": self.score_a,
            "score_b": self.score_b,
            "score_diff": self.score_diff,
            "trap_steps_a": self.trap_steps_a,
            "trap_steps_b": self.trap_steps_b,
            "treasure_steps_a": self.treasure_steps_a,
            "treasure_steps_b": self.treasure_steps_b,
            "battles": self.battles,
            "ai_calls": self.ai_calls,
            "ai_elapsed_ns": self.ai_elapsed_ns,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "nodes": self.nodes,
            "partition_used": self.partition_used,
            "wall_ns": self.wall_ns,
            "checks": dict(self.checks),
        }


@dataclasses.dataclass(slots=True)
class BatchOutcome:
    """``run_batch`` 的返回值（records/errors/summary 同源，保证可复现）。"""

    config: BatchConfig
    units: tuple[PairedUnit, ...]
    records: list[CaseRecord]
    errors: list[dict[str, object]]
    checks: dict[str, object]
    summary: dict[str, object]
    elapsed_ns: int = 0

    @property
    def csv_rows(self) -> list[dict[str, str]]:
        return [rec.csv_dict() for rec in self.records]

    @property
    def exit_code(self) -> int:
        return 0 if not self.errors else 1


# --------------------------------------------------------------------------------------
# 种子推导 / 骰值表 / 实验矩阵
# --------------------------------------------------------------------------------------
def derive_map_seeds(config: BatchConfig) -> tuple[int, ...]:
    """地图 seed 清单（前缀稳定：``--maps`` 变大不改变已有地图 seed）。"""
    return tuple(config.seed + i for i in range(config.maps))


def derive_dice_seeds(config: BatchConfig) -> tuple[int, ...]:
    """骰序 seed 清单（与地图 seed 推导区间不相交）。"""
    return tuple(
        config.seed + DICE_SEED_OFFSET + DICE_SEED_STRIDE * j for j in range(config.dice_seeds)
    )


def make_dice_table(dice_seed: int, max_turns: int) -> tuple[int, ...]:
    """预生成 ``2*max_turns`` 个骰值（按 ``(turn_index, seat)`` 顺序）。

    使用**独立**的 ``random.Random(dice_seed)``；骰值表生成后不再消耗 RNG，
    因此 AI 搜索/分块推演不会改变骰序（04「搜索不消耗 RNG」）。
    """
    if max_turns < 1:
        raise BatchConfigError(f"max_turns must be >= 1, got {max_turns}")
    rng = random.Random(dice_seed)
    length = 2 * max_turns
    return tuple(rng.randint(DICE_MIN, DICE_MAX) for _ in range(length))


def dice_index(turn_index: int, seat: str) -> int:
    """06 §3：``turn_index*2 + (0 for A else 1)``。"""
    if turn_index < 0:
        raise ValueError(f"turn_index must be >= 0, got {turn_index}")
    seat_text = str(getattr(seat, "value", seat)).upper()
    if seat_text not in ("A", "B"):
        raise ValueError(f"seat must be A/B, got {seat!r}")
    return turn_index * 2 + (0 if seat_text == "A" else 1)


def dice_at(table: typing.Sequence[int], turn_index: int, seat: str) -> int:
    """按取模索引读取骰值（与 Simulator 的 ``dice_seed_table`` 语义一致）。"""
    if not table:
        raise ValueError("dice table is empty")
    return int(table[dice_index(turn_index, seat) % len(table)])


def partition_enabled_for(mode: str, config: BatchConfig) -> bool:
    """分治路径是否对机器搜索生效。

    * ``--partition on/off`` 显式覆盖；
    * ``auto``：EASY 走贪心，不做 DP 搜索 → ``False``；NORMAL/HARD 按
      ``地图格数 >= partition_threshold``（阈值 144 = 12×12）。
    """
    if mode not in MODES:
        raise BatchConfigError(f"unknown mode {mode!r}")
    if config.partition == "on":
        return True
    if config.partition == "off":
        return False
    if mode == "EASY":
        return False
    return config.map_area >= config.partition_threshold


def build_case_matrix(config: BatchConfig) -> list[PairedUnit]:
    """构造配对实验矩阵：``maps × dice_seeds`` 单元 × 3 模式。"""
    config.validate()
    map_seeds = derive_map_seeds(config)
    dice_seed_list = derive_dice_seeds(config)
    units: list[PairedUnit] = []
    index = 0
    for map_index, map_seed in enumerate(map_seeds):
        for dice_index_, dice_seed in enumerate(dice_seed_list):
            table = make_dice_table(dice_seed, config.max_turns)
            cases = tuple(
                CaseSpec(
                    unit_index=index,
                    map_index=map_index,
                    dice_index=dice_index_,
                    map_seed=map_seed,
                    dice_seed=dice_seed,
                    mode=mode,
                    depth=config.depth,
                    candidate_limit=config.candidate_limit,
                    partition_threshold=config.partition_threshold,
                    partition_enabled=partition_enabled_for(mode, config),
                    human_model_id=config.resolved_human_model_id,
                    human_strategy=config.human_strategy,
                    height=config.height,
                    width=config.width,
                    walls=config.walls,
                    treasures=config.treasures,
                    traps=config.traps,
                    max_turns=config.max_turns,
                    dice_table=table,
                )
                for mode in MODES
            )
            units.append(
                PairedUnit(
                    index=index,
                    map_index=map_index,
                    dice_index=dice_index_,
                    map_seed=map_seed,
                    dice_seed=dice_seed,
                    dice_table=table,
                    cases=cases,
                )
            )
            index += 1
    return units


# --------------------------------------------------------------------------------------
# 统计口径（04「主因变量」/「配对分析」）
# --------------------------------------------------------------------------------------
def _numbers(values: typing.Iterable[object]) -> list[float]:
    out: list[float] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            out.append(float(value))
            continue
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


def mean(values: typing.Iterable[object]) -> float | None:
    vals = _numbers(values)
    if not vals:
        return None
    return math.fsum(vals) / len(vals)


def median(values: typing.Iterable[object]) -> float | None:
    vals = sorted(_numbers(values))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2 == 1:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def sample_std(values: typing.Iterable[object]) -> float | None:
    """样本标准差（分母 ``n-1``）；``n < 2`` 返回 ``None``（不假装是 0）。"""
    vals = _numbers(values)
    n = len(vals)
    if n < 2:
        return None
    mu = math.fsum(vals) / n
    return math.sqrt(math.fsum((v - mu) ** 2 for v in vals) / (n - 1))


def percentile(values: typing.Iterable[object], q: float) -> float | None:
    """线性插值分位数（``q`` ∈ [0, 100]），与常见 numpy 默认口径一致。"""
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"q 必须在 [0, 100]，得到 {q}")
    vals = sorted(_numbers(values))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vals[int(pos)]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def bootstrap_cluster_ci(
    clusters: typing.Mapping[object, typing.Sequence[float]],
    *,
    resamples: int = 2000,
    alpha: float = 0.05,
    rng: random.Random | None = None,
    method: str = "cluster-bootstrap by map_seed",
) -> dict[str, object]:
    """按地图 seed 分簇重抽样的百分位区间（04：不把 270 局当独立地图）。

    ``clusters``：``{map_seed: [单元级数值, ...]}``。重抽样单位是**簇（地图 seed）**，
    命中簇时把该簇内所有配对单元一起纳入，从而保留簇内相关性。
    """
    keys = sorted(clusters, key=repr)
    units = [list(clusters[k]) for k in keys]
    pooled = [v for unit in units for v in unit]
    info: dict[str, object] = {
        "method": method,
        "resamples": int(resamples),
        "alpha": float(alpha),
        "clusters": len(keys),
        "units": len(pooled),
        "point_estimate": mean(pooled),
        "ci_low": None,
        "ci_high": None,
    }
    if not pooled or resamples <= 0:
        return info
    rng = rng or random.Random(0)
    n_clusters = len(units)
    stats: list[float] = []
    for _ in range(int(resamples)):
        picked = [units[rng.randrange(n_clusters)] for _ in range(n_clusters)]
        vals = [v for unit in picked for v in unit]
        stats.append(math.fsum(vals) / len(vals))
    info["ci_low"] = percentile(stats, 100.0 * alpha / 2.0)
    info["ci_high"] = percentile(stats, 100.0 * (1.0 - alpha / 2.0))
    return info


def _mode_stats(records: typing.Sequence[CaseRecord]) -> dict[str, object]:
    """单模式聚合：AI 胜率 / 得分差 / 踩陷阱 / 对战 / 耗时 / DP 命中。"""
    rows = list(records)
    timeouts = [r for r in rows if r.draw_timeout]
    normal = [r for r in rows if not r.draw_timeout]
    wins = sum(1 for r in normal if r.winner == WINNER_B)
    losses = sum(1 for r in normal if r.winner == WINNER_A)
    ties = sum(1 for r in normal if r.winner is None)
    denom = wins + losses + ties
    total = len(rows)
    diffs = [r.score_diff for r in rows]
    # 截尾口径：超时局的分数被 500 回合反复对战/踩陷阱放大，不能与正常局同池聚合
    diffs_scored = [r.score_diff for r in normal]
    elapsed = [r.ai_elapsed_ns for r in rows if r.ai_elapsed_ns is not None]
    per_call = [
        r.ai_elapsed_ns / r.ai_calls
        for r in rows
        if r.ai_elapsed_ns is not None and r.ai_calls
    ]
    hits = sum(r.cache_hits for r in rows if r.cache_hits is not None)
    misses = sum(r.cache_misses for r in rows if r.cache_misses is not None)

    def rate(numerator: float, denominator: float) -> float | None:
        return None if denominator <= 0 else numerator / denominator

    return {
        "games": total,
        "normal_games": len(normal),
        "timeouts": len(timeouts),
        "wins_b": wins,
        "losses_b": losses,
        "ties": ties,
        "win_rate_denominator": denom,
        # 04 口径：平局贡献 0；超时行不进分母
        "win_rate": rate(float(wins), float(denom)),
        "draw_rate": rate(float(ties), float(denom)),
        "timeout_rate": rate(float(len(timeouts)), float(total)),
        # 超时保守区间（04：同时报告含超时的保守范围）
        "win_rate_timeouts_as_losses": rate(float(wins), float(denom + len(timeouts))),
        "win_rate_timeouts_as_wins": rate(float(wins + len(timeouts)), float(denom + len(timeouts))),
        "score_a_mean": mean(r.score_a for r in rows),
        "score_b_mean": mean(r.score_b for r in rows),
        "score_diff_mean": mean(diffs),
        "score_diff_std": sample_std(diffs),
        "score_diff_median": median(diffs),
        # 主口径（截尾）：仅非超时局；超时局另列 *_incl_timeout
        "score_diff_games_scored": len(diffs_scored),
        "score_diff_mean_scored": mean(diffs_scored),
        "score_diff_std_scored": sample_std(diffs_scored),
        "score_diff_median_scored": median(diffs_scored),
        "score_diff_mean_incl_timeout": mean(diffs),
        "score_diff_note": (
            "score_diff = score_b - score_a (机器 - 人类)；主口径剔除 draw_timeout 局，"
            "并给出含超时版本用于透明性"
        ),
        "turns_mean": mean(r.turns for r in rows),
        "trap_steps_a_mean": mean(r.trap_steps_a for r in rows),
        "trap_steps_b_mean": mean(r.trap_steps_b for r in rows),
        "treasure_steps_a_mean": mean(r.treasure_steps_a for r in rows),
        "treasure_steps_b_mean": mean(r.treasure_steps_b for r in rows),
        "battles_mean": mean(r.battles for r in rows),
        "ai_calls_mean": mean(r.ai_calls for r in rows),
        "nodes_mean": mean(r.nodes for r in rows),
        "cache_hit_rate": rate(float(hits), float(hits + misses)),
        "cache_hits_sum": hits,
        "cache_misses_sum": misses,
        # 决策耗时：缺失不填 0，仅在存在的样本上取分位数
        "ai_elapsed_ns_present": len(elapsed),
        "ai_elapsed_ns_missing": total - len(elapsed),
        "ai_elapsed_ns_mean": mean(elapsed),
        "ai_elapsed_ns_median": median(elapsed),
        "ai_elapsed_ns_p95": percentile(elapsed, 95.0),
        "ai_elapsed_ns_per_game_median": median(elapsed),
        "ai_ns_per_call_median": median(per_call),
        "ai_ns_per_call_p95": percentile(per_call, 95.0),
        "wall_ns_median": median([r.wall_ns for r in rows]),
        "wall_ns_p95": percentile([r.wall_ns for r in rows], 95.0),
    }


#: 配对对比变量（04：每单元差值 与 95% bootstrap 区间）。
#: 第三项 = True 表示「任一侧超时则剔除该配对单元」（截尾口径；超时局的计数被 500 回合放大）。
_PAIRED_VARIABLES: tuple[tuple[str, typing.Callable[[CaseRecord], float | None], bool], ...] = (
    (
        "score_diff",
        lambda r: None if r.draw_timeout or r.score_diff is None else float(r.score_diff),
        True,
    ),
    (
        "score_diff_incl_timeout",
        lambda r: None if r.score_diff is None else float(r.score_diff),
        False,
    ),
    (
        "trap_steps_a",
        lambda r: None if r.draw_timeout or r.trap_steps_a is None else float(r.trap_steps_a),
        True,
    ),
    (
        "trap_steps_b",
        lambda r: None if r.draw_timeout or r.trap_steps_b is None else float(r.trap_steps_b),
        True,
    ),
    (
        "battles",
        lambda r: None if r.draw_timeout or r.battles is None else float(r.battles),
        True,
    ),
    # 平局贡献 0、超时行剔除，与 win_rate 口径一致
    (
        "win_b",
        lambda r: None
        if r.draw_timeout
        else (1.0 if r.winner == WINNER_B else 0.0),
        True,
    ),
)

#: 04：EASY vs NORMAL 隔离算法差异；NORMAL vs HARD 隔离可见性差异。
PAIRED_CONTRASTS: tuple[tuple[str, str], ...] = (("NORMAL", "EASY"), ("HARD", "NORMAL"))


def paired_analysis(
    records: typing.Sequence[CaseRecord], config: BatchConfig
) -> dict[str, object]:
    """配对差值 + 按地图 seed 分簇的 95% bootstrap 区间。"""
    by_mode: dict[str, dict[tuple[int, int], CaseRecord]] = {m: {} for m in MODES}
    for rec in records:
        by_mode[rec.mode][rec.case.unit_key] = rec
    rng = random.Random(
        config.bootstrap_seed if config.bootstrap_seed is not None else (config.seed ^ 0x5EED)
    )
    out: dict[str, object] = {}
    for hi_mode, lo_mode in PAIRED_CONTRASTS:
        label = f"{hi_mode}-{lo_mode}"
        contrast: dict[str, object] = {}
        for name, extractor, censor_pairs in _PAIRED_VARIABLES:
            clusters: dict[int, list[float]] = {}
            dropped = 0
            for unit_key in sorted(set(by_mode[hi_mode]) & set(by_mode[lo_mode])):
                hi_row, lo_row = by_mode[hi_mode][unit_key], by_mode[lo_mode][unit_key]
                if censor_pairs and (hi_row.draw_timeout or lo_row.draw_timeout):
                    dropped += 1  # 截尾：任一侧超时 → 该配对单元不参与差值
                    continue
                hi_val = extractor(hi_row)
                lo_val = extractor(lo_row)
                if hi_val is None or lo_val is None:
                    dropped += 1
                    continue
                clusters.setdefault(unit_key[0], []).append(hi_val - lo_val)
            diffs = [v for unit in clusters.values() for v in unit]
            if not diffs:
                contrast[name] = {
                    "units": 0,
                    "clusters": 0,
                    "units_dropped_censored": dropped,
                    "mean_diff": None,
                    "std_diff": None,
                    "median_diff": None,
                    "ci_low": None,
                    "ci_high": None,
                }
                continue
            ci = bootstrap_cluster_ci(
                clusters, resamples=config.bootstrap_resamples, rng=rng
            )
            contrast[name] = {
                "units": len(diffs),
                "clusters": len(clusters),
                "units_dropped_censored": dropped,
                "mean_diff": mean(diffs),
                "std_diff": sample_std(diffs),
                "median_diff": median(diffs),
                "ci_low": ci["ci_low"],
                "ci_high": ci["ci_high"],
                "ci95": [ci["ci_low"], ci["ci_high"]],
                "bootstrap": ci,
            }
        out[label] = contrast
    return out


def aggregate_records(
    records: typing.Sequence[CaseRecord], config: BatchConfig
) -> dict[str, object]:
    """分模式聚合 + 配对分析 + 超时计数（JSON 摘要主体）。"""
    per_mode = {mode: _mode_stats([r for r in records if r.mode == mode]) for mode in MODES}
    timeouts = [r for r in records if r.draw_timeout]
    return {
        "per_mode": per_mode,
        "paired": paired_analysis(records, config),
        "timeouts": {
            "total": len(timeouts),
            "by_mode": {
                mode: sum(1 for r in timeouts if r.mode == mode) for mode in MODES
            },
            "rate_of_all_rows": None if not records else len(timeouts) / len(records),
            "excluded_from_win_rate_denominator": True,
            "note": "draw_timeout=True 的行 winner=DRAW，仅计入 timeout_rate 与超时保守区间",
        },
        "winner_rates_note": WIN_RATE_CONVENTION,
        "stats_version": STATS_VERSION,
    }


# --------------------------------------------------------------------------------------
# Runner 协议（唯一接通点）
# --------------------------------------------------------------------------------------
class CaseRunner(typing.Protocol):
    """把 ``CaseSpec`` 变成一行原始观测；阶段 1 用假 runner，阶段 2 用 ``SimulatorCaseRunner``。"""

    def run_case(self, case: CaseSpec) -> typing.Mapping[str, object]:  # pragma: no cover
        ...


def _console_safe(text: str) -> str:
    """控制台/重定向下稳定：非 ASCII 字符降级为 ``?``（完整原文仍在 JSON errors 里）。"""
    return text.encode("ascii", "replace").decode("ascii")


def _bool_text(value: bool) -> str:
    return "True" if value else "False"


def _num_text(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return _bool_text(value)
    return str(value)


def _opt_int(value: object, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    raise TypeError(f"{field} expected int, got {type(value).__name__}: {value!r}")


def _opt_bool(value: object, field: str) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise TypeError(f"{field} expected bool, got {type(value).__name__}: {value!r}")


_REQUIRED_INTS: tuple[str, ...] = (
    "score_a",
    "score_b",
    "turns",
    "trap_steps_a",
    "trap_steps_b",
    "battles",
    "ai_calls",
    "cache_hits",
    "cache_misses",
)
_OPTIONAL_INTS: tuple[str, ...] = (
    "ai_elapsed_ns",
    "treasure_steps_a",
    "treasure_steps_b",
    "nodes",
)


def record_from_outcome(
    case: CaseSpec, outcome: typing.Mapping[str, object], wall_ns: int | None
) -> CaseRecord:
    """校验并归一化 runner 输出（缺字段 → 抛错，不静默补 0）。"""
    if not isinstance(outcome, typing.Mapping):
        raise TypeError(f"runner must return a Mapping, got {type(outcome).__name__}")
    winner = outcome.get("winner")
    winner = None if winner in (None, "") else str(winner)
    if winner not in (WINNER_A, WINNER_B, None):
        raise ValueError(f"winner must be A/B/None, got {winner!r}")
    map_id = str(outcome.get("map_id") or "")
    if not map_id:
        raise ValueError("runner did not return map_id (required CSV column per docs/04)")
    rec = CaseRecord(case=case, map_id=map_id, winner=winner, wall_ns=wall_ns)
    rec.draw_timeout = bool(_opt_bool(outcome.get("draw_timeout", False), "draw_timeout"))
    for field in _REQUIRED_INTS:
        if field not in outcome:
            raise KeyError(f"runner outcome missing required field {field!r}")
        value = _opt_int(outcome[field], field)
        if value is None:
            raise ValueError(f"runner outcome field {field} must not be None (required)")
        setattr(rec, field, value)
    for field in _OPTIONAL_INTS:
        setattr(rec, field, _opt_int(outcome.get(field), field))
    rec.partition_used = _opt_bool(outcome.get("partition_used"), "partition_used")
    seq = outcome.get("dice_sequence")
    rec.dice_sequence = tuple(int(x) for x in seq) if seq else ()
    if rec.draw_timeout and rec.winner is not None:
        raise ValueError("winner must be None when draw_timeout=True (06 s4.9)")
    rec.checks = {
        "dice_sequence_matches_table": (
            None
            if not rec.dice_sequence
            else tuple(rec.dice_sequence) == case.dice_table[: len(rec.dice_sequence)]
        ),
        # 容忍 turns 的两种可能口径（回合数 vs 座位手数），只做观测不做硬判定
        "dice_sequence_covers_turns": (
            None
            if not rec.dice_sequence
            else len(rec.dice_sequence)
            in {
                rec.turns or 0,
                2 * (rec.turns or 0) - 1,
                2 * (rec.turns or 0),
                2 * (rec.turns or 0) + 1,
            }
        ),
        "timed_out_consistent": outcome.get("timed_out") is None
        or bool(outcome.get("timed_out")) == rec.draw_timeout,
        # 配置条件是否与实测分治路径一致（不一致只说明 AiModule 内部另有判据，不阻断）
        "partition_used_matches_config_condition": rec.partition_used is None
        or rec.partition_used == case.partition_enabled,
    }
    return rec


# --------------------------------------------------------------------------------------
# 阶段 2 接通：ViewMap 人类代理 + Simulator runner
# --------------------------------------------------------------------------------------
def probe_modules() -> tuple[list[str], dict[str, str]]:
    """返回 (缺失模块, 导入失败信息)；缺失 = 尚未落地，失败 = 已落地但坏了。"""
    missing: list[str] = []
    failures: dict[str, str] = {}
    for name in TREASURE_GAME_MODULES:
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError:
            missing.append(name)  # 父包 treasure_game 都还不存在 = 尚未落地
            continue
        except Exception as exc:  # 导入期异常
            failures[name] = f"{type(exc).__name__}: {exc}"
            continue
        if spec is None:
            missing.append(name)
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
    return missing, failures


def simulator_ready() -> tuple[bool, str]:
    """阶段 2 门禁：``treasure_game`` 五个运行模块是否都可用。"""
    missing, failures = probe_modules()
    if failures:
        return False, "module import failed: " + "; ".join(
            f"{k} -> {v}" for k, v in failures.items()
        )
    if missing:
        return False, "modules not landed yet: " + ", ".join(missing)
    return True, "ready"


_VIEW_FIELDS = ("pos_a", "pos_b", "score_a", "score_b", "turn", "turn_index")


def _looks_like_public_state_view(candidate: object) -> bool:
    """``PublicStateView`` 鸭子类型识别：字段齐全且**不持有真图**。"""
    if candidate is None or hasattr(candidate, "true_map"):
        return False
    if not hasattr(candidate, "mode"):
        return False
    return all(hasattr(candidate, field) for field in _VIEW_FIELDS)


class ViewHeuristicIO:
    """`GameIO` 适配器：A 座位交给只读视图的 ``HumanPolicy``（绝不接触 TrueMap）。

    06 §2（Lead 2026-09-28 修订）冻结签名为
    ``render(view, state_view, events)`` + ``next_action(seat, dice_N, view, state_view)``；
    本类**同时兼容**修订前的三参 ``next_action(seat, dice_N, view)``（此时依赖 ``render``
    已缓存的 ``PublicStateView``）。为容忍参数命名/位置差异，按 ``PublicStateView``
    字段做鸭子类型识别，并显式排除任何持有 ``true_map`` 的对象。
    """

    def __init__(self, human_policy: object) -> None:
        self._policy = human_policy
        self._state_view: object | None = None
        self.render_calls = 0
        self.action_calls = 0
        self.state_view_sources: set[str] = set()

    # --- GameIO 协议 ---
    def render(self, *args: object, **kwargs: object) -> None:
        self.render_calls += 1
        if self._remember_state_view([*args, *kwargs.values()]):
            self.state_view_sources.add("render")

    def next_action(
        self,
        seat: object,
        dice_N: int,
        view: object,
        state_view: object | None = None,
        *args: object,
        **kwargs: object,
    ) -> tuple[object, ...]:
        self.action_calls += 1
        if self._remember_state_view([state_view, *args, *kwargs.values()]):
            self.state_view_sources.add("next_action")
        if self._state_view is None:
            raise RuntimeError(
                "Simulator must pass state_view to GameIO.next_action(seat, dice_N, view, "
                "state_view) or call GameIO.render(view, state_view, events) first; otherwise "
                "HumanPolicy.choose has no PublicStateView (06 s2/s3)."
            )
        return tuple(self._policy.choose(view, seat, dice_N, self._state_view))  # type: ignore[attr-defined]

    def log(self, message: str) -> None:
        return None

    def _remember_state_view(self, candidates: typing.Iterable[object]) -> bool:
        for candidate in candidates:
            if _looks_like_public_state_view(candidate):
                self._state_view = candidate
                return True
        return False


class SimulatorCaseRunner:
    """阶段 2：把 ``CaseSpec`` 接到冻结的 Map/Ai/Simulator/HumanPolicy 签名上。

    这是唯一一处依赖真实 ``treasure_game`` 的代码；契约漂移只需改这里。
    """

    def __init__(self, config: BatchConfig, *, modules: object | None = None) -> None:
        """``modules`` 是测试接缝：注入严格按 06 §3 的假模块命名空间，
        即可在不依赖真实 Simulator 的情况下验证本类与驱动的调用约定。
        """
        if modules is None:
            ready, detail = simulator_ready()
            if not ready:
                raise RunnerUnavailable(detail)
            self._tg = types.SimpleNamespace(
                **{
                    name.rsplit(".", 1)[1]: importlib.import_module(name)
                    for name in TREASURE_GAME_MODULES
                }
            )
        else:
            self._tg = modules
        self._config = config
        self._map_cache: dict[tuple[object, ...], tuple[object, object, object]] = {}
        self.sub_checks: dict[str, object] = {}
        #: 地图生成累计耗时（每 map_seed 只生成一次，其余配对单元复用；供 JSON 的耗时外推）
        self.map_gen_ns_total = 0
        self.map_gen_calls = 0

    # ---- 地图：同一 map_seed 在所有配对单元/模式间复用（同 map_id、同出生点）----
    def _map_for(self, case: CaseSpec) -> tuple[object, object, object]:
        key = (
            case.map_seed,
            case.height,
            case.width,
            case.walls,
            case.treasures,
            case.traps,
        )
        cached = self._map_cache.get(key)
        if cached is not None:
            return cached
        rng = random.Random(case.map_seed)  # 地图 RNG 与骰子 RNG 相互独立
        map_started = time.perf_counter_ns()
        true_map, pos_a, pos_b = self._tg.map_module.MapModule.generate(
            case.height,
            case.width,
            case.walls,
            case.treasures,
            case.traps,
            rng,
            trap_value=-2,
        )
        self.map_gen_ns_total += time.perf_counter_ns() - map_started
        self.map_gen_calls += 1
        if pos_a == pos_b:
            raise ValueError(f"map seed {case.map_seed}: spawn points coincide at {pos_a}")
        for pos in (pos_a, pos_b):
            if true_map.is_wall(pos):
                raise ValueError(f"map seed {case.map_seed}: spawn point inside a wall: {pos}")
        self._map_cache[key] = (true_map, pos_a, pos_b)
        return self._map_cache[key]

    def run_case(self, case: CaseSpec) -> dict[str, object]:
        tg = self._tg
        true_map, pos_a, pos_b = self._map_for(case)
        mode = tg.models.GameMode(case.mode)
        initial_state = tg.models.GameState(
            true_map=true_map,
            pos_a=pos_a,
            pos_b=pos_b,
            score_a=0,
            score_b=0,
            turn=tg.models.Seat.A,
            mode=mode,
            turn_index=0,
        )
        ai = tg.ai_module.AiModule(
            depth=case.depth,
            candidate_limit=case.candidate_limit,
            partition_threshold=case.partition_threshold,
            use_cache=True,
            use_pruning=True,
        )
        human = tg.human_policy.HumanPolicy(
            strategy=case.human_strategy, rng=random.Random(case.dice_seed)
        )
        io = ViewHeuristicIO(human)
        simulator = tg.simulator.Simulator(
            initial_state=initial_state,
            io=io,
            rng=random.Random(case.dice_seed),
            policy_a=tg.simulator.SeatPolicy.HUMAN,
            policy_b=tg.simulator.SeatPolicy[MODE_MACHINE_POLICY[case.mode]],
            max_turns=case.max_turns,
            ai=ai,
            dice_seed_table=case.dice_table,
        )
        result = simulator.run_human_vs_ai(mode)
        metrics = dict(result.metrics)
        missing = [key for key in METRIC_KEYS_REQUIRED if key not in metrics]
        if missing:
            raise MetricContractError(
                "GameResult.metrics missing frozen keys from 06 s5.5: "
                f"{missing} (map_seed={case.map_seed}, dice_seed={case.dice_seed}, "
                f"mode={case.mode})"
            )
        dice_sequence = tuple(int(x) for x in metrics["dice_sequence"])
        return {
            "map_id": str(true_map.map_id),
            "winner": result.winner,
            "draw_timeout": bool(result.draw_timeout),
            "timed_out": bool(metrics["timed_out"]),
            "score_a": int(result.score_a),
            "score_b": int(result.score_b),
            "turns": int(result.turns),
            "trap_steps_a": int(metrics["trap_steps_a"]),
            "trap_steps_b": int(metrics["trap_steps_b"]),
            "treasure_steps_a": int(metrics["treasure_steps_a"]),
            "treasure_steps_b": int(metrics["treasure_steps_b"]),
            "battles": int(metrics["battles"]),
            "ai_calls": int(metrics["ai_calls"]),
            "ai_elapsed_ns": int(metrics["ai_elapsed_ns"]),
            "cache_hits": int(metrics["cache_hits"]),
            "cache_misses": int(metrics["cache_misses"]),
            "nodes": int(metrics["nodes"]),
            "partition_used": bool(metrics["partition_used"]),
            "dice_sequence": dice_sequence,
        }


# --------------------------------------------------------------------------------------
# 批量驱动
# --------------------------------------------------------------------------------------
def _format_progress(case: CaseSpec, rec: CaseRecord | None, wall_ns: int | None, done: int,
                     total: int) -> str:
    head = f"[run_batch] [{done:>3}/{total}] map={case.map_seed} dice={case.dice_seed} {case.mode:<6}"
    if rec is None:
        return f"{head} ERROR"
    ai_ms = "n/a" if rec.ai_elapsed_ns is None else f"{rec.ai_elapsed_ns / 1e6:.2f}ms"
    wall_ms = "n/a" if wall_ns is None else f"{wall_ns / 1e6:.1f}ms"
    flag = " TIMEOUT" if rec.draw_timeout else ""
    return (
        f"{head} winner={rec.winner or WINNER_DRAW:<4} score={rec.score_a}:{rec.score_b} "
        f"turns={rec.turns} traps={rec.trap_steps_a}/{rec.trap_steps_b} "
        f"battles={rec.battles} ai={rec.ai_calls} calls {ai_ms} wall={wall_ms}{flag}"
    )


def run_batch(
    config: BatchConfig,
    *,
    runner: object | None = None,
    progress: typing.Callable[[str], None] | None = None,
) -> BatchOutcome:
    """执行完整实验矩阵；``runner`` 注入用于阶段 1 单测与消融替换。"""
    config.validate()
    units = build_case_matrix(config)
    if runner is None:
        runner = SimulatorCaseRunner(config)
    if progress is None:
        emitter: typing.Callable[[str], None] = (
            (lambda _msg: None) if config.quiet else (lambda msg: print(msg, flush=True))
        )
    else:
        emitter = progress

    total_cases = sum(len(unit.cases) for unit in units)
    planned = total_cases
    records: list[CaseRecord] = []
    errors: list[dict[str, object]] = []
    aborted_units: set[int] = set()
    map_ids: dict[int, str] = {}
    started = time.perf_counter_ns()
    done = 0
    stop = False

    emitter(
        "[run_batch] plan: "
        f"units={len(units)} cases={planned} maps={config.maps} dice_seeds={config.dice_seeds} "
        f"map_seeds={list(derive_map_seeds(config))} dice_seeds={list(derive_dice_seeds(config))} "
        f"grid={config.height}x{config.width} walls={config.walls} treasures={config.treasures} "
        f"traps={config.traps} depth={config.depth} K={config.candidate_limit} "
        f"threshold={config.partition_threshold} max_turns={config.max_turns} "
        f"partition={config.partition} human={config.resolved_human_model_id}"
    )

    for unit in units:
        for case in unit.cases:
            done += 1
            if stop or unit.index in aborted_units:
                errors.append(
                    {
                        "map_seed": case.map_seed,
                        "dice_seed": case.dice_seed,
                        "mode": case.mode,
                        "unit_index": unit.index,
                        "error_type": "SkippedAfterGroupError",
                        "message": "同一配对单元已因异常停止（04：保留错误并停止该组，样本不静默丢弃）",
                    }
                )
                emitter(f"[run_batch] [{done:>3}/{planned}] skipped {case.mode} (unit aborted)")
                continue
            t0 = time.perf_counter_ns()
            try:
                outcome = runner.run_case(case)  # type: ignore[attr-defined]
                wall_ns = time.perf_counter_ns() - t0
                rec = record_from_outcome(case, outcome, wall_ns)
            except Exception as exc:  # noqa: BLE001 —— 保留异常类型，不做静默丢弃
                wall_ns = time.perf_counter_ns() - t0
                errors.append(
                    {
                        "map_seed": case.map_seed,
                        "dice_seed": case.dice_seed,
                        "mode": case.mode,
                        "unit_index": unit.index,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "wall_ns": wall_ns,
                    }
                )
                aborted_units.add(unit.index)
                emitter(
                    f"[run_batch] [{done:>3}/{planned}] map={case.map_seed} "
                    f"dice={case.dice_seed} {case.mode:<6} ERROR {type(exc).__name__}: "
                    f"{_console_safe(str(exc))}"
                )
                if config.fail_fast:
                    stop = True
                    break
                continue
            known = map_ids.setdefault(case.map_seed, rec.map_id)
            if known != rec.map_id:
                rec.checks["map_id_consistent"] = False
                errors.append(
                    {
                        "map_seed": case.map_seed,
                        "dice_seed": case.dice_seed,
                        "mode": case.mode,
                        "unit_index": unit.index,
                        "error_type": "MapIdMismatch",
                        "message": (
                            "同一 map_seed 出现不同 map_id："
                            f"{known!r} vs {rec.map_id!r}（地图需按 map_seed 复用同布局同出生点）"
                        ),
                    }
                )
            else:
                rec.checks["map_id_consistent"] = True
            records.append(rec)
            emitter(_format_progress(case, rec, wall_ns, done, planned))
        if stop:
            break

    elapsed_ns = time.perf_counter_ns() - started
    checks = _aggregate_checks(records, errors)
    summary = _build_summary(
        config,
        units,
        records,
        errors,
        checks,
        planned=planned,
        elapsed_ns=elapsed_ns,
        aborted_units=sorted(aborted_units),
        map_gen_ns_total=getattr(runner, "map_gen_ns_total", None),
        map_gen_calls=getattr(runner, "map_gen_calls", None),
    )
    return BatchOutcome(
        config=config,
        units=tuple(units),
        records=records,
        errors=errors,
        checks=checks,
        summary=summary,
        elapsed_ns=elapsed_ns,
    )


def _aggregate_checks(
    records: typing.Sequence[CaseRecord], errors: typing.Sequence[typing.Mapping[str, object]]
) -> dict[str, object]:
    names = sorted({key for rec in records for key in rec.checks})
    out: dict[str, object] = {}
    for name in names:
        values = [rec.checks.get(name) for rec in records]
        out[name] = {
            "ok": sum(1 for v in values if v is True),
            "failed": sum(1 for v in values if v is False),
            "unknown": sum(1 for v in values if v is None),
        }
    out["errors_by_type"] = {}
    for err in errors:
        key = str(err.get("error_type"))
        out["errors_by_type"][key] = out["errors_by_type"].get(key, 0) + 1  # type: ignore[union-attr]
    out["notes"] = {
        "dice_sequence_matches_table": "实际使用的骰序列必须是注入骰表的前缀（复现性硬不变量）",
        "dice_sequence_covers_turns": "骰序列长度与 turns 的口径一致性观测（容忍回合/手数两种口径）",
        "timed_out_consistent": "metrics['timed_out'] 必须等于 GameResult.draw_timeout",
        "partition_used_matches_config_condition": (
            "实测分治路径是否等于配置条件（EASY 恒 False；auto 下 NORMAL/HARD = 格数>=阈值）；"
            "不一致只说明 AiModule 内部另有判据，不视为错误"
        ),
        "map_id_consistent": "同一 map_seed 的所有行必须共用同一张地图指纹（同图配对的前提）",
        "failed_semantics": "failed 计数是「观测与预期不符」的诊断，不代表实验异常；异常只计入 errors",
    }
    return out


def _source_metadata() -> dict[str, object]:
    fingerprints: dict[str, str | None] = {}
    for rel in FINGERPRINT_FILES:
        path = SRC_DIR / rel
        try:
            fingerprints[rel] = (
                hashlib.sha1(path.read_bytes()).hexdigest()[:16] if path.is_file() else None
            )
        except OSError:
            fingerprints[rel] = None
    commit = branch = None
    dirty: bool | None = None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            commit = proc.stdout.strip() or None
            proc2 = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            branch = proc2.stdout.strip() if proc2.returncode == 0 else None
            proc3 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            dirty = bool(proc3.stdout.strip()) if proc3.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        commit = branch = None
    return {
        "source_commit": commit,
        "source_branch": branch,
        "source_dirty": dirty,
        "run_batch_version": RUN_BATCH_VERSION,
        "map_gen_version": MAP_GEN_VERSION,
        "dice_table_version": DICE_TABLE_VERSION,
        "stats_version": STATS_VERSION,
        "module_fingerprints_sha1_16": fingerprints,
    }


def _environment_metadata() -> dict[str, object]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
        "python_full_version": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "runtime_dependencies": ["python-stdlib-only"],
        "pytest_required": False,
        "note": "本摘要由 experiments/run_batch.py 单进程生成，与 pytest 无关",
    }


def _human_duration(ns: float | None) -> str | None:
    """给人类可读的耗时估计（粗估，仅用于报告说明）。"""
    if ns is None:
        return None
    seconds = float(ns) / 1e9
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60.0)
    if minutes < 60:
        return f"{int(minutes)}m {rest:.0f}s"
    hours, minutes = divmod(minutes, 60.0)
    return f"{int(hours)}h {int(minutes)}m"


def _arms_metadata(config: BatchConfig) -> dict[str, object]:
    """记录两组地图臂的元数据（Lead 要求：主批次 12×12 与 10×10 单体基线各自可查）。"""
    arms: dict[str, object] = {}
    for name, (height, width) in ARM_PRESETS.items():
        arm_config = dataclasses.replace(config, height=height, width=width, arm=name)
        arms[name] = {
            "grid": f"{height}x{width}",
            "area": height * width,
            "walls": arm_config.walls,
            "treasures": arm_config.treasures,
            "traps": arm_config.traps,
            "partition_threshold": arm_config.partition_threshold,
            "partition_enabled_by_mode": arm_config.partition_flags(),
            "role": ARM_ROLES[name],
            "reproduce": (
                f"python experiments/run_batch.py --arm {name} --seed {config.seed} "
                f"--maps {config.maps} --dice-seeds {config.dice_seeds} "
                f"--output {DEFAULT_OUTPUT_DIR}/batch_{name}_seed{config.seed}"
                f"_maps{config.maps}_dice{config.dice_seeds}_{height}x{width}"
                f"_w{arm_config.walls}t{arm_config.treasures}p{arm_config.traps}.csv"
            ),
            "is_current": name == config.arm_label,
        }
    return arms


def _timing_metadata(
    records: typing.Sequence[CaseRecord],
    *,
    elapsed_ns: int,
    map_gen_ns_total: int | None,
    map_gen_calls: int | None,
) -> dict[str, object]:
    """墙钟与全量外推（Lead 要求：把 270 局的预计耗时写进 JSON 摘要）。"""
    walls = [r.wall_ns for r in records if r.wall_ns is not None]
    per_case_mean = mean(walls)
    estimated = None if per_case_mean is None else per_case_mean * FULL_SCALE_CASES
    return {
        "wall_clock_ns": elapsed_ns,
        "wall_clock_human": _human_duration(elapsed_ns),
        "cases_run": len(records),
        "ns_per_case_mean": per_case_mean,
        "ns_per_case_median": median(walls),
        "ns_per_case_p95": percentile(walls, 95.0),
        "map_gen_ns_total": map_gen_ns_total,
        "map_gen_calls": map_gen_calls,
        "full_scale_cases": FULL_SCALE_CASES,
        "full_scale_basis": "30 map seeds × 3 dice seeds × 3 modes = 270 cases (docs/04)",
        "estimated_full_scale_ns": estimated,
        "estimated_full_scale_human": _human_duration(estimated),
        "estimate_note": (
            "粗估 = 本批实测每局平均墙钟 × 270；已含地图生成首次成本与模式混合，"
            "真实全量运行以墙钟为准（分治/OPT 开销随地图与局面变化）"
        ),
    }


def _build_summary(
    config: BatchConfig,
    units: typing.Sequence[PairedUnit],
    records: typing.Sequence[CaseRecord],
    errors: typing.Sequence[typing.Mapping[str, object]],
    checks: typing.Mapping[str, object],
    *,
    planned: int,
    elapsed_ns: int,
    aborted_units: typing.Sequence[int],
    map_gen_ns_total: int | None = None,
    map_gen_calls: int | None = None,
) -> dict[str, object]:
    map_seeds = list(derive_map_seeds(config))
    dice_seed_list = list(derive_dice_seeds(config))
    dice_tables: dict[str, object] = {}
    for unit in units:
        if str(unit.dice_seed) in dice_tables:
            continue
        table = unit.dice_table
        dice_tables[str(unit.dice_seed)] = {
            "length": len(table),
            "head": list(table[:8]),
            "sha1_16": hashlib.sha1(bytes(table)).hexdigest()[:16],
            "index_rule": "turn_index*2 + (0 if seat A else 1), modulo len(table)",
        }
    err_types: dict[str, int] = {}
    for err in errors:
        key = str(err.get("error_type"))
        err_types[key] = err_types.get(key, 0) + 1
    summary: dict[str, object] = {
        "schema_version": 1,
        "run_batch_version": RUN_BATCH_VERSION,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": _environment_metadata(),
        "source": _source_metadata(),
        "config": config.describe(),
        "seeds": {
            "root_seed": config.seed,
            "map_seeds": map_seeds,
            "dice_seeds": dice_seed_list,
            "map_seed_rule": "map_seed = root_seed + i",
            "dice_seed_rule": (
                f"dice_seed = root_seed + {DICE_SEED_OFFSET} + {DICE_SEED_STRIDE}*j"
            ),
            "rng_independence": {
                "map": "random.Random(map_seed)",
                "dice": "random.Random(dice_seed)",
                "human": "random.Random(dice_seed)",
                "search_consumes_rng": False,
            },
            "shared_across_arms": True,
            "shared_across_arms_note": (
                "main/baseline 两组地图臂使用同一 seed 清单，便于在同一张图的同源参数下"
                "配对比较单体 OPT 与分治（04「消融/分治」）"
            ),
        },
        "dice_tables": dice_tables,
        "matrix": {
            "paired_units": len(units),
            "modes_per_unit": len(MODES),
            "cases_planned": planned,
            "cases_run": len(records),
            "mode_order": list(MODES),
            "unit_key": "(map_seed, dice_seed)",
            "shared_within_unit": ["map_seed", "map_id", "spawns", "dice_seed", "dice_table"],
            "map_arm": {
                "label": config.arm_label,
                "grid": f"{config.height}x{config.width}",
                "area": config.map_area,
                "partition_threshold": config.partition_threshold,
                "partition_enabled_by_mode": config.partition_flags(),
                "role": ARM_ROLES.get(config.arm_label, "自定义网格臂"),
            },
            "arms": _arms_metadata(config),
            "controlled": {
                "depth": config.depth,
                "K": config.candidate_limit,
                "partition_threshold": config.partition_threshold,
                "max_turns": config.max_turns,
                "map_grid": f"{config.height}x{config.width}",
                "map_area": config.map_area,
                "walls": config.walls,
                "treasures": config.treasures,
                "traps": config.traps,
                "start_scores": {"score_a": 0, "score_b": 0},
                "first_turn": "A",
                "human_model_id": config.resolved_human_model_id,
            },
            "independent_variable": {
                "EASY": "greedy machine, full visibility",
                "NORMAL": "OPT machine, full visibility",
                "HARD": "OPT machine, hidden untriggered traps in the human view",
                "note": "EASY vs NORMAL 隔离算法差异；NORMAL vs HARD 隔离可见性差异；机器始终读真值",
            },
            "partition_mode": config.partition,
        },
        "csv": {
            "columns": list(CSV_COLUMNS),
            "rows": len(records),
            "winner_encoding": {
                "A": "human seat A wins",
                "B": "machine seat B wins",
                "DRAW": "non-timeout tie (winner is None)",
            },
            "bool_encoding": "True/False",
            "missing_encoding": "empty string (never imputed as 0)",
            "utf8": True,
            "line_terminator": "\\n",
        },
        "counts": {
            "cases_planned": planned,
            "cases_run": len(records),
            "cases_errored": sum(
                1 for e in errors if e.get("error_type") != "SkippedAfterGroupError"
            ),
            "cases_skipped_after_group_error": sum(
                1 for e in errors if e.get("error_type") == "SkippedAfterGroupError"
            ),
            "units_planned": len(units),
            "units_aborted": len(set(aborted_units)),
            "errors_by_type": err_types,
        },
        "timing": _timing_metadata(
            records,
            elapsed_ns=elapsed_ns,
            map_gen_ns_total=map_gen_ns_total,
            map_gen_calls=map_gen_calls,
        ),
        "aggregate": aggregate_records(records, config),
        "checks": dict(checks),
        "per_game": [rec.json_dict() for rec in records],
        "errors": [dict(err) for err in errors],
    }
    return summary


# --------------------------------------------------------------------------------------
# 写出
# --------------------------------------------------------------------------------------
def write_csv(path: pathlib.Path | str, records: typing.Sequence[CaseRecord]) -> int:
    """按冻结列序写 CSV（UTF-8、``\\n``、12 行 smoke 即 1 行表头 + 12 行数据）。"""
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec.csv_dict())
    return len(records)


def summary_path_for(csv_path: pathlib.Path | str) -> pathlib.Path:
    """``smoke.csv`` → ``smoke.summary.json``（同目录）。"""
    return pathlib.Path(csv_path).with_suffix(".summary.json")


def write_summary(path: pathlib.Path | str, payload: typing.Mapping[str, object]) -> pathlib.Path:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    target.write_text(text + "\n", encoding="utf-8")
    return target


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_batch.py",
        description="W4-01 批量配对定量仿真实验驱动（EASY/NORMAL/HARD × 地图 seed × 骰序 seed）",
    )
    parser.add_argument("--seed", type=int, default=42, help="根 seed（默认 42）")
    parser.add_argument("--maps", type=int, default=30, help="地图 seed 数（默认 30）")
    parser.add_argument(
        "--dice-seeds", type=int, default=3, help="每个地图的骰序 seed 数（默认 3）"
    )
    parser.add_argument(
        "--arm",
        choices=("auto", "main", "baseline"),
        default=DEFAULT_ARM,
        help=(
            "地图臂预设：main = 12×12（=分治阈值 144，NORMAL/HARD partition_enabled=True，"
            "默认）；baseline = 10×10（单体 OPT，partition_enabled 恒 False）"
        ),
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=None,
        help=(
            "CSV 输出路径（缺省按参数派生："
            f"{DEFAULT_OUTPUT_DIR}/{OUTPUT_FILENAME_CONVENTION}，不覆盖历史结果）"
        ),
    )
    parser.add_argument(
        "--summary",
        type=pathlib.Path,
        default=None,
        help="JSON 摘要路径（缺省 = CSV 同目录 <stem>.summary.json）",
    )
    parser.add_argument(
        "--height", type=int, default=None, help="地图行数（缺省取 --arm 预设：main 12 / baseline 10）"
    )
    parser.add_argument(
        "--width", type=int, default=None, help="地图列数（缺省取 --arm 预设：main 12 / baseline 10）"
    )
    parser.add_argument("--walls", type=int, default=16, help="墙数（默认 16）")
    parser.add_argument("--treasures", type=int, default=8, help="宝藏数（默认 8）")
    parser.add_argument("--traps", type=int, default=6, help="陷阱数（默认 6）")
    parser.add_argument("--depth", type=int, default=3, help="OPT 搜索深度（默认 3）")
    parser.add_argument(
        "--candidate-limit", type=int, default=24, help="OPT 候选上限 K（默认 24）"
    )
    parser.add_argument(
        "--partition-threshold",
        type=int,
        default=DEFAULT_PARTITION_THRESHOLD,
        help="分治阈值（默认 144 = 12×12；设 9999 可在同图上强制单体 OPT，用于同图同骰分治对照）",
    )
    parser.add_argument("--max-turns", type=int, default=500, help="单局回合上限（默认 500）")
    parser.add_argument(
        "--human-strategy",
        default=DEFAULT_HUMAN_STRATEGY,
        help=f"座位 A 视图启发式策略（默认 {DEFAULT_HUMAN_STRATEGY}）",
    )
    parser.add_argument(
        "--human-model-id",
        default="",
        help="human_model_id 列取值（缺省由 --human-strategy 派生：VIEW_HEURISTIC:<strategy>:v1）",
    )
    parser.add_argument(
        "--partition",
        choices=("auto", "on", "off"),
        default="auto",
        help="分治开关：auto = 按地图格数 >= 阈值（EASY 恒 False）",
    )
    parser.add_argument(
        "--bootstrap", type=int, default=2000, help="分簇 bootstrap 重抽样次数（默认 2000）"
    )
    parser.add_argument("--bootstrap-seed", type=int, default=None, help="bootstrap RNG seed")
    parser.add_argument("--fail-fast", action="store_true", help="首个异常即停止整批")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总行")
    return parser


def config_from_args(args: argparse.Namespace) -> BatchConfig:
    preset_height, preset_width = ARM_PRESETS[DEFAULT_ARM if args.arm == "auto" else args.arm]
    return BatchConfig(
        seed=args.seed,
        maps=args.maps,
        dice_seeds=args.dice_seeds,
        height=preset_height if args.height is None else args.height,
        width=preset_width if args.width is None else args.width,
        walls=args.walls,
        treasures=args.treasures,
        traps=args.traps,
        depth=args.depth,
        candidate_limit=args.candidate_limit,
        partition_threshold=args.partition_threshold,
        max_turns=args.max_turns,
        human_strategy=args.human_strategy,
        human_model_id=args.human_model_id,
        bootstrap_resamples=args.bootstrap,
        bootstrap_seed=args.bootstrap_seed,
        partition=args.partition,
        arm=args.arm,
        fail_fast=args.fail_fast,
        quiet=args.quiet,
    )


def default_output_path(config: BatchConfig) -> pathlib.Path:
    """按参数派生默认文件名（可反查 seed/地图/骰序/网格，避免覆盖历史结果）。

    非默认分治配置会追加后缀（``_thr9999`` / ``_poff``），因此「同网格同 seed 的
    分治 vs 单体对照」两次运行不会互相覆盖，也不需要手工指定 ``--output``。
    """
    suffix = ""
    if config.human_strategy != DEFAULT_HUMAN_STRATEGY:
        suffix += f"_{config.human_strategy.lower()}"
    if config.partition_threshold != DEFAULT_PARTITION_THRESHOLD:
        suffix += f"_thr{config.partition_threshold}"
    if config.partition != "auto":
        suffix += f"_p{config.partition}"
    return pathlib.Path(DEFAULT_OUTPUT_DIR) / (
        f"batch_{config.arm_label}_seed{config.seed}_maps{config.maps}"
        f"_dice{config.dice_seeds}_{config.height}x{config.width}"
        f"_w{config.walls}t{config.treasures}p{config.traps}{suffix}.csv"
    )


def _print_summary_lines(outcome: BatchOutcome) -> None:
    per_mode = outcome.summary["aggregate"]["per_mode"]  # type: ignore[index]
    for mode in MODES:
        stats = per_mode[mode]  # type: ignore[index]

        def fmt(value: object, digits: int = 3) -> str:
            return "n/a" if value is None else f"{float(value):.{digits}f}"

        p95_ms = (
            None if stats["ai_elapsed_ns_p95"] is None else float(stats["ai_elapsed_ns_p95"]) / 1e6
        )
        diff_text = _fmt_num(stats["score_diff_mean_scored"])
        if stats["timeouts"]:
            diff_text += f" (incl-timeout {_fmt_num(stats['score_diff_mean_incl_timeout'])})"
        print(
            f"[run_batch] mode {mode:<6} n={stats['games']} normal={stats['normal_games']} "
            f"wins/losses/ties={stats['wins_b']}/{stats['losses_b']}/{stats['ties']} "
            f"win_rate={fmt(stats['win_rate'])} score_diff={diff_text}"
            f"+-{_fmt_num(stats['score_diff_std_scored'])} "
            f"trap_a={fmt(stats['trap_steps_a_mean'])} "
            f"trap_b={fmt(stats['trap_steps_b_mean'])} ai_ms_p95={fmt(p95_ms, 2)} "
            f"timeouts={stats['timeouts']}",
            flush=True,
        )
    paired = outcome.summary["aggregate"]["paired"]  # type: ignore[index]
    for label in ("NORMAL-EASY", "HARD-NORMAL"):
        diff = paired[label]["score_diff"]  # type: ignore[index]
        print(
            f"[run_batch] paired {label:<12} score_diff mean={_fmt_num(diff['mean_diff'])} "
            f"ci95=[{_fmt_num(diff['ci_low'])}, {_fmt_num(diff['ci_high'])}] units={diff['units']} "
            f"clusters={diff['clusters']}",
            flush=True,
        )


def _fmt_num(value: object, digits: int = 4) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def main(argv: typing.Sequence[str] | None = None, *, runner: object | None = None) -> int:
    """CLI 入口；返回进程退出码（0 成功 / 1 有用例异常 / 2 配置非法 / 4 运行模块未落地）。"""
    try:  # 控制台/重定向下都稳定输出
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = config_from_args(args)
        config.validate()
    except BatchConfigError as exc:
        print(f"[run_batch] config error: {exc}", file=sys.stderr, flush=True)
        return 2

    try:
        outcome = run_batch(config, runner=runner)
    except RunnerUnavailable as exc:
        print(
            f"[run_batch] runtime modules not available yet (stage-1 expected): {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 4
    except BatchConfigError as exc:
        print(f"[run_batch] config error: {exc}", file=sys.stderr, flush=True)
        return 2

    csv_path = pathlib.Path(args.output) if args.output else default_output_path(config)
    json_path = pathlib.Path(args.summary) if args.summary else summary_path_for(csv_path)
    overwrote = csv_path.exists() or json_path.exists()
    outcome.summary["output"] = {
        "csv": str(csv_path),
        "summary_json": str(json_path),
        "overwrote_existing": overwrote,
        "filename_convention": f"{DEFAULT_OUTPUT_DIR}/{OUTPUT_FILENAME_CONVENTION}",
        "explicit_output": args.output is not None,
    }
    if overwrote:
        print(
            f"[run_batch] WARNING: target already existed and was overwritten: {csv_path}",
            flush=True,
        )
    rows = write_csv(csv_path, outcome.records)
    write_summary(json_path, outcome.summary)

    timing = outcome.summary["timing"]
    print(
        f"[run_batch] csv={csv_path} rows={rows} header=1 summary={json_path} "
        f"errors={len(outcome.errors)} "
        f"timeouts={outcome.summary['aggregate']['timeouts']['total']} "
        f"wall={outcome.elapsed_ns / 1e9:.2f}s "
        f"est_full_scale_{timing['full_scale_cases']}={timing['estimated_full_scale_human']}",
        flush=True,
    )
    _print_summary_lines(outcome)
    exit_code = outcome.exit_code
    print(
        f"[run_batch] done exit={exit_code} rows={rows} (含表头 {rows + 1} 行) "
        f"planned={outcome.summary['counts']['cases_planned']}",
        flush=True,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
