"""``Project/tests/test_battle.py`` —— BattleModule 终点对战结算（W1-03）。

覆盖 ``docs/04-test-plan.md`` 用例 5（``test_spec_case_5_boxed_loser_still_loses_points``）与
对战规则的全部边界：未重合不结算、同分平局不传送、败方全图随机传送、无空位原地不动但分数照改、
不消耗资源、不递归触发、``ViewMap`` 误入抛 ``TypeError``、坏坐标抛 ``ValueError``。
"""

from __future__ import annotations

import dataclasses
import inspect
import random

import pytest

from treasure_game.battle_module import BattleModule
from treasure_game.map_module import MapModule
from treasure_game.models import GameMode, Position, Seat, TrueMap

# spec 用例 5：3×3，中心 (1,1) 为空地，四邻全为墙
BOXED_ROWS = ((1, 1, 1), (1, 0, 1), (1, 1, 1))
# 全空地，用于验证败方全图随机传送
OPEN_ROWS = ((0, 0, 0), (0, 0, 0), (0, 0, 0))
CENTER = Position(1, 1)

BATTLE_WIN_MESSAGE = "触发对战：A +5 分，B -5 分"
BATTLE_DRAW_MESSAGE = "触发对战：分数相同，平局"


def _true_map(
    rows,
    treasures=(),
    traps=(),
    *,
    remaining=None,
    active=None,
    revealed=(),
) -> TrueMap:
    """直接构造 ``TrueMap``；``remaining``/``active`` 默认"全部资源都还在"。"""
    terrain = tuple(tuple(int(value) for value in row) for row in rows)
    treasure_layer = {Position(row, col): int(value) for row, col, value in treasures}
    trap_layer = {Position(row, col): int(value) for row, col, value in traps}
    return TrueMap(
        height=len(terrain),
        width=len(terrain[0]),
        terrain=terrain,
        treasure_values=treasure_layer,
        trap_values=trap_layer,
        remaining_treasures=frozenset(treasure_layer) if remaining is None else frozenset(remaining),
        active_traps=frozenset(trap_layer) if active is None else frozenset(active),
        revealed_traps=frozenset(revealed),
    )


# =========================================================================== #
# spec 用例 5 + 三条主分支
# =========================================================================== #


def test_public_api_matches_frozen_contract():
    """06 §1/§3 冻结：``trigger_battle`` 必须是 ``@staticmethod``，参数顺序与默认值逐字一致。"""
    assert isinstance(inspect.getattr_static(BattleModule, "trigger_battle"), staticmethod)
    signature = inspect.signature(BattleModule.trigger_battle)
    assert list(signature.parameters) == [
        "true_map",
        "pos_a",
        "pos_b",
        "score_a",
        "score_b",
        "actor_a",
        "actor_b",
        "win_score",
    ]
    assert signature.parameters["actor_a"].default is Seat.A
    assert signature.parameters["actor_b"].default is Seat.B
    assert signature.parameters["win_score"].default == 5


def test_spec_case_5_boxed_loser_still_loses_points():
    """3×3 中心四周全墙：分数正常结算，位置无法推开。"""
    true_map = _true_map(BOXED_ROWS)

    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 10, 5)

    assert result.triggered is True
    assert (result.score_a, result.score_b) == (15, 0)
    assert result.pos_a == CENTER
    assert result.pos_b == CENTER  # 四邻全障碍 → 败方留在原格
    assert len(result.events) == 1
    assert result.events[0].kind == "BATTLE"
    assert result.events[0].message == BATTLE_WIN_MESSAGE


def test_draw_keeps_scores_and_positions():
    true_map = _true_map(OPEN_ROWS)

    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 5, 5)

    assert result.triggered is True
    assert (result.score_a, result.score_b) == (5, 5)  # 平局不改分
    assert (result.pos_a, result.pos_b) == (CENTER, CENTER)  # 平局不推开
    assert len(result.events) == 1
    assert result.events[0].kind == "BATTLE"
    assert result.events[0].message == BATTLE_DRAW_MESSAGE


