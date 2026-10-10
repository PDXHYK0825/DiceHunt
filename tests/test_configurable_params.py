"""选做加分项 2「可配置参数」的回归测试（Lead 添加）。

覆盖三个可配置项：对战奖惩分数、骰子范围、OPT 搜索深度。
这些参数由 Lead 在 `simulator.py` + `__main__.py` 中接入，故测试放在独立文件里，
避免与既有 owner 的测试文件冲突。
"""
from __future__ import annotations

import io as _io_module
import pathlib
import random
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve()
_PROJECT = _HERE.parents[1]
for _path in (str(_PROJECT), str(_PROJECT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import treasure_game.__main__ as cli  # noqa: E402
from treasure_game.battle_module import BattleModule  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, Position, Seat, TrueMap  # noqa: E402
from treasure_game.simulator import SeatPolicy, Simulator  # noqa: E402


def _state(mode: GameMode = GameMode.NORMAL) -> GameState:
    true_map, pos_a, pos_b = MapModule.generate(
        8, 8, 8, 3, 2, random.Random(11), treasure_value=2, trap_value=-2
    )
    return GameState(true_map=true_map, pos_a=pos_a, pos_b=pos_b,
                     score_a=0, score_b=0, turn=Seat.A, mode=mode, turn_index=0)


# --------------------------------------------------------------------------- #
# 1. 骰子范围
# --------------------------------------------------------------------------- #
def test_dice_range_is_respected_in_generated_sequence() -> None:
    """`--dice-range 2-4` 时整局骰值必须落在 [2,4]，且不再出现 1/5/6。"""
    sim = Simulator(initial_state=_state(), rng=random.Random(3),
                    policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT,
                    max_turns=60, dice_min=2, dice_max=4)
    result = sim.run_machine_vs_machine()
    seq = list(result.metrics["dice_sequence"])
    assert seq, "骰序不应为空"
    assert all(2 <= v <= 4 for v in seq), f"骰序越界：{sorted(set(seq))}"
    assert 1 not in seq and 5 not in seq and 6 not in seq


def test_default_dice_range_is_one_to_six() -> None:
    """默认必须仍是规格的 1-6（回归保护）。"""
    sim = Simulator(initial_state=_state(), rng=random.Random(5),
                    policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT, max_turns=80)
    seq = list(sim.run_machine_vs_machine().metrics["dice_sequence"])
    assert all(1 <= v <= 6 for v in seq)


@pytest.mark.parametrize("lo,hi", [(0, 6), (6, 1), (-1, 3)])
def test_invalid_dice_range_raises_value_error(lo: int, hi: int) -> None:
    with pytest.raises(ValueError):
        Simulator(initial_state=_state(), dice_min=lo, dice_max=hi)


def test_dice_seed_table_must_fit_the_configured_range() -> None:
    """注入骰表时表格值必须落在配置范围内，否则报错（不能静默越界）。"""
    with pytest.raises(ValueError):
        Simulator(initial_state=_state(), dice_seed_table=[1, 2, 3], dice_min=2, dice_max=4)
    # 落在范围内则接受
    sim = Simulator(initial_state=_state(), policy_a=SeatPolicy.GREEDY,
                    policy_b=SeatPolicy.OPT, dice_seed_table=[2, 3, 4],
                    dice_min=2, dice_max=4, max_turns=4)
    seq = list(sim.run_machine_vs_machine().metrics["dice_sequence"])
    assert seq and all(2 <= v <= 4 for v in seq)


# --------------------------------------------------------------------------- #
# 2. 对战奖惩分数
# --------------------------------------------------------------------------- #
def test_battle_win_score_is_applied_by_simulator() -> None:
    """`battle_win_score=9` 时必须体现在对战结算上（而不是硬编码的 5）。"""
    # 3x3 全空、双方同格起手、宝藏放在别处（保证不是"初态即终局"）
    true_map = TrueMap.create(height=3, width=3,
                              terrain=[[0, 0, 0], [0, 0, 0], [0, 0, 0]],
                              treasures={Position(2, 2): 2}, traps={})
    direct = BattleModule.trigger_battle(true_map, Position(0, 0), Position(0, 0), 10, 5,
                                        win_score=9)
    assert (direct.score_a, direct.score_b) == (19, -4)

    state = GameState(true_map=true_map, pos_a=Position(0, 0), pos_b=Position(0, 0),
                      score_a=10, score_b=5, turn=Seat.A, mode=GameMode.NORMAL, turn_index=0)
    sim = Simulator(initial_state=state, policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT,
                    max_turns=1, dice_seed_table=[1], battle_win_score=9)
    result = sim.run_machine_vs_machine()
    # 双方同格起手；本回合若仍重合，就必须用配置的 9 分结算（而不是 5）
    if result.metrics["battles"] >= 1:
        delta = abs(result.score_a - 10) + abs(result.score_b - 5)
        assert delta in (18, 10), f"结算差额 {delta} 既不是 ±9 也不是平局"
    # 无论是否触发，最终分数都必须来自配置而非默认值 5
    assert not (result.score_a == 15 and result.score_b == 0), "仍在使用默认 +5/-5"


def test_default_battle_win_score_is_five() -> None:
    from treasure_game.models import TrueMap
    true_map = TrueMap.create(height=3, width=3,
                              terrain=[[1, 1, 1], [1, 0, 1], [1, 1, 1]],
                              treasures={}, traps={})
    r = BattleModule.trigger_battle(true_map, Position(1, 1), Position(1, 1), 10, 5)
    assert (r.score_a, r.score_b) == (15, 0)


def test_negative_battle_win_score_is_rejected() -> None:
    with pytest.raises(ValueError):
        Simulator(initial_state=_state(), battle_win_score=-1)


# --------------------------------------------------------------------------- #
# 3. 搜索深度 + CLI 解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("argv,expected", [
    (["--depth", "4"], 4),
    (["--depth", "1"], 1),
    (["--win-score", "9"], 9),
    (["--dice-range", "2-4"], (2, 4)),
    (["--dice-range", "1~6"], (1, 6)),
])
def test_cli_accepts_configurable_parameters(argv, expected) -> None:
    args = cli._parse_args(argv)
    if argv[0] == "--depth":
        assert args.depth == expected
    elif argv[0] == "--win-score":
        assert args.win_score == expected
    else:
        assert args.dice_range == expected


