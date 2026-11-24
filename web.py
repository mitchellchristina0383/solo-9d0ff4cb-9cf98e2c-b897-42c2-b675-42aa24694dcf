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
        "started_ts": None,        # 本轮开始的 epoch 秒（过滤服务端会话用）
        "finished_ts": None,       # 本轮下载结束的 epoch 秒
        "result": None,
        "live": {"client_port": None, "client_ip": None,
                 "client_addr": None, "server_addr": None,
                 "server_tid_port": None},
        "server_session": None,    # 本轮服务端会话的定格快照（终值）
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


# 本端与服务端时钟容差（Compose 下同宿主机时钟一致；跨机部署时吸收 NTP 小偏差）
CLOCK_SKEW = 2.0
# 捕获终态快照的默认轮询间隔/兜底窗口
CAPTURE_POLL = 0.2
CAPTURE_FALLBACK_HORIZON = 3.0


def _match_run_session(stats: Optional[dict], client_ip, client_port,
                       t_min: Optional[float] = None,
                       t_max: Optional[float] = None,
                       prefer: str = "any") -> Optional[dict]:
    """在 stats 里定位“本轮下载”的服务端会话。

    匹配键 = 源端口 + 会话开始时间窗（started_ts ∈ [t_min, t_max]），
    同组候选多个时取 seq 最大（最新）的一条——因此：
    * 同源 IP:端口被新会话复用时，不会把新会话错配给旧结果；
    * 旧历史会话也不会错配给新一轮运行。
    prefer="final" 时只在已关闭（history）的会话里找，用于捕获终值；
    prefer="active" 时先看活动会话；默认 "any" 全体取最新。
    """
    if not stats or client_port is None or "sessions" not in stats:
        return None

    def in_window(sess: dict) -> bool:
        if sess.get("client_port") != client_port:
            return False
        ts = sess.get("started_ts")
        if ts is None:
            return True  # 旧版 stats 无时间戳时退化为仅按端口
        if t_min is not None and ts < t_min:
            return False
        if t_max is not None and ts > t_max:
            return False
        return True

    def newest(candidates) -> Optional[dict]:
        if not candidates:
            return None
        exact = [s for s in candidates
                 if not client_ip or s.get("client_ip") in (None, client_ip)]
        if exact:
            candidates = exact
        candidates.sort(key=lambda s: (s.get("seq") or 0,
                                       s.get("started_ts") or 0.0))
        return candidates[-1]

    history = [s for s in stats.get("history", []) if in_window(s)]
    active = [s for s in stats.get("sessions", []) if in_window(s)]
    if prefer == "final":
        return newest(history)
    if prefer == "active":
        return newest(active) or newest(history)
    return newest(history + active)


def _capture_final_session(lane_id: str, run_token: str,
                           client_ip, client_port,
                           run_started: float, run_finished: float) -> None:
    """本轮下载结束后，把该轮服务端会话的**终态**快照定格到路状态里。

    之后 /api/state 直接返回定格值，不再向服务端历史重新匹配——
    同源端口被复用、历史滚出 30 条上限、页面关闭重开都不影响展示。
    末个 ACK 若丢失，服务端会话要等超时重试耗尽才关闭，故捕获窗口
    覆盖完整重试周期；期间页面仍走实时匹配兜底。
    """
    if client_port is None:
        return
    t_min = run_started - CLOCK_SKEW
    t_max = run_finished + CLOCK_SKEW
    start = time.monotonic()
    deadline = start + CAPTURE_FALLBACK_HORIZON
    best: Optional[dict] = None
    while True:
        with lanes_lock:
            if lanes[lane_id]["run_token"] != run_token:
                return  # 该路已重新试跑，旧捕获作废
        stats = fetch_stats()
        if stats and "sessions" in stats:
            cfg = stats.get("config") or {}
            try:
                horizon = (float(cfg.get("timeout_seconds", 1.0))
                           * (int(cfg.get("max_retries", 4)) + 1) + 2.0)
                deadline = max(deadline, start + horizon)
            except (TypeError, ValueError):
                pass
            snap = _match_run_session(stats, client_ip, client_port,
                                      t_min, t_max, prefer="final")
            if snap is not None:
                best = snap      # 已进 history 的终态
                break
            snap = _match_run_session(stats, client_ip, client_port,
                                      t_min, t_max, prefer="active")
            if snap is not None:
                best = snap      # 会话仍在推进，先记最新观测
        if time.monotonic() >= deadline:
            break
        time.sleep(CAPTURE_POLL)

    if best is None:
        return
    with lanes_lock:
        if lanes[lane_id]["run_token"] != run_token:
            return
        lanes[lane_id]["server_session"] = best


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
        lane["started_ts"] = time.time()
        live = lane["live"]

    run_started = time.time()
    result = terminal.download_once(TFTP_HOST, TFTP_PORT, mode=mode, live=live)
    run_finished = time.time()

    with lanes_lock:
        if lanes[lane_id]["run_token"] != run_token:
            return  # 已被新一轮运行取代
        lanes[lane_id]["status"] = "done" if result.get("ok") else "error"
        lanes[lane_id]["result"] = result
        lanes[lane_id]["finished_ts"] = run_finished

    # 本轮真实 UDP 下载已结束：把对应服务端会话的终态计数定格到该路，
    # 与页面是否打开无关；直到该路重新试跑前都展示这个终值。
    _capture_final_session(lane_id, run_token,
                           result.get("client_ip"), result.get("client_port"),
                           run_started, run_finished)


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
            frozen = lane.get("server_session")
            if frozen is not None:
                # 已定格：本轮终值，不随服务端历史滚动/端口复用变化
                lane["server_session_frozen"] = True
            else:
                lane["server_session_frozen"] = False
                client_port = result.get("client_port") or live.get("client_port")
                client_ip = result.get("client_ip") or live.get("client_ip")
                started_ts = lane.get("started_ts")
                finished_ts = lane.get("finished_ts")
                t_min = started_ts - CLOCK_SKEW if started_ts else None
                t_max = finished_ts + CLOCK_SKEW if finished_ts else None
                if finished_ts:
                    # 已结束但定格尚未完成：先看 history 终态，再看活动会话
                    lane["server_session"] = (
                        _match_run_session(stats, client_ip, client_port,
                                           t_min, t_max, prefer="final")
                        or _match_run_session(stats, client_ip, client_port,
                                              t_min, t_max, prefer="active"))
                else:
                    lane["server_session"] = _match_run_session(
                        stats, client_ip, client_port, t_min, t_max,
                        prefer="active")
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