def test_no_overlap_is_not_triggered_and_changes_nothing():
    true_map = _true_map(OPEN_ROWS)
    pos_a, pos_b = Position(0, 0), Position(2, 2)

    result = BattleModule.trigger_battle(true_map, pos_a, pos_b, 10, 5)

    assert result.triggered is False
    assert (result.pos_a, result.pos_b) == (pos_a, pos_b)
    assert (result.score_a, result.score_b) == (10, 5)
    assert result.events == ()


def test_adjacent_but_not_overlapping_is_not_triggered():
    """中途路过对手不结算：只有终点重合才触发（06 §4.7）。"""
    true_map = _true_map(OPEN_ROWS)
    result = BattleModule.trigger_battle(true_map, CENTER, Position(1, 2), 9, 9)
    assert result.triggered is False
    assert (result.pos_a, result.pos_b) == (CENTER, Position(1, 2))
    assert result.events == ()


# =========================================================================== #
# 败方随机传送
# =========================================================================== #


@pytest.mark.parametrize(
    "target", [Position(r, c) for r in range(3) for c in range(3) if (r, c) != (1, 1)]
)
def test_loser_b_teleported_to_random_empty_with_winner_unchanged(monkeypatch, target):
    """全图每个空白格（含非相邻格）都参与随机抽取，胜方保持原位。"""
    true_map = _true_map(OPEN_ROWS)
    candidates = []

    def choose(positions):
        candidates.extend(positions)
        return target

    monkeypatch.setattr(random, "choice", choose)

    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 10, 5)

    assert (result.score_a, result.score_b) == (15, 0)
    assert result.pos_a == CENTER  # 胜方不动
    assert result.pos_b == target
    assert set(candidates) == {
        Position(r, c) for r in range(3) for c in range(3) if (r, c) != (1, 1)
    }


def test_loser_a_teleported_when_b_wins(monkeypatch):
    true_map = _true_map(OPEN_ROWS)
    monkeypatch.setattr(random, "choice", lambda positions: positions[-1])

    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 5, 12)

    assert (result.score_a, result.score_b) == (0, 17)  # B 胜：B +5、A -5
    assert result.pos_a == Position(2, 2)
    assert result.pos_b == CENTER
    assert result.events[0].message == "触发对战：B +5 分，A -5 分"


def test_teleport_excludes_walls_resources_and_winner_but_allows_collected_treasure(monkeypatch):
    """排除所有仍显示为墙/资源的格子，已拾取宝藏的格子已是空白格。"""
    true_map = _true_map(
        ((1, 0, 0), (0, 0, 0), (0, 0, 0)),
        treasures=((1, 0, 4), (0, 2, 2)),
        traps=((0, 1, -2), (1, 2, -2)),
        remaining={Position(0, 2)},
        active={Position(1, 2)},
        revealed={Position(0, 1)},
    )
    candidates = []

    def choose(positions):
        candidates.extend(positions)
        return Position(1, 0)

    monkeypatch.setattr(random, "choice", choose)

    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 3, 8)

    assert (result.score_a, result.score_b) == (-2, 13)
    assert result.pos_a == Position(1, 0)
    assert result.pos_b == CENTER
    assert set(candidates) == {Position(1, 0), Position(2, 0), Position(2, 1), Position(2, 2)}


def test_repeated_battles_can_choose_different_empty_cells(monkeypatch):
    true_map = _true_map(OPEN_ROWS)
    monkeypatch.setattr(random, "choice", random.Random(12345).choice)
    destinations = {
        BattleModule.trigger_battle(true_map, CENTER, CENTER, 10, 5).pos_b
        for _ in range(100)
    }
    assert destinations == {
        Position(r, c) for r in range(3) for c in range(3) if (r, c) != (1, 1)
    }


def test_loser_teleports_when_adjacent_cells_are_walls():
    true_map = _true_map(((0, 1, 1), (1, 0, 1), (1, 1, 1)))
    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 10, 5)
    assert (result.pos_a, result.pos_b) == (CENTER, Position(0, 0))


def test_loser_stays_when_no_empty_cell_but_scores_still_change():
    true_map = _true_map(BOXED_ROWS)
    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, -1, 7)
    assert result.triggered is True
    assert (result.score_a, result.score_b) == (-6, 12)
    assert (result.pos_a, result.pos_b) == (CENTER, CENTER)


