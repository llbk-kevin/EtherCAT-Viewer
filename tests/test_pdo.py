import struct

import pytest

from viewer.pdo import AI, AO, Entry, Mapping, validated_voltage, volts

@pytest.mark.parametrize("status_bits", [0, 8, 16])
def test_decode_signed_input_formats(status_bits):
    entries = []
    offset = 0
    for index in AI:
        if status_bits:
            entries.append(Entry(index, 0, status_bits, offset))
            offset += status_bits
        entries.append(Entry(index, 0x11, 16, offset))
        offset += 16
    raw = [-32768, -1, 0, 32767]
    data = b"".join((42).to_bytes(status_bits // 8, "little") + struct.pack("<h", value)
                    if status_bits else struct.pack("<h", value) for value in raw)
    codec = Mapping(entries, len(data))
    assert codec.decode(data) == (raw, [{"status": 42} if status_bits else None] * 4)
    assert [volts(raw[0]), volts(raw[-1])] == [-10, 10]
    with pytest.raises(ValueError, match="长度"):
        codec.decode(data[:-1])

def test_mapping_checks_actual_length_and_identity():
    good = [Entry(index, 0x11, 16, i * 16) for i, index in enumerate(AI)]
    with pytest.raises(ValueError, match="缓冲区"):
        Mapping(good, 9)
    with pytest.raises(ValueError, match="未知"):
        Mapping([Entry(0x6011, 0x11, 16, 0), *good[1:]], 8)

def test_output_respects_mapping_order():
    codec = Mapping([Entry(index, 1, 16, i * 16) for i, index in enumerate(reversed(AO))], 8, True)
    assert struct.unpack("<4h", codec.encode([1, 0, 0, -0.5])) == (-1638, 0, 0, 3277)

@pytest.mark.parametrize("value", [True, "0.5", float("nan"), float("inf"), -1.001, 2, None])
def test_output_validation(value):
    with pytest.raises(ValueError):
        validated_voltage(value)
