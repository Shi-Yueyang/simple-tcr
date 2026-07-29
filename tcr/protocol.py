"""
TCR wire-protocol codec: CRC, byte-stuffing, packet builder, and message decoder.
"""

import struct

from . import constants as C


# ═══════════════════════════════════════════════════════════════════════════════
# CRC
# ═══════════════════════════════════════════════════════════════════════════════

def reflect_bits(val: int, width: int) -> int:
    """Reflect the lower *width* bits of *val*."""
    result = 0
    for i in range(width):
        if val & (1 << i):
            result |= 1 << (width - 1 - i)
    return result


def calc_crc(data: bytes,
             size: int = C.CRC_SIZE,
             poly: int = C.CRC_POLY,
             init: int = C.CRC_INIT,
             xor_out: int = C.CRC_XOR,
             reflect_in: bool = C.CRC_REFLECT_IN,
             reflect_out: bool = C.CRC_REFLECT_OUT) -> int:
    """Generic CRC calculator (default: 48-bit with railway polynomial)."""
    mask = (1 << size) - 1
    topbit = 1 << (size - 1)
    poly &= mask
    crc = init & mask

    for byte in data:
        b = byte & 0xFF
        if reflect_in:
            b = reflect_bits(b, 8)
        crc ^= b << (size - 8)
        for _ in range(8):
            if crc & topbit:
                crc = ((crc << 1) ^ poly) & mask
            else:
                crc = (crc << 1) & mask

    if reflect_out:
        crc = reflect_bits(crc, size)
    crc ^= xor_out
    return crc & mask


# ═══════════════════════════════════════════════════════════════════════════════
# Byte-stuffing (escape / unescape)
# ═══════════════════════════════════════════════════════════════════════════════