def test_custom_win_score_is_used_in_scores_and_message():
    true_map = _true_map(OPEN_ROWS)

    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 3, 9, win_score=3)

    assert (result.score_a, result.score_b) == (0, 12)
    assert result.pos_a != CENTER and result.pos_b == CENTER  # A 是败方
    assert result.events[0].message == "触发对战：B +3 分，A -3 分"


def test_zero_win_score_still_triggers_battle():
    true_map = _true_map(OPEN_ROWS)
    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 2, 1, win_score=0)
    assert result.triggered is True
    assert (result.score_a, result.score_b) == (2, 1)
    assert result.pos_a == CENTER and result.pos_b != CENTER


def test_negative_scores_compare_by_original_values():
    """允许任意整数分数（含负）；先按原始分数比较再改分。"""
    true_map = _true_map(OPEN_ROWS)
    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, -1, -6)
    assert (result.score_a, result.score_b) == (4, -11)


# =========================================================================== #
# 座位标签 / 事件 / 副作用边界
# =========================================================================== #


def test_seat_labels_follow_actor_arguments():
    """``pos_a``/``score_a`` 属于 ``actor_a``：交换座位参数时文案标签随之交换。"""
    true_map = _true_map(OPEN_ROWS)
    result = BattleModule.trigger_battle(
        true_map, CENTER, CENTER, 10, 5, actor_a=Seat.B, actor_b=Seat.A
    )
    assert (result.score_a, result.score_b) == (15, 0)
    assert result.events[0].message == "触发对战：B +5 分，A -5 分"


def test_exactly_one_battle_event_per_call():
    """不递归触发第二次对战：一次调用只产生一条 BATTLE 事件。"""
    true_map = _true_map(BOXED_ROWS)
    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 10, 5)
    assert sum(1 for event in result.events if event.kind == "BATTLE") == 1
    assert len(result.events) == 1
    assert result.events[0].score_delta == 0  # 分数差由 BattleResult 承载，事件不改分


def test_battle_does_not_consume_resources():
    true_map = _true_map(
        OPEN_ROWS,
        treasures=((1, 0, 4),),  # 未被拾取的宝藏
        traps=((0, 1, -2),),  # 未触发的陷阱
    )
    before = (
        frozenset(true_map.remaining_treasures),
        frozenset(true_map.active_traps),
        frozenset(true_map.revealed_traps),
    )

    BattleModule.trigger_battle(true_map, CENTER, CENTER, 10, 5)

    assert (
        frozenset(true_map.remaining_treasures),
        frozenset(true_map.active_traps),
        frozenset(true_map.revealed_traps),
    ) == before


def test_battle_rejects_view_map():
    view = MapModule(_true_map(OPEN_ROWS)).get_view_map(GameMode.EASY)
    with pytest.raises(TypeError):
        BattleModule.trigger_battle(view, CENTER, CENTER, 1, 2)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pos_a, pos_b, reason",
    [
        (Position(0, 0), CENTER, "pos_a 是墙"),
        (CENTER, Position(0, 0), "pos_b 是墙"),
        (Position(-1, 0), CENTER, "pos_a 越界"),
        (CENTER, Position(3, 3), "pos_b 越界"),
    ],
)
def test_battle_rejects_wall_or_out_of_bounds_positions(pos_a, pos_b, reason):
    true_map = _true_map(BOXED_ROWS)
    with pytest.raises(ValueError):
        BattleModule.trigger_battle(true_map, pos_a, pos_b, 1, 2)


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        (dict(score_a=1.5), "score_a 非 int"),
        (dict(score_b="5"), "score_b 非 int"),
        (dict(win_score=-5), "win_score 为负"),
        (dict(win_score=2.5), "win_score 非 int"),
        (dict(actor_a="A"), "actor_a 非 Seat"),
        (dict(actor_b=None), "actor_b 非 Seat"),
    ],
)
def test_battle_rejects_invalid_arguments(kwargs, reason):
    true_map = _true_map(BOXED_ROWS)
    arguments = dict(score_a=10, score_b=5)
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        BattleModule.trigger_battle(true_map, CENTER, CENTER, **arguments)


def test_battle_result_is_frozen():
    true_map = _true_map(OPEN_ROWS)
    result = BattleModule.trigger_battle(true_map, CENTER, CENTER, 10, 5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.score_a = 99  # type: ignore[misc]
