#!/usr/bin/env python3
"""试跑页服务端重传统计的回归自检（端口复用 / 历史滚动 / 按轮定格）。

针对的缺陷：丢首块下载完成后，若普通终端复用同一源 IP:端口，试跑页会把
新会话的服务端重传次数填进旧结果；后续下载把原会话挤出 30 条历史后，
计数直接消失。修复后：每路计数随本轮真实 UDP 下载定格，直到该路重新试跑。

用法（需已启动 web + tftp 双进程/双容器）：
    python scripts/web_state_checks.py \
        --web http://127.0.0.1:18080 \
        --tftp-host 127.0.0.1 --tftp-port 1069 \
        --stats http://127.0.0.1:18001/stats

注意：T2 要让“普通终端”绑定到与某路完全相同的源 IP:端口，因此本脚本必须
运行在与 web 进程相同的网络命名空间里（本机模式直接跑；Compose 模式用
`docker compose exec web python /app/scripts/web_state_checks.py ...`）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firmware as fw  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m {name} {detail}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {name} {detail}")
    return cond


def http_json(method: str, url: str, body=None, timeout: float = 3.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Web:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def state(self) -> dict:
        return http_json("GET", self.base + "/api/state")

    def server_stats(self) -> dict:
        return http_json("GET", self.base + "/api/server-stats")

    def start_lane(self, lane: str, mode: str) -> None:
        http_json("POST", f"{self.base}/api/lane/{lane}/start", {"mode": mode})

    def start_both(self, modes: dict) -> None:
        http_json("POST", self.base + "/api/both/start", {"modes": modes})


def wait_lane(web: Web, lane: str, timeout: float = 25.0) -> dict:
    """等到该路 done/error 且服务端会话已定格（server_session_frozen）。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        st = web.state()
        last = st["lanes"][lane]
        if (last["status"] in ("done", "error")
                and last.get("server_session_frozen")):
            return last
        time.sleep(0.3)
    raise RuntimeError(f"等待路 {lane} 完成并定格超时："
                       f"{json.dumps(last, ensure_ascii=False)[:400]}")


