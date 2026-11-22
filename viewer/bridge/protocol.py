"""原版 E1682 及本项目扩展 v1 的有界 TCP 分帧。"""
import json
import struct

MAX_PAYLOAD = 1024 * 1024
CAPS, CLAIM, RELEASE, START, STOP = 0xE100, 0xE101, 0xE102, 0xE110, 0xE111
OUTPUT, WAVE, RATE, STATUS, SUBSCRIBE = 0xE120, 0xE121, 0xE122, 0xE130, 0xE131
DATA, EVENT, ERROR = 0xE1F0, 0xE1F1, 0xE1FF
LEGACY_LENGTHS = {0xA111: 11, 0xA112: 12, 0xA113: 11, 0xA114: 12, 0xA115: 12,
                  0xA505: 7, 0xA659: 11, 0xA65B: 10, 0xA3E2: 9, 0xA3D0: 10,
                  0xA452: 11, 0xA3D1: 8}


def legacy_command(opcode, payload=b""):
    return b"\xaa\x55" + bytes([7+len(payload)]) + struct.pack("<H", opcode) + payload + b"\x55\xaa"


async def read_legacy(reader):
    head = await reader.readexactly(5)
    if head[:2] != b"\xaa\x55":
        raise ValueError("原版帧头错误")
    length, opcode = head[2], struct.unpack_from("<H", head, 3)[0]
    if length == 255 and opcode == 0xA65A:
        prefix = await reader.readexactly(4)
        frame = head + prefix + await reader.readexactly(2*(prefix[3]+1)+2)
    elif length == 255 and opcode == 0xA4E1:
        prefix = await reader.readexactly(2)
        frame = head + prefix + await reader.readexactly(prefix[0]+2)
    else:
        actual = 13 if opcode in range(0xA221, 0xA226) and length == 12 else length
        if actual < 7 or actual > 255:
            raise ValueError("原版命令长度无效")
        if opcode in LEGACY_LENGTHS and actual != LEGACY_LENGTHS[opcode]:
            raise ValueError("原版命令长度与操作码不符")
        frame = head + await reader.readexactly(actual-5)
    if frame[-2:] != b"\x55\xaa":
        raise ValueError("原版帧尾错误")
    return opcode, frame[5:-2]


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def extended_frame(opcode, body=b""):
    payload = b"\x01" + body
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("扩展报文超过 1 MiB")
    return b"\xaa\x55\xff" + struct.pack("<HI", opcode, len(payload)) + payload + b"\x55\xaa"


def extended_json(opcode, value):
    return extended_frame(opcode, json_bytes(value))


async def read_extended(reader):
    head = await reader.readexactly(9)
    if head[:3] != b"\xaa\x55\xff":
        raise ValueError("扩展帧头错误")
    opcode, length = struct.unpack_from("<HI", head, 3)
    if not 1 <= length <= MAX_PAYLOAD:
        raise ValueError("扩展长度无效")
    payload = await reader.readexactly(length+2)
    if payload[0] != 1 or payload[-2:] != b"\x55\xaa":
        raise ValueError("扩展版本或帧尾错误")
    return opcode, payload[1:-2]


def data_frame(meta, data):
    header = json_bytes(meta)
    return extended_frame(DATA, struct.pack("<I", len(header)) + header + data)


def legacy_adc(sequence, data):
    if len(data) != 1440:
        raise ValueError("原版 ADC 数据区必须为 1440 字节")
    return struct.pack("<2sBBIII", b"\xaa\xaa", 0, 0, 0, 0, sequence & 0xffffffff) + data + bytes(4)


def dac_voltage(code, mode):
    if type(code) is not int or not 0 <= code <= 4095:
        raise ValueError("DAC 码值必须为 0–4095")
    return code * (20 if mode == "bipolar" else 3.3) / 4095 - (10 if mode == "bipolar" else 0)
