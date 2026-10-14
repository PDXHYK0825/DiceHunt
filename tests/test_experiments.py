#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""W4-01 实验驱动单测（Owner: experiments-dev）。

覆盖范围（阶段 1，不依赖 ``simulator.py`` 落地）：
* 冻结 CSV schema 的列名与顺序（20 列，独立于实现硬编码比对）；
* 实验矩阵规模：2×2 → 4 配对单元 × 3 模式 = 12 局；默认 30×3 → 90 单元 = 270 局；
* 配对单元内三模式共享同地图（同 map_seed/同 map_id）与同一骰序表；
* 地图 RNG 与骰子 RNG 相互独立、骰值表前缀稳定、按 ``(turn_index, seat)`` 索引；
* CLI ``--maps 2 --dice-seeds 2`` 写出 **1 行表头 + 12 行数据** + ``*.summary.json``；
* 统计口径：AI 胜率 = ``wins/(wins+losses+ties)``（平局贡献 0）、超时行不进分母、
  样本标准差 n−1、中位数/p95、按地图 seed 分簇的 bootstrap 95% 区间；
* 截尾口径：缺失耗时输出空串且不参与统计（绝不填 0）；
* 异常口径：保留错误类型、停止该配对单元、样本不静默丢弃；
* ``GameResult.metrics`` 拒绝缺键（06 §5.5）。

