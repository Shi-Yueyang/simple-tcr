#!/usr/bin/env python3
"""
TCR Daemon — Headless TCR (Track Circuit Reader) protocol simulator.

Communicates with the train over UDP and a PXI over a serial port.
Replaces the original PyQt6 GUI application.

Usage:
    python tcr_daemon.py
    python tcr_daemon.py --udp-bind 0.0.0.0:9000 --udp-target 192.168.1.1:9001
    python tcr_daemon.py --pxi-port /dev/ttyUSB0 --pxi-baud 115200
"""

import argparse
import logging
import signal
import socket
import struct
import sys
import time
from collections import deque

import serial

# ═══════════════════════════════════════════════════════════════════════════════
# Constants — edit these per deployment
# ═══════════════════════════════════════════════════════════════════════════════

UDP_BIND_HOST = "0.0.0.0"
UDP_BIND_PORT = 9000
UDP_TARGET_HOST = "127.0.0.1"
UDP_TARGET_PORT = 9001

PXI_SERIAL_PORT = "/dev/ttyUSB0"
PXI_SERIAL_BAUD = 9600

# ═══════════════════════════════════════════════════════════════════════════════
# Protocol constants
# ═══════════════════════════════════════════════════════════════════════════════

FRAME_HEADER = 0x7E
FRAME_TAIL = 0x7F
ESC_BYTE = 0x7D
ESC_MAP = {0x7D: 0x5D, 0x7E: 0x5E, 0x7F: 0x5F}
UNESC_MAP = {0x5D: 0x7D, 0x5E: 0x7E, 0x5F: 0x7F}

CRC_INIT = 0
CRC_XOR = 0
CRC_REFLECT_IN = False
CRC_REFLECT_OUT = False

# CRC configuration by size (poly changes with width)
CRC_CONFIG = {
    32: {"poly": 0x1EDC6F41},       # CRC-32C (Castagnoli)
    48: {"poly": 0x112352C2320AB},  # Railway-specific
}

# Defaults (overridable via --crc-size)
CRC_SIZE = 32
CRC_POLY = CRC_CONFIG[CRC_SIZE]["poly"]

# Timing (milliseconds)
SEND_101_INTERVAL_MS = 200
RETRY_INTERVAL_MS = 300
RETRY_MAX = 3          # retry this many times
RETRY_BEFORE_108 = 5   # send 108 (failure) on this attempt
SEND_GAP_MIN_MS = 100   # minimum gap between sends
ADJUSTMENT_WINDOW = 16  # number of 012 samples before responding

# TCR modes
TCR_MODE_AUTO_LOCKING = 0x5A
TCR_MODE_BALISE_LOCKING = 0xA5
TCR_LOCKING_DOWN = 0xD3
TCR_LOCKING_UP = 0xE2

# Message type mapping
TYPE_MAP = {
    101: 0x65, 102: 0x66, 103: 0x67, 104: 0x68,
    105: 0x69, 106: 0x6A, 107: 0x6B, 108: 0x6C,
}
TYPE_MAP_INV = {v: k for k, v in TYPE_MAP.items()}

LOG_TYPE_MAP = {
    0x0B: "011", 0x0C: "012", 0x0D: "013",
    0x0E: "014", 0x0F: "015", 0x10: "016",
    0x65: "101", 0x66: "102", 0x67: "103", 0x68: "104",
    0x69: "105", 0x6A: "106", 0x6B: "107", 0x6C: "108",
    0x9A: "TrackSide",
}

# ═══════════════════════════════════════════════════════════════════════════════
# Frequency lookup tables
# ═══════════════════════════════════════════════════════════════════════════════

# Used for 015, 016, 105, 107 messages
CARRY_FREQ_MAP_015_016_105_107 = {
    "1698.7": 0x05, "1701.4": 0x01,
    "1998.7": 0x06, "2001.4": 0x02,
    "2298.7": 0x07, "2301.4": 0x03,
    "2598.7": 0x08, "2601.4": 0x04,
    "550.0": 0x09, "650.0": 0x0A, "750.0": 0x0B, "850.0": 0x0C,
}