@pytest.mark.parametrize("argv", [
    ["--depth", "0"], ["--depth", "abc"],
    ["--win-score", "-3"], ["--win-score", "x"],
    ["--dice-range", "6-1"], ["--dice-range", "abc"], ["--dice-range", "1-"],
])
def test_cli_rejects_invalid_configurable_parameters(argv) -> None:
    out = _io_module.StringIO()
    code = cli.main(argv, stdin=_io_module.StringIO("0\n"), stdout=out)
    assert code == 2
    assert "参数错误" in out.getvalue()


def test_depth_flag_changes_search_effort_but_not_legality() -> None:
    """深度 1 与深度 3 都必须返回合法序列；深度更高时搜索节点数不应更少。"""
    from treasure_game.ai_module import AiModule
    from treasure_game.models import Direction
    state = _state()
    shallow, deep = AiModule(depth=1), AiModule(depth=3)
    for ai in (shallow, deep):
        seq = ai.get_opt_move(state, 3, Seat.B)
        assert len(seq) <= 3, f"返回序列长度 {len(seq)} 超过骰子值 3"
        assert all(isinstance(d, Direction) for d in seq)
    assert deep.get_diagnostics().nodes >= shallow.get_diagnostics().nodes, (
        f"depth=3 节点数 {deep.get_diagnostics().nodes} 少于 "
        f"depth=1 的 {shallow.get_diagnostics().nodes}"
    )