阶段 2 接通后仍成立：``test_real_simulator_*`` 用 ``pytest.mark.skipif`` 守卫，
``treasure_game`` 落地即自动生效；其余用例通过注入假 runner 与真实 Simulator 解耦。
"""

from __future__ import annotations

import csv
import dataclasses
import enum
import hashlib
import json
import math
import pathlib
import random
import sys
import types
import typing

import pytest

# --------------------------------------------------------------------------------------
# 路径引导：run_tests.cmd 已注入 PYTHONPATH，这里再显式兜底以支持直接 pytest 本文件
# --------------------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve()
PROJECT_ROOT = _HERE.parents[1]           # <repo>/Project
REPO_ROOT = _HERE.parents[2]              # <repo>
SRC_DIR = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import experiments.run_batch as rb  # noqa: E402  （必须在 sys.path 引导之后导入）

assert pathlib.Path(rb.__file__).resolve() == (
    PROJECT_ROOT / "experiments" / "run_batch.py"
).resolve(), f"导入了错误的 experiments.run_batch：{rb.__file__}"

#: 04-test-plan「记录 schema」逐字硬编码（测试不依赖实现里的常量，防实现自证）
FROZEN_COLUMNS = (
    "map_seed,dice_seed,map_id,mode,depth,K,partition_enabled,human_model_id,"
    "score_a,score_b,winner,draw_timeout,turns,trap_steps_a,trap_steps_b,battles,"
    "ai_calls,ai_elapsed_ns,cache_hits,cache_misses"
).split(",")

SIM_READY, SIM_DETAIL = rb.simulator_ready()

#: map_module 单独的就绪探针：用于「同图/同出生点」控制条件的真实生成器校验
try:  # pragma: no cover - 取决于队友模块落地进度
    from treasure_game.map_module import MapModule as _RealMapModule

    MAP_READY, MAP_DETAIL = True, "ready"
except Exception as _exc:  # noqa: BLE001
    _RealMapModule = None  # type: ignore[assignment]
    MAP_READY, MAP_DETAIL = False, f"{type(_exc).__name__}: {_exc}"

#: human_policy 单独的就绪探针：A 座位视图代理可以早于 Simulator 先接通验证
try:  # pragma: no cover - 取决于队友模块落地进度
    from treasure_game.human_policy import HumanPolicy as _RealHumanPolicy

    HUMAN_READY, HUMAN_DETAIL = True, "ready"
except Exception as _exc:  # noqa: BLE001
    _RealHumanPolicy = None  # type: ignore[assignment]
    HUMAN_READY, HUMAN_DETAIL = False, f"{type(_exc).__name__}: {_exc}"


# --------------------------------------------------------------------------------------
# 假 runner：确定性伪观测，不 import treasure_game
# --------------------------------------------------------------------------------------
def fake_map_id(case: "rb.CaseSpec") -> str:
    """只依赖地图参数 + map_seed —— 与真实 map_id 的语义一致（同图同指纹）。"""
    payload = (
        f"{case.height}x{case.width}|w{case.walls}|t{case.treasures}|p{case.traps}|s{case.map_seed}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def fake_outcome(
    case: "rb.CaseSpec",
    *,
    timeout: bool = False,
    elapsed: bool = True,
    map_id: str | None = None,
) -> dict[str, object]:
    mode_index = rb.MODES.index(case.mode)
    base = (case.map_seed * 31 + case.dice_seed * 17 + mode_index * 7) % 4096
    score_a = base % 13
    score_b = (base * 7 + 5) % 17
    if timeout:
        winner: str | None = None
        turns = case.max_turns
    else:
        winner = "B" if score_b > score_a else ("A" if score_a > score_b else None)
        turns = 12 + (base % 23)
    return {
        "map_id": map_id if map_id is not None else fake_map_id(case),
        "winner": winner,
        "draw_timeout": timeout,
        "timed_out": timeout,
        "score_a": score_a,
        "score_b": score_b,
        "turns": turns,
        "trap_steps_a": base % 4,
        "trap_steps_b": (base + 1) % 3,
        "treasure_steps_a": 1 + base % 5,
        "treasure_steps_b": 1 + (base + 2) % 5,
        "battles": base % 3,
        "ai_calls": turns,
        "ai_elapsed_ns": (1_000_000 + base * 1000) if elapsed else None,
        "cache_hits": base % 11,
        "cache_misses": (base + 3) % 7,
        "nodes": 100 + base,
        "partition_used": case.partition_enabled,
        "dice_sequence": list(case.dice_table[: 2 * turns]),
    }


class FakeRunner:
    """注入式假 runner：可指定失败单元 / 强制超时 / 缺失耗时 / 伪造 map_id。"""

    def __init__(
        self,
        *,
        fail_for: "typing.Iterable[tuple[int, int, str]]" = (),
        force_timeout_for: "typing.Iterable[tuple[int, int, str]]" = (),
        missing_elapsed_for: "typing.Iterable[tuple[int, int, str]]" = (),
        map_id_of: "typing.Callable[[rb.CaseSpec], str] | None" = None,
    ) -> None:
        self.fail_for = set(fail_for)
        self.force_timeout_for = set(force_timeout_for)
        self.missing_elapsed_for = set(missing_elapsed_for)
        self.map_id_of = map_id_of
        self.calls: list[rb.CaseSpec] = []

    def run_case(self, case: "rb.CaseSpec") -> dict[str, object]:
        self.calls.append(case)
        key = case.row_key
        if key in self.fail_for:
            raise RuntimeError(f"fake failure for {key}")
        return fake_outcome(
            case,
            timeout=key in self.force_timeout_for,
            elapsed=key not in self.missing_elapsed_for,
            map_id=None if self.map_id_of is None else self.map_id_of(case),
        )


def quiet_run(config: "rb.BatchConfig", runner: object) -> "rb.BatchOutcome":
    return rb.run_batch(config, runner=runner, progress=lambda _msg: None)


def read_rows(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------------------
# 1. schema / 矩阵规模
# --------------------------------------------------------------------------------------
def test_csv_schema_is_frozen_20_columns_in_order() -> None:
    assert ",".join(rb.CSV_COLUMNS) == ",".join(FROZEN_COLUMNS)
    assert len(rb.CSV_COLUMNS) == 20


def test_smoke_matrix_is_four_paired_units_and_twelve_cases() -> None:
    config = rb.BatchConfig(seed=42, maps=2, dice_seeds=2)
    units = rb.build_case_matrix(config)
    assert len(units) == 4
    assert [len(unit.cases) for unit in units] == [3, 3, 3, 3]
    cases = [case for unit in units for case in unit.cases]
    assert len(cases) == 12
    assert [case.mode for case in cases] == ["EASY", "NORMAL", "HARD"] * 4
    assert len({case.row_key for case in cases}) == 12
    assert len({(unit.map_seed, unit.dice_seed) for unit in units}) == 4
    assert rb.BatchConfig().maps == 30 and rb.BatchConfig().dice_seeds == 3


def test_default_scale_is_270_games() -> None:
    config = rb.BatchConfig()  # 30 地图 seed × 3 骰序 seed
    units = rb.build_case_matrix(config)
    assert len(units) == 90
    assert sum(len(unit.cases) for unit in units) == 270
    assert len({unit.map_seed for unit in units}) == 30
    assert len({unit.dice_seed for unit in units}) == 3


def test_paired_unit_shares_map_seed_and_dice_table() -> None:
    config = rb.BatchConfig(seed=42, maps=2, dice_seeds=2)
    units = rb.build_case_matrix(config)
    for unit in units:
        assert {case.map_seed for case in unit.cases} == {unit.map_seed}
        assert {case.dice_seed for case in unit.cases} == {unit.dice_seed}
        # 三模式共用同一张骰序表（同一对象、同一内容）
        assert len({id(case.dice_table) for case in unit.cases}) == 1
        assert all(case.dice_table == unit.dice_table for case in unit.cases)
        assert unit.dice_table == rb.make_dice_table(unit.dice_seed, config.max_turns)
    tables = {unit.dice_seed: unit.dice_table for unit in units}
    assert len({tables[k] for k in tables}) == 2  # 两个骰序 seed → 两张不同的表


def test_matrix_reproducible_and_prefix_stable() -> None:
    small = rb.build_case_matrix(rb.BatchConfig(seed=42, maps=2, dice_seeds=2))
    again = rb.build_case_matrix(rb.BatchConfig(seed=42, maps=2, dice_seeds=2))
    assert [unit.map_seed for unit in small] == [unit.map_seed for unit in again] == [42, 42, 43, 43]
    large = rb.build_case_matrix(rb.BatchConfig(seed=42, maps=5, dice_seeds=2))
    assert [unit.map_seed for unit in large][:4] == [unit.map_seed for unit in small]
    assert [unit.dice_seed for unit in large][:4] == [unit.dice_seed for unit in small]


# --------------------------------------------------------------------------------------
# 2. RNG 独立性 / 骰值表索引
# --------------------------------------------------------------------------------------
def test_dice_table_depends_only_on_dice_seed() -> None:
    table = rb.make_dice_table(100045, 50)
    # 期间大量消耗地图 RNG（random.Random(42)）不得影响骰表
    map_rng = random.Random(42)
    for _ in range(10_000):
        map_rng.randint(0, 10**6)
    assert rb.make_dice_table(100045, 50) == table
    assert rb.make_dice_table(100052, 50) != table
    assert set(table) <= {1, 2, 3, 4, 5, 6}
    # 前缀稳定：max_turns 变大不改变已有骰序
    assert rb.make_dice_table(100045, 10) == table[:20]
    assert len(table) == 100


def test_dice_table_is_indexed_by_turn_index_and_seat() -> None:
    table = rb.make_dice_table(7, 4)  # 长度 8
    assert rb.dice_index(0, "A") == 0
    assert rb.dice_index(0, "B") == 1
    assert rb.dice_index(3, "B") == 7
    ordered = [rb.dice_at(table, turn, seat) for turn in range(4) for seat in ("A", "B")]
    assert ordered == list(table)
    assert rb.dice_at(table, 4, "A") == table[0]  # 06 §3：取模索引
    with pytest.raises(ValueError):
        rb.dice_index(-1, "A")
    with pytest.raises(ValueError):
        rb.dice_index(0, "C")


def test_seed_lists_are_disjoint_and_recorded() -> None:
    config = rb.BatchConfig(seed=42, maps=30, dice_seeds=3)
    map_seeds = rb.derive_map_seeds(config)
    dice_seeds = rb.derive_dice_seeds(config)
    assert map_seeds == tuple(range(42, 72))
    assert not set(map_seeds) & set(dice_seeds)


# --------------------------------------------------------------------------------------
# 3. 分治开关 / 配置校验
# --------------------------------------------------------------------------------------
def test_partition_flag_follows_threshold_and_mode() -> None:
    small = rb.BatchConfig(seed=42, maps=1, dice_seeds=1, height=10, width=10)
    flags = {case.mode: case.partition_enabled for case in rb.build_case_matrix(small)[0].cases}
    assert flags == {"EASY": False, "NORMAL": False, "HARD": False}  # 100 < 144
    big = rb.BatchConfig(
        seed=42,
        maps=1,
        dice_seeds=1,
        height=12,
        width=12,
        walls=20,
        treasures=10,
        traps=8,
        partition_threshold=144,
    )
    flags = {case.mode: case.partition_enabled for case in rb.build_case_matrix(big)[0].cases}
    assert flags == {"EASY": False, "NORMAL": True, "HARD": True}  # 144 >= 144
    forced = dataclasses.replace(small, partition="on")
    flags = {case.mode: case.partition_enabled for case in rb.build_case_matrix(forced)[0].cases}
    assert flags == {"EASY": True, "NORMAL": True, "HARD": True}
    off = dataclasses.replace(big, partition="off")
    flags = {case.mode: case.partition_enabled for case in rb.build_case_matrix(off)[0].cases}
    assert flags == {"EASY": False, "NORMAL": False, "HARD": False}


def test_default_arm_is_main_12x12_and_baseline_is_10x10() -> None:
    """Lead 裁决：主批次 12×12（= 阈值 144 → NORMAL/HARD 启用分治）；基线 10×10 单体。"""
    main = rb.BatchConfig()
    assert (main.height, main.width) == (12, 12)
    assert main.map_area == main.partition_threshold == 144
    assert main.arm_label == "main"
    assert main.partition_flags() == {"EASY": False, "NORMAL": True, "HARD": True}
    assert main.walls + main.treasures + main.traps + 2 <= main.map_area

    baseline = rb.BatchConfig(arm="baseline", height=10, width=10)
    assert baseline.arm_label == "baseline"
    assert baseline.partition_flags() == {"EASY": False, "NORMAL": False, "HARD": False}
    # 未显式给 arm 时按网格推导
    assert rb.BatchConfig(height=10, width=10).arm_label == "baseline"
    assert rb.BatchConfig(height=14, width=14).arm_label == "custom-14x14"
    with pytest.raises(rb.BatchConfigError):
        rb.BatchConfig(arm="tiny").validate()


def test_default_output_filename_is_parameter_derived() -> None:
    """结果文件名必须能反查 seed/地图/骰序/网格，避免覆盖历史结果。"""
    path = rb.default_output_path(rb.BatchConfig(seed=7, maps=6, dice_seeds=3))
    assert path.name == "batch_main_seed7_maps6_dice3_12x12_w16t8p6.csv"
    assert path.parent.as_posix() == "experiments/results"
    baseline_path = rb.default_output_path(
        rb.BatchConfig(seed=7, maps=6, dice_seeds=3, arm="baseline", height=10, width=10)
    )
    assert baseline_path.name.startswith("batch_baseline_seed7_maps6_dice3_10x10")
    assert baseline_path != path
    # 同网格同 seed 的分治/单体对照不得互相覆盖：非默认阈值或显式分治开关加后缀
    mono_path = rb.default_output_path(rb.BatchConfig(seed=7, maps=6, dice_seeds=3,
                                                      partition_threshold=9999))
    assert mono_path.name.endswith("_thr9999.csv")
    assert mono_path != path
    off_path = rb.default_output_path(
        rb.BatchConfig(seed=7, maps=6, dice_seeds=3, partition="off")
    )
    assert off_path.name.endswith("_poff.csv")
    assert off_path != path


def test_cli_arm_baseline_is_single_body_opt(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "baseline.csv"
    code = rb.main(
        [
            "--arm",
            "baseline",
            "--seed",
            "42",
            "--maps",
            "1",
            "--dice-seeds",
            "1",
            "--output",
            str(out),
            "--bootstrap",
            "50",
            "--quiet",
        ],
        runner=FakeRunner(),
    )
    assert code == 0
    rows = read_rows(out)
    assert len(rows) == 3
    assert {row["partition_enabled"] for row in rows} == {"False"}
    payload = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert payload["config"]["arm"] == "baseline"
    assert payload["config"]["map_grid"] == "10x10"
    assert payload["config"]["map_area"] == 100
    assert payload["matrix"]["map_arm"]["partition_enabled_by_mode"] == {
        "EASY": False,
        "NORMAL": False,
        "HARD": False,
    }
    # JSON 摘要同时记录两组地图臂的元数据（主批次 12×12 + 基线 10×10）
    arms = payload["matrix"]["arms"]
    assert arms["main"]["grid"] == "12x12" and arms["main"]["area"] == 144
    assert arms["main"]["partition_enabled_by_mode"]["NORMAL"] is True
    assert arms["baseline"]["grid"] == "10x10" and arms["baseline"]["area"] == 100
    assert arms["baseline"]["partition_enabled_by_mode"]["NORMAL"] is False
    assert arms["baseline"]["is_current"] is True and arms["main"]["is_current"] is False
    assert "--arm baseline" in arms["baseline"]["reproduce"]
    assert "--arm main" in arms["main"]["reproduce"]


def test_cli_arm_main_is_twelve_by_twelve_with_partition(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "main.csv"
    code = rb.main(
        ["--maps", "1", "--dice-seeds", "1", "--output", str(out), "--bootstrap", "50", "--quiet"],
        runner=FakeRunner(),
    )
    assert code == 0
    rows = read_rows(out)
    flags = {row["mode"]: row["partition_enabled"] for row in rows}
    assert flags == {"EASY": "False", "NORMAL": "True", "HARD": "True"}
    payload = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert payload["config"]["arm"] == "main"
    assert payload["config"]["map_grid"] == "12x12"
    assert payload["matrix"]["map_arm"]["area"] == 144
    assert payload["matrix"]["arms"]["main"]["is_current"] is True
    # 显式 --height/--width 覆盖 arm 预设
    over = tmp_path / "over.csv"
    assert (
        rb.main(
            [
                "--arm", "main", "--height", "14", "--width", "14",
                "--maps", "1", "--dice-seeds", "1", "--output", str(over),
                "--bootstrap", "20", "--quiet",
            ],
            runner=FakeRunner(),
        )
        == 0
    )
    payload = json.loads(over.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert payload["config"]["map_grid"] == "14x14"
    assert payload["config"]["arm"] == "main"  # 显式 arm 优先于网格推导


def test_summary_timing_extrapolation_and_overwrite_flag(tmp_path: pathlib.Path) -> None:
    """Lead 要求：JSON 摘要要给出 270 局预计耗时，并记录是否覆盖了历史结果。"""
    out = tmp_path / "timed.csv"
    argv = [
        "--maps", "2", "--dice-seeds", "1", "--output", str(out),
        "--bootstrap", "20", "--quiet",
    ]
    assert rb.main(argv, runner=FakeRunner()) == 0
    payload = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    timing = payload["timing"]
    assert timing["cases_run"] == 6
    assert timing["full_scale_cases"] == 270
    assert timing["ns_per_case_mean"] is not None
    assert timing["estimated_full_scale_ns"] == pytest.approx(
        timing["ns_per_case_mean"] * 270
    )
    assert timing["estimated_full_scale_human"]
    assert payload["output"]["overwrote_existing"] is False
    assert payload["output"]["explicit_output"] is True
    assert payload["output"]["csv"] == str(out)

    # 第二次写同一路径 → 记录被覆盖（不静默假装是新结果）
    assert rb.main(argv, runner=FakeRunner()) == 0
    payload2 = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert payload2["output"]["overwrote_existing"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"maps": 0},
        {"dice_seeds": 0},
        {"height": 3, "width": 3, "walls": 99},
        {"depth": 0},
        {"max_turns": 0},
        {"partition": "sometimes"},
        {"bootstrap_resamples": -1},
        {"arm": "tiny"},
    ],
)
def test_invalid_config_raises(kwargs: dict[str, object]) -> None:
    with pytest.raises(rb.BatchConfigError):
        rb.BatchConfig(**kwargs).validate()  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# 4. CLI：12 行 smoke（表头 + 12 行）+ JSON 摘要
# --------------------------------------------------------------------------------------
def test_cli_smoke_writes_header_plus_12_rows(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "results" / "smoke.csv"
    code = rb.main(
        [
            "--seed",
            "42",
            "--maps",
            "2",
            "--dice-seeds",
            "2",
            "--output",
            str(out),
            "--bootstrap",
            "200",
            "--quiet",
        ],
        runner=FakeRunner(),
    )
    assert code == 0
    assert out.is_file()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 13, "smoke 必须是 1 行表头 + 12 行数据"
    assert lines[0] == ",".join(FROZEN_COLUMNS)
    rows = read_rows(out)
    assert len(rows) == 12
    assert [row["mode"] for row in rows] == ["EASY", "NORMAL", "HARD"] * 4
    # 每配对单元三模式：同 map_seed / 同 dice_seed / 同 map_id / 同分隔与超参
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault((row["map_seed"], row["dice_seed"]), []).append(row)
    assert len(groups) == 4
    for group in groups.values():
        assert len(group) == 3
        assert len({row["map_id"] for row in group}) == 1
        assert len({row["dice_seed"] for row in group}) == 1
        assert len({row["map_seed"] for row in group}) == 1
    assert {row["depth"] for row in rows} == {"3"}
    assert {row["K"] for row in rows} == {"24"}
    assert {row["human_model_id"] for row in rows} == {"VIEW_HEURISTIC:GATHER:v1"}
    assert {row["winner"] for row in rows} <= {"A", "B", "DRAW"}
    assert {row["draw_timeout"] for row in rows} <= {"True", "False"}
    assert all(row["ai_elapsed_ns"] != "" for row in rows)
    # JSON 摘要与 CSV 同目录
    summary_path = out.with_suffix(".summary.json")
    assert summary_path.is_file()


def test_smoke_twelve_rows_via_api(tmp_path: pathlib.Path) -> None:
    """12 行 smoke 的可断言包装：阶段 2 接通后仍用同一断言。"""
    config = rb.BatchConfig(seed=42, maps=2, dice_seeds=2, bootstrap_resamples=100, quiet=True)
    runner = FakeRunner()
    outcome = quiet_run(config, runner)
    assert len(outcome.records) == 12
    assert outcome.summary["counts"]["cases_planned"] == 12
    assert outcome.summary["counts"]["cases_run"] == 12
    assert outcome.summary["matrix"]["cases_planned"] == 12
    assert len(runner.calls) == 12
    assert len(outcome.errors) == 0
    assert outcome.exit_code == 0
    assert rb.write_csv(tmp_path / "smoke.csv", outcome.records) == 12
    rows = read_rows(tmp_path / "smoke.csv")
    assert len(rows) == 12
    # 同一 map_seed 的地图在所有行上一致
    per_map = {row["map_seed"]: row["map_id"] for row in rows}
    assert per_map["42"] == fake_map_id(runner.calls[0])
    assert rb.summary_path_for(tmp_path / "smoke.csv").name == "smoke.summary.json"


def test_summary_json_contract(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "smoke.csv"
    code = rb.main(
        [
            "--seed",
            "42",
            "--maps",
            "2",
            "--dice-seeds",
            "2",
            "--output",
            str(out),
            "--bootstrap",
            "100",
            "--quiet",
        ],
        runner=FakeRunner(),
    )
    assert code == 0
    payload = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["environment"]["python_version"]
    assert payload["environment"]["platform"]
    assert payload["environment"]["pytest_required"] is False
    assert payload["config"]["depth"] == 3
    assert payload["config"]["K"] == 24
    assert payload["config"]["partition_threshold"] == 144
    assert payload["config"]["max_turns"] == 500
    assert payload["seeds"]["root_seed"] == 42
    assert payload["seeds"]["map_seeds"] == [42, 43]
    assert payload["seeds"]["dice_seeds"] == [100045, 100052]
    assert payload["seeds"]["rng_independence"]["search_consumes_rng"] is False
    assert payload["csv"]["columns"] == FROZEN_COLUMNS
    assert payload["csv"]["rows"] == 12
    assert payload["counts"]["cases_run"] == 12
    assert payload["counts"]["cases_errored"] == 0
    assert set(payload["aggregate"]["per_mode"]) == {"EASY", "NORMAL", "HARD"}
    assert payload["aggregate"]["timeouts"]["total"] == 0
    assert payload["aggregate"]["timeouts"]["excluded_from_win_rate_denominator"] is True
    assert set(payload["aggregate"]["paired"]) == {"NORMAL-EASY", "HARD-NORMAL"}
    assert len(payload["per_game"]) == 12
    assert payload["dice_tables"]["100045"]["length"] == 1000
    assert payload["dice_tables"]["100045"]["index_rule"].startswith("turn_index*2")
    assert payload["errors"] == []
    assert payload["source"]["dice_table_version"].startswith("dice-table/v1")


def test_cli_config_error_returns_2_and_writes_nothing(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "never.csv"
    assert rb.main(["--maps", "0", "--output", str(out)], runner=FakeRunner()) == 2
    assert not out.exists()
    assert (
        rb.main(["--maps", "1", "--dice-seeds", "1", "--walls", "999", "--output", str(out)],
                runner=FakeRunner())
        == 2
    )
    assert not out.exists()


@pytest.mark.skipif(SIM_READY, reason=f"treasure_game 已落地：{SIM_DETAIL}")
def test_cli_without_simulator_reports_unavailable(tmp_path: pathlib.Path) -> None:
    """阶段 1：真实 runner 不可用时必须显式报「未落地」（退出码 4），不得伪造数据。"""
    out = tmp_path / "stage1.csv"
    assert rb.main(["--maps", "1", "--dice-seeds", "1", "--output", str(out), "--quiet"]) == 4
    assert not out.exists()
    assert not out.with_suffix(".summary.json").exists()


# --------------------------------------------------------------------------------------
# 5. 统计口径
# --------------------------------------------------------------------------------------
def test_statistics_helpers_match_conventions() -> None:
    assert rb.mean([1, 2, 3, 4]) == pytest.approx(2.5)
    assert rb.mean([]) is None
    assert rb.median([3, 1, 2]) == 2
    assert rb.median([4, 1, 2, 3]) == pytest.approx(2.5)
    assert rb.median([]) is None
    # 样本标准差（n−1）：[1,2,3,4] → sqrt(5/3)
    assert rb.sample_std([1, 2, 3, 4]) == pytest.approx(math.sqrt(5.0 / 3.0))
    assert rb.sample_std([1]) is None  # n<2 不假装是 0
    assert rb.sample_std([]) is None
    # 线性插值分位数
    assert rb.percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert rb.percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert rb.percentile([7], 95) == 7
    assert rb.percentile([], 50) is None
    with pytest.raises(ValueError):
        rb.percentile([1, 2], 101)
    # 含 None 的输入只在存在样本上统计
    assert rb.mean([None, 2, 4]) == pytest.approx(3.0)


def test_win_rate_denominator_counts_wins_losses_ties_only() -> None:
    config = rb.BatchConfig(seed=42, maps=2, dice_seeds=1, bootstrap_resamples=50, quiet=True)
    cases = {case.row_key: case for unit in rb.build_case_matrix(config) for case in unit.cases}
    timeout_key = (43, 100045, "EASY")
    assert timeout_key in cases
    normal_cases = [case for key, case in cases.items() if case.mode == "EASY" and key != timeout_key]
    wins = sum(1 for case in normal_cases if fake_outcome(case)["winner"] == "B")
    losses = sum(1 for case in normal_cases if fake_outcome(case)["winner"] == "A")
    ties = len(normal_cases) - wins - losses
    assert wins + losses + ties == len(normal_cases) == 1

    outcome = quiet_run(config, FakeRunner(force_timeout_for={timeout_key}))
    stats = outcome.summary["aggregate"]["per_mode"]["EASY"]
    assert stats["games"] == 2
    assert stats["timeouts"] == 1
    assert stats["normal_games"] == 1
    assert stats["wins_b"] == wins and stats["losses_b"] == losses and stats["ties"] == ties
    assert stats["win_rate_denominator"] == wins + losses + ties  # 超时行不在分母
    assert stats["win_rate"] == pytest.approx(wins / (wins + losses + ties))
    assert stats["timeout_rate"] == pytest.approx(0.5)
    assert outcome.summary["aggregate"]["timeouts"]["by_mode"]["EASY"] == 1


def test_all_timeout_mode_has_no_win_rate_denominator() -> None:
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1, bootstrap_resamples=50, quiet=True)
    key = (42, 100045, "NORMAL")
    outcome = quiet_run(config, FakeRunner(force_timeout_for={key}))
    stats = outcome.summary["aggregate"]["per_mode"]["NORMAL"]
    assert stats["win_rate_denominator"] == 0
    assert stats["win_rate"] is None  # 分母 0 → 不假装是 0 胜率
    assert stats["timeout_rate"] == 1.0
    # 超时保守区间（04：同时报告含超时的保守范围）
    assert stats["win_rate_timeouts_as_losses"] == 0.0
    assert stats["win_rate_timeouts_as_wins"] == 1.0
    row = next(rec for rec in outcome.records if rec.mode == "NORMAL")
    assert row.winner is None and row.draw_timeout is True
    assert row.csv_dict()["winner"] == "DRAW"
    assert row.csv_dict()["draw_timeout"] == "True"


def test_missing_elapsed_is_not_zero_filled(tmp_path: pathlib.Path) -> None:
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1, bootstrap_resamples=50, quiet=True)
    missing_key = (42, 100045, "EASY")
    outcome = quiet_run(config, FakeRunner(missing_elapsed_for={missing_key}))
    row = next(rec for rec in outcome.records if rec.mode == "EASY")
    assert row.ai_elapsed_ns is None
    assert row.csv_dict()["ai_elapsed_ns"] == ""  # 空串，绝不是 0
    path = tmp_path / "missing.csv"
    rb.write_csv(path, outcome.records)
    csv_row = next(r for r in read_rows(path) if r["mode"] == "EASY")
    assert csv_row["ai_elapsed_ns"] == ""
    assert csv_row["ai_calls"] != ""
    stats = outcome.summary["aggregate"]["per_mode"]["EASY"]
    assert stats["ai_elapsed_ns_missing"] == 1
    assert stats["ai_elapsed_ns_present"] == 0
    assert stats["ai_elapsed_ns_mean"] is None
    assert stats["ai_elapsed_ns_median"] is None
    assert stats["ai_elapsed_ns_p95"] is None
    # 另外两个模式仍有耗时样本
    assert outcome.summary["aggregate"]["per_mode"]["NORMAL"]["ai_elapsed_ns_present"] == 1


def test_bootstrap_cluster_ci_is_deterministic_and_clustered() -> None:
    clusters = {42: [1.0, 1.0, 1.0], 43: [5.0, 5.0, 5.0]}
    first = rb.bootstrap_cluster_ci(clusters, resamples=400, rng=random.Random(1))
    second = rb.bootstrap_cluster_ci(clusters, resamples=400, rng=random.Random(1))
    assert first == second  # 固定 rng → 完全可复现
    assert first["clusters"] == 2 and first["units"] == 6
    assert first["point_estimate"] == pytest.approx(3.0)
    assert 1.0 <= first["ci_low"] <= first["ci_high"] <= 5.0
    assert first["method"] == "cluster-bootstrap by map_seed"
    # 单簇 → 重抽样不引入簇间方差
    single = rb.bootstrap_cluster_ci({42: [1.0, 3.0]}, resamples=200, rng=random.Random(1))
    assert single["ci_low"] == single["ci_high"] == pytest.approx(2.0)
    assert rb.bootstrap_cluster_ci({}, resamples=10)["point_estimate"] is None
    assert rb.bootstrap_cluster_ci({42: [1.0]}, resamples=0)["ci_low"] is None


def test_paired_analysis_is_grouped_by_map_seed() -> None:
    config = rb.BatchConfig(seed=42, maps=3, dice_seeds=2, bootstrap_resamples=150, quiet=True)
    outcome = quiet_run(config, FakeRunner())
    paired = outcome.summary["aggregate"]["paired"]
    assert set(paired) == {"NORMAL-EASY", "HARD-NORMAL"}
    for label in paired:
        for variable in ("score_diff", "trap_steps_a", "trap_steps_b", "battles", "win_b"):
            entry = paired[label][variable]
            assert entry["units"] == 6  # 3 地图 × 2 骰序
            assert entry["clusters"] == 3  # 按 map_seed 分簇
            assert entry["ci_low"] is not None and entry["ci_high"] is not None
            assert entry["ci_low"] <= entry["ci_high"]
    # 分簇数量与地图 seed 数一致（不是把 6 个单元当独立样本）
    assert paired["NORMAL-EASY"]["score_diff"]["clusters"] == 3
    assert paired["NORMAL-EASY"]["score_diff"]["bootstrap"]["resamples"] == 150
    assert outcome.summary["aggregate"]["winner_rates_note"] == rb.WIN_RATE_CONVENTION
    assert "0" in rb.WIN_RATE_CONVENTION  # 平局贡献 0 的口径字符串被记录


def test_score_diff_is_machine_minus_human() -> None:
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1, bootstrap_resamples=50, quiet=True)
    outcome = quiet_run(config, FakeRunner())
    for rec in outcome.records:
        assert rec.score_diff == rec.score_b - rec.score_a
    stats = outcome.summary["aggregate"]["per_mode"]["HARD"]
    expected = rb.mean([rec.score_diff for rec in outcome.records if rec.mode == "HARD"])
    assert stats["score_diff_mean"] == pytest.approx(expected)
    assert stats["score_diff_std"] is None  # 单样本 → n−1 无定义，不填 0


# --------------------------------------------------------------------------------------
# 6. 异常 / 契约守卫
# --------------------------------------------------------------------------------------
def test_group_error_is_preserved_and_group_stops() -> None:
    config = rb.BatchConfig(seed=42, maps=2, dice_seeds=2, bootstrap_resamples=50, quiet=True)
    runner = FakeRunner(fail_for={(42, 100045, "NORMAL")})
    outcome = quiet_run(config, runner)
    # 12 局 - 2（失败的那个 + 同单元被停止的 HARD）
    assert len(outcome.records) == 10
    assert len(runner.calls) == 11
    error_types = sorted(str(err["error_type"]) for err in outcome.errors)
    assert error_types == ["RuntimeError", "SkippedAfterGroupError"]
    ran = {(rec.map_seed, rec.dice_seed, rec.mode) for rec in outcome.records}
    assert (42, 100045, "EASY") in ran
    assert (42, 100045, "NORMAL") not in ran
    assert (42, 100045, "HARD") not in ran
    assert len([key for key in ran if key[0] == 43]) == 6
    assert outcome.exit_code == 1
    assert outcome.summary["counts"]["cases_errored"] == 1
    assert outcome.summary["counts"]["cases_skipped_after_group_error"] == 1
    assert outcome.summary["counts"]["units_aborted"] == 1
    assert outcome.summary["counts"]["errors_by_type"]["RuntimeError"] == 1


def test_fail_fast_stops_whole_batch() -> None:
    config = rb.BatchConfig(
        seed=42, maps=2, dice_seeds=2, bootstrap_resamples=50, quiet=True, fail_fast=True
    )
    outcome = quiet_run(config, FakeRunner(fail_for={(42, 100045, "NORMAL")}))
    assert len(outcome.records) == 1  # 只有第一个 EASY 成功
    assert len(outcome.errors) == 1
    assert outcome.summary["counts"]["cases_run"] == 1


def test_map_id_mismatch_is_flagged() -> None:
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=2, bootstrap_resamples=50, quiet=True)
    runner = FakeRunner(map_id_of=lambda case: f"bad{case.dice_seed:06d}")
    outcome = quiet_run(config, runner)
    assert any(str(err["error_type"]) == "MapIdMismatch" for err in outcome.errors)
    assert outcome.summary["checks"]["map_id_consistent"]["failed"] >= 1
    assert outcome.exit_code == 1


def test_record_from_outcome_rejects_contract_violations() -> None:
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1)
    case = rb.build_case_matrix(config)[0].cases[0]
    good = fake_outcome(case)
    record = rb.record_from_outcome(case, good, 123)
    assert record.map_id == fake_map_id(case)
    assert record.checks["dice_sequence_matches_table"] is True
    assert record.checks["timed_out_consistent"] is True
    assert record.csv_dict()["turns"] == str(good["turns"])

    for missing in ("score_a", "score_b", "turns", "trap_steps_a", "ai_calls", "cache_hits"):
        broken = dict(good)
        del broken[missing]
        with pytest.raises(KeyError):
            rb.record_from_outcome(case, broken, None)

    bad_winner = dict(good, winner="C")
    with pytest.raises(ValueError):
        rb.record_from_outcome(case, bad_winner, None)

    bad_timeout = dict(good, winner="B", draw_timeout=True)
    with pytest.raises(ValueError):
        rb.record_from_outcome(case, bad_timeout, None)

    no_map_id = dict(good, map_id="")
    with pytest.raises(ValueError):
        rb.record_from_outcome(case, no_map_id, None)

    not_mapping = ["not", "a", "mapping"]
    with pytest.raises(TypeError):
        rb.record_from_outcome(case, not_mapping, None)  # type: ignore[arg-type]

    # 骰序与注入表不一致时只记为检查失败，不伪造数据
    tampered = dict(good, dice_sequence=[1] * len(good["dice_sequence"]))
    record = rb.record_from_outcome(case, tampered, None)
    assert record.checks["dice_sequence_matches_table"] is False


def test_csv_row_projection_matches_columns() -> None:
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1)
    case = rb.build_case_matrix(config)[0].cases[0]
    record = rb.record_from_outcome(case, fake_outcome(case), 5)
    row = record.csv_dict()
    assert list(row) == list(rb.CSV_COLUMNS)
    assert row["map_seed"] == "42"
    assert row["mode"] == "EASY"
    assert row["K"] == "24"
    assert row["partition_enabled"] == "False"
    assert row["human_model_id"] == "VIEW_HEURISTIC:GATHER:v1"


# --------------------------------------------------------------------------------------
# 7. 适配层集成契约：真实 models/map_module + 严格按 06 §3 的假 AI/人工/Simulator
#    （阶段 1 就能验证「驱动 → 冻结签名」的调用约定，不必等 simulator.py 落地）
# --------------------------------------------------------------------------------------
class _FakeAiModule:
    """AiModule 假实现：参数名严格取自 06 §3（驱动传错参数名 → TypeError）。"""

    instances: list["_FakeAiModule"] = []

    def __init__(
        self,
        depth: int = 3,
        candidate_limit: int = 24,
        partition_threshold: int = 144,
        use_cache: bool = True,
        use_pruning: bool = True,
        rng: object = None,
    ) -> None:
        if rng is not None:
            raise AssertionError("实验必须让搜索保持确定性：AiModule 不应注入 rng")
        self.depth = depth
        self.candidate_limit = candidate_limit
        self.partition_threshold = partition_threshold
        self.use_cache = use_cache
        self.use_pruning = use_pruning
        type(self).instances.append(self)


class _FakeHumanPolicy:
    """HumanPolicy 假实现；记录它实际收到的入参（视图代理不得接触真图）。"""

    seen: list[dict[str, object]] = []

    def __init__(self, strategy: str = "GATHER", rng: object = None) -> None:
        self.strategy = strategy
        self.rng = rng

    def choose(self, view: object, seat: object, dice_N: int, public: object) -> tuple[object, ...]:
        from treasure_game.models import Direction

        record = {
            "view_type": type(view).__name__,
            "view_has_true_map": hasattr(view, "true_map"),
            "public_type": type(public).__name__,
            "public_dice_N": getattr(public, "dice_N", None),
            "dice_N": dice_N,
            "seat": getattr(seat, "value", seat),
            "strategy": self.strategy,
            "rng_is_random": isinstance(self.rng, random.Random),
        }
        type(self).seen.append(record)
        assert record["view_has_true_map"] is False, "视图代理绝不能拿到 TrueMap"
        assert record["public_dice_N"] == dice_N, "PublicStateView.dice_N 必须与本回合骰值一致"
        return (Direction.UP,)


class _FakeSeatPolicy(enum.Enum):
    HUMAN = "HUMAN"
    GREEDY = "GREEDY"
    OPT = "OPT"
    OPT_PARTITION = "OPT_PARTITION"


class _FakeSimulator:
    """Simulator 假实现：参数名严格取自 06 §3，返回真实 ``GameResult``。

    ``next_action_style`` 覆盖三种调用形态：
    ``state_view``（06 修订后签名）/ ``legacy``（旧三参）/ ``before_render``（违反顺序，应报错）。
    """

    last_kwargs: dict[str, object] = {}
    last_table: tuple[int, ...] = ()
    instances = 0
    next_action_style = "state_view"

    def __init__(
        self,
        initial_state: object,
        io: object = None,
        rng: object = None,
        policy_a: _FakeSeatPolicy = _FakeSeatPolicy.HUMAN,
        policy_b: _FakeSeatPolicy = _FakeSeatPolicy.OPT,
        max_turns: int = 500,
        ai: object = None,
        dice_seed_table: object = None,
    ) -> None:
        type(self).instances += 1
        self._state = initial_state
        self._io = io
        self._policy_a = policy_a
        self._policy_b = policy_b
        self._max_turns = max_turns
        self._ai = ai
        self._table = tuple(dice_seed_table or ())
        self._rng = rng
        type(self).last_table = self._table
        type(self).last_kwargs = {
            "policy_a": policy_a,
            "policy_b": policy_b,
            "max_turns": max_turns,
            "ai_depth": getattr(ai, "depth", None),
            "ai_K": getattr(ai, "candidate_limit", None),
            "ai_threshold": getattr(ai, "partition_threshold", None),
            "ai_use_cache": getattr(ai, "use_cache", None),
            "ai_use_pruning": getattr(ai, "use_pruning", None),
            "table_len": len(self._table),
            "rng_is_random": isinstance(rng, random.Random),
        }

    def run_human_vs_ai(self, game_mode: object) -> object:
        from treasure_game.map_module import MapModule
        from treasure_game.models import GameEvent, GameResult, PublicStateView, Seat

        mode = game_mode
        state = self._state
        style = type(self).next_action_style
        seq: list[int] = []
        events: list[GameEvent] = []
        turns = 0
        while turns < min(self._max_turns, 3) and state.true_map.remaining_treasures:
            for seat in (Seat.A, Seat.B):
                dice_N = int(self._table[(turns * 2 + (0 if seat is Seat.A else 1)) % len(self._table)])
                seq.append(dice_N)
                if seat is Seat.A:
                    view = MapModule(state.true_map).get_view_map(mode)
                    public = PublicStateView(
                        mode=mode,
                        pos_a=state.pos_a,
                        pos_b=state.pos_b,
                        score_a=state.score_a,
                        score_b=state.score_b,
                        turn=seat,
                        turn_index=turns,
                        dice_N=dice_N,
                        remaining_treasure_count=len(state.true_map.remaining_treasures),
                    )
                    if style == "before_render":
                        self._io.next_action(seat, dice_N, view)
                    elif style == "legacy":
                        self._io.render(view, public, tuple(events))
                        self._io.next_action(seat, dice_N, view)
                    else:
                        self._io.render(view, public, tuple(events))
                        self._io.next_action(seat, dice_N, view, public)
            turns += 1
        final_state = dataclasses.replace(state, turn_index=turns)
        metrics: dict[str, object] = {
            "battles": 1,
            "trap_steps_a": 1,
            "trap_steps_b": 2,
            "treasure_steps_a": 3,
            "treasure_steps_b": 4,
            "ai_calls": turns * 2,
            "ai_elapsed_ns": 123456,
            "cache_hits": 5,
            "cache_misses": 6,
            "nodes": 7,
            "partition_used": False,
            "dice_sequence": list(seq),
            "timed_out": False,
        }
        return GameResult(
            mode=mode,
            final_state=final_state,
            winner="B",
            score_a=1,
            score_b=2,
            turns=turns,
            events=tuple(events),
            draw_timeout=False,
            metrics=metrics,
        )


def _fake_tg_namespace() -> types.SimpleNamespace:
    """真实 models/map_module + 假 AI/人工/Simulator（模拟 06 §3 五个模块）。"""
    import treasure_game.map_module as real_map_module
    import treasure_game.models as real_models

    return types.SimpleNamespace(
        models=real_models,
        map_module=real_map_module,
        human_policy=types.SimpleNamespace(HumanPolicy=_FakeHumanPolicy),
        ai_module=types.SimpleNamespace(AiModule=_FakeAiModule),
        simulator=types.SimpleNamespace(SeatPolicy=_FakeSeatPolicy, Simulator=_FakeSimulator),
    )


@pytest.mark.skipif(not MAP_READY, reason=f"map_module 尚未落地：{MAP_DETAIL}")
@pytest.mark.parametrize("style", ["state_view", "legacy"])
def test_adapter_contract_with_strict_fake_modules(
    style: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两种 GameIO 形态（06 修订签名 / 旧三参）都必须跑通并给出相同观测。"""
    monkeypatch.setattr(_FakeSimulator, "next_action_style", style)
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1)
    cases = rb.build_case_matrix(config)[0].cases
    easy = next(case for case in cases if case.mode == "EASY")
    hard = next(case for case in cases if case.mode == "HARD")
    runner = rb.SimulatorCaseRunner(config, modules=_fake_tg_namespace())

    outcome = runner.run_case(easy)
    record = rb.record_from_outcome(easy, outcome, 42)
    kwargs = _FakeSimulator.last_kwargs
    assert _FakeSimulator.last_table == easy.dice_table  # 注入的是本单元的预生成骰表
    assert kwargs["policy_a"] is _FakeSeatPolicy.HUMAN
    assert kwargs["policy_b"] is _FakeSeatPolicy.GREEDY  # EASY → 贪心
    assert kwargs["max_turns"] == 500
    assert (kwargs["ai_depth"], kwargs["ai_K"], kwargs["ai_threshold"]) == (3, 24, 144)
    assert kwargs["ai_use_cache"] is True and kwargs["ai_use_pruning"] is True
    assert kwargs["rng_is_random"] is True
    assert len(record.map_id) == 16  # 真实 TrueMap.map_id = sha1[:16]
    assert record.checks["dice_sequence_matches_table"] is True
    assert record.checks["timed_out_consistent"] is True
    assert record.checks["partition_used_matches_config_condition"] is True
    assert record.csv_dict()["ai_elapsed_ns"] == "123456"
    assert record.csv_dict()["turns"] == "3"

    seen = _FakeHumanPolicy.seen[-1]
    assert seen["view_type"] == "ViewMap" and seen["view_has_true_map"] is False
    assert seen["public_type"] == "PublicStateView"
    assert seen["public_dice_N"] == seen["dice_N"]
    assert seen["seat"] == "A"
    assert seen["strategy"] == "GATHER"
    assert seen["rng_is_random"] is True  # 同单元三模式同一条随机流

    # NORMAL/HARD → OPT；同一 case 重跑逐位一致（地图复用 + 骰表注入 → 可复现）
    assert runner.run_case(hard)["dice_sequence"]
    assert _FakeSimulator.last_kwargs["policy_b"] is _FakeSeatPolicy.OPT
    assert runner.run_case(easy) == outcome