def raw_normal_download(host: str, port: int, bind_port: int = 0) -> dict:
    """“普通终端”：真实 UDP 正常逐块确认下载。bind_port 可指定源端口复用。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", bind_port))
    sock.settimeout(3.0)
    local_port = sock.getsockname()[1]
    buf = bytearray()
    retrans = 0
    arrivals = {}
    try:
        sock.sendto(fw.rrq_packet(), (host, port))
        while True:
            pkt, addr = sock.recvfrom(65535)
            if fw.parse_opcode(pkt) != fw.OP_DATA:
                continue
            block, payload = fw.parse_data(pkt)
            arrivals[block] = arrivals.get(block, 0) + 1
            if arrivals[block] >= 2:
                retrans += 1
            if arrivals[block] == 1:
                buf.extend(payload)
            sock.sendto(fw.ack_packet(block), addr)
            if len(payload) < fw.BLOCK_SIZE:
                break
    finally:
        sock.close()
    import hashlib
    return {"port": local_port, "bytes": len(buf), "retrans": retrans,
            "sha_ok": hashlib.sha256(bytes(buf)).hexdigest() == fw.IMAGE_SHA256}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default=os.environ.get("WEB_URL",
                                                    "http://127.0.0.1:18080"))
    ap.add_argument("--tftp-host", default=os.environ.get("TFTP_HOST",
                                                          "127.0.0.1"))
    ap.add_argument("--tftp-port", type=int,
                    default=int(os.environ.get("TFTP_PORT", "1069")))
    ap.add_argument("--stats", default=os.environ.get(
        "STATS_URL", "http://127.0.0.1:18001/stats"))
    args = ap.parse_args()

    web = Web(args.web)
    st0 = web.state()
    for lid in ("A", "B"):
        if st0["lanes"][lid]["status"] in ("starting", "running"):
            raise SystemExit(f"路 {lid} 正在运行，等其结束后再自检")
    srv = web.server_stats()
    if "sessions" not in srv:
        raise SystemExit(f"服务端 stats 不可达：{srv}")
    print(f"web={args.web} tftp={args.tftp_host}:{args.tftp_port} "
          f"stats={args.stats}")
    print(f"服务端 history 上限 30 条，当前 {len(srv['history'])} 条；"
          f"slots {srv['slots']['active']}/{srv['slots']['max']}")

    # ------------------------------------------------ T1 丢首块基线
    print("\n[T1] A 路丢首块下载：服务端计数随本轮定格为 1")
    web.start_lane("A", "lose-first")
    lane_a = wait_lane(web, "A")
    ra = lane_a["result"]
    ss_a = lane_a["server_session"]
    check("A 路下载成功且摘要一致", bool(ra["ok"] and ra["digest_match"]),
          f"bytes={ra['bytes_received']}")
    check("A 路终端观测重传=1", ra["retransmissions_seen"] == 1)
    check("A 路服务端计数=1（超时 1 / 重复RRQ 0）且已定格",
          ss_a is not None and ss_a["retransmit_count"] == 1
          and ss_a["timeout_retransmits"] == 1
          and ss_a["duplicate_rrq_resends"] == 0
          and lane_a["server_session_frozen"],
          json.dumps(None if not ss_a else
                     {k: ss_a[k] for k in ("retransmit_count",
                                           "timeout_retransmits", "seq")},
                     ensure_ascii=False))
    a_ip, a_port = ra["client_ip"], ra["client_port"]
    a_seq = ss_a["seq"]
    print(f"    · A 路终端 {a_ip}:{a_port}，服务端会话 seq={a_seq}")

    # ---------------------------------- T2 普通终端复用同一源 IP:端口
    print("\n[T2] 普通终端复用同一源 IP:端口：旧结果不被新会话覆盖")
    dl = raw_normal_download(args.tftp_host, args.tftp_port, bind_port=a_port)
    check("复用端口的普通下载成功且 0 重传",
          dl["port"] == a_port and dl["sha_ok"] and dl["retrans"] == 0,
          f"port={dl['port']} bytes={dl['bytes']}")
    time.sleep(0.5)  # 等服务端把新会话写入 history
    stats = web.server_stats()
    same_port = [s for s in stats["history"] if s["client_port"] == a_port]
    new_sess = max(same_port, key=lambda s: s["seq"]) if same_port else None
    check("服务端确已出现同端口新会话（seq 更大、计数 0）",
          new_sess is not None and new_sess["seq"] > a_seq
          and new_sess["retransmit_count"] == 0,
          json.dumps(None if not new_sess else
                     {k: new_sess[k] for k in ("seq", "retransmit_count")},
                     ensure_ascii=False))
    lane_a2 = web.state()["lanes"]["A"]
    check("A 路仍显示旧会话终值 1（未被新会话覆盖）",
          lane_a2["server_session"] is not None
          and lane_a2["server_session"]["seq"] == a_seq
          and lane_a2["server_session"]["retransmit_count"] == 1
          and lane_a2["server_session_frozen"],
          f"seq={None if not lane_a2['server_session'] else lane_a2['server_session']['seq']}")

    # ---------------------------------- T3 历史滚动：挤出 30 条上限
    print("\n[T3] 后续下载把原会话挤出历史（30 条上限）：定格值仍在")
    n_extra = 32
    for i in range(n_extra):
        d = raw_normal_download(args.tftp_host, args.tftp_port)
        if not d["sha_ok"]:
            check(f"第 {i + 1}/{n_extra} 次滚动下载摘要一致", False)
            break
    else:
        check(f"{n_extra} 次滚动下载全部成功", True)
    time.sleep(0.5)
    stats = web.server_stats()
    seqs = [s["seq"] for s in stats["history"]]
    check(f"原会话 seq={a_seq} 已被挤出 30 条历史",
          a_seq not in seqs and len(seqs) == 30,
          f"history={len(seqs)} 条")
    lane_a3 = web.state()["lanes"]["A"]
    check("A 路仍显示终值 1（页面重开也能看到）",
          lane_a3["server_session"] is not None
          and lane_a3["server_session"]["seq"] == a_seq
          and lane_a3["server_session"]["retransmit_count"] == 1
          and lane_a3["server_session_frozen"])

    # ---------------------------------- T4 两路各自独立、重跑才更新
    print("\n[T4] B 路独立试跑 / A 路重跑才更新")
    web.start_lane("B", "normal")
    lane_b = wait_lane(web, "B")
    check("B 路正常下载服务端计数=0 且定格",
          lane_b["server_session"] is not None
          and lane_b["server_session"]["retransmit_count"] == 0
          and lane_b["server_session_frozen"])
    lane_a4 = web.state()["lanes"]["A"]
    check("B 路试跑不影响 A 路终值",
          lane_a4["server_session"]["seq"] == a_seq
          and lane_a4["server_session"]["retransmit_count"] == 1)
    web.start_lane("A", "normal")
    lane_a5 = wait_lane(web, "A")
    check("A 路重新试跑后更新为本轮新值（0）",
          lane_a5["server_session"] is not None
          and lane_a5["server_session"]["seq"] > a_seq
          and lane_a5["server_session"]["retransmit_count"] == 0
          and lane_a5["server_session_frozen"])

    # ---------------------------------- T5 两路并发主流程
    print("\n[T5] 同时发起两路（A 丢首块 / B 正常）：并发与计数回归")
    web.start_both({"A": "lose-first", "B": "normal"})
    la, lb = wait_lane(web, "A"), wait_lane(web, "B")
    check("A 路终值=1、B 路终值=0、均定格",
          la["server_session"]["retransmit_count"] == 1
          and lb["server_session"]["retransmit_count"] == 0
          and la["server_session_frozen"] and lb["server_session_frozen"])
    check("两路摘要均一致",
          bool(la["result"]["digest_match"] and lb["result"]["digest_match"]))

    print(f"\n==== 结果：{PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
