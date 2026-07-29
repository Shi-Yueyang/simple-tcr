"""
Default configuration and protocol constants for the TCR engine.

Edit the top section per deployment; the rest is the wire-protocol definition.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Defaults — edit these per deployment
# ═══════════════════════════════════════════════════════════════════════════════

UDP_BIND_HOST = "0.0.0.0"
UDP_BIND_PORT = 9000
UDP_TARGET_HOST = "127.0.0.1"
UDP_TARGET_PORT = 9001

PXI_SERIAL_PORT = None  # set to a device path to enable track-side input
PXI_SERIAL_BAUD = 9600

# ═══════════════════════════════════════════════════════════════════════════════
# Framing
# ═══════════════════════════════════════════════════════════════════════════════

FRAME_HEADER = 0x7E
FRAME_TAIL = 0x7F
ESC_BYTE = 0x7D
ESC_MAP = {0x7D: 0x5D, 0x7E: 0x5E, 0x7F: 0x5F}
UNESC_MAP = {0x5D: 0x7D, 0x5E: 0x7E, 0x5F: 0x7F}

# ═══════════════════════════════════════════════════════════════════════════════
# CRC
# ═══════════════════════════════════════════════════════════════════════════════

CRC_INIT = 0
CRC_XOR = 0
CRC_REFLECT_IN = False
CRC_REFLECT_OUT = False

CRC_CONFIG = {
    32: {"poly": 0x1EDC6F41},       # CRC-32C (Castagnoli)
    48: {"poly": 0x112352C2320AB},  # Railway-specific
}

CRC_SIZE = 32
CRC_POLY = CRC_CONFIG[CRC_SIZE]["poly"]

# ═══════════════════════════════════════════════════════════════════════════════
# Timing (milliseconds)
# ═══════════════════════════════════════════════════════════════════════════════

SEND_101_INTERVAL_MS = 200
RETRY_INTERVAL_MS = 300
RETRY_MAX = 3          # retry this many times
RETRY_BEFORE_108 = 5   # send 108 (failure) on this attempt
SEND_GAP_MIN_MS = 100   # minimum gap between sends
ADJUSTMENT_WINDOW = 16  # number of 012 samples before responding

# ═══════════════════════════════════════════════════════════════════════════════
# TCR modes
# ═══════════════════════════════════════════════════════════════════════════════

TCR_MODE_AUTO_LOCKING = 0x5A
TCR_MODE_BALISE_LOCKING = 0xA5
TCR_LOCKING_DOWN = 0xD3
TCR_LOCKING_UP = 0xE2

# ═══════════════════════════════════════════════════════════════════════════════
# Message type mapping
# ═══════════════════════════════════════════════════════════════════════════════

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