@pytest.mark.skipif(not MAP_READY, reason=f"map_module 尚未落地：{MAP_DETAIL}")
def test_adapter_passes_configured_strategy_to_human_policy() -> None:
    """驱动的 --human-strategy 必须原样传给 HumanPolicy（不依赖该策略已实现）。"""
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1, human_strategy="CAUTIOUS")
    case = rb.build_case_matrix(config)[0].cases[0]
    assert case.human_model_id == "VIEW_HEURISTIC:CAUTIOUS:v1"
    runner = rb.SimulatorCaseRunner(config, modules=_fake_tg_namespace())
    runner.run_case(case)
    assert _FakeHumanPolicy.seen[-1]["strategy"] == "CAUTIOUS"


@pytest.mark.skipif(not MAP_READY, reason=f"map_module 尚未落地：{MAP_DETAIL}")
def test_ordering_violation_surfaces_as_recorded_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """违反 render→next_action 顺序时必须报清晰错误并记入 JSON，不得静默产出垃圾数据。"""
    monkeypatch.setattr(_FakeSimulator, "next_action_style", "before_render")
    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1, bootstrap_resamples=20, quiet=True)
    runner = rb.SimulatorCaseRunner(config, modules=_fake_tg_namespace())
    outcome = quiet_run(config, runner)
    assert outcome.records == []
    assert sorted(str(err["error_type"]) for err in outcome.errors) == [
        "RuntimeError",
        "SkippedAfterGroupError",
        "SkippedAfterGroupError",
    ]
    assert "state_view" in str(outcome.errors[0]["message"])
    assert outcome.exit_code == 1


