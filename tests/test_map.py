"""``Project/tests/test_map.py`` —— MapModule：随机生成 / 三模式视图 / txt 读写（W1-02）。

覆盖 ``docs/04-test-plan.md`` 的：spec 用例 3 的视图差异、生成连通与可复现、
以及"无效/非矩形 txt""重合/占墙出生点""生成无解""失败导出残局"等失败路径。

夹具直接构造 ``TrueMap``（不经过 ``MapModule``），避免"用被测代码造夹具"。
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib
import random

import pytest

from treasure_game.map_module import MapModule
from treasure_game.models import (
    GameMode,
    IllegalStateError,
    MapFormatError,
    MapGenerationError,
    Position,
    TrueMap,
    ViewMap,
)
from treasure_game.path_module import PathModule

# --------------------------------------------------------------------------- #
# 夹具：3×4 手工地图（含 2 墙、2 宝藏、1 陷阱），与 spec 用例 3 的可见性场景同构
# --------------------------------------------------------------------------- #
FIXTURE_ROWS = (
    (0, 1, 0, 0),
    (0, 0, 0, 1),
    (0, 0, 0, 0),
)
FIXTURE_TREASURES = ((0, 3, 6), (2, 0, 4))
FIXTURE_TRAPS = ((1, 1, -3),)
POS_A = Position(0, 0)
POS_B = Position(2, 3)
TREASURE_POS = Position(0, 3)
TRAP_POS = Position(1, 1)


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


def _fixture_map() -> TrueMap:
    return _true_map(FIXTURE_ROWS, FIXTURE_TREASURES, FIXTURE_TRAPS)


def _non_wall_cells(true_map: TrueMap) -> set[tuple[int, int]]:
    return {
        (row, col)
        for row in range(true_map.height)
        for col in range(true_map.width)
        if true_map.terrain[row][col] == 0
    }


# =========================================================================== #
# 1. 随机生成
# =========================================================================== #


def test_public_api_matches_frozen_contract():
    """06 §1/§3 冻结：``generate``/``load_txt``/``save_txt`` 必须是 ``@staticmethod``，默认值逐字一致。"""
    for name in ("generate", "load_txt", "save_txt"):
        assert isinstance(inspect.getattr_static(MapModule, name), staticmethod), name
    assert not isinstance(inspect.getattr_static(MapModule, "get_view_map"), staticmethod)

    signature = inspect.signature(MapModule.generate)
    assert list(signature.parameters) == [
        "height",
        "width",
        "walls",
        "treasures",
        "traps",
        "rng",
        "treasure_value",
        "trap_value",
        "max_attempts",
    ]
    assert signature.parameters["treasure_value"].default == 2
    assert signature.parameters["trap_value"].default == -2
    assert signature.parameters["max_attempts"].default == 1000
    assert list(inspect.signature(MapModule.load_txt).parameters) == ["path"]
    assert list(inspect.signature(MapModule.save_txt).parameters) == [
        "path",
        "true_map",
        "pos_a",
        "pos_b",
    ]


def test_generate_counts_birthpoints_and_connectivity():
    true_map, pos_a, pos_b = MapModule.generate(6, 7, walls=8, treasures=5, traps=4, rng=random.Random(20240501))

    # 数量精确：墙 / 宝藏 / 陷阱
    assert sum(1 for row in true_map.terrain for value in row if value == 1) == 8
    assert len(true_map.treasure_values) == 5
    assert len(true_map.trap_values) == 4
    # 默认分值 +2 / -2（D10）
    assert set(true_map.treasure_values.values()) == {2}
    assert set(true_map.trap_values.values()) == {-2}
    # 初始动态层：全部宝藏剩余、全部陷阱未触发、无已暴露陷阱
    assert true_map.remaining_treasures == frozenset(true_map.treasure_values)
    assert true_map.active_traps == frozenset(true_map.trap_values)
    assert true_map.revealed_traps == frozenset()
    assert true_map.map_id  # 静态指纹非空

    # 出生点：互不重合、均为空地（非墙且不占资源）
    assert pos_a != pos_b
    for pos in (pos_a, pos_b):
        assert true_map.terrain[pos.row][pos.col] == 0
        assert pos not in true_map.treasure_values
        assert pos not in true_map.trap_values

    # 所有非墙格与 A 连通（BFS 复用 PathModule），B 必然可达
    distances = PathModule.bfs_distances(true_map, pos_a)
    assert {(pos.row, pos.col) for pos in distances} == _non_wall_cells(true_map)
    assert pos_b in distances


def test_generate_is_reproducible_for_same_seed():
    first = MapModule.generate(5, 6, walls=6, treasures=4, traps=3, rng=random.Random(42))
    second = MapModule.generate(5, 6, walls=6, treasures=4, traps=3, rng=random.Random(42))

    map_a, pos_a_a, pos_b_a = first
    map_b, pos_a_b, pos_b_b = second
    assert (pos_a_a, pos_b_a) == (pos_a_b, pos_b_b)
    assert map_a.map_id == map_b.map_id
    assert map_a.terrain == map_b.terrain
    assert dict(map_a.treasure_values) == dict(map_b.treasure_values)
    assert dict(map_a.trap_values) == dict(map_b.trap_values)


def test_generate_uses_only_injected_rng(monkeypatch):
    """ADR-08：生成器不得触碰全局 ``random``。"""

    def boom(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("MapModule.generate 使用了全局 random")

    for name in ("shuffle", "randint", "choice", "randrange", "sample", "random"):
        monkeypatch.setattr(random, name, boom)

    true_map, pos_a, pos_b = MapModule.generate(4, 4, walls=3, treasures=2, traps=2, rng=random.Random(7))
    assert true_map.height == 4 and true_map.width == 4
    assert pos_a != pos_b


def test_generate_supports_custom_values():
    true_map, _, _ = MapModule.generate(
        4, 5, walls=2, treasures=3, traps=2, rng=random.Random(9), treasure_value=7, trap_value=-9
    )
    assert set(true_map.treasure_values.values()) == {7}
    assert set(true_map.trap_values.values()) == {-9}
    view = MapModule(true_map).get_view_map(GameMode.NORMAL)
    assert 7 in {value for row in view.cells for value in row}
    assert -9 in {value for row in view.cells for value in row}


def test_generate_exact_fit_one_by_two():
    true_map, pos_a, pos_b = MapModule.generate(1, 2, walls=0, treasures=0, traps=0, rng=random.Random(0))
    assert true_map.terrain == ((0, 0),)
    assert {pos_a, pos_b} == {Position(0, 0), Position(0, 1)}


def test_generate_narrow_map_retries_until_connected():
    """1×8 只有"墙全部贴在两端"才连通（约 7%/次），1000 次重试内必须成功。"""
    true_map, pos_a, pos_b = MapModule.generate(1, 8, walls=3, treasures=2, traps=1, rng=random.Random(5))
    assert pos_a != pos_b
    assert {(pos.row, pos.col) for pos in PathModule.bfs_distances(true_map, pos_a)} == _non_wall_cells(true_map)


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        (dict(height=0, width=3, walls=0, treasures=0, traps=0), "height=0"),
        (dict(height=3, width=0, walls=0, treasures=0, traps=0), "width=0"),
        (dict(height=-1, width=3, walls=0, treasures=0, traps=0), "height<0"),
        (dict(height=3, width=3, walls=-1, treasures=0, traps=0), "walls<0"),
        (dict(height=3, width=3, walls=0, treasures=-1, traps=0), "treasures<0"),
        (dict(height=3, width=3, walls=0, treasures=0, traps=-1), "traps<0"),
        (dict(height=1, width=3, walls=0, treasures=2, traps=0), "容量不足 3 < 2+0+2+0"),
        (dict(height=2, width=2, walls=1, treasures=1, traps=1), "容量不足 4 < 5"),
    ],
)
def test_generate_rejects_invalid_parameters(kwargs, reason):
    with pytest.raises(ValueError) as excinfo:
        MapModule.generate(rng=random.Random(1), **kwargs)
    assert excinfo.type is ValueError, f"{reason} 应抛纯 ValueError：{excinfo.type}"


@pytest.mark.parametrize(
    "overrides, reason",
    [
        (dict(treasure_value=1), "宝藏分值 1 已被墙占用（D1）"),
        (dict(treasure_value=0), "宝藏分值必须 >= 2"),
        (dict(trap_value=0), "陷阱扣分必须 <= -1"),
        (dict(trap_value=1), "陷阱扣分不能为正"),
        (dict(max_attempts=0), "max_attempts 必须为正"),
        (dict(max_attempts=-3), "max_attempts 不能为负"),
    ],
)
def test_generate_rejects_invalid_value_parameters(overrides, reason):
    with pytest.raises(ValueError) as excinfo:
        MapModule.generate(3, 3, walls=1, treasures=1, traps=1, rng=random.Random(1), **overrides)
    assert excinfo.type is ValueError, f"{reason} 应抛纯 ValueError：{excinfo.type}"


def test_generate_raises_after_max_attempts_exhausted(monkeypatch):
    """生成无解：每次布局都不连通 → 用满 ``max_attempts`` 后抛 ``MapGenerationError``。"""
    calls: list[int] = []

    def always_disconnected(*args, **kwargs):
        calls.append(1)
        return None

    monkeypatch.setattr(MapModule, "_try_layout", always_disconnected)

    with pytest.raises(MapGenerationError) as excinfo:
        MapModule.generate(4, 4, walls=4, treasures=2, traps=2, rng=random.Random(1), max_attempts=7)
    assert len(calls) == 7
    assert "7 次尝试" in str(excinfo.value)


def test_generate_rejects_non_random_rng():
    with pytest.raises(TypeError):
        MapModule.generate(3, 3, walls=1, treasures=1, traps=1, rng=object())  # type: ignore[arg-type]


# =========================================================================== #
# 2. 视图（spec 用例 3 的可见性差异）
# =========================================================================== #


@pytest.mark.parametrize("mode", [GameMode.EASY, GameMode.NORMAL])
def test_view_easy_and_normal_show_everything(mode):
    true_map = _fixture_map()
    view = MapModule(true_map).get_view_map(mode)

    assert isinstance(view, ViewMap)
    assert view.mode is mode
    assert (view.height, view.width) == (true_map.height, true_map.width)
    assert view.cell_value(0, 1) == 1  # 墙
    assert view.cell_value(1, 3) == 1  # 墙
    assert view.cell_value(0, 3) == 6  # 剩余宝藏 +v
    assert view.cell_value(2, 0) == 4  # 剩余宝藏 +v
    assert view.cell_value(1, 1) == -3  # 未触发陷阱 -v（完全信息）
    assert view.cell_value(0, 0) == 0  # 空地
    assert view.spent_traps == frozenset()


def test_view_hard_hides_untriggered_trap():
    """spec 用例 3：HARD 开局含陷阱的格子视图为 0，同图 NORMAL 一开始就是负值。"""
    true_map = _fixture_map()

    hard_view = MapModule(true_map).get_view_map(GameMode.HARD)
    assert hard_view.cell_value(TRAP_POS.row, TRAP_POS.col) == 0
    assert hard_view.spent_traps == frozenset()

    normal_view = MapModule(true_map).get_view_map(GameMode.NORMAL)
    assert normal_view.cell_value(TRAP_POS.row, TRAP_POS.col) == -3

    # HARD 只藏陷阱：墙与宝藏照旧可见
    assert hard_view.cell_value(0, 1) == 1
    assert hard_view.cell_value(0, 3) == 6


def test_view_hard_reveals_triggered_trap_and_marks_spent():
    true_map = _fixture_map()
    played = dataclasses.replace(
        true_map,
        active_traps=frozenset(),
        revealed_traps=frozenset({TRAP_POS}),
    )
    view = MapModule(played).get_view_map(GameMode.HARD)

    assert view.cell_value(TRAP_POS.row, TRAP_POS.col) == -3  # 暴露后显示原始负值
    assert view.spent_traps == frozenset({TRAP_POS})  # 再踩不扣分
    # 其余未触发陷阱仍隐藏（本图仅一个陷阱，故无其它差异）
    assert view.cell_value(0, 0) == 0


def test_view_hard_hides_picked_treasure():
    true_map = _fixture_map()
    picked = dataclasses.replace(
        true_map, remaining_treasures=true_map.remaining_treasures - {TREASURE_POS}
    )
    view = MapModule(picked).get_view_map(GameMode.HARD)
    assert view.cell_value(TREASURE_POS.row, TREASURE_POS.col) == 0
    assert view.cell_value(2, 0) == 4


def test_view_is_detached_frozen_snapshot():
    true_map = _fixture_map()
    view = MapModule(true_map).get_view_map(GameMode.EASY)

    assert view is not true_map
    assert not isinstance(view, TrueMap)
    # 不暴露 TrueMap 内部引用：行元组是新建的，也没有资源层属性
    assert view.cells[0] is not true_map.terrain[0]
    for row_view, row_true in zip(view.cells, true_map.terrain):
        assert row_view is not row_true
    assert not hasattr(view, "treasure_values")
    assert not hasattr(view, "trap_values")
    assert not hasattr(view, "remaining_treasures")

    with pytest.raises(dataclasses.FrozenInstanceError):
        view.cells = ()  # type: ignore[misc]


def test_view_snapshot_not_affected_by_later_state_changes():
    true_map = _fixture_map()
    before = MapModule(true_map).get_view_map(GameMode.NORMAL)
    picked = dataclasses.replace(
        true_map, remaining_treasures=true_map.remaining_treasures - {TREASURE_POS}
    )
    after = MapModule(picked).get_view_map(GameMode.NORMAL)

    assert before.cell_value(TREASURE_POS.row, TREASURE_POS.col) == 6
    assert after.cell_value(TREASURE_POS.row, TREASURE_POS.col) == 0
    assert TREASURE_POS not in before.spent_traps


def test_get_view_map_rejects_unknown_mode():
    with pytest.raises(ValueError):
        MapModule(_fixture_map()).get_view_map("HARD")  # type: ignore[arg-type]


def test_map_module_rejects_view_map():
    view = MapModule(_fixture_map()).get_view_map(GameMode.EASY)
    with pytest.raises(TypeError):
        MapModule(view)  # type: ignore[arg-type]


# =========================================================================== #
# 3. txt 读写
# =========================================================================== #


def test_save_txt_writes_frozen_format(tmp_path):
    path = tmp_path / "fixture.txt"
    MapModule.save_txt(path, _fixture_map(), POS_A, POS_B)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "3 4"
    assert lines[1] == "0 1 0 6"
    assert lines[2] == "0 -3 0 1"
    assert lines[3] == "4 0 0 0"
    assert lines[4] == "A 0 0"
    assert lines[5] == "B 2 3"
    assert len(lines) == 6


def test_save_load_round_trip(tmp_path):
    true_map = _fixture_map()
    path = tmp_path / "round_trip.txt"

    MapModule.save_txt(path, true_map, POS_A, POS_B)
    loaded, pos_a, pos_b = MapModule.load_txt(path)

    assert (pos_a, pos_b) == (POS_A, POS_B)
    assert (loaded.height, loaded.width) == (true_map.height, true_map.width)
    assert loaded.terrain == true_map.terrain
    assert dict(loaded.treasure_values) == dict(true_map.treasure_values)
    assert dict(loaded.trap_values) == dict(true_map.trap_values)
    assert loaded.remaining_treasures == true_map.remaining_treasures
    assert loaded.active_traps == true_map.active_traps
    assert loaded.revealed_traps == frozenset()
    # 视图也逐格一致
    assert MapModule(loaded).get_view_map(GameMode.NORMAL).cells == MapModule(true_map).get_view_map(GameMode.NORMAL).cells


def test_save_load_round_trip_of_generated_map(tmp_path):
    true_map, pos_a, pos_b = MapModule.generate(6, 6, walls=7, treasures=5, traps=4, rng=random.Random(31))
    path = tmp_path / "generated.txt"

    MapModule.save_txt(path, true_map, pos_a, pos_b)
    loaded, loaded_a, loaded_b = MapModule.load_txt(path)

    assert (loaded_a, loaded_b) == (pos_a, pos_b)
    assert loaded.map_id == true_map.map_id  # 静态指纹往返不变
    assert loaded.terrain == true_map.terrain
    assert dict(loaded.treasure_values) == dict(true_map.treasure_values)
    assert dict(loaded.trap_values) == dict(true_map.trap_values)


def test_load_txt_accepts_empty_resource_map(tmp_path):
    path = tmp_path / "empty_resources.txt"
    path.write_text("2 2\n0 0\n0 0\nA 0 0\nB 1 1\n", encoding="utf-8")

    true_map, pos_a, pos_b = MapModule.load_txt(path)
    assert true_map.treasure_values == {}
    assert true_map.trap_values == {}
    assert true_map.is_finished()  # 初态没有宝藏 → 零回合终局（D11）
    assert (pos_a, pos_b) == (Position(0, 0), Position(1, 1))


def test_load_txt_accepts_unlabeled_birth_points(tmp_path):
    path = tmp_path / "unlabeled.txt"
    path.write_text("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\n0 0\n2 3\n", encoding="utf-8")

    true_map, pos_a, pos_b = MapModule.load_txt(path)
    assert (pos_a, pos_b) == (POS_A, POS_B)
    assert dict(true_map.treasure_values) == {TREASURE_POS: 6, Position(2, 0): 4}


def test_load_txt_tolerates_bom_and_blank_lines(tmp_path):
    path = tmp_path / "bom.txt"
    path.write_text("\ufeff3 4\n0 1 0 6\n\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 3\n\n", encoding="utf-8")

    true_map, pos_a, pos_b = MapModule.load_txt(path)
    assert (pos_a, pos_b) == (POS_A, POS_B)
    assert dict(true_map.trap_values) == {TRAP_POS: -3}


VALID_TEXT = "3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 3\n"


@pytest.mark.parametrize(
    "text, reason",
    [
        ("", "空文件"),
        ("\n\n", "只有空行"),
        ("3\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 3\n", "首行缺 width"),
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\n", "缺 B 行"),
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\n", "缺两行出生点"),
        (VALID_TEXT + "9\n", "多余行"),
        ("3 4\n0 1 0\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 3\n", "第 1 行只有 3 个整数（非矩形）"),
        ("3 4\n0 1 0 6 0\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 3\n", "第 1 行 5 个整数（非矩形）"),
        ("3 4\n0 1 0 x\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 3\n", "矩阵含非整数"),
        ("3 4.5\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 3\n", "首行含非整数"),
        ("0 4\nA 0 0\nB 0 1\n", "height=0"),
        ("-1 4\nA 0 0\nB 0 1\n", "height<0"),
        ("3 0\nA 0 0\nB 0 1\n", "width=0"),
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nB 0 0\nA 2 3\n", "出生点行标签互换"),
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0 0\nB 2 3\n", "A 行字段过多"),
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\nB z 3\n", "B 行坐标非整数"),
    ],
)
def test_load_txt_rejects_malformed_text(tmp_path, text, reason):
    path = tmp_path / "bad.txt"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MapFormatError) as excinfo:
        MapModule.load_txt(path)
    assert isinstance(excinfo.value, ValueError), reason


@pytest.mark.parametrize(
    "text, reason",
    [
        # A、B 重合
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 2 3\nB 2 3\n", "出生点重合"),
        # A 落在墙上
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 1\nB 2 3\n", "A 在墙上"),
        # B 落在墙上
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 1 3\n", "B 在墙上"),
        # 出生点越界
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 5 0\nB 2 3\n", "A 越界"),
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 2 9\n", "B 越界"),
        # 出生点占资源
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 3\nB 2 3\n", "A 占宝藏"),
        ("3 4\n0 1 0 6\n0 -3 0 1\n4 0 0 0\nA 0 0\nB 1 1\n", "B 占陷阱"),
        # 非墙格不连通：1×4 中间是墙，A 被隔离
        ("1 4\n0 1 0 0\nA 0 0\nB 0 3\n", "A 被墙隔离"),
        ("3 4\n0 1 0 0\n1 1 0 0\n0 0 0 0\nA 0 0\nB 2 3\n", "左上角孤立"),
    ],
)
def test_load_txt_rejects_illegal_geometry(tmp_path, text, reason):
    path = tmp_path / "illegal.txt"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MapFormatError):
        MapModule.load_txt(path)


def test_load_txt_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        MapModule.load_txt(tmp_path / "not_exists.txt")


def test_save_txt_rejects_played_map(tmp_path):
    path = tmp_path / "played.txt"
    true_map = _fixture_map()

    picked = dataclasses.replace(
        true_map, remaining_treasures=true_map.remaining_treasures - {TREASURE_POS}
    )
    with pytest.raises(IllegalStateError):
        MapModule.save_txt(path, picked, POS_A, POS_B)
    assert not path.exists()

    triggered = dataclasses.replace(
        true_map, active_traps=frozenset(), revealed_traps=frozenset({TRAP_POS})
    )
    with pytest.raises(IllegalStateError):
        MapModule.save_txt(path, triggered, POS_A, POS_B)
    assert not path.exists()


@pytest.mark.parametrize(
    "pos_a, pos_b, reason",
    [
        (Position(0, 1), POS_B, "A 在墙上"),
        (POS_A, Position(1, 3), "B 在墙上"),
        (Position(0, 0), Position(0, 0), "出生点重合"),
        (Position(9, 9), POS_B, "A 越界"),
        (Position(0, 3), POS_B, "A 占宝藏"),
        (POS_A, TRAP_POS, "B 占陷阱"),
    ],
)
def test_save_txt_rejects_invalid_birth_points(tmp_path, pos_a, pos_b, reason):
    path = tmp_path / "bad_birth.txt"
    with pytest.raises(ValueError) as excinfo:
        MapModule.save_txt(path, _fixture_map(), pos_a, pos_b)
    assert isinstance(excinfo.value, MapFormatError), reason
    assert not path.exists()


def test_save_txt_rejects_view_map(tmp_path):
    view = MapModule(_fixture_map()).get_view_map(GameMode.EASY)
    with pytest.raises(TypeError):
        MapModule.save_txt(tmp_path / "view.txt", view, POS_A, POS_B)  # type: ignore[arg-type]


def test_map_module_rejects_malformed_true_map():
    """底图含 0/1 之外的值必须被拒绝（models 只校验形状）。"""
    broken = TrueMap(
        height=2,
        width=2,
        terrain=((0, 2), (0, 0)),
        treasure_values={},
        trap_values={},
        remaining_treasures=frozenset(),
        active_traps=frozenset(),
        revealed_traps=frozenset(),
    )
    with pytest.raises(ValueError):
        MapModule(broken)


def test_load_txt_path_argument_accepts_str(tmp_path):
    path = tmp_path / "as_str.txt"
    path.write_text(VALID_TEXT, encoding="utf-8")
    true_map, pos_a, pos_b = MapModule.load_txt(str(path))  # type: ignore[arg-type]
    assert (pos_a, pos_b) == (POS_A, POS_B)
    assert isinstance(true_map, TrueMap)
    assert pathlib.Path(path).exists()
