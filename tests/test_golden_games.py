#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""黄金对局回归（Owner: simulator-dev，04 测试计划「黄金局」层）。

口径
----
* 2 张**手工**小图（低墙密度）× 3 种模式（EASY/NORMAL/HARD）× 固定骰序表；
* 座位 A 使用**只读视图**的确定性人工代理（BFS 走向最近的可见宝藏，最多走 ``dice_N`` 步）；
* 座位 B 使用真实 ``AiModule``（``depth=2, candidate_limit=8, partition_threshold=999``，
  以便在 6×6 / 7×7 小图上快速且确定地求解；阈值设高即不走分治）；
* 比较**事件种类/座位/位置/分数序列**与终局（比分、胜者、回合数、超时）；
  快照**不含**任何耗时（``ai_elapsed_ns``）与搜索内部计数（``cache_hits/nodes``），
  因此不会因为机器快慢而漂移。

快照更新流程（必须附规则/算法变更原因）::

    python Project/tests/test_golden_games.py --dump

会打印可直接替换下面 ``GOLDEN`` 字面量的内容。
"""

from __future__ import annotations

import pathlib
import sys
from collections import deque

_HERE = pathlib.Path(__file__).resolve()
_PROJECT = _HERE.parents[1]
for _path in (str(_PROJECT), str(_PROJECT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from treasure_game.models import (  # noqa: E402
    Direction,
    GameMode,
    GameState,
    Position,
    Seat,
    TrueMap,
)
from treasure_game.simulator import METRIC_KEYS, SeatPolicy, Simulator  # noqa: E402

try:
    from treasure_game.ai_module import AiModule

    _AI_READY = True
except Exception as _exc:  # noqa: BLE001
    AiModule = None  # type: ignore[assignment]
    _AI_READY = False
    _AI_DETAIL = f"{type(_exc).__name__}: {_exc}"

#: 固定骰序表：6,5,4,3,2,1 循环；按 ``turn_index*2 + (0 for A else 1)`` 取模索引。
DICE_TABLE: tuple[int, ...] = tuple(((index * 5) % 6) + 1 for index in range(400))

#: 手工小图（不使用随机生成器；出生点为空地且不重合；含多枚宝藏与陷阱）。
MAPS: dict[str, dict[str, object]] = {
    "cross": {
        "grid": [
            [0, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 1, 0],
            [0, 2, 0, -2, 0, 0],
            [0, 0, 0, 2, 1, 0],
            [0, -2, 0, 0, 0, 2],
            [0, 0, 0, 0, 0, 0],
        ],
        "a": Position(0, 0),
        "b": Position(5, 5),
    },
    "ring": {
        "grid": [
            [0, 0, 0, 0, 0, 0, 0],
            [0, 1, 1, 0, 1, 1, 0],
            [0, 1, 0, 0, 0, 1, 0],
            [2, 0, 0, 1, 0, 0, 2],
            [0, 1, 0, -2, 0, 1, 0],
            [0, 1, 1, 0, 1, 1, 0],
            [0, -2, 0, 2, 0, 0, 0],
        ],
        "a": Position(0, 0),
        "b": Position(6, 6),
    },
}

MODES: tuple[GameMode, ...] = (GameMode.EASY, GameMode.NORMAL, GameMode.HARD)

_NEIGHBOR_ORDER = (Direction.UP, Direction.LEFT, Direction.DOWN, Direction.RIGHT)

#: 黄金快照（由 ``--dump`` 生成；改动规则/AI 后必须重新生成并说明原因）。
GOLDEN: dict[str, dict[str, object]] = {
    'cross:EASY': {
        'events': 'MOVE:A:1,0:0|MOVE:B:4,5:0|TREASURE:B:4,5:2|MOVE:A:2,0:0|MOVE:A:2,1:0|TREASURE:A:2,1:2|MOVE:B:4,4:0|MOVE:B:4,3:0|MOVE:B:3,3:0|TREASURE:B:3,3:2|GAME_OVER:-:-:0',
        'score_a': 2,
        'score_b': 4,
        'winner': 'B',
        'turns': 2,
        'draw_timeout': False,
        'dice': '1,6,5,4',
        'battles': 0,
        'trap_a': 0,
        'trap_b': 0,
        'treasure_a': 1,
        'treasure_b': 2,
    },
    'cross:NORMAL': {
        'events': 'MOVE:A:1,0:0|MOVE:B:4,5:0|TREASURE:B:4,5:2|MOVE:B:4,4:0|MOVE:B:4,3:0|MOVE:B:3,3:0|TREASURE:B:3,3:2|MOVE:A:2,0:0|MOVE:A:2,1:0|TREASURE:A:2,1:2|GAME_OVER:-:-:0',
        'score_a': 2,
        'score_b': 4,
        'winner': 'B',
        'turns': 2,
        'draw_timeout': False,
        'dice': '1,6,5',
        'battles': 0,
        'trap_a': 0,
        'trap_b': 0,
        'treasure_a': 1,
        'treasure_b': 2,
    },
    'cross:HARD': {
        'events': 'MOVE:A:1,0:0|MOVE:B:4,5:0|TREASURE:B:4,5:2|MOVE:B:4,4:0|MOVE:B:4,3:0|MOVE:B:3,3:0|TREASURE:B:3,3:2|MOVE:A:2,0:0|MOVE:A:2,1:0|TREASURE:A:2,1:2|GAME_OVER:-:-:0',
        'score_a': 2,
        'score_b': 4,
        'winner': 'B',
        'turns': 2,
        'draw_timeout': False,
        'dice': '1,6,5',
        'battles': 0,
        'trap_a': 0,
        'trap_b': 0,
        'treasure_a': 1,
        'treasure_b': 2,
    },
    'ring:EASY': {
        'events': 'MOVE:A:1,0:0|MOVE:B:5,6:0|MOVE:B:4,6:0|MOVE:B:3,6:0|TREASURE:B:3,6:2|MOVE:A:2,0:0|MOVE:A:3,0:0|TREASURE:A:3,0:2|MOVE:B:3,5:0|MOVE:B:3,4:0|MOVE:B:4,4:0|MOVE:B:4,3:0|TRAP:B:4,3:-2|MOVE:A:4,0:0|MOVE:A:5,0:0|MOVE:A:6,0:0|MOVE:B:5,3:0|MOVE:B:6,3:0|TREASURE:B:6,3:2|GAME_OVER:-:-:0',
        'score_a': 2,
        'score_b': 2,
        'winner': None,
        'turns': 3,
        'draw_timeout': False,
        'dice': '1,6,5,4,3,2',
        'battles': 0,
        'trap_a': 0,
        'trap_b': 1,
        'treasure_a': 1,
        'treasure_b': 2,
    },
    'ring:NORMAL': {
        'events': 'MOVE:A:1,0:0|MOVE:B:5,6:0|MOVE:B:4,6:0|MOVE:B:3,6:0|TREASURE:B:3,6:2|MOVE:A:2,0:0|MOVE:A:3,0:0|TREASURE:A:3,0:2|MOVE:A:4,0:0|MOVE:A:5,0:0|MOVE:A:6,0:0|MOVE:B:3,5:0|MOVE:B:3,4:0|MOVE:A:6,1:0|TRAP:A:6,1:-2|MOVE:B:4,4:0|MOVE:B:4,3:0|TRAP:B:4,3:-2|MOVE:B:5,3:0|MOVE:B:6,3:0|TREASURE:B:6,3:2|MOVE:B:6,2:0|MOVE:B:6,1:0|BATTLE:-:-:0|GAME_OVER:-:-:0',
        'score_a': -5,
        'score_b': 7,
        'winner': 'B',
        'turns': 4,
        'draw_timeout': False,
        'dice': '1,6,5,4,3,2,1,6',
        'battles': 1,
        'trap_a': 1,
        'trap_b': 1,
        'treasure_a': 1,
        'treasure_b': 2,
    },
    'ring:HARD': {
        'events': 'MOVE:A:1,0:0|MOVE:B:5,6:0|MOVE:B:4,6:0|MOVE:B:3,6:0|TREASURE:B:3,6:2|MOVE:A:2,0:0|MOVE:A:3,0:0|TREASURE:A:3,0:2|MOVE:A:4,0:0|MOVE:A:5,0:0|MOVE:A:6,0:0|MOVE:B:3,5:0|MOVE:B:3,4:0|MOVE:A:6,1:0|TRAP:A:6,1:-2|MOVE:B:4,4:0|MOVE:B:4,3:0|TRAP:B:4,3:-2|MOVE:B:5,3:0|MOVE:B:6,3:0|TREASURE:B:6,3:2|MOVE:B:6,2:0|MOVE:B:6,1:0|BATTLE:-:-:0|GAME_OVER:-:-:0',
        'score_a': -5,
        'score_b': 7,
        'winner': 'B',
        'turns': 4,
        'draw_timeout': False,
        'dice': '1,6,5,4,3,2,1,6',
        'battles': 1,
        'trap_a': 1,
        'trap_b': 1,
        'treasure_a': 1,
        'treasure_b': 2,
    },
}


class ViewSeekingIO:
    """只读 ``ViewMap`` 的确定性人工代理（A 座位）：BFS 走向最近的可见宝藏。"""

    def __init__(self) -> None:
        self.views: list[object] = []
        self.state_views: list[object] = []
        self.render_events: list[tuple] = []

    def render(self, view, state_view, events=()):  # noqa: ANN001
        self.views.append(view)
        self.state_views.append(state_view)
        self.render_events.append(tuple(events))

    def next_action(self, seat, dice_N, view, state_view):  # noqa: ANN001
        start = state_view.pos_a if seat is Seat.A else state_view.pos_b
        path = self._path_to_nearest_treasure(view, start)
        return tuple(path[:dice_N])

    def log(self, message: str) -> None:  # pragma: no cover - 代理不需要日志
        return None

    @staticmethod
    def _path_to_nearest_treasure(view, start: Position) -> list[Direction]:
        """视图上的 BFS：邻序固定 上/左/下/右，目标是"最近的可达宝藏"（值 >= 2）。"""
        parents: dict[Position, tuple[Position, Direction] | None] = {start: None}
        queue: deque[Position] = deque([start])
        while queue:
            current = queue.popleft()
            if view.cell_value(current.row, current.col) >= 2:
                steps: list[Direction] = []
                node = current
                while parents[node] is not None:
                    previous, direction = parents[node]  # type: ignore[misc]
                    steps.append(direction)
                    node = previous
                steps.reverse()
                return steps
            for direction in _NEIGHBOR_ORDER:
                drow, dcol = direction.delta
                nxt = Position(current.row + drow, current.col + dcol)
                if nxt in parents or not view.in_bounds(nxt.row, nxt.col):
                    continue
                if view.cell_value(nxt.row, nxt.col) == 1:
                    continue
                parents[nxt] = (current, direction)
                queue.append(nxt)
        return []


def run_case(map_name: str, mode: GameMode) -> object:
    """跑一局黄金对局并返回 ``GameResult``。"""
    spec = MAPS[map_name]
    true_map = TrueMap.from_grid(spec["grid"])  # type: ignore[arg-type]
    state = GameState(
        true_map=true_map,
        pos_a=spec["a"],  # type: ignore[arg-type]
        pos_b=spec["b"],  # type: ignore[arg-type]
        score_a=0,
        score_b=0,
        turn=Seat.A,
        mode=mode,
        turn_index=0,
    )
    simulator = Simulator(
        initial_state=state,
        io=ViewSeekingIO(),
        rng=None,  # 骰序完全由 DICE_TABLE 决定
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,  # 具体由 mode 路由：EASY→GREEDY，其余→OPT
        max_turns=200,
        ai=AiModule(depth=2, candidate_limit=8, partition_threshold=999),  # type: ignore[misc]
        dice_seed_table=DICE_TABLE,
    )
    return simulator.run_human_vs_ai(mode)


def snapshot(result) -> dict[str, object]:
    """可比较的稳定快照：事件序列 + 终局 + 骰序 + 计数（不含耗时/内部计数）。"""
    return {
        "events": "|".join(
            "{kind}:{actor}:{pos}:{delta}".format(
                kind=event.kind,
                actor="-" if event.actor is None else event.actor.value,
                pos="-" if event.position is None else f"{event.position.row},{event.position.col}",
                delta=event.score_delta,
            )
            for event in result.events
        ),
        "score_a": result.score_a,
        "score_b": result.score_b,
        "winner": result.winner,
        "turns": result.turns,
        "draw_timeout": result.draw_timeout,
        "dice": ",".join(str(value) for value in result.metrics["dice_sequence"]),
        "battles": result.metrics["battles"],
        "trap_a": result.metrics["trap_steps_a"],
        "trap_b": result.metrics["trap_steps_b"],
        "treasure_a": result.metrics["treasure_steps_a"],
        "treasure_b": result.metrics["treasure_steps_b"],
    }


def golden_keys() -> list[str]:
    return [f"{name}:{mode.value}" for name in MAPS for mode in MODES]


def collect_golden() -> dict[str, dict[str, object]]:
    return {
        f"{name}:{mode.value}": snapshot(run_case(name, mode)) for name in MAPS for mode in MODES
    }


# --------------------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------------------
import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not _AI_READY, reason="ai_module 尚未落地")


def test_golden_snapshot_covers_two_maps_times_three_modes() -> None:
    assert sorted(GOLDEN) == sorted(golden_keys()), "黄金快照必须覆盖 2 图 × 3 模式"
    assert len(MAPS) == 2 and len(MODES) == 3


def test_golden_games_match_snapshot() -> None:
    """真实事件/分数/终局序列必须与黄金快照逐字一致（回归门）。"""
    for map_name in MAPS:
        for mode in MODES:
            key = f"{map_name}:{mode.value}"
            assert key in GOLDEN, f"缺少黄金快照 {key}（先跑 --dump 生成）"
            actual = snapshot(run_case(map_name, mode))
            expected = GOLDEN[key]
            assert actual == expected, (
                f"黄金对局 {key} 与快照不一致：\n"
                f"  实际 events={actual['events']}\n"
                f"  快照 events={expected['events']}\n"
                f"  实际终局={ {k: actual[k] for k in ('score_a','score_b','winner','turns','draw_timeout')} }\n"
                f"  快照终局={ {k: expected[k] for k in ('score_a','score_b','winner','turns','draw_timeout')} }"
            )


def test_golden_games_are_reproducible_and_do_not_leak_timing() -> None:
    """同一参数重复跑逐字一致；快照里不得出现耗时/内部搜索计数。"""
    for map_name in MAPS:
        for mode in MODES:
            first = snapshot(run_case(map_name, mode))
            second = snapshot(run_case(map_name, mode))
            assert first == second, f"{map_name}:{mode.value} 重复运行结果不稳定"
            keys = set(first)
            assert not keys & {"ai_elapsed_ns", "cache_hits", "cache_misses", "nodes"}, (
                "快照不得包含耗时/内部计数"
            )


def test_golden_games_terminate_and_are_logically_consistent() -> None:
    """终局口径自检：非超时局必定宝藏耗尽；计数与事件一致；骰序是注入表前缀。"""
    for map_name in MAPS:
        for mode in MODES:
            result = run_case(map_name, mode)
            key = f"{map_name}:{mode.value}"
            assert set(METRIC_KEYS) <= set(result.metrics), key
            assert result.metrics["timed_out"] is result.draw_timeout
            if result.draw_timeout:
                assert result.winner is None
                assert result.turns == 200
            else:
                assert result.final_state.true_map.is_finished(), f"{key} 未耗尽宝藏却不是超时"
                assert result.events[-1].kind == "GAME_OVER"
                assert result.winner == (
                    "A" if result.score_a > result.score_b
                    else ("B" if result.score_b > result.score_a else None)
                )
            assert result.metrics["battles"] == sum(
                1 for event in result.events if event.kind == "BATTLE"
            )
            assert result.metrics["treasure_steps_a"] == sum(
                1 for event in result.events if event.kind == "TREASURE" and event.actor is Seat.A
            )
            assert result.metrics["treasure_steps_b"] == sum(
                1 for event in result.events if event.kind == "TREASURE" and event.actor is Seat.B
            )
            dice = list(result.metrics["dice_sequence"])
            assert dice == list(DICE_TABLE[: len(dice)]), f"{key} 骰序必须是注入表前缀"
            assert result.turns * 2 - 1 <= len(dice) <= result.turns * 2


def test_golden_hard_hides_unrevealed_traps_from_human_agent() -> None:
    """HARD 局中，人工代理收到的每一帧视图都不得暴露"当时尚未触发"的陷阱。"""
    for map_name in MAPS:
        spec = MAPS[map_name]
        true_map = TrueMap.from_grid(spec["grid"])  # type: ignore[arg-type]
        io = ViewSeekingIO()
        state = GameState(
            true_map=true_map, pos_a=spec["a"], pos_b=spec["b"],  # type: ignore[arg-type]
            score_a=0, score_b=0, turn=Seat.A, mode=GameMode.HARD, turn_index=0,
        )
        result = Simulator(
            initial_state=state, io=io, policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT,
            max_turns=200, ai=AiModule(depth=2, candidate_limit=8, partition_threshold=999),  # type: ignore[misc]
            dice_seed_table=DICE_TABLE,
        ).run_human_vs_ai(GameMode.HARD)
        assert io.views, "HARD 局必须渲染视图"
        first_view = io.views[0]
        for pos in true_map.trap_values:
            assert first_view.cell_value(pos.row, pos.col) == 0, (
                f"{map_name}: HARD 首帧泄露了未触发陷阱 {pos}"
            )
        # HARD 局里每次陷阱触发都是"首次踩中"（触发后即失效）→ 文案必须是隐藏提示
        for event in result.events:
            if event.kind == "TRAP":
                assert event.message.startswith("【踩到隐藏陷阱！扣除 ")
                assert event.score_delta == true_map.trap_values[event.position]


if __name__ == "__main__":  # pragma: no cover - 快照生成器
    if "--dump" not in sys.argv:
        print(__doc__)
        raise SystemExit(0)
    if not _AI_READY:
        print(f"ai_module 不可用：{_AI_DETAIL}")
        raise SystemExit(1)
    print("GOLDEN: dict[str, dict[str, object]] = {")
    for key, value in collect_golden().items():
        print(f"    {key!r}: {{")
        for field, field_value in value.items():
            print(f"        {field!r}: {field_value!r},")
        print("    },")
    print("}")