@pytest.mark.skipif(not MAP_READY, reason=f"map_module 尚未落地：{MAP_DETAIL}")
def test_twelve_row_smoke_with_real_maps_and_fake_simulator(tmp_path: pathlib.Path) -> None:
    """真实地图生成 + 假 Simulator：12 行 smoke 的控制条件（同图/同骰/统计）全链路成立。"""
    config = rb.BatchConfig(seed=42, maps=2, dice_seeds=2, bootstrap_resamples=100, quiet=True)
    runner = rb.SimulatorCaseRunner(config, modules=_fake_tg_namespace())
    outcome = quiet_run(config, runner)
    assert len(outcome.records) == 12
    assert outcome.exit_code == 0
    assert len(runner._map_cache) == 2  # 每 map_seed 只生成一次地图
    assert runner.map_gen_calls == 2
    assert runner.map_gen_ns_total > 0
    rows = outcome.csv_rows
    assert len({(row["map_seed"], row["dice_seed"], row["mode"]) for row in rows}) == 12
    per_map = {row["map_seed"]: row["map_id"] for row in rows}
    assert len(set(per_map.values())) == 2
    assert outcome.summary["checks"]["map_id_consistent"]["failed"] == 0
    assert outcome.summary["checks"]["dice_sequence_matches_table"]["failed"] == 0
    assert outcome.summary["timing"]["map_gen_calls"] == 2
    assert rb.write_csv(tmp_path / "smoke.csv", outcome.records) == 12
    assert len(read_rows(tmp_path / "smoke.csv")) == 12