# Used for 101, 102 messages (different coding than above)
CARRY_FREQ_MAP_101_102 = {
    "1698.7": 0x05, "1701.4": 0x05,
    "1998.7": 0x02, "2001.4": 0x06,
    "2298.7": 0x03, "2301.4": 0x07,
    "2598.7": 0x08, "2601.4": 0x08,
    "550.0": 0x09, "650.0": 0x0A, "750.0": 0x0B, "850.0": 0x0C,
}

# Inverse maps for decoding
CARRY_FREQ_MAP_015_016_105_107_INV = {v: k for k, v in CARRY_FREQ_MAP_015_016_105_107.items()}
CARRY_FREQ_MAP_101_102_INV = {v: k for k, v in CARRY_FREQ_MAP_101_102.items()}

# Low (modulation) frequency map
LOW_FREQ_MAP = {
    "29.0": 0x03, "27.9": 0x05, "26.8": 0x09, "25.7": 0x11,
    "24.6": 0x21, "23.5": 0x06, "22.4": 0x0A, "21.3": 0x12,
    "20.2": 0x22, "19.1": 0x0C, "18.0": 0x14, "16.9": 0x24,
    "15.8": 0x18, "14.7": 0x28, "13.6": 0x30, "12.5": 0x3C,
    "11.4": 0x3A, "10.3": 0x36,
}
LOW_FREQ_MAP_INV = {v: k for k, v in LOW_FREQ_MAP.items()}

# When carrier is a switching frequency (550-850), remap low freq codes
LOW_FREQ_MAP_WHEN_SHIFT = {
    0x03: 0xF6, 0x05: 0xC5, 0x06: 0xEE, 0x09: 0xFC,
    0x11: 0xC9, 0x0A: 0xF5, 0x12: 0xCF, 0x21: 0xFA,
    0x22: 0xED, 0x0C: 0xDE, 0x14: 0xF9, 0x24: 0xF3,
    0x18: 0xDB, 0x28: 0xDD, 0x30: 0xEB, 0x3C: 0xC3,
    0x3A: 0xE7, 0x36: 0xD7,
}

