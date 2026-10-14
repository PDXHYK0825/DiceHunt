"""Web 交互回归：骰子门控、隐藏陷阱、机机回合、取消与 HTTP 接口。"""

import json
import random
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from treasure_game.gui import GameController
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


def test_map_generation_and_edit_keep_corners_empty(game):
    state = game.command("/api/map/generate", {
        "height": 5, "width": 7, "walls": 5, "treasures": 4, "traps": 2, "seed": 9,
    })
    assert state["pos_a"] == [0, 0] and state["pos_b"] == [4, 6]
    with pytest.raises(ValueError, match="空地"):
        game.command("/api/map/edit", {"row": 4, "col": 6})
    assert "A 0 0" in game.download() and "B 4 6" in game.download()


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