def test_human_strategy_flows_into_model_id_and_filename(tmp_path: pathlib.Path) -> None:
    """策略名必须如实进 CSV 的 human_model_id 与派生文件名（v1 GATHER 结果不被覆盖）。"""
    out = tmp_path / "cautious.csv"
    code = rb.main(
        [
            "--human-strategy", "CAUTIOUS", "--maps", "1", "--dice-seeds", "1",
            "--output", str(out), "--bootstrap", "20", "--quiet",
        ],
        runner=FakeRunner(),
    )
    assert code == 0
    assert {row["human_model_id"] for row in read_rows(out)} == {"VIEW_HEURISTIC:CAUTIOUS:v1"}
    payload = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert payload["config"]["human_strategy"] == "CAUTIOUS"
    assert payload["config"]["human_model_id"] == "VIEW_HEURISTIC:CAUTIOUS:v1"
    # 派生文件名按策略加后缀；显式 human_model_id 优先
    assert rb.default_output_path(rb.BatchConfig(human_strategy="CAUTIOUS")).name.endswith(
        "_cautious.csv"
    )
    assert not rb.default_output_path(rb.BatchConfig()).name.endswith("_cautious.csv")
    assert (
        rb.BatchConfig(human_strategy="CAUTIOUS", human_model_id="MY:ID").resolved_human_model_id
        == "MY:ID"
    )