SHIFT_FREQ_CODES = {
    CARRY_FREQ_MAP_015_016_105_107.get(f, 0)
    for f in ["550.0", "650.0", "750.0", "850.0"]
}


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
             size: int = CRC_SIZE,
             poly: int = CRC_POLY,
             init: int = CRC_INIT,
             xor_out: int = CRC_XOR,
             reflect_in: bool = CRC_REFLECT_IN,
             reflect_out: bool = CRC_REFLECT_OUT) -> int:
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
        if byte in ESC_MAP:
            out.extend([ESC_BYTE, ESC_MAP[byte]])
        else:
            out.append(byte)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    """Reverse the escape transformation."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == ESC_BYTE and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt in UNESC_MAP:
                out.append(UNESC_MAP[nxt])
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


# ═══════════════════════════════════════════════════════════════════════════════
# Packet builder
# ═══════════════════════════════════════════════════════════════════════════════

def pack(msg_type, fmt, *values, crc_size=CRC_SIZE):
    """Build a complete TCR packet.

    *msg_type* is the logical type (101–108, or hex equivalent).
    *fmt* is a struct format string for the data area.
    *values* are the field values to pack.
    """
    cfg = CRC_CONFIG[crc_size]
    type_byte = TYPE_MAP.get(msg_type, msg_type)
    data = struct.pack("!" + fmt, *values)
    esc = escape(data)
    hdr = bytes([FRAME_HEADER, type_byte, 2 + len(esc)])
    crc_val = calc_crc(hdr + esc, size=crc_size, poly=cfg["poly"])
    crc_bytes = crc_val.to_bytes(crc_size // 8, "big")
    return hdr + esc + escape(crc_bytes) + bytes([FRAME_TAIL])


# ═══════════════════════════════════════════════════════════════════════════════
# Message decoder — for human-readable logging
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_fields(data: bytes, fmt: str) -> tuple:
    """Parse *data* with struct format, returning the unpacked values.
       Extra trailing bytes (CRC) are silently ignored."""
    size = struct.calcsize(fmt)
    return struct.unpack(fmt, data[:size])


def _mode_str(mode: int) -> str:
    if mode == TCR_MODE_AUTO_LOCKING:
        return "AutoLock"
    if mode == TCR_MODE_BALISE_LOCKING:
        return "BaliseLock"
    return f"Unknown({mode:#04x})"


def _direction_str(d: int) -> str:
    if d == TCR_LOCKING_DOWN:
        return "Down"
    if d == TCR_LOCKING_UP:
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
    low = LOW_FREQ_MAP_INV.get(vals[1], "?")
    carry = CARRY_FREQ_MAP_101_102_INV.get(vals[2], "?")
    return f"101: PeriodID={vals[0]}, LowFreq={low}Hz, CarryFreq={carry}MHz, Status={vals[3]}, CodeStatus={vals[4]}"


def decode_102(data: bytes) -> str:
    vals = _parse_fields(data, "!IBBBH")
    old_cf = CARRY_FREQ_MAP_101_102_INV.get(vals[1], "?")
    new_cf = CARRY_FREQ_MAP_101_102_INV.get(vals[2], "?")
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
    carry = CARRY_FREQ_MAP_015_016_105_107_INV.get(vals[1], "?")
    return f"105: PeriodID={vals[0]}, CarryFreq={carry}MHz"


def decode_106(data: bytes) -> str:
    vals = _parse_fields(data, "!I")
    return f"106: PeriodID={vals[0]}"


def decode_107(data: bytes) -> str:
    vals = _parse_fields(data, "!IB")
    carry = CARRY_FREQ_MAP_015_016_105_107_INV.get(vals[1], "?")
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
    if packet[0] == FRAME_HEADER and packet[-1] == FRAME_TAIL:
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

    return f"{LOG_TYPE_MAP.get(msg_type, f'{msg_type:#04x}')}: {packet.hex(' ')}"


# ═══════════════════════════════════════════════════════════════════════════════
# TCR Protocol Engine
# ═══════════════════════════════════════════════════════════════════════════════

class TcrEngine:
    """Headless TCR protocol state machine."""

    def __init__(self, udp_bind, udp_target, pxi_port, pxi_baud, crc_size=CRC_SIZE):
        self.log = logging.getLogger("tcr")

        # ── Config ──
        self.udp_bind = udp_bind          # (host, port)
        self.udp_target = udp_target      # (host, port)
        self.pxi_port = pxi_port
        self.pxi_baud = pxi_baud
        self._crc_size = crc_size

        # ── State ──
        self.start_time_ms = int(time.time() * 1000)
        self.tcr_mode = None          # 0x5A (auto) or 0xA5 (balise)
        self.tcr_up_down_locking = None  # 0xD3 (down) or 0xE2 (up)
        self.carry_freq = deque([1698.7, 1698.7], maxlen=2)
        self.modulation_freq = 11.4
        self.track_joint_nid = 0
        self.pxi_packet_received_time_ms = 0
        self.adjustment_factors = deque(maxlen=ADJUSTMENT_WINDOW)

        # ── Retry state ──
        self.is_102_responded = True
        self.is_105_responded = True
        self.is_107_responded = True

        # ── Timers (deadline in monotonic seconds; inf = inactive) ──
        self._t101 = float('inf')          # periodic 101 send
        self._t102 = float('inf')          # 102 retry
        self._t102_attempt = 0
        self._t105 = float('inf')          # 105 retry
        self._t105_attempt = 0
        self._t107 = float('inf')          # 107 retry
        self._t107_attempt = 0

        # ── Running flag ──
        self._running = False

        # ── Send rate limiting ──
        self._last_send_ms = 0

        # ── I/O ──
        self._udp_sock = None
        self._pxi_serial = None

        # ── Serial read buffer ──
        self._serial_buf = bytearray()

    # ── Packet builders ────────────────────────────────────────────────

    def _period_id(self) -> int:
        adj = self.adjustment_factors[-1] if self.adjustment_factors else 0
        t = int(time.time() * 1000) - self.start_time_ms + adj
        return max(0, min(t, 0xFFFFFFFF))

    def _carry_freq(self) -> str:
        return f"{self.carry_freq[-1]:.1f}"

    def _low_freq_code(self) -> int:
        code = LOW_FREQ_MAP.get(f"{self.modulation_freq:.1f}", 0)
        carry = CARRY_FREQ_MAP_101_102.get(self._carry_freq(), 0)
        if carry in SHIFT_FREQ_CODES:
            code = LOW_FREQ_MAP_WHEN_SHIFT.get(code, code)
        return code

    def _build_101(self) -> bytes:
        return pack(101, "IBBBB", self._period_id(), self._low_freq_code(),
                    CARRY_FREQ_MAP_101_102.get(self._carry_freq(), 0), 255, 170,
                    crc_size=self._crc_size)

    def _build_102(self) -> bytes:
        itj_time = int(time.time() * 1000 - self.pxi_packet_received_time_ms)
        return pack(102, "IBBBH", self._period_id(), 0,
                    CARRY_FREQ_MAP_101_102.get(self._carry_freq(), 0),
                    self.track_joint_nid, itj_time,
                    crc_size=self._crc_size)

    def _build_103(self) -> bytes:
        return pack(103, "IBB", self._period_id(),
                    self.tcr_up_down_locking or 0, self.tcr_mode or 0,
                    crc_size=self._crc_size)

    def _build_104(self) -> bytes:
        return pack(104, "IBBB", self._period_id(), 254, 85, 79,
                    crc_size=self._crc_size)

    def _build_105(self) -> bytes:
        return pack(105, "IB", self._period_id(),
                    CARRY_FREQ_MAP_015_016_105_107.get(self._carry_freq(), 0),
                    crc_size=self._crc_size)

    def _build_106(self) -> bytes:
        return pack(106, "I", self._period_id(),
                    crc_size=self._crc_size)

    def _build_107(self) -> bytes:
        return pack(107, "IB", self._period_id(),
                    CARRY_FREQ_MAP_015_016_105_107.get(self._carry_freq(), 0),
                    crc_size=self._crc_size)

    def _build_108(self) -> bytes:
        return pack(108, "II", self._period_id(), 0,
                    crc_size=self._crc_size)

    # ── Sending ────────────────────────────────────────────────────────

    def _send_packet(self, packet: bytes):
        """Send a packet via UDP to the target train."""
        now_ms = int(time.time() * 1000)
        gap = now_ms - self._last_send_ms
        if gap < SEND_GAP_MIN_MS:
            time.sleep((SEND_GAP_MIN_MS - gap) / 1000.0)

        self._udp_sock.sendto(packet, self.udp_target)
        self._last_send_ms = int(time.time() * 1000)

        desc = decode_message(packet)
        self.log.debug("[SEND] %s", desc)

    # ── Timers ────────────────────────────────────────────────────────

    def _fire_timers(self, now):
        if now >= self._t101:
            self._t101 = now + SEND_101_INTERVAL_MS / 1000.0
            if self.tcr_mode is not None and self.tcr_up_down_locking is not None:
                self._send_packet(self._build_101())

        if now >= self._t102:
            self._fire_102_retry(now)
        if now >= self._t105:
            self._fire_105_retry(now)
        if now >= self._t107:
            self._fire_107_retry(now)

    def _fire_102_retry(self, now):
        if self.is_102_responded:
            self._t102 = float('inf')
            return
        if self._t102_attempt <= RETRY_MAX:
            self.log.info("102 retry %d/3", self._t102_attempt)
            self._send_packet(self._build_102())
            self._t102_attempt += 1
            self._t102 = now + RETRY_INTERVAL_MS / 1000.0
        elif self._t102_attempt < RETRY_BEFORE_108:
            self._t102_attempt += 1
            self._t102 = now + RETRY_INTERVAL_MS / 1000.0
        elif self._t102_attempt == RETRY_BEFORE_108:
            self.log.warning("102 no response, sending 108")
            self._send_packet(self._build_108())
            self._t102 = float('inf')

    def _fire_105_retry(self, now):
        if self.is_105_responded:
            self._t105 = float('inf')
            return
        if self._t105_attempt <= RETRY_MAX:
            self.log.debug("105 retry %d/3", self._t105_attempt)
            self._send_packet(self._build_105())
            self._t105_attempt += 1
            self._t105 = now + RETRY_INTERVAL_MS / 1000.0
        elif self._t105_attempt < RETRY_BEFORE_108:
            self._t105_attempt += 1
            self._t105 = now + RETRY_INTERVAL_MS / 1000.0
        elif self._t105_attempt == RETRY_BEFORE_108:
            self.log.warning("105 no response, sending 108")
            self._send_packet(self._build_108())
            self._t105 = float('inf')

    def _fire_107_retry(self, now):
        if self.is_107_responded:
            self._t107 = float('inf')
            return
        if self._t107_attempt <= RETRY_MAX:
            self.log.debug("107 retry %d/3", self._t107_attempt)
            self._send_packet(self._build_107())
            self._t107_attempt += 1
            self._t107 = now + RETRY_INTERVAL_MS / 1000.0
        elif self._t107_attempt < RETRY_BEFORE_108:
            self._t107_attempt += 1
            self._t107 = now + RETRY_INTERVAL_MS / 1000.0
        elif self._t107_attempt == RETRY_BEFORE_108:
            self.log.warning("107 no response, sending 108")
            self._send_packet(self._build_108())
            self._t107 = float('inf')

    # ── Message handlers ───────────────────────────────────────────────

    def _handle_main_message(self, packet: bytes):
        """Handle a message from the train (via UDP)."""
        # Strip framing if present
        if packet[0] == FRAME_HEADER and packet[-1] == FRAME_TAIL:
            inner = unescape(packet[1:-1])
        else:
            inner = unescape(packet)

        if len(inner) < 2:
            return

        msg_type = inner[0]
        desc = decode_message(packet)
        # Only the locking request (011) is a key recv event; samples/ACKs are debug.
        recv_level = logging.INFO if msg_type == 0x0B else logging.DEBUG
        self.log.log(recv_level, "[RECV] %s", desc)

        # ── 011: Track circuit code info → respond 103 (locking) ──
        if msg_type == 0x0B:
            vals = _parse_fields(inner[2:], "!IBB")
            self.tcr_mode = vals[1]
            self.tcr_up_down_locking = vals[2]
            self._send_packet(self._build_103())

        # ── 012: Time calibration → respond 106 (after enough samples) ──
        elif msg_type == 0x0C:
            vals = _parse_fields(inner[2:], "!I")
            train_period_id = vals[0]
            local_time = int(time.time() * 1000) - self.start_time_ms
            self.adjustment_factors.append(train_period_id - local_time)
            if len(self.adjustment_factors) >= ADJUSTMENT_WINDOW:
                self._send_packet(self._build_106())

        # ── 013: Self-test request → respond 104 ──
        elif msg_type == 0x0D:
            self._send_packet(self._build_104())

        # ── 014: ACK of 102 ──
        elif msg_type == 0x0E:
            self.is_102_responded = True

        # ── 015: ACK of 105 ──
        elif msg_type == 0x0F:
            self.is_105_responded = True

        # ── 016: ACK of 107 ──
        elif msg_type == 0x10:
            self.is_107_responded = True

    def _handle_pxi_message(self, packet: bytes):
        """Handle a 13-byte PXI track-side packet."""
        desc = decode_message(packet)
        self.log.debug("[RECV] %s", desc)

        self.track_joint_nid += 1
        raw_upper = int.from_bytes(packet[5:8], "big")
        raw_lower = int.from_bytes(packet[8:11], "big")
        raw_mod = int.from_bytes(packet[11:13], "big")

        carry = round((raw_upper + raw_lower) / 214.7483648 / 2, 1)

        if packet[1] == 1:
            mod = round(11059200 / 24 / (65536 - (raw_mod - 27.5)), 1)
        else:
            mod = round(11059200 / 48 / (65536 - (raw_mod - 27.5)), 1)

        # Log frequency only when it changes (key event); unchanged samples are debug.
        prev_carry = self.carry_freq[-1]
        prev_mod = self.modulation_freq
        self.carry_freq.append(carry)
        self.modulation_freq = mod
        freq_changed = (carry != prev_carry) or (mod != prev_mod)
        self.log.log(logging.INFO if freq_changed else logging.DEBUG,
                    "[Trackside] Freq: Mod=%sHz, Carry=%sMHz", mod, carry)

        # Trigger 102 if locked
        if self.tcr_mode is not None and self.tcr_up_down_locking is not None:
            self.pxi_packet_received_time_ms = int(time.time() * 1000)
            self.is_102_responded = False
            self._t102_attempt = 1
            self._t102 = time.monotonic() + RETRY_INTERVAL_MS / 1000.0

    # ── Serial buffer management ───────────────────────────────────────

    def _feed_serial(self):
        """Read available bytes from PXI serial port into buffer, extract 13-byte packets."""
        try:
            chunk = self._pxi_serial.read(self._pxi_serial.in_waiting or 1)
        except serial.SerialException as e:
            self.log.error("Serial read error: %s", e)
            return

        if not chunk:
            return

        self._serial_buf.extend(chunk)

        # Extract complete 13-byte packets starting with 0x9A
        while len(self._serial_buf) >= 13:
            # Find next 0x9A marker
            idx = self._serial_buf.find(0x9A)
            if idx < 0:
                # No marker found — keep only last 12 bytes (could be partial header)
                self._serial_buf = self._serial_buf[-12:]
                break
            if idx > 0:
                # Discard bytes before marker (resync)
                self.log.debug("Discarding %d bytes before 0x9A marker", idx)
                self._serial_buf = self._serial_buf[idx:]

            if len(self._serial_buf) < 13:
                break

            # Extract one complete packet
            pkt = bytes(self._serial_buf[:13])
            self._serial_buf = self._serial_buf[13:]
            if pkt[0] == 0x9A:
                self._handle_pxi_message(pkt)
            else:
                self.log.debug("Unexpected serial byte %#04x, discarding", pkt[0])

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self):
        """Open I/O and begin the event loop."""
        self.log.info("Starting TCR daemon...")

        # UDP socket for train communication
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.bind(self.udp_bind)
        self._udp_sock.setblocking(False)
        self.log.info("UDP: listening on %s:%d, target %s:%d",
                       self.udp_bind[0], self.udp_bind[1],
                       self.udp_target[0], self.udp_target[1])

        # Serial port for PXI communication
        self._pxi_serial = serial.Serial(self.pxi_port, self.pxi_baud, timeout=0)
        self.log.info("PXI serial: %s @ %d baud", self.pxi_port, self.pxi_baud)

        # Kick off periodic 101 timer
        self._t101 = time.monotonic() + SEND_101_INTERVAL_MS / 1000.0

        self._running = True

    def stop(self):
        """Close I/O and stop the event loop."""
        self.log.info("Stopping TCR daemon...")
        self._running = False
        if self._udp_sock:
            self._udp_sock.close()
            self._udp_sock = None
        if self._pxi_serial and self._pxi_serial.is_open:
            self._pxi_serial.close()
            self._pxi_serial = None

    def run(self):
        """Main loop: poll UDP + serial, fire due timers."""
        while self._running:
            # ── UDP (train channel) ──
            try:
                data = self._udp_sock.recvfrom(4096)[0]
                if data:
                    self._handle_main_message(data)
            except BlockingIOError:
                pass
            except OSError:
                break

            # ── Serial (PXI channel) ──
            if self._pxi_serial.in_waiting:
                self._feed_serial()

            # ── Fire due timers ──
            self._fire_timers(time.monotonic())

            time.sleep(0.05)


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def setup_logging(level: str = "INFO"):
    """Configure logging to stdout at the given level."""
    logger = logging.getLogger("tcr")
    numeric = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(numeric)
    ch.setFormatter(fmt)
    logger.addHandler(ch)


def parse_args():
    p = argparse.ArgumentParser(
        prog="tcr_daemon.py",
        description=(
            "Headless TCR (Track Circuit Reader) protocol simulator.\n"
            "Bridges the train over UDP with a PXI over a serial port, "
            "acting as the TCR in a railway signaling system.\n"
            "Replaces the original PyQt6 GUI application."
        ),
        epilog=(
            "examples:\n"
            "  %(prog)s                                      # run with all defaults\n"
            "  %(prog)s --udp-bind 0.0.0.0:9000 --udp-target 192.168.1.1:9001\n"
            "  %(prog)s --pxi-port /dev/ttyUSB0 --pxi-baud 115200\n"
            "  %(prog)s --crc-size 48                        # use 48-bit railway CRC\n"
            "\n"
            "channels:\n"
            "  UDP   train (European Vital Computer) channel. The daemon listens on\n"
            "        --udp-bind for train messages (011-016) and sends replies\n"
            "        (101-108) to --udp-target.\n"
            "  PXI   track-side equipment reached over a serial port. Receives\n"
            "        fixed 13-byte frames starting with 0x9A.\n"
            "\n"
            "messages (train -> TCR):  011 lock, 012 time sync, 013 self-test, 014-016 ACKs\n"
            "messages (TCR -> train):  101 periodic, 102 joint, 103 lock ack,\n"
            "                        104 self-test, 105/107 carrier, 106 time ack, 108 failure\n"
            "\n"
            "test:  python3 test_daemon.py  (uses a PTY virtual serial port)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--udp-bind",
                   default=f"{UDP_BIND_HOST}:{UDP_BIND_PORT}",
                   metavar="HOST:PORT",
                   help="UDP address the daemon listens on for train messages "
                        "(default: %(default)s)")
    p.add_argument("--udp-target",
                   default=f"{UDP_TARGET_HOST}:{UDP_TARGET_PORT}",
                   metavar="HOST:PORT",
                   help="UDP address to send TCR replies to (train side) "
                        "(default: %(default)s)")
    p.add_argument("--pxi-port",
                   default=PXI_SERIAL_PORT,
                   metavar="DEV",
                   help="serial port device for the PXI track-side equipment, "
                        "e.g. /dev/ttyUSB0 or COM3 (default: %(default)s)")
    p.add_argument("--pxi-baud",
                   type=int, default=PXI_SERIAL_BAUD,
                   metavar="N",
                   help="baud rate for the PXI serial port (default: %(default)s)")
    p.add_argument("--crc-size",
                   type=int, default=CRC_SIZE, choices=[32, 48],
                   metavar="{32,48}",
                   help="CRC width in bits: 32 = CRC-32C (Castagnoli), "
                        "48 = railway polynomial (default: %(default)s)")
    p.add_argument("--log-level",
                   default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                   metavar="LEVEL",
                   help="logging verbosity for stdout "
                        "(default: %(default)s)")
    return p.parse_args()


def _parse_addr(s: str) -> tuple:
    host, port = s.rsplit(":", 1)
    return host, int(port)


def main():
    args = parse_args()
    setup_logging(args.log_level)

    udp_bind = _parse_addr(args.udp_bind)
    udp_target = _parse_addr(args.udp_target)

    log = logging.getLogger("tcr")
    log.info("TCR Daemon starting (UDP %s:%d → %s:%d, PXI %s @ %d)",
             udp_bind[0], udp_bind[1],
             udp_target[0], udp_target[1],
             args.pxi_port, args.pxi_baud)

    engine = TcrEngine(udp_bind, udp_target, args.pxi_port, args.pxi_baud,
                       crc_size=args.crc_size)

    def _shutdown(signum, frame):
        log.info("Received signal %d, shutting down", signum)
        engine.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        engine.start()
        engine.run()
    except Exception:
        log.exception("Fatal error")
    finally:
        engine.stop()
        log.info("TCR Daemon stopped")


if __name__ == "__main__":
    main()
