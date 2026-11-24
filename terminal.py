"""网页背后的 TFTP 测试终端：真实 UDP 报文收发。

* download_once：执行一路 octet / 512 块下载，支持
    - mode="normal"     每块收到立即确认
    - mode="lose-first" 第 1 块第一次到达时故意丢弃（不确认），等服务端超时重传后再确认
  返回可直接 JSON 序列化的过程数据、重传观测与 SHA-256 摘要校验。
* full_slot_probe：验证 2 名额上限、满载拒绝第三路、超时后归还名额、归还后可再接入。
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional

import firmware as fw


def discover_source_ip(target_host: str, target_port: int) -> str:
    """查出访问 target 时内核会选用的本机源 IP（仅用于页面展示/与 stats 对账）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_host, target_port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _fetch_stats(stats_url: str, timeout: float = 1.5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(stats_url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def download_once(
    host: str,
    port: int,
    mode: str = "normal",
    filename: str = fw.IMAGE_NAME,
    overall_timeout: float = 20.0,
    live: Optional[dict] = None,
) -> dict:
    """执行一路下载。任何异常都收敛到返回结构里，不向调用线程外抛。"""
    started = time.time()
    t0 = time.monotonic()

    def ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    result: Dict[str, object] = {
        "ok": False,
        "mode": mode,
        "filename": filename,
        "client_ip": None,
        "client_port": None,
        "client_addr": None,
        "server_addr": None,
        "server_tid_port": None,
        "bytes_received": 0,
        "blocks_new": 0,
        "data_packets": 0,
        "acks_sent": 0,
        "retransmissions_seen": 0,
        "arrivals": {},
        "dropped_first_block": False,
        "terminator_empty_block": False,
        "terminator_acked": False,
        "digest_actual": None,
        "digest_expected": fw.IMAGE_SHA256,
        "digest_match": False,
        "elapsed_ms": None,
        "error": None,
        "error_code": None,
        "events": [],
    }
    events: List[dict] = result["events"]  # type: ignore[assignment]

    def ev(kind: str, text: str) -> None:
        events.append({"t_ms": ms(), "kind": kind, "text": text})

    if mode not in ("normal", "lose-first"):
        result["error"] = f"unknown mode {mode!r}"
        result["elapsed_ms"] = ms()
        return result

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.5)
    local_port = sock.getsockname()[1]
    source_ip = discover_source_ip(host, port)
    result["client_ip"] = source_ip
    result["client_port"] = local_port
    result["client_addr"] = f"{source_ip}:{local_port}"
    if live is not None:
        live.update({
            "client_ip": source_ip,
            "client_port": local_port,
            "client_addr": result["client_addr"],
        })
    ev("bind", f"测试终端绑定 {source_ip}:{local_port}")

    buffer = bytearray()
    arrivals: Dict[int, int] = {}
    first_block_discard_done = False

    try:
        req = fw.rrq_packet(filename, "octet")
        sock.sendto(req, (host, port))
        ev("rrq", f"发送 RRQ {filename!r} octet -> {host}:{port}")

        while time.monotonic() - t0 < overall_timeout:
            try:
                packet, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                result["error"] = f"UDP socket error: {exc!r}"
                ev("error", str(result["error"]))
                break

            if result["server_addr"] is None:
                result["server_addr"] = f"{addr[0]}:{addr[1]}"
                result["server_tid_port"] = addr[1]
                if live is not None:
                    live.update({"server_addr": result["server_addr"],
                                 "server_tid_port": addr[1]})
                ev("tid", f"服务端传输端口 {addr[1]}")

            opcode = fw.parse_opcode(packet)
            if opcode == fw.OP_ERROR:
                code, message = fw.parse_error(packet)
                result["error_code"] = code
                result["error"] = f"TFTP ERROR {code}: {message}"
                ev("server-error", result["error"])
                break

            if opcode != fw.OP_DATA:
                ev("ignore", f"忽略非 DATA 报文 opcode={opcode}")
                continue

            block, payload = fw.parse_data(packet)
            if block is None:
                ev("ignore", "无法解析的 DATA 报文")
                continue

            result["data_packets"] = int(result["data_packets"]) + 1
            arrivals[block] = arrivals.get(block, 0) + 1
            result["arrivals"] = {str(k): v for k, v in sorted(arrivals.items())}
            if arrivals[block] >= 2:
                result["retransmissions_seen"] = int(result["retransmissions_seen"]) + 1
                ev("retransmit", f"收到服务端重传 DATA block={block} "
                                 f"(第 {arrivals[block]} 次到达)")

            # 丢包脚本：第 1 块第一次到达时直接丢弃，不确认、不落盘
            if (mode == "lose-first" and block == 1
                    and not first_block_discard_done):
                first_block_discard_done = True
                result["dropped_first_block"] = True
                ev("drop", "按脚本丢弃首次到达的 DATA block=1，不回 ACK")
                continue

            # 重复到达（且之前已处理）：重新 ACK，推动对端计时器状态收敛
            if block <= int(result["blocks_new"]):
                sock.sendto(fw.ack_packet(block), addr)
                result["acks_sent"] = int(result["acks_sent"]) + 1
                ev("re-ack", f"重复 DATA block={block}，重新 ACK")
                continue

            # TFTP 停等等协议保证按块号顺序到达
            expected = int(result["blocks_new"]) + 1
            if block != expected:
                ev("out-of-order", f"收到 block={block}，期望 {expected}；仍按现状 ACK")

            buffer.extend(payload)
            result["blocks_new"] = int(result["blocks_new"]) + 1
            sock.sendto(fw.ack_packet(block), addr)
            result["acks_sent"] = int(result["acks_sent"]) + 1
            ev("ack", f"落盘 {len(payload)}B 并 ACK block={block}")

            if len(payload) < fw.BLOCK_SIZE:
                if len(payload) == 0:
                    result["terminator_empty_block"] = True
                    ev("terminator", "收到整数倍镜像的末尾空块 block="
                                     f"{block}，已发 ACK，等待服务端确认传完语义")
                result["terminator_acked"] = True
                result["ok"] = True
                break

        else:
            result["error"] = f"整体超时 {overall_timeout:.0f}s，传输未完成"
            ev("timeout", result["error"])

    finally:
        result["bytes_received"] = len(buffer)
        if buffer:
            digest = hashlib.sha256(bytes(buffer)).hexdigest()
            result["digest_actual"] = digest
            result["digest_match"] = (digest == fw.IMAGE_SHA256
                                      and len(buffer) == fw.IMAGE_SIZE)
        result["elapsed_ms"] = ms()
        try:
            sock.close()
        except OSError:
            pass
        ev("finish", f"ok={result['ok']} bytes={len(buffer)} "
                     f"match={result['digest_match']}")

    return result


# ---------------------------------------------------------------- 满载自检

def _wait_stats(stats_url: str, predicate: Callable[[dict], bool],
                timeout: float, interval: float = 0.2) -> Optional[dict]:
    deadline = time.monotonic() + timeout
    last: Optional[dict] = None
    while time.monotonic() < deadline:
        last = _fetch_stats(stats_url)
        if last is not None and predicate(last):
            return last
        time.sleep(interval)
    return last


class _Holder:
    """占住一个名额的测试终端：发 RRQ、收 DATA1，但永远不 ACK；后台抽干报文。"""

    def __init__(self, host: str, port: int, tag: str):
        self.tag = tag
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.2)
        self.local_port = self.sock.getsockname()[1]
        self.addr = f"{discover_source_ip(host, port)}:{self.local_port}"
        self.data1 = threading.Event()
        self.data1_source_port: Optional[int] = None
        self.data_packets = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()
        self.sock.sendto(fw.rrq_packet(), (host, port))

    def _drain(self) -> None:
        while not self.stop.is_set():
            try:
                pkt, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if fw.parse_opcode(pkt) == fw.OP_DATA:
                self.data_packets += 1
                block, _ = fw.parse_data(pkt)
                if block == 1 and self.data1_source_port is None:
                    self.data1_source_port = addr[1]
                    self.data1.set()

    def resend_rrq(self, host: str, port: int) -> None:
        self.sock.sendto(fw.rrq_packet(), (host, port))

    def close(self) -> None:
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def _one_shot_rrq_expect_error(host: str, port: int,
                               timeout: float = 2.0) -> dict:
    """新源端口发一路 RRQ，收集服务端第一份应答（期望 ERROR 拒绝）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    s.settimeout(timeout)
    local_port = s.getsockname()[1]
    out = {"client_port": local_port, "rejected": False,
           "opcode": None, "code": None, "message": None,
           "source_port": None, "got_response": False}
    try:
        s.sendto(fw.rrq_packet(), (host, port))
        pkt, addr = s.recvfrom(65535)
        out["got_response"] = True
        out["source_port"] = addr[1]
        out["opcode"] = fw.parse_opcode(pkt)
        if out["opcode"] == fw.OP_ERROR:
            code, msg = fw.parse_error(pkt)
            out["rejected"] = True
            out["code"] = code
            out["message"] = msg
    except socket.timeout:
        out["message"] = "等待拒绝报文超时"
    except OSError as exc:
        out["message"] = repr(exc)
    finally:
        s.close()
    return out


def full_slot_probe(host: str, port: int, stats_url: str,
                    emit: Callable[[str, str], None]) -> dict:
    """完整跑一遍：占满 2 路 -> 第三路被拒 -> 重复 RRQ 不增名额 ->
    等最后超时归还 -> 新终端可完整下载且摘要正确。"""
    report: Dict[str, object] = {
        "ok": False, "started_at": time.strftime("%H:%M:%S"),
        "steps": [], "third_client": None, "duplicate_rrq": None,
        "release_elapsed_ms": None, "download_after_release": None,
    }
    steps: List[dict] = report["steps"]  # type: ignore[assignment]

    def step(name: str, ok: bool, detail: str) -> None:
        row = {"name": name, "ok": ok, "detail": detail,
               "t": time.strftime("%H:%M:%S")}
        steps.append(row)
        emit(name, detail)

    stats = _fetch_stats(stats_url)
    if stats is None:
        step("stats", False, f"无法读取服务端 stats：{stats_url}")
        return report
    if stats["slots"]["active"] != 0:
        step("precondition", False,
             f"服务端当前有 {stats['slots']['active']} 路占用，请等待其结束后再自检")
        return report

    h1 = _Holder(host, port, "H1")
    h2 = _Holder(host, port, "H2")
    try:
        if not (h1.data1.wait(2.5) and h2.data1.wait(2.5)):
            step("occupy", False, "两个占位终端未都收到 DATA1")
            return report
        stats = _wait_stats(stats_url, lambda s: s["slots"]["active"] == 2, 3.0)
        if not stats or stats["slots"]["active"] != 2:
            step("occupy", False, "服务端名额没有达到 2/2")
            return report
        step("occupy", True,
             f"两路占位成功：{h1.addr}(TID {h1.data1_source_port})、"
             f"{h2.addr}(TID {h2.data1_source_port})，slots=2/2")

        # 同一源端口重复 RRQ：应从原传输端口补发，且不增加名额
        before = stats
        h1.resend_rrq(host, port)
        time.sleep(0.4)
        stats2 = _fetch_stats(stats_url)
        sess1 = next((x for x in stats2["sessions"]
                      if x["client_port"] == h1.local_port), None)
        dup_ok = (
            stats2["slots"]["active"] == 2
            and sess1 is not None
            and sess1["duplicate_rrq_resends"] >= 1
            and h1.data_packets >= 2
        )
        same_port = (h1.data1_source_port == stats2["config"]["bound_port"])
        report["duplicate_rrq"] = {
            "ok": dup_ok and same_port,
            "active_still": stats2["slots"]["active"],
            "resends": sess1["duplicate_rrq_resends"] if sess1 else None,
            "server_port": stats2["config"]["bound_port"],
            "data_source_port": h1.data1_source_port,
        }
        step("duplicate-rrq", bool(dup_ok and same_port),
             f"重复 RRQ 后名额仍为 {stats2['slots']['active']}/2，"
             f"补发 {sess1['duplicate_rrq_resends'] if sess1 else '?'} 次，"
             f"报文来源端口 {h1.data1_source_port}=={stats2['config']['bound_port']}")

        # 第三路：应被 ERROR 拒绝
        third = _one_shot_rrq_expect_error(host, port)
        report["third_client"] = third
        stats3 = _fetch_stats(stats_url)
        reject_ok = third["rejected"] and stats3["totals"]["rejected_full"] >= 1
        if third["rejected"]:
            third_detail = (f"第三路收到 ERROR code={third['code']} "
                            f"{third['message']!r}")
        else:
            third_detail = "第三路未被拒绝: " + str(third["message"])
        step("reject-third", reject_ok, third_detail)

        # 等待两个占位会话在最后一次重试超时后释放
        cfg = stats3["config"]
        max_wait = (cfg["max_retries"] + 1) * cfg["timeout_seconds"] + 3.0
        t0 = time.monotonic()
        drained = _wait_stats(stats_url, lambda s: s["slots"]["active"] == 0,
                              max_wait, 0.25)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        report["release_elapsed_ms"] = elapsed_ms
        release_ok = bool(drained and drained["slots"]["active"] == 0)
        step("release-on-timeout", release_ok,
             f"停止确认后约 {elapsed_ms/1000:.1f}s 名额全部归还"
             if release_ok else "等待名额归还超时")

        # 归还后新终端应能正常完整下载
        dl = download_once(host, port, mode="normal", overall_timeout=8.0)
        report["download_after_release"] = {
            "ok": dl["ok"], "digest_match": dl["digest_match"],
            "bytes_received": dl["bytes_received"],
            "retransmissions_seen": dl["retransmissions_seen"],
            "elapsed_ms": dl["elapsed_ms"], "error": dl["error"],
        }
        step("download-after-release",
             bool(dl["ok"] and dl["digest_match"]),
             f"新终端下载 {dl['bytes_received']}B，"
             f"SHA-256 {'一致' if dl['digest_match'] else '不一致'}，"
             f"重传观测 {dl['retransmissions_seen']}")

        report["ok"] = all(s["ok"] for s in steps)
        return report
    finally:
        h1.close()
        h2.close()