@pytest.mark.skipif(
    not (MAP_READY and HUMAN_READY),
    reason=f"map_module/human_policy 尚未落地：{MAP_DETAIL} / {HUMAN_DETAIL}",
)
def test_real_human_policy_works_behind_view_heuristic_io() -> None:
    """A 座位契约（可早于 Simulator 验证）：真实 ``HumanPolicy`` 经 ``ViewHeuristicIO``
    只看到 ``ViewMap`` + ``PublicStateView``，返回合法动作序列，且策略确定性可复现。"""
    from treasure_game.models import Direction, GameMode, PublicStateView, Seat

    config = rb.BatchConfig(seed=42, maps=1, dice_seeds=1)
    case = rb.build_case_matrix(config)[0].cases[0]
    true_map, pos_a, pos_b = _RealMapModule.generate(  # type: ignore[union-attr]
        case.height, case.width, case.walls, case.treasures, case.traps,
        random.Random(case.map_seed), trap_value=-2,
    )
    view = _RealMapModule(true_map).get_view_map(GameMode(case.mode))  # type: ignore[operator]
    public = PublicStateView(
        mode=GameMode(case.mode),
        pos_a=pos_a,
        pos_b=pos_b,
        score_a=0,
        score_b=0,
        turn=Seat.A,
        turn_index=0,
        dice_N=4,
        remaining_treasure_count=len(true_map.remaining_treasures),
    )
    assert not hasattr(view, "true_map"), "ViewMap 不得携带真图"

    def run_once() -> tuple[object, ...]:
        policy = _RealHumanPolicy(strategy=case.human_strategy, rng=random.Random(case.dice_seed))
        io = rb.ViewHeuristicIO(policy)
        io.render(view, public, ())
        return io.next_action(Seat.A, 4, view, public)

    actions = run_once()
    assert isinstance(actions, tuple)
    assert all(isinstance(action, Direction) for action in actions)
    assert len(actions) <= 4  # 不超过骰值步数
    assert run_once() == actions, "固定骰序/固定策略下，A 座位动作必须逐位可复现"

    # HARD 视图：未触发陷阱必须呈 0（不泄露隐藏陷阱）
    hard_view = _RealMapModule(true_map).get_view_map(GameMode.HARD)  # type: ignore[operator]
    hidden = [pos for pos in true_map.active_traps]
    assert hidden, "该地图应有未触发陷阱才能验证脱敏"
    for pos in hidden:
        assert hard_view.cell_value(pos.row, pos.col) == 0


