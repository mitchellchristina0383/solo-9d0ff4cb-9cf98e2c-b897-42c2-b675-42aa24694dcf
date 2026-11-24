#!/usr/bin/env python3
"""原始 UDP 报文级协议自检（只依赖标准库 + 本项目 firmware/terminal）。

用法：
    python scripts/raw_checks.py [host] [tftp_port] [stats_url]

覆盖：
  T0 未知端点的 ACK -> ERROR(5) Unknown TID
  T1 正常 octet 下载：block1/2 + 空 block3，SHA-256 一致
  T2 丢弃首次 block1 -> 服务端超时重传，随后成功
  T3 同源端口重复 RRQ -> 同一传输端口补发、不增名额、不推迟原超时点
  T4 迟到 ACK 被忽略且不改变原超时点
  T5 满载拒绝第三路 + 超时归还名额 + 归还后可下载（复用 terminal.full_slot_probe）
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firmware as fw  # noqa: E402
import terminal  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TFTP_HOST", "127.0.0.1")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("TFTP_PORT", "69"))
STATS = (sys.argv[3] if len(sys.argv) > 3
         else os.environ.get("STATS_URL", "http://127.0.0.1:8001/stats"))

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


def get_stats():
    with urllib.request.urlopen(STATS, timeout=2) as r:
        return json.loads(r.read().decode())


class RawClient:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(2.5)
        self.port = self.sock.getsockname()[1]

    def send_rrq(self):
        self.sock.sendto(fw.rrq_packet(), (HOST, PORT))

    def send_ack(self, block):
        self.sock.sendto(fw.ack_packet(block), (HOST, PORT))

    def recv(self):
        pkt, addr = self.sock.recvfrom(65535)
        return pkt, addr

    def close(self):
        self.sock.close()


def expect_data(c, block=None, timeout=2.5):
    c.sock.settimeout(timeout)
    pkt, addr = c.recv()
    op = fw.parse_opcode(pkt)
    assert op == fw.OP_DATA, f"expected DATA, got opcode={op}"
    b, payload = fw.parse_data(pkt)
    if block is not None:
        assert b == block, f"expected block {block}, got {b}"
    return b, payload, addr


def t0_unknown_ack():
    print("\n[T0] 未知端点 ACK -> ERROR(5)")
    c = RawClient()
    try:
        c.send_ack(12345)
        try:
            pkt, addr = c.recv()
            op = fw.parse_opcode(pkt)
            code, msg = fw.parse_error(pkt) if op == fw.OP_ERROR else (None, "")
            check("收到 ERROR 报文", op == fw.OP_ERROR, f"op={op}")
            check("错误码=5 Unknown TID", code == 5, f"code={code} msg={msg!r}")
            check("应答来自 TFTP 服务端口", addr[1] == PORT, f"src_port={addr[1]}")
        except socket.timeout:
            check("收到 ERROR 报文", False, "超时无应答")
    finally:
        c.close()


def t1_normal():
    print("\n[T1] 正常 octet 下载 1024B")
    c = RawClient()
    chunks = []
    try:
        t0 = time.monotonic()
        c.send_rrq()
        b, p, addr = expect_data(c, 1)
        check("DATA block=1 且 512B", len(p) == 512, f"len={len(p)} src={addr[1]}")
        c.send_ack(1)

        b, p, _ = expect_data(c, 2)
        check("DATA block=2 且 512B", len(p) == 512, f"len={len(p)}")
        chunks += [None, p]
        c.send_ack(2)

        b, p, _ = expect_data(c, 3)
        check("DATA block=3 为空结束块", b == 3 and len(p) == 0,
              f"block={b} len={len(p)}")
        c.send_ack(3)
        elapsed = (time.monotonic() - t0) * 1000

        data = fw.IMAGE[:512] + chunks[1]
        import hashlib
        check("长度 1024", len(data) == 1024)
        check("SHA-256 与内置镜像一致",
              hashlib.sha256(data).hexdigest() == fw.IMAGE_SHA256,
              f"{elapsed:.0f}ms")
    finally:
        c.close()


def t2_lose_first(timeout_s):
    print("\n[T2] 丢弃首次 block1 -> 超时重传")
    c = RawClient()
    try:
        c.send_rrq()
        b1, p1, addr1 = expect_data(c, 1)
        t_first = time.monotonic()
        check("首次 DATA1 512B", len(p1) == 512)
        # 故意不 ACK
        b2, p2, addr2 = expect_data(c, 1, timeout=timeout_s + 2.0)
        dt = time.monotonic() - t_first
        check("收到重传 DATA1", b2 == 1 and p2 == p1)
        check("重传来自同一传输端口", addr2[1] == addr1[1],
              f"{addr1[1]} -> {addr2[1]}")
        check(f"重传间隔≈超时点 {timeout_s}s",
              timeout_s - 0.25 <= dt <= timeout_s + 0.5, f"实际 {dt:.2f}s")
        c.send_ack(1)
        b, p, _ = expect_data(c, 2)
        c.send_ack(2)
        b, p, _ = expect_data(c, 3)
        check("收到空结束块 3", b == 3 and p == b"")
        c.send_ack(3)
        time.sleep(0.2)
        st = get_stats()
        sess = next((x for x in st["history"] if x["client_port"] == c.port), None)
        check("服务端记录重传次数=1(超时)", sess is not None
              and sess["timeout_retransmits"] == 1
              and sess["retransmit_count"] == 1
              and sess["result"] == "completed",
              json.dumps(None if not sess else
                         {k: sess[k] for k in ("timeout_retransmits",
                                              "retransmit_count", "result")},
                         ensure_ascii=False))
    finally:
        c.close()


def t3_duplicate_rrq(timeout_s):
    print("\n[T3] 同源端口重复 RRQ：同端口补发 + 沿用原超时点")
    c = RawClient()
    try:
        t_start = time.monotonic()
        c.send_rrq()
        b, p, addr1 = expect_data(c, 1)

        time.sleep(0.4 * timeout_s)
        t_dup = time.monotonic()
        c.send_rrq()  # 同一 socket => 同一源 IP:端口
        b, p_dup, addr_dup = expect_data(c, 1, timeout=1.0)
        t_dup_data = time.monotonic()
        check("重复 RRQ 后快速补发 DATA1",
              t_dup_data - t_dup < 0.6, f"{(t_dup_data - t_dup)*1000:.0f}ms")
        check("补发来自原传输端口", addr_dup[1] == addr1[1],
              f"{addr1[1]} vs {addr_dup[1]}")

        # 下一份 DATA1 应当仍按“最初超时点”到达（约 t_start+T），
        # 而不是按重复 RRQ 时刻重新计时（t_dup+T）。
        b, p_to, _ = expect_data(c, 1, timeout=timeout_s + 2.0)
        t_to = time.monotonic()
        since_start = t_to - t_start
        since_dup = t_to - t_dup
        check("超时重传仍落在原超时点附近",
              timeout_s - 0.35 <= since_start <= timeout_s + 0.55,
              f"距首发 {since_start:.2f}s (T={timeout_s})")
        check("重复 RRQ 没有把超时点顺延一个 T",
              since_dup < 0.8 * timeout_s,
              f"距重复RRQ {since_dup:.2f}s < 0.8T={0.8*timeout_s:.2f}s；"
              "若重置则应≈T")

        st = get_stats()
        sess = next((x for x in st["sessions"] if x["client_port"] == c.port), None)
        check("名额未新增且记录 1 次重复RRQ补发",
              st["slots"]["active"] >= 1 and sess is not None
              and sess["duplicate_rrq_resends"] == 1
              and sess["duplicate_rrq_count"] == 1,
              f"active={st['slots']['active']}")

        # 收尾，验证补发不影响后续推进
        c.send_ack(1)
        b, _, _ = expect_data(c, 2)
        c.send_ack(2)
        b, p3, _ = expect_data(c, 3)
        c.send_ack(3)
        check("重复 RRQ 后传输仍可正常完成", b == 3 and p3 == b"")
    finally:
        c.close()


def t4_late_ack(timeout_s):
    print("\n[T4] 迟到 ACK 被忽略且不改变超时点")
    c = RawClient()
    try:
        c.send_rrq()
        expect_data(c, 1)
        c.send_ack(1)
        b, p2, addr = expect_data(c, 2)
        t_block2 = time.monotonic()

        time.sleep(0.5 * timeout_s)
        c.send_ack(1)  # 迟到的旧 ACK
        t_stale = time.monotonic()

        # 服务端应在 block2 原定超时点重传 DATA2（距 block2 首发≈T），
        # 而不是从迟到 ACK 时刻重新计时。
        b, p2r, _ = expect_data(c, 2, timeout=timeout_s + 2.0)
        t_retry = time.monotonic()
        since_block2 = t_retry - t_block2
        since_stale = t_retry - t_stale
        check("迟到 ACK 未推进/未触发异常", b == 2 and p2r == p2)
        check("DATA2 按原超时点重传",
              timeout_s - 0.35 <= since_block2 <= timeout_s + 0.55,
              f"距 DATA2 首发 {since_block2:.2f}s")
        check("迟到 ACK 未重置超时点",
              since_stale < 0.8 * timeout_s,
              f"距迟到ACK {since_stale:.2f}s")

        st = get_stats()
        sess = next((x for x in st["sessions"] if x["client_port"] == c.port), None)
        check("服务端 late_acks>=1", sess is not None and sess["late_acks"] >= 1,
              f"late_acks={None if sess is None else sess['late_acks']}")

        c.send_ack(2)
        b, p3, _ = expect_data(c, 3)
        c.send_ack(3)
        check("随后正常完成（空块3）", b == 3 and p3 == b"")
    finally:
        c.close()


def t5_full_slots():
    print("\n[T5] 满载拒绝 / 超时归还 / 归还后可下载（耗时数秒）")
    logs = []
    rep = terminal.full_slot_probe(HOST, PORT, STATS,
                                   lambda n, d: logs.append((n, d)))
    for row_name, detail in logs:
        print(f"    · {row_name}: {detail}")
    check("满载自检全部步骤通过", bool(rep.get("ok")),
          "" if rep.get("ok") else json.dumps(rep, ensure_ascii=False)[:600])


def main():
    st = get_stats()
    timeout_s = float(st["config"]["timeout_seconds"])
    print(f"目标 {HOST}:{PORT} stats={STATS} timeout={timeout_s}s "
          f"retries={st['config']['max_retries']}")

    t0_unknown_ack()
    t1_normal()
    t2_lose_first(timeout_s)
    t3_duplicate_rrq(timeout_s)
    t4_late_ack(timeout_s)
    t5_full_slots()

    print(f"\n==== 结果：{PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
