"""Web 交互回归：骰子门控、隐藏陷阱、机机回合、取消与 HTTP 接口。"""

import json
import random
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from treasure_game.gui import GameController
from treasure_game.ai_module import AiModule
from treasure_game.models import GameMode
from treasure_game.web import WebGame, make_server


class IdleAI:
    def get_greedy_move(self, state, dice_N, seat=None):
        return ()

    def get_opt_move(self, state, dice_N, seat=None):
        return ()


@pytest.fixture
def game(tmp_path):
    controller = GameController(rng=random.Random(0), ai_factory=IdleAI, max_turns=2)
    path = tmp_path / "map.txt"
    path.write_text("2 3\n0 -2 2\n0 0 0\nA 0 0\nB 1 2\n", encoding="utf-8")
    controller.load_txt(path)
    controller.set_mode(GameMode.HARD)
    session = WebGame(controller)
    yield session
    session.command("/api/stop", {})
    if session.worker:
        session.worker.join(3)
        assert not session.worker.is_alive()


def wait_for(game, phase):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = game.snapshot()
        if snapshot["phase"] == phase:
            return snapshot
        time.sleep(0.005)
    pytest.fail(f"等待 {phase} 超时，当前状态：{game.snapshot()['phase']}")


def test_human_roll_hides_value_and_trap_until_clicked(game):
    game.command("/api/start", {"kind": "human"})
    state = wait_for(game, "roll")
    assert state["dice"] is None and state["cells"][0][1] == 0
    assert state["events"] == []
    with pytest.raises(ValueError, match="先投骰"):
        game.command("/api/action", {"frame_id": state["frame_id"], "action": "d d"})
    with pytest.raises(ValueError, match="更新"):
        game.command("/api/roll", {"frame_id": state["frame_id"] - 1})
    with pytest.raises(ValueError, match="中止"):
        game.command("/api/map/edit", {"row": 0, "col": 1})
    rolled = game.command("/api/roll", {"frame_id": state["frame_id"]})
    assert rolled["phase"] == "rolling" and rolled["dice"] == 4
    with pytest.raises(ValueError, match="投过"):
        game.command("/api/roll", {"frame_id": state["frame_id"]})
    game.command("/api/ready", {"frame_id": state["frame_id"]})
    wait_for(game, "input")
    with pytest.raises(ValueError, match="非法"):
        game.command("/api/action", {"frame_id": state["frame_id"], "action": "d x"})
    game.command("/api/action", {"frame_id": state["frame_id"], "action": "d d"})
    final = wait_for(game, "finished")
    trap = next(event for event in final["events"] if event["kind"] == "TRAP")
    assert trap["actor"] == "A" and "A（人类）" in trap["message"]
    assert "陷阱已暴露" in trap["message"] and final["cells"][0][1] == -2
    assert final["score_a"] == 0 and final["report_ready"]
    assert "模式：" in game.download(report=True)


def test_machine_turns_automatically_roll_once_and_stop_at_timeout(game):
    game.command("/api/start", {"kind": "machine"})
    seen = []
    for actor in ("A", "B", "A", "B"):
        state = wait_for(game, "rolling")
        assert state["turn"] == actor and state["dice"] is not None
        seen.append(state["dice"])
        game.command("/api/ready", {"frame_id": state["frame_id"]})
    final = wait_for(game, "finished")
    assert final["result"]["timeout"]
    assert game.controller.last_result.metrics["dice_sequence"] == seen
    assert final["labels"]["A"] == "贪心 AI"