def escape(data: bytes) -> bytes:
    """Escape 0x7D, 0x7E, 0x7F bytes."""
    out = bytearray()
    for byte in data:
        if byte in C.ESC_MAP:
            out.extend([C.ESC_BYTE, C.ESC_MAP[byte]])
        else:
            out.append(byte)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    """Reverse the escape transformation."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == C.ESC_BYTE and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt in C.UNESC_MAP:
                out.append(C.UNESC_MAP[nxt])
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


# ═══════════════════════════════════════════════════════════════════════════════
# Packet builder
# ═══════════════════════════════════════════════════════════════════════════════

def pack(msg_type, fmt, *values, crc_size=C.CRC_SIZE):
    """Build a complete TCR packet.

    *msg_type* is the logical type (101–108, or hex equivalent).
    *fmt* is a struct format string for the data area.
    *values* are the field values to pack.
    """
    cfg = C.CRC_CONFIG[crc_size]
    type_byte = C.TYPE_MAP.get(msg_type, msg_type)
    data = struct.pack("!" + fmt, *values)
    esc = escape(data)
    hdr = bytes([C.FRAME_HEADER, type_byte, 2 + len(esc)])
    crc_val = calc_crc(hdr + esc, size=crc_size, poly=cfg["poly"])
    crc_bytes = crc_val.to_bytes(crc_size // 8, "big")
    return hdr + esc + escape(crc_bytes) + bytes([C.FRAME_TAIL])


# ═══════════════════════════════════════════════════════════════════════════════
# Message decoder — for human-readable logging
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_fields(data: bytes, fmt: str) -> tuple:
    """Parse *data* with struct format, returning the unpacked values.
       Extra trailing bytes (CRC) are silently ignored."""
    size = struct.calcsize(fmt)
    return struct.unpack(fmt, data[:size])


def _mode_str(mode: int) -> str:
    if mode == C.TCR_MODE_AUTO_LOCKING:
        return "AutoLock"
    if mode == C.TCR_MODE_BALISE_LOCKING:
        return "BaliseLock"
    return f"Unknown({mode:#04x})"


def _direction_str(d: int) -> str:
    if d == C.TCR_LOCKING_DOWN:
        return "Down"
    if d == C.TCR_LOCKING_UP:
        return "Up"
    return f"Unknown({d:#04x})"


def decode_pxi(packet: bytes) -> str:
    """Decode a 13-byte PXI (track-side) packet."""
    mode = packet[1]
    raw_upper = int.from_bytes(packet[5:8], "big")
    raw_lower = int.from_bytes(packet[8:11], "big")
    raw_mod = int.from_bytes(packet[11:13], "big")

    carry = round((raw_upper + raw_lower) / 214.7483648 / 2, 1)

    if mode == 1:
        mod = round(11059200 / 24 / (65536 - (raw_mod - 27.5)), 1)
    else:
        mod = round(11059200 / 48 / (65536 - (raw_mod - 27.5)), 1)

    return f"TrackSide: ModFreq={mod}Hz, CarryFreq={carry}MHz"


def decode_011(data: bytes) -> str:
    vals = _parse_fields(data, "!IBB")
    return f"011: PeriodID={vals[0]}, Mode={_mode_str(vals[1])}, Direction={_direction_str(vals[2])}"


def decode_012(data: bytes) -> str:
    period_id = _parse_fields(data, "!I")[0]
    if len(data) == 7:
        # New format: PeriodID, Hour, Minute, Second
        _, hour, minute, second = _parse_fields(data, "!IBBB")
        return f"012: PeriodID={period_id}, Time={hour:02d}:{minute:02d}:{second:02d}"
    if len(data) == 13:
        # Old format: PeriodID + flags + Km(3) + Speed + Sleep
        # Workaround for 3-byte kilometer field: insert a 0x00 pad byte
        padded = data[:7] + b"\x00" + data[7:]
        vals = _parse_fields(padded, "!IBBBLHB")
        return f"012: PeriodID={vals[0]}, Km={vals[4]}, Speed={vals[5]}, Sleep={vals[6]}"
    return f"012: PeriodID={period_id}, <unrecognized layout ({len(data)} bytes)>"


def decode_013(data: bytes) -> str:
    vals = _parse_fields(data, "!IB")
    return f"013: PeriodID={vals[0]}, ProtoVer={vals[1]:#04x}"


def decode_014(data: bytes) -> str:
    vals = _parse_fields(data, "!IB")
    return f"014: PeriodID={vals[0]}, JointNID={vals[1]}"


def decode_101(data: bytes) -> str:
    vals = _parse_fields(data, "!IBBBB")
    low = C.LOW_FREQ_MAP_INV.get(vals[1], "?")
    carry = C.CARRY_FREQ_MAP_101_102_INV.get(vals[2], "?")
    return f"101: PeriodID={vals[0]}, LowFreq={low}Hz, CarryFreq={carry}MHz, Status={vals[3]}, CodeStatus={vals[4]}"


def decode_102(data: bytes) -> str:
    vals = _parse_fields(data, "!IBBBH")
    old_cf = C.CARRY_FREQ_MAP_101_102_INV.get(vals[1], "?")
    new_cf = C.CARRY_FREQ_MAP_101_102_INV.get(vals[2], "?")
    return f"102: PeriodID={vals[0]}, OldCF={old_cf}, NewCF={new_cf}, JointNID={vals[3]}, Delay={vals[4]}ms"


def decode_103(data: bytes) -> str:
    vals = _parse_fields(data, "!IBB")
    return f"103: PeriodID={vals[0]}, Mode={_mode_str(vals[2])}, Direction={_direction_str(vals[1])}"


def decode_104(data: bytes) -> str:
    vals = _parse_fields(data, "!IBBB")
    suggest = "Down" if vals[1] == 0xFE else ("Up" if vals[1] == 0x01 else "Unknown")
    test = "OK" if vals[2] == 0x55 else ("Fail" if vals[2] == 0xAA else f"{vals[2]:#04x}")
    return f"104: PeriodID={vals[0]}, Suggest={suggest}, SelfTest={test}, ProtoVer={vals[3]:#04x}"


def decode_105(data: bytes) -> str:
    vals = _parse_fields(data, "!IB")
    carry = C.CARRY_FREQ_MAP_015_016_105_107_INV.get(vals[1], "?")
    return f"105: PeriodID={vals[0]}, CarryFreq={carry}MHz"


def decode_106(data: bytes) -> str:
    vals = _parse_fields(data, "!I")
    return f"106: PeriodID={vals[0]}"


def decode_107(data: bytes) -> str:
    vals = _parse_fields(data, "!IB")
    carry = C.CARRY_FREQ_MAP_015_016_105_107_INV.get(vals[1], "?")
    return f"107: PeriodID={vals[0]}, CarryFreq={carry}MHz"


def decode_108(data: bytes) -> str:
    vals = _parse_fields(data, "!II")
    return f"108: PeriodID={vals[0]}, Failure={vals[1]}"


DECODERS = {
    0x0B: decode_011, 0x0C: decode_012, 0x0D: decode_013,
    0x0E: decode_014, 0x0F: lambda d: f"015: {d.hex()}",
    0x10: lambda d: f"016: {d.hex()}",
    0x65: decode_101, 0x66: decode_102, 0x67: decode_103, 0x68: decode_104,
    0x69: decode_105, 0x6A: decode_106, 0x6B: decode_107, 0x6C: decode_108,
    0x9A: decode_pxi,
}


def decode_message(packet: bytes) -> str:
    """Return a human-readable description of a packet."""
    if not packet:
        return "empty"
    if packet[0] == 0x9A:
        decoder = DECODERS.get(0x9A)
        if decoder:
            try:
                return decoder(packet)
            except Exception:
                pass
        return f"TrackSide: {packet.hex(' ')}"

    # Structural parse on the ESCAPED stream. The Length byte counts
    # MsgType + Length + (escaped) Data, so it indexes the escaped bytes.
    # The 3-byte header is literal on the wire; only Data and CRC are
    # escaped, so unescape the data region after slicing it by Length.
    if packet[0] == C.FRAME_HEADER and packet[-1] == C.FRAME_TAIL:
        esc_inner = packet[1:-1]
    else:
        esc_inner = packet

    if len(esc_inner) < 2:
        return f"Short: {esc_inner.hex(' ')}"

    msg_type = esc_inner[0]
    length = esc_inner[1]
    data_len = length - 2 if length >= 2 else 0
    payload = unescape(esc_inner[2:2 + data_len])  # data area only (excludes CRC)

    decoder = DECODERS.get(msg_type)
    if decoder:
        try:
            return decoder(payload)
        except Exception:
            pass

    return f"{C.LOG_TYPE_MAP.get(msg_type, f'{msg_type:#04x}')}: {packet.hex(' ')}"