# --------------------------------------------------------------------------------------
# 8. 真实生成器守卫 + 阶段 2 守卫（simulator.py 落地后自动生效）
# --------------------------------------------------------------------------------------
@pytest.mark.skipif(not MAP_READY, reason=f"map_module 尚未落地：{MAP_DETAIL}")
def test_real_map_generation_shares_layout_per_map_seed() -> None:
    """真实生成器上的「同图保证」：同 map_seed → 同 map_id、同出生点、同资源数。

    这是配对实验（同单元三模式共用一张地图）的前提；地图 RNG 必须是
    ``random.Random(map_seed)`` 且不含其他随机源。重新生成时**必须复用矩阵里的
    同源参数**（不硬编码尺寸），否则断言的是两套配置。
    """
    config = rb.BatchConfig(seed=42, maps=2, dice_seeds=2)
    layout: dict[int, tuple[tuple[object, ...], str, object, object]] = {}
    for case in (c for unit in rb.build_case_matrix(config) for c in unit.cases):
        if case.map_seed in layout:
            continue
        params = (case.height, case.width, case.walls, case.treasures, case.traps)
        true_map, pos_a, pos_b = _RealMapModule.generate(  # type: ignore[union-attr]
            *params,
            random.Random(case.map_seed),
            trap_value=-2,
        )
        assert pos_a != pos_b, "出生点必须不同"
        assert not true_map.is_wall(pos_a) and not true_map.is_wall(pos_b)
        assert true_map.height == case.height and true_map.width == case.width
        assert len(true_map.treasure_values) == case.treasures
        assert len(true_map.trap_values) == case.traps
        layout[case.map_seed] = (params, true_map.map_id, pos_a, pos_b)

    assert len(layout) == 2
    assert len({map_id for _, map_id, _, _ in layout.values()}) == 2
    # 重新生成（模拟不同骰序单元/不同模式各自的构造路径）必须逐字一致
    for map_seed, (params, map_id, pos_a, pos_b) in layout.items():
        again = _RealMapModule.generate(  # type: ignore[union-attr]
            *params, random.Random(map_seed), trap_value=-2
        )
        assert (again[0].map_id, again[1], again[2]) == (map_id, pos_a, pos_b)