def test_ai_calculation_updates_same_log_entry_and_exports(game, tmp_path):
    started, proceed = threading.Event(), threading.Event()

    class WaitingAI(AiModule):
        def get_greedy_move(self, state, dice_N, seat):
            started.set()
            assert proceed.wait(3)
            return super().get_greedy_move(state, dice_N, seat)

    game.controller.ai_factory = lambda: WaitingAI(depth=1)
    path = tmp_path / "calculation-map.txt"
    path.write_text("4 4\n0 0 0 3\n0 0 0 0\n0 0 0 0\n4 0 0 0\nA 0 0\nB 3 3\n", encoding="utf-8")
    game.controller.load_txt(path)
    game.command("/api/start", {"kind": "machine"})
    state = wait_for(game, "rolling")
    game.command("/api/ready", {"frame_id": state["frame_id"]})
    try:
        assert started.wait(2)
        thinking = game.snapshot()
        entry = next(item for item in thinking["logs"] if item["kind"] == "ai")
        assert thinking["phase"] == "thinking" and entry["status"] == "thinking"
        assert "第 1 轮 · A（贪心 AI）" in entry["message"]
        assert f"骰子 N={state['dice']}" in entry["details"][0]
    finally:
        proceed.set()
    next_turn = wait_for(game, "rolling")
    completed = next(item for item in next_turn["logs"] if item["id"] == entry["id"])
    assert completed["status"] == "complete" and next_turn["turn"] == "B"
    assert "BFS 距离" in "\n".join(completed["details"])
    assert entry["status"] == "thinking"  # 已返回的快照不随后台计算改变。
    game.command("/api/ready", {"frame_id": next_turn["frame_id"]})
    final = wait_for(game, "finished")
    calculations = [item for item in final["logs"] if item["kind"] == "ai"]
    assert len(calculations) == 2
    assert "B（OPT 最优 AI）" in calculations[1]["message"]
    assert "困难模式隐藏" in "\n".join(calculations[1]["details"])
    assert "AI 计算过程" in game.download(report=True)
    # 新局清空记录，同时分配新 ID，页面不会继承上一局的展开状态。
    game.command("/api/start", {"kind": "machine"})
    restarted = wait_for(game, "rolling")
    assert restarted["logs"] == []


def test_failed_ai_calculation_closes_pending_entry(game):
    class BrokenAI(IdleAI):
        def get_greedy_move(self, state, dice_N, seat=None):
            raise RuntimeError("测试计算失败")

    game.controller.ai_factory = BrokenAI
    game.command("/api/start", {"kind": "machine"})
    state = wait_for(game, "rolling")
    game.command("/api/ready", {"frame_id": state["frame_id"]})
    final = wait_for(game, "error")
    assert not final["running"]
    entry = next(item for item in final["logs"] if item["kind"] == "ai")
    assert entry["status"] == "error"


@pytest.mark.parametrize("phase", ["roll", "rolling", "input"])
def test_stop_releases_waits_without_report(game, phase):
    game.command("/api/start", {"kind": "human"})
    state = wait_for(game, "roll")
    if phase != "roll":
        game.command("/api/roll", {"frame_id": state["frame_id"]})
    if phase == "input":
        game.command("/api/ready", {"frame_id": state["frame_id"]})
    wait_for(game, phase)
    game.command("/api/stop", {})
    final = wait_for(game, "aborted")
    assert not final["running"] and not final["report_ready"]


def test_map_generation_and_edit_keep_random_birthpoints_empty(game):
    state = game.command("/api/map/generate", {
        "height": 5, "width": 7, "walls": 5, "treasures": 4, "traps": 2, "seed": 9,
    })
    assert state["pos_a"] != state["pos_b"]
    for key in ("pos_a", "pos_b"):
        row, col = state[key]
        assert state["cells"][row][col] == 0
        with pytest.raises(ValueError, match="空地"):
            game.command("/api/map/edit", {"row": row, "col": col})
    exported = game.download()
    assert f"A {state['pos_a'][0]} {state['pos_a'][1]}" in exported
    assert f"B {state['pos_b'][0]} {state['pos_b'][1]}" in exported


def test_http_assets_state_map_export_and_bad_requests(game):
    server = make_server(0, game)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for route, marker in (("/", b"DiceHunt"), ("/app.js", b"/api/roll"), ("/styles.css", b".board")):
            with urlopen(base + route, timeout=3) as response:
                assert response.status == 200 and marker in response.read()
        with urlopen(base + "/api/state", timeout=3) as response:
            state = json.load(response)
            assert state["cells"][0][1] == 0
        with urlopen(base + "/api/map/download", timeout=3) as response:
            assert "attachment" in response.headers["Content-Disposition"]
            assert b"A 0 0" in response.read()
        for route, body, headers, status in (
            ("/api/start", b"{", {}, 400),
            ("/api/start", b'{}', {"Origin": "https://example.com"}, 403),
            ("/api/unknown", b'{}', {}, 400),
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + route, data=body, headers=headers), timeout=3)
            assert error.value.code == status
            assert "error" in json.load(error.value)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
