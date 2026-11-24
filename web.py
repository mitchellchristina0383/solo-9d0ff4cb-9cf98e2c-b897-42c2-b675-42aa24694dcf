"""Flask 试跑页：同时发起两路真实 TFTP 下载。

页面 -> 本服务 JSON API -> terminal.py 测试终端 -> 真实 UDP -> server.py TFTP 服务。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from typing import Dict, Optional

from flask import Flask, jsonify, render_template, request

import firmware as fw
import terminal

TFTP_HOST = os.environ.get("TFTP_HOST", "127.0.0.1")
TFTP_PORT = int(os.environ.get("TFTP_PORT", "1069"))
STATS_URL = os.environ.get("STATS_URL", "http://127.0.0.1:18001/stats")
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))

app = Flask(__name__)

LANE_IDS = ("A", "B")
MODES = ("normal", "lose-first")


def _blank_lane(lane_id: str) -> dict:
    return {
        "id": lane_id,
        "status": "idle",          # idle | starting | running | done | error
        "mode": "normal",
        "run_token": None,
        "started_at": None,
        "result": None,
        "live": {"client_port": None, "client_ip": None,
                 "client_addr": None, "server_addr": None,
                 "server_tid_port": None},
        "server_session": None,
    }


lanes: Dict[str, dict] = {lid: _blank_lane(lid) for lid in LANE_IDS}
lanes_lock = threading.RLock()

full_test: dict = {
    "status": "idle",              # idle | running | done | error
    "report": None,
    "log": [],
}
full_test_lock = threading.RLock()


# ---------------------------------------------------------------- 工具
def fetch_stats(timeout: float = 1.2) -> Optional[dict]:
    try:
        with urllib.request.urlopen(STATS_URL, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"reachable": False, "error": str(exc), "stats_url": STATS_URL}


def _find_server_session(stats: Optional[dict], client_port) -> Optional[dict]:
    if not stats or client_port is None or "sessions" not in stats:
        return None
    for sess in stats.get("sessions", []):
        if sess.get("client_port") == client_port:
            return sess
    # 已结束的会话落在 history 里
    for sess in stats.get("history", []):
        if sess.get("client_port") == client_port:
            return sess
    return None


def _lane_worker(lane_id: str, mode: str, run_token: str,
                 barrier: Optional[threading.Barrier]) -> None:
    if barrier is not None:
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass

    with lanes_lock:
        lane = lanes[lane_id]
        lane["status"] = "running"
        lane["started_at"] = time.strftime("%H:%M:%S")
        live = lane["live"]

    result = terminal.download_once(TFTP_HOST, TFTP_PORT, mode=mode, live=live)

    with lanes_lock:
        if lanes[lane_id]["run_token"] != run_token:
            return  # 已被新一轮运行取代
        lanes[lane_id]["status"] = "done" if result.get("ok") else "error"
        lanes[lane_id]["result"] = result


def _start_lanes(targets, modes: Dict[str, str], simultaneous: bool):
    """启动 targets 中的路。simultaneous=True 时用栅栏让两路尽量同时发 RRQ。"""
    run_token = uuid.uuid4().hex[:12]
    with lanes_lock:
        busy = [lid for lid in targets
                if lanes[lid]["status"] in ("starting", "running")]
        if busy:
            return None, (jsonify({"ok": False,
                                   "error": f"路 {','.join(busy)} 正在下载中"}), 409)
        chosen = {}
        for lid in targets:
            mode = (modes or {}).get(lid, "normal")
            if mode not in MODES:
                mode = "normal"
            lane = _blank_lane(lid)
            lane.update({"mode": mode, "status": "starting",
                         "run_token": run_token})
            lanes[lid] = lane
            chosen[lid] = mode

        barrier = threading.Barrier(len(targets)) if simultaneous else None
        for lid in targets:
            t = threading.Thread(
                target=_lane_worker,
                args=(lid, chosen[lid], run_token, barrier),
                daemon=True,
            )
            t.start()
    return run_token, None


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/")
def index():
    return render_template(
        "index.html",
        image_name=fw.IMAGE_NAME,
        image_size=fw.IMAGE_SIZE,
        block_size=fw.BLOCK_SIZE,
        image_sha=fw.IMAGE_SHA256,
        tftp_target=f"{TFTP_HOST}:{TFTP_PORT}",
        stats_url=STATS_URL,
    )


@app.get("/api/config")
def api_config():
    return jsonify({
        "image_name": fw.IMAGE_NAME,
        "image_size": fw.IMAGE_SIZE,
        "block_size": fw.BLOCK_SIZE,
        "last_block_number": fw.LAST_BLOCK_NUMBER,
        "image_sha256": fw.IMAGE_SHA256,
        "tftp_host": TFTP_HOST,
        "tftp_port": TFTP_PORT,
        "stats_url": STATS_URL,
        "max_slots": 2,
        "modes": list(MODES),
    })


@app.post("/api/both/start")
def api_both_start():
    body = request.get_json(silent=True) or {}
    modes = body.get("modes") if isinstance(body.get("modes"), dict) else body
    run_token, err = _start_lanes(list(LANE_IDS), modes or {}, simultaneous=True)
    if err is not None:
        return err
    return jsonify({"ok": True, "run_token": run_token})


@app.post("/api/lane/<lane_id>/start")
def api_lane_start(lane_id: str):
    if lane_id not in LANE_IDS:
        return jsonify({"ok": False, "error": "unknown lane"}), 404
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "normal")
    run_token, err = _start_lanes([lane_id], {lane_id: mode}, simultaneous=False)
    if err is not None:
        return err
    return jsonify({"ok": True, "run_token": run_token})


@app.get("/api/state")
def api_state():
    stats = fetch_stats()
    with lanes_lock:
        lane_view = {}
        for lid in LANE_IDS:
            lane = dict(lanes[lid])
            result = lane.get("result") or {}
            live = dict(lane.get("live") or {})
            lane["live"] = live
            client_port = result.get("client_port") or live.get("client_port")
            lane["server_session"] = _find_server_session(stats, client_port)
            lane_view[lid] = lane
    return jsonify({
        "ok": True,
        "time": time.strftime("%H:%M:%S"),
        "lanes": lane_view,
        "server": stats,
    })


@app.get("/api/server-stats")
def api_server_stats():
    stats = fetch_stats()
    if stats is None:
        return jsonify({"reachable": False, "error": "stats fetch failed"}), 502
    return jsonify(stats)


# -------------------------------------------------------- 满载/名额自检
def _full_test_worker() -> None:
    def emit(name: str, detail: str) -> None:
        with full_test_lock:
            full_test["log"].append({"name": name, "detail": detail,
                                     "t": time.strftime("%H:%M:%S")})

    try:
        report = terminal.full_slot_probe(TFTP_HOST, TFTP_PORT, STATS_URL, emit)
        with full_test_lock:
            full_test["status"] = "done" if report.get("ok") else "error"
            full_test["report"] = report
    except Exception as exc:  # 自检线程不能静默死掉
        with full_test_lock:
            full_test["status"] = "error"
            full_test["report"] = {"ok": False, "fatal": repr(exc)}


@app.post("/api/full-test/start")
def api_full_test_start():
    with full_test_lock:
        if full_test["status"] == "running":
            return jsonify({"ok": False, "error": "自检已在运行"}), 409
        full_test["status"] = "running"
        full_test["report"] = None
        full_test["log"] = []
    threading.Thread(target=_full_test_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/full-test/state")
def api_full_test_state():
    with full_test_lock:
        return jsonify({
            "status": full_test["status"],
            "report": full_test["report"],
            "log": list(full_test["log"]),
        })


if __name__ == "__main__":
    # 兜底：直接 `python web.py` 且 TFTP 指向本机时，在守护线程内自带 TFTP 服务，
    # 做到单命令可试跑。Docker Compose 的 web 服务显式 TFTP_AUTOSTART=0，
    # 走独立的 tftp 容器（真实跨容器 UDP）。
    if os.environ.get("TFTP_AUTOSTART", "1") == "1" and TFTP_HOST in (
            "127.0.0.1", "localhost", "::1"):
        try:
            from urllib.parse import urlparse
            import server as tftp_server

            tftp_server.TFTP_HOST = "0.0.0.0"
            tftp_server.TFTP_PORT = TFTP_PORT
            parsed = urlparse(STATS_URL)
            if parsed.port:
                tftp_server.STATS_PORT = parsed.port
            threading.Thread(
                target=lambda: tftp_server.TFTPServer().serve_forever(),
                daemon=True,
            ).start()
            print(f"[web] TFTP_AUTOSTART: 内置 TFTP 已在 udp/{TFTP_PORT} 启动",
                  flush=True)
        except Exception as exc:  # 端口被占/无权限时不影响网页，可改用外部 tftp
            print(f"[web] TFTP_AUTOSTART 跳过：{exc!r}", flush=True)

    print(f"[web] Flask on {WEB_HOST}:{WEB_PORT}, "
          f"tftp={TFTP_HOST}:{TFTP_PORT}, stats={STATS_URL}", flush=True)
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, debug=False)