@pytest.mark.skipif(not MAP_READY, reason=f"map_module 尚未落地：{MAP_DETAIL}")
def test_both_map_arms_generate_on_real_generator() -> None:
    """两组地图臂都能在真实生成器上跑通，且 partition 标志符合 Lead 裁决：

    * ``main`` = 12×12（144 格 = 阈值）→ NORMAL/HARD ``partition_enabled=True``；
    * ``baseline`` = 10×10（100 格 < 阈值）→ 三模式恒 ``False``（单体 OPT 基线）。
    """
    for arm, (height, width) in rb.ARM_PRESETS.items():
        config = rb.BatchConfig(arm=arm, height=height, width=width)
        true_map, pos_a, pos_b = _RealMapModule.generate(  # type: ignore[union-attr]
            height,
            width,
            config.walls,
            config.treasures,
            config.traps,
            random.Random(43),
            trap_value=-2,
        )
        assert (true_map.height, true_map.width) == (height, width)
        assert pos_a != pos_b
        flags = config.partition_flags()
        expected = config.map_area >= config.partition_threshold
        assert flags["EASY"] is False
        assert flags["NORMAL"] is expected and flags["HARD"] is expected
    assert rb.BatchConfig().partition_flags()["NORMAL"] is True  # 默认 = main 12×12


@pytest.mark.skipif(SIM_READY, reason=f"treasure_game 已落地：{SIM_DETAIL}")
def test_simulator_case_runner_reports_unavailable() -> None:
    """阶段 1：真实 runner 缺依赖时必须显式失败，不得返回伪造观测。"""
    with pytest.raises(rb.RunnerUnavailable):
        rb.SimulatorCaseRunner(rb.BatchConfig(maps=1, dice_seeds=1))


@pytest.mark.skipif(not SIM_READY, reason=f"treasure_game 尚未落地：{SIM_DETAIL}")
def test_real_simulator_frozen_signatures() -> None:
    """06 §3 冻结签名守卫：实验驱动只依赖这些参数名。"""
    import inspect

    from treasure_game.ai_module import AiModule
    from treasure_game.simulator import SeatPolicy, Simulator

    simulator_params = set(inspect.signature(Simulator.__init__).parameters)
    for name in (
        "initial_state",
        "io",
        "rng",
        "policy_a",
        "policy_b",
        "max_turns",
        "ai",
        "dice_seed_table",
    ):
        assert name in simulator_params, f"Simulator.__init__ 缺少 06 §3 参数 {name}"
    policy_names = {member.name for member in SeatPolicy}
    # Lead 裁决（06 §2 注）：SeatPolicy 只有这四个取值；VIEW_HEURISTIC 是座位 A 的策略名，
    # 由本驱动用 policy_a=HUMAN + 注入视图代理 GameIO 实现。
    assert policy_names == {"HUMAN", "GREEDY", "OPT", "OPT_PARTITION"}
    ai_params = set(inspect.signature(AiModule.__init__).parameters)
    for name in ("depth", "candidate_limit", "partition_threshold", "use_cache", "use_pruning"):
        assert name in ai_params, f"AiModule.__init__ 缺少 06 §3 参数 {name}"


@pytest.mark.skipif(not SIM_READY, reason=f"treasure_game 尚未落地：{SIM_DETAIL}")
def test_real_simulator_three_row_smoke(tmp_path: pathlib.Path) -> None:
    """真实接通烟测（1 地图 × 1 骰序 × 3 模式 = 3 行；12 行正式烟测由 CLI 验收执行）。"""
    out = tmp_path / "real_smoke.csv"
    code = rb.main(
        [
            "--seed",
            "42",
            "--maps",
            "1",
            "--dice-seeds",
            "1",
            "--output",
            str(out),
            "--bootstrap",
            "200",
            "--quiet",
        ]
    )
    assert code == 0, "真实 Simulator 接通后 smoke 必须 0 退出"
    rows = read_rows(out)
    assert len(rows) == 3
    assert {row["mode"] for row in rows} == {"EASY", "NORMAL", "HARD"}
    assert len({row["map_id"] for row in rows}) == 1  # 三模式共用同一张地图
    assert {row["draw_timeout"] for row in rows} <= {"True", "False"}
    payload = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert payload["checks"]["dice_sequence_matches_table"]["failed"] == 0
    assert payload["checks"]["timed_out_consistent"]["failed"] == 0
    assert payload["aggregate"]["per_mode"]["EASY"]["games"] == 1
    assert payload["config"]["treasure_distribution"] == {
        **{str(value): 0.099 for value in range(1, 11)}, "15": 0.005, "20": 0.005
    }
    case = rb.build_case_matrix(rb.BatchConfig(seed=42, maps=1, dice_seeds=1))[0].cases[0]
    generated, _, _ = _RealMapModule.generate(
        case.height, case.width, case.walls, case.treasures, case.traps, random.Random(case.map_seed)
    )
    assert {row["map_id"] for row in rows} == {generated.map_id}
