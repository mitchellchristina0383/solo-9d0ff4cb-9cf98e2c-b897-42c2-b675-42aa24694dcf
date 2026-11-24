"""TFTP 只读服务（octet / 512 字节块）。

关键语义（对应任务要求）：
* 全服务只用一个 UDP 套接字，所有 DATA（含重传）都从同一个服务传输端口发出，
  因此“同一源 IP:端口重复 RRQ”触发的补发天然来自“原传输端口”。
* 最多 MAX_SLOTS(=2) 个并发终端，按客户端 (ip, port) 占名额；
  同一 (ip, port) 的重复 RRQ 找到原会话补发当前待确认块，不新增名额。
* 只有 ACK 块号 == 当前待确认块号 才推进；迟到/重复 ACK 一律忽略。
* 重复 RRQ 立即补发待确认块，但**不改变原定超时点**，也不消耗超时重试次数；
  迟到 ACK 同样不改变超时点。
* 超时后按固定 TIMEOUT_SECONDS 重新计时并重试；达到 MAX_RETRIES 后最后一次
  超时即结束会话、归还名额。传完（含 1024 字节镜像的空结束块 ACK）也归还。
* 名额已满时，对新终端 RRQ 回 TFTP ERROR 拒绝。

另起一个只读 HTTP stats 端口，供网页展示各路服务端重传次数/名额。
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque, Dict, List, Optional, Tuple

import firmware as fw

TFTP_HOST = os.environ.get("TFTP_HOST", "0.0.0.0")
TFTP_PORT = int(os.environ.get("TFTP_PORT", "69"))
STATS_HOST = os.environ.get("STATS_HOST", "0.0.0.0")
STATS_PORT = int(os.environ.get("STATS_PORT", "8001"))
TIMEOUT_SECONDS = float(os.environ.get("TFTP_TIMEOUT", "1.0"))
MAX_RETRIES = int(os.environ.get("TFTP_MAX_RETRIES", "4"))
MAX_SLOTS = 2

Addr = Tuple[str, int]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class Session:
    """一个客户端 (ip, port) 的一次读传输。所有访问都在 server.lock 下。"""

    def __init__(self, addr: Addr, filename: str, mode: str, tid_port: int):
        self.addr = addr
        self.filename = filename
        self.mode = mode
        self.tid_port = tid_port

        self.block = 1                 # 当前“待确认”块号
        self.retries_done = 0         # 已发生的超时重传次数
        self.deadline = time.monotonic() + TIMEOUT_SECONDS

        self.state = "active"         # active | completed | timeout | aborted | rejected?
        self.result: Optional[str] = None
        self.close_reason: Optional[str] = None

        self.initial_wall = time.time()
        self.finished_wall: Optional[float] = None
        self.started_mono = time.monotonic()
        self.duration_ms: Optional[int] = None

        # 计数
        self.data_sent = 0            # DATA 报文总发送次数（含重传、含空块）
        self.acks_valid = 0
        self.timeout_retransmits = 0
        self.duplicate_rrq_count = 0
        self.duplicate_rrq_resends = 0
        self.late_acks = 0
        self.wrong_tid_packets = 0

        self.events: Deque[dict] = deque(maxlen=15)
        self._event("session-start", f"RRQ {filename!r} {mode}")

    def _event(self, kind: str, text: str) -> None:
        self.events.append({
            "t_ms": int((time.monotonic() - self.started_mono) * 1000),
            "kind": kind,
            "text": text,
        })

    # 任何“再发一次当前块”都走这里，便于统一计数
    def send_current(self, sock: socket.socket, *, cause: str) -> None:
        pkt = fw.data_packet(self.block, fw.block_payload(self.block))
        sock.sendto(pkt, self.addr)
        self.data_sent += 1
        if self.data_sent == 1:
            pass  # 首发，不算重传
        elif cause == "timeout":
            self.timeout_retransmits += 1
        elif cause == "duplicate-rrq":
            self.duplicate_rrq_resends += 1
        self._event(cause, f"send DATA block={self.block} cause={cause}")

    @property
    def retransmit_total(self) -> int:
        return self.timeout_retransmits + self.duplicate_rrq_resends

    def finish(self, result: str, reason: str) -> None:
        self.state = result
        self.result = result
        self.close_reason = reason
        self.finished_wall = time.time()
        self.duration_ms = int((time.monotonic() - self.started_mono) * 1000)
        self._event(result, reason)

    def snapshot(self, now_mono: float) -> dict:
        return {
            "client_ip": self.addr[0],
            "client_port": self.addr[1],
            "client": f"{self.addr[0]}:{self.addr[1]}",
            "tid_port": self.tid_port,
            "filename": self.filename,
            "mode": self.mode,
            "state": self.state,
            "result": self.result,
            "close_reason": self.close_reason,
            "block": self.block,
            "last_block": fw.LAST_BLOCK_NUMBER,
            "retries_done": self.retries_done,
            "max_retries": MAX_RETRIES,
            "timeout_seconds": TIMEOUT_SECONDS,
            "deadline_in_seconds": round(max(0.0, self.deadline - now_mono), 3),
            "data_sent": self.data_sent,
            "acks_valid": self.acks_valid,
            "retransmit_count": self.retransmit_total,
            "timeout_retransmits": self.timeout_retransmits,
            "duplicate_rrq_count": self.duplicate_rrq_count,
            "duplicate_rrq_resends": self.duplicate_rrq_resends,
            "late_acks": self.late_acks,
            "started_at": time.strftime("%H:%M:%S", time.localtime(self.initial_wall)),
            "duration_ms": self.duration_ms,
            "events": list(self.events),
        }


class TFTPServer:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        self.sock.bind((TFTP_HOST, TFTP_PORT))
        self.bound_port = self.sock.getsockname()[1]

        self.lock = threading.RLock()
        self.sessions: Dict[Addr, Session] = {}
        self.history: Deque[dict] = deque(maxlen=30)

        self.total_started = 0
        self.total_completed = 0
        self.total_timed_out = 0
        self.total_aborted = 0
        self.rejected_full = 0
        self.last_reject: Optional[dict] = None
        self.started_at = _now_iso()
        self._stats_server: Optional[ThreadingHTTPServer] = None

        print(f"[tftp] listening udp on {TFTP_HOST}:{TFTP_PORT} "
              f"(bound port {self.bound_port}), slots={MAX_SLOTS}, "
              f"timeout={TIMEOUT_SECONDS}s, max_retries={MAX_RETRIES}", flush=True)
        print(f"[tftp] image {fw.IMAGE_NAME} {fw.IMAGE_SIZE} bytes "
              f"sha256={fw.IMAGE_SHA256}", flush=True)

    # ------------------------------------------------------------ stats HTTP
    def start_stats_http(self) -> None:
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默默认访问日志
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/healthz":
                    self._send(200, b"ok\n", "text/plain; charset=utf-8")
                    return
                if path == "/stats":
                    payload = json.dumps(server_ref.stats(), ensure_ascii=False, indent=2)
                    self._send(200, payload.encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                self._send(404, b'{"error":"not found"}',
                           "application/json; charset=utf-8")

        self._stats_server = ThreadingHTTPServer((STATS_HOST, STATS_PORT), Handler)
        t = threading.Thread(target=self._stats_server.serve_forever, daemon=True)
        t.start()
        print(f"[tftp] stats http on {STATS_HOST}:{STATS_PORT}/stats", flush=True)

    def stats(self) -> dict:
        now_mono = time.monotonic()
        with self.lock:
            active = [s.snapshot(now_mono) for s in self.sessions.values()]
            return {
                "service": "tftp-demo",
                "started_at": self.started_at,
                "time": _now_iso(),
                "config": {
                    "image_name": fw.IMAGE_NAME,
                    "image_size": fw.IMAGE_SIZE,
                    "block_size": fw.BLOCK_SIZE,
                    "last_block_number": fw.LAST_BLOCK_NUMBER,
                    "image_sha256": fw.IMAGE_SHA256,
                    "max_slots": MAX_SLOTS,
                    "timeout_seconds": TIMEOUT_SECONDS,
                    "max_retries": MAX_RETRIES,
                    "tftp_port": TFTP_PORT,
                    "bound_port": self.bound_port,
                    "stats_port": STATS_PORT,
                },
                "slots": {"active": len(self.sessions), "max": MAX_SLOTS},
                "totals": {
                    "started": self.total_started,
                    "completed": self.total_completed,
                    "timed_out": self.total_timed_out,
                    "aborted": self.total_aborted,
                    "rejected_full": self.rejected_full,
                },
                "last_reject": self.last_reject,
                "sessions": active,
                "history": list(self.history),
            }

    # ------------------------------------------------------------ 工具方法
    def _send_error(self, addr: Addr, code: int, message: str) -> None:
        try:
            self.sock.sendto(fw.error_packet(code, message), addr)
        except OSError as exc:
            print(f"[tftp] send_error to {addr} failed: {exc}", flush=True)

    def _close_session(self, sess: Session) -> None:
        """从名额表摘除并写入 history。调用方持锁。"""
        existed = self.sessions.pop(sess.addr, None)
        snap = sess.snapshot(time.monotonic())
        self.history.appendleft(snap)
        if sess.result == "completed":
            self.total_completed += 1
            print(f"[tftp] COMPLETED {sess.addr[0]}:{sess.addr[1]} "
                  f"retransmits={sess.retransmit_total}", flush=True)
        elif sess.result == "timeout":
            self.total_timed_out += 1
            print(f"[tftp] TIMEOUT-RELEASE {sess.addr[0]}:{sess.addr[1]} "
                  f"after {sess.retries_done} timeout-retries "
                  f"(retransmits total {sess.retransmit_total})", flush=True)
        elif sess.result == "aborted":
            self.total_aborted += 1
            print(f"[tftp] ABORTED {sess.addr} reason={sess.close_reason}", flush=True)
        if existed is None:
            # 理论上不应发生（重复收尾），保留诊断
            print(f"[tftp] warning: session {sess.addr} closed but not in table",
                  flush=True)

    # ------------------------------------------------------------ 报文处理
    def handle_packet(self, packet: bytes, addr: Addr) -> None:
        opcode = fw.parse_opcode(packet)
        if opcode is None:
            return

        if opcode == fw.OP_RRQ:
            self._handle_rrq(packet, addr)
        elif opcode == fw.OP_ACK:
            self._handle_ack(packet, addr)
        elif opcode == fw.OP_ERROR:
            sess = self.sessions.get(addr)
            if sess is not None:
                sess.finish("aborted", "client sent ERROR")
                self._close_session(sess)
        elif opcode == fw.OP_WRQ:
            self._send_error(addr, fw.ERR_ACCESS_VIOLATION,
                             "read-only TFTP service: WRQ not allowed")
        else:
            self._send_error(addr, fw.ERR_ILLEGAL_OPERATION,
                             f"unsupported opcode {opcode}")

    def _handle_rrq(self, packet: bytes, addr: Addr) -> None:
        try:
            filename, mode, _options = fw.parse_rrq(packet)
        except ValueError:
            self._send_error(addr, fw.ERR_ILLEGAL_OPERATION, "malformed RRQ")
            return

        existing = self.sessions.get(addr)
        if existing is not None:
            # —— 同一源 IP:端口重发读请求：原会话补发待确认块 ——
            existing.duplicate_rrq_count += 1
            if existing.state == "active":
                existing.send_current(self.sock, cause="duplicate-rrq")
                # 刻意不修改 existing.deadline / retries_done：沿用原超时点
                print(f"[tftp] DUP-RRQ {addr[0]}:{addr[1]} -> resend block "
                      f"{existing.block} from original port {self.bound_port}; "
                      f"deadline in {max(0.0, existing.deadline - time.monotonic()):.3f}s",
                      flush=True)
            return

        # —— 新终端：名额检查 ——
        if len(self.sessions) >= MAX_SLOTS:
            self.rejected_full += 1
            self.last_reject = {
                "client": f"{addr[0]}:{addr[1]}",
                "at": _now_iso(),
                "active": len(self.sessions),
                "max": MAX_SLOTS,
                "message": "server busy: all transfer slots are in use",
            }
            self._send_error(
                addr, fw.ERR_NOT_DEFINED,
                f"server busy: at most {MAX_SLOTS} concurrent terminals, try later",
            )
            print(f"[tftp] REJECT(full) {addr[0]}:{addr[1]}", flush=True)
            return

        if mode != "octet":
            self._send_error(addr, fw.ERR_ILLEGAL_OPERATION,
                             "only octet transfer mode is supported")
            print(f"[tftp] REJECT(mode={mode}) {addr}", flush=True)
            return

        sess = Session(addr, filename or fw.IMAGE_NAME, mode, self.bound_port)
        self.sessions[addr] = sess
        self.total_started += 1
        sess.send_current(self.sock, cause="initial")
        print(f"[tftp] RRQ accepted {addr[0]}:{addr[1]} -> DATA block 1 "
              f"from port {self.bound_port} (slots {len(self.sessions)}/{MAX_SLOTS})",
              flush=True)

    def _handle_ack(self, packet: bytes, addr: Addr) -> None:
        block = fw.parse_ack(packet)
        if block is None:
            return
        sess = self.sessions.get(addr)
        if sess is None:
            # 没有对应传输（或已结束）：标准 TID 错误
            self._send_error(addr, fw.ERR_UNKNOWN_TID,
                             "Unknown transfer ID: no session for this endpoint")
            return

        if block != sess.block:
            # 迟到的旧 ACK 或超前 ACK：忽略，绝不推进，绝不改动 deadline
            sess.late_acks += 1
            sess._event("late-ack",
                        f"ignore ACK block={block}, waiting block={sess.block} "
                        f"(deadline unchanged "
                        f"{max(0.0, sess.deadline - time.monotonic()):.3f}s)")
            print(f"[tftp] LATE/STALE-ACK {addr} ack={block} "
                  f"waiting={sess.block} -> ignored, deadline unchanged", flush=True)
            return

        # —— 唯一能推进传输的情况：当前待确认块的有效 ACK ——
        sess.acks_valid += 1
        if sess.block == fw.LAST_BLOCK_NUMBER:
            # 1024 是 512 的整数倍：块 3 是空块，收到 ACK(3) 才确认传完
            sess.finish("completed",
                        f"ACK {block} of final (empty) block received")
            self._close_session(sess)
            return

        sess.block += 1
        sess.retries_done = 0
        sess.deadline = time.monotonic() + TIMEOUT_SECONDS
        sess.send_current(self.sock, cause="initial")

    # ------------------------------------------------------------ 超时
    def handle_timeouts(self) -> None:
        now = time.monotonic()
        for addr, sess in list(self.sessions.items()):
            if sess.deadline > now:
                continue
            if sess.retries_done >= MAX_RETRIES:
                # 最后一次重试也超时：归还名额
                sess.finish(
                    "timeout",
                    f"final timeout after {MAX_RETRIES} timeout retransmit(s)",
                )
                self._close_session(sess)
                continue

            sess.retries_done += 1
            sess.send_current(self.sock, cause="timeout")
            # 下一次超时点从本次重发时刻重新计时
            sess.deadline = now + TIMEOUT_SECONDS
            print(f"[tftp] TIMEOUT-RETRY {addr[0]}:{addr[1]} "
                  f"block={sess.block} retry={sess.retries_done}/{MAX_RETRIES}",
                  flush=True)

    def _next_wait(self) -> float:
        if not self.sessions:
            return 0.5
        now = time.monotonic()
        nearest = min(s.deadline for s in self.sessions.values())
        return min(1.0, max(0.02, nearest - now))

    # ------------------------------------------------------------ 主循环
    def serve_forever(self) -> None:
        self.start_stats_http()
        while True:
            with self.lock:
                wait = self._next_wait()
            self.sock.settimeout(wait)
            try:
                packet, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                with self.lock:
                    self.handle_timeouts()
                continue
            except OSError as exc:
                # 例如对端端口关闭导致的 ICMP 错误等：不要打死服务
                print(f"[tftp] socket error: {exc!r}", flush=True)
                time.sleep(0.05)
                continue

            with self.lock:
                try:
                    self.handle_packet(packet, addr)
                    self.handle_timeouts()
                except Exception as exc:  # 单包异常不能影响整个服务
                    print(f"[tftp] handler error from {addr}: {exc!r}", flush=True)


def main() -> None:
    TFTPServer().serve_forever()


if __name__ == "__main__":
    main()
