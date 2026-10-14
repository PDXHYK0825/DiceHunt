"""本地 Web 界面：复用 GameController / GuiIO / Simulator，无额外运行依赖。

启动：python -m treasure_game.web，然后打开 http://127.0.0.1:8765。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from treasure_game.gui import GameController, GuiIO, SEAT_LABELS_HUMAN_VS_AI, parse_action_text
from treasure_game.models import GameMode, Seat
from treasure_game.simulator import HumanAbortError

ASSETS = pathlib.Path(__file__).resolve().parents[2] / "web"


def _position(pos):
    return [pos.row, pos.col]


class WebGame:
    """本地单局会话；浏览器确认骰子动画后才推进下一步。"""

    def __init__(self, controller=None):
        self.controller = controller or GameController()
        if not self.controller.loaded:
            self.controller.generate_map(8, 8, 8, 3, 2, seed=42)
        self.condition = threading.Condition()
        self.running = False
        self.stopped = False
        self.worker = None
        self.labels = dict(SEAT_LABELS_HUMAN_VS_AI[self.controller.mode])
        self.logs = []
        self.frame_id = 0
        self.dice_N = None
        self.ready = False
        self.action = None
        self.data = {}
        self.preview()

    def _view(self, view, pos_a, pos_b):
        return {
            "height": view.height,
            "width": view.width,
            "cells": view.cells,
            "treasures": [_position(pos) for pos in sorted(view.treasure_cells, key=lambda p: (p.row, p.col))],
            "spent_traps": [_position(pos) for pos in view.spent_traps],
            "pos_a": _position(pos_a),
            "pos_b": _position(pos_b),
            "mode": view.mode.value,
        }

    def preview(self):
        view = self.controller.authoring_view()
        self.data = {
            **self._view(view, self.controller.pos_a, self.controller.pos_b),
            "score_a": 0, "score_b": 0, "round": 0, "turn": "A",
            "remaining": len(view.treasure_cells), "dice": None,
            "phase": "idle", "frame_id": self.frame_id, "events": [], "result": None,
        }

    def snapshot(self):
        with self.condition:
            return {
                **self.data,
                "running": self.running,
                "labels": {seat.value: label for seat, label in self.labels.items()},
                "logs": list(self.logs[-200:]),
                "report_ready": self.controller.last_result is not None and not self.running,
            }

    def append_log(self, message):
        with self.condition:
            self.logs.append(str(message))

    def apply_frame(self, frame):
        with self.condition:
            if self.stopped:
                raise HumanAbortError("对局已中止")
            state = frame.state_view
            self.frame_id += 1
            self.dice_N = state.dice_N
            events = []
            for event in frame.events:
                name = (
                    f"{event.actor.value}（{self.labels[event.actor]}）"
                    if event.actor is not None else ""
                )
                message = f"触发玩家：{name}\n{event.message}" if event.kind == "TRAP" else event.message
                events.append({
                    "kind": event.kind, "actor": event.actor.value if event.actor else None,
                    "position": _position(event.position) if event.position else None,
                    "message": message,
                })
                self.logs.append(f"[{event.kind}] {message}")
            self.data = {
                **self._view(frame.view, state.pos_a, state.pos_b),
                "score_a": state.score_a, "score_b": state.score_b,
                "round": state.turn_index + 1, "turn": state.turn.value,
                "remaining": state.remaining_treasure_count,
                "dice": None if frame.manual_dice else state.dice_N,
                "phase": "roll" if frame.manual_dice else "rolling" if frame.roll_dice else "settling",
                "frame_id": self.frame_id, "events": events, "result": None,
            }
            if frame.roll_dice:
                self.ready = False
                self.condition.wait_for(lambda: self.ready or self.stopped)
                if self.stopped:
                    raise HumanAbortError("对局已中止")
                self.data["phase"] = "thinking"

    def request_human_action(self, seat, dice_N, view, state_view):
        with self.condition:
            if self.stopped:
                return None
            self.action = None
            self.data["phase"] = "input"
            self.condition.wait_for(lambda: self.action is not None or self.stopped)
            return None if self.stopped else self.action

    def start(self, kind):
        if kind not in ("human", "machine"):
            raise ValueError("对局类型必须是 human 或 machine")
        self.running = True
        self.stopped = False
        self.logs.clear()
        self.controller.last_result = None
        self.labels = (
            dict(SEAT_LABELS_HUMAN_VS_AI[self.controller.mode]) if kind == "human"
            else {Seat.A: "贪心 AI", Seat.B: "OPT 最优 AI"}
        )
        self.preview()
        self.data["phase"] = "thinking"
        self.worker = threading.Thread(target=self._run, args=(kind,), daemon=True)
        self.worker.start()

    def _run(self, kind):
        io = GuiIO(sink=self, seat_labels=self.labels)
        phase = "finished"
        outcome = None
        error = None
        try:
            if kind == "human":
                result = self.controller.run_human_vs_ai(io=io, seat_labels=self.labels)
            else:
                result = self.controller.run_machine_vs_machine(io=io)
            outcome = {
                "winner": result.winner, "turns": result.turns,
                "score_a": result.score_a, "score_b": result.score_b,
                "timeout": result.draw_timeout,
            }
        except HumanAbortError:
            phase = "aborted"
        except Exception as exc:
            phase, error = "error", str(exc)
        finally:
            with self.condition:
                if self.stopped:
                    phase, outcome = "aborted", None
                    self.controller.last_result = None
                if error:
                    self.logs.append(f"对局失败：{error}")
                self.data.update(phase=phase, result=outcome)
                self.running = False
                self.condition.notify_all()

    def command(self, route, payload):
        with self.condition:
            game_routes = {"/api/roll", "/api/ready", "/api/action", "/api/stop"}
            if self.running and route not in game_routes:
                raise ValueError("请先中止当前对局再修改地图或模式")
            if route in ("/api/roll", "/api/ready", "/api/action"):
                if not self.running or payload.get("frame_id") != self.frame_id:
                    raise ValueError("回合已经更新，请等待页面刷新")
            if route == "/api/map/generate":
                self.controller.generate_map(
                    payload["height"], payload["width"], payload["walls"],
                    payload["treasures"], payload["traps"], seed=payload.get("seed"),
                )
                self.logs = ["已生成地图：A 固定左上角，B 固定右下角。"]
                self.preview()
            elif route == "/api/map/edit":
                self.logs.append(self.controller.cycle_cell(payload["row"], payload["col"]))
                self.preview()
            elif route == "/api/map/load":
                with tempfile.TemporaryDirectory(prefix="dicehunt-map-") as directory:
                    path = pathlib.Path(directory) / "map.txt"
                    path.write_text(payload["text"], encoding="utf-8")
                    self.controller.load_txt(path)
                self.controller.source = "浏览器导入地图"
                self.logs = ["已读取 txt 地图。"]
                self.preview()
            elif route == "/api/mode":
                self.controller.set_mode(GameMode(payload["mode"]))
                self.labels = dict(SEAT_LABELS_HUMAN_VS_AI[self.controller.mode])
                self.preview()
            elif route == "/api/start":
                self.start(payload["kind"])
            elif route == "/api/roll":
                if self.data["phase"] != "roll":
                    raise ValueError("本回合已经投过骰子")
                self.data.update(dice=self.dice_N, phase="rolling")
            elif route == "/api/ready":
                if self.data["phase"] != "rolling":
                    raise ValueError("当前没有等待完成的骰子动画")
                self.ready = True
                self.data["phase"] = "thinking"
                self.condition.notify_all()
            elif route == "/api/action":
                if self.data["phase"] != "input":
                    raise ValueError("请先投骰并等待动画完成")
                action, error = parse_action_text(payload["action"], self.dice_N)
                if error:
                    raise ValueError(error)
                self.action = action
                self.data["phase"] = "thinking"
                self.condition.notify_all()
            elif route == "/api/stop":
                self.stopped = True
                if self.running:
                    self.data["phase"] = "stopping"
                self.condition.notify_all()
            else:
                raise ValueError("未知接口")
        return self.snapshot()

    def download(self, report=False):
        with self.condition:
            if self.running:
                raise ValueError("对局进行中，请等待结束后导出")
            if report:
                return self.controller.build_report_text(seat_labels=self.labels)
            with tempfile.TemporaryDirectory(prefix="dicehunt-export-") as directory:
                path = self.controller.save_txt(pathlib.Path(directory) / "map.txt")
                return path.read_text(encoding="utf-8")


def make_server(port=8765, game=None):
    session = game or WebGame()

    class Handler(BaseHTTPRequestHandler):
        def send(self, data, content_type="application/json; charset=utf-8", status=200, filename=None):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, dict) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route == "/api/state":
                self.send(session.snapshot())
            elif route in ("/api/map/download", "/api/report"):
                try:
                    report = route == "/api/report"
                    self.send(session.download(report).encode("utf-8"), "text/plain; charset=utf-8",
                              filename="dicehunt-report.txt" if report else "dicehunt-map.txt")
                except ValueError as exc:
                    self.send({"error": str(exc)}, status=400)
            elif route in ("/", "/index.html", "/app.js", "/styles.css"):
                name = "index.html" if route == "/" else route[1:]
                mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
                self.send((ASSETS / name).read_bytes(), mime[pathlib.Path(name).suffix] + "; charset=utf-8")
            else:
                self.send({"error": "页面不存在"}, status=404)

        def do_POST(self):  # noqa: N802
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError("请求内容为空或过大")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象")
                origin = self.headers.get("Origin")
                if origin and origin not in (
                    f"http://127.0.0.1:{self.server.server_port}",
                    f"http://localhost:{self.server.server_port}",
                ):
                    self.send({"error": "仅接受本地页面请求"}, status=403)
                    return
                self.send(session.command(self.path, payload))
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                self.send({"error": str(exc)}, status=400)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="DiceHunt 本地 Web 前端")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = make_server(args.port)
    print(f"DiceHunt Web: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
