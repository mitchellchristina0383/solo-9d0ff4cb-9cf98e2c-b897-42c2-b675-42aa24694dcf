"""内置样例镜像 + TFTP(RFC1350) 常量与报文编解码。

只实现只读、octet、固定 512 字节块。
"""
from __future__ import annotations

import hashlib
import struct
from typing import List, Optional, Tuple

# TFTP 操作码
OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5

# TFTP 错误码
ERR_NOT_DEFINED = 0
ERR_FILE_NOT_FOUND = 1
ERR_ACCESS_VIOLATION = 2
ERR_ILLEGAL_OPERATION = 4
ERR_UNKNOWN_TID = 5

BLOCK_SIZE = 512
IMAGE_SIZE = 1024
IMAGE_NAME = "sample.img"


def build_image() -> bytes:
    """生成一份确定性的 1024 字节“样例固件镜像”。"""
    header = bytearray(32)
    header[0:8] = b"TFTPDEMO"
    struct.pack_into("<HH", header, 8, 1, 0)          # major.minor = 1.0
    struct.pack_into("<I", header, 12, IMAGE_SIZE)    # 镜像长度
    header[16:32] = b"2026-11-24 sample fw"[:16].ljust(16, b" ")

    body = bytearray()
    for i in range(IMAGE_SIZE - len(header)):
        body.append((i * 31 + (i // 256) * 7) & 0xFF)
    return bytes(header) + bytes(body)


IMAGE: bytes = build_image()
assert len(IMAGE) == IMAGE_SIZE
IMAGE_SHA256 = hashlib.sha256(IMAGE).hexdigest()

# 1024 = 2 * 512，整块数之后还有一个“空块”作为结束块（块号 3），
# 必须收到对块 3 的 ACK 才算传完。
LAST_BLOCK_NUMBER = IMAGE_SIZE // BLOCK_SIZE + 1


def block_payload(block_number: int) -> bytes:
    """块号从 1 开始；整数倍长度时最后一个块号对应 b'' 空块。"""
    start = (block_number - 1) * BLOCK_SIZE
    return IMAGE[start:start + BLOCK_SIZE]


# ---------------------------------------------------------------- 编解码

def rrq_packet(filename: str = IMAGE_NAME, mode: str = "octet") -> bytes:
    return (
        struct.pack("!H", OP_RRQ)
        + filename.encode("utf-8") + b"\x00"
        + mode.encode("utf-8") + b"\x00"
    )


def data_packet(block_number: int, payload: bytes) -> bytes:
    return struct.pack("!HH", OP_DATA, block_number) + payload


def ack_packet(block_number: int) -> bytes:
    return struct.pack("!HH", OP_ACK, block_number)


def error_packet(code: int, message: str) -> bytes:
    return (
        struct.pack("!HH", OP_ERROR, code)
        + message.encode("utf-8", "replace") + b"\x00"
    )


def parse_opcode(packet: bytes) -> Optional[int]:
    if len(packet) < 2:
        return None
    return struct.unpack("!H", packet[:2])[0]


def parse_ack(packet: bytes) -> Optional[int]:
    if len(packet) < 4 or parse_opcode(packet) != OP_ACK:
        return None
    return struct.unpack("!H", packet[2:4])[0]


def parse_data(packet: bytes) -> Tuple[Optional[int], bytes]:
    if len(packet) < 4 or parse_opcode(packet) != OP_DATA:
        return None, b""
    block = struct.unpack("!H", packet[2:4])[0]
    return block, packet[4:]


def parse_error(packet: bytes) -> Tuple[Optional[int], str]:
    if len(packet) < 4 or parse_opcode(packet) != OP_ERROR:
        return None, ""
    code = struct.unpack("!H", packet[2:4])[0]
    rest = packet[4:]
    message = rest.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return code, message


def parse_rrq(packet: bytes) -> Tuple[str, str, List[str]]:
    """返回 (filename, mode_lower, 其余 option 字符串列表)。畸形报文抛 ValueError。"""
    if parse_opcode(packet) != OP_RRQ:
        raise ValueError("not an RRQ")
    parts = packet[2:].split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    if len(parts) < 2:
        raise ValueError("malformed RRQ")
    filename = parts[0].decode("utf-8", "replace")
    mode = parts[1].decode("utf-8", "replace").lower()
    options = [p.decode("utf-8", "replace") for p in parts[2:]]
    return filename, mode, options


if __name__ == "__main__":
    # 导出内置样例镜像：python firmware.py [输出路径]
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_image.bin"
    with open(out, "wb") as f:
        f.write(IMAGE)
    print(f"wrote {len(IMAGE)} bytes -> {out}")
    print(f"sha256 {IMAGE_SHA256}")
