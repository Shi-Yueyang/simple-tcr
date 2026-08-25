"""
TCR protocol state machine.
"""

import logging
import socket
import threading
import time
from collections import deque

import serial

from . import constants as C
from .protocol import pack, decode_message, unescape, _parse_fields


class TcrEngine:
    """Headless TCR protocol state machine."""

    def __init__(self, udp_bind, udp_target, pxi_port, pxi_baud,
                 crc_size=C.CRC_SIZE, carry_freq=1698.7, mod_freq=11.4):
        self.log = logging.getLogger("tcr")

        # ── Config ──
        self.udp_bind = udp_bind          # (host, port)
        self.udp_target = udp_target      # (host, port)
        self.pxi_port = pxi_port
        self.pxi_baud = pxi_baud
        self._crc_size = crc_size

        # ── Settings ──
        self.allow_101_without_lock = False

        # ── State ──
        self.start_time_ms = int(time.time() * 1000)
        self.tcr_mode = None          # 0x5A (auto) or 0xA5 (balise)
        self.tcr_up_down_locking = None  # 0xD3 (down) or 0xE2 (up)
        self.carry_freq = deque([carry_freq, carry_freq], maxlen=2)
        self.modulation_freq = mod_freq
        self.track_joint_nid = 0
        self.pxi_packet_received_time_ms = 0
        self.adjustment_factors = deque(maxlen=C.ADJUSTMENT_WINDOW)

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

        # ── Thread safety ──
        self._lock = threading.Lock()

    # ── Packet builders ────────────────────────────────────────────────

    def _period_id(self) -> int:
        adj = self.adjustment_factors[-1] if self.adjustment_factors else 0
        t = int(time.time() * 1000) - self.start_time_ms + adj
        return max(0, min(t, 0xFFFFFFFF))

    def _carry_freq(self) -> str:
        return f"{self.carry_freq[-1]:.1f}"

    def _low_freq_code(self) -> int:
        code = C.LOW_FREQ_MAP.get(f"{self.modulation_freq:.1f}", 0)
        carry = C.CARRY_FREQ_MAP_101_102.get(self._carry_freq(), 0)
        if carry in C.SHIFT_FREQ_CODES:
            code = C.LOW_FREQ_MAP_WHEN_SHIFT.get(code, code)
        return code

    def _build_101(self) -> bytes:
        return pack(101, "IBBBB", self._period_id(), self._low_freq_code(),
                    C.CARRY_FREQ_MAP_101_102.get(self._carry_freq(), 0), 255, 170,
                    crc_size=self._crc_size)

    def _build_102(self) -> bytes:
        itj_time = int(time.time() * 1000 - self.pxi_packet_received_time_ms)
        return pack(102, "IBBBH", self._period_id(), 0,
                    C.CARRY_FREQ_MAP_101_102.get(self._carry_freq(), 0),
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
                    C.CARRY_FREQ_MAP_015_016_105_107.get(self._carry_freq(), 0),
                    crc_size=self._crc_size)

    def _build_106(self) -> bytes:
        return pack(106, "I", self._period_id(),
                    crc_size=self._crc_size)

    def _build_107(self) -> bytes:
        return pack(107, "IB", self._period_id(),
                    C.CARRY_FREQ_MAP_015_016_105_107.get(self._carry_freq(), 0),
                    crc_size=self._crc_size)

    def _build_108(self) -> bytes:
        return pack(108, "II", self._period_id(), 0,
                    crc_size=self._crc_size)

    # ── Sending ────────────────────────────────────────────────────────

    def _send_packet(self, packet: bytes):
        """Send a packet via UDP to the target train."""
        now_ms = int(time.time() * 1000)
        gap = now_ms - self._last_send_ms
        if gap < C.SEND_GAP_MIN_MS:
            time.sleep((C.SEND_GAP_MIN_MS - gap) / 1000.0)

        self._udp_sock.sendto(packet, self.udp_target)
        self._last_send_ms = int(time.time() * 1000)

        desc = decode_message(packet)
        self.log.debug("[SEND] %s", desc)

    # ── Timers ────────────────────────────────────────────────────────

    def _fire_timers(self, now):
        if now >= self._t101:
            self._t101 = now + C.SEND_101_INTERVAL_MS / 1000.0
            if self.allow_101_without_lock or (self.tcr_mode is not None and self.tcr_up_down_locking is not None):
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
        if self._t102_attempt <= C.RETRY_MAX:
            self.log.info("102 retry %d/3", self._t102_attempt)
            self._send_packet(self._build_102())
            self._t102_attempt += 1
            self._t102 = now + C.RETRY_INTERVAL_MS / 1000.0
        elif self._t102_attempt < C.RETRY_BEFORE_108:
            self._t102_attempt += 1
            self._t102 = now + C.RETRY_INTERVAL_MS / 1000.0
        elif self._t102_attempt == C.RETRY_BEFORE_108:
            self.log.warning("102 no response, sending 108")
            self._send_packet(self._build_108())
            self._t102 = float('inf')

    def _fire_105_retry(self, now):
        if self.is_105_responded:
            self._t105 = float('inf')
            return
        if self._t105_attempt <= C.RETRY_MAX:
            self.log.debug("105 retry %d/3", self._t105_attempt)
            self._send_packet(self._build_105())
            self._t105_attempt += 1
            self._t105 = now + C.RETRY_INTERVAL_MS / 1000.0
        elif self._t105_attempt < C.RETRY_BEFORE_108:
            self._t105_attempt += 1
            self._t105 = now + C.RETRY_INTERVAL_MS / 1000.0
        elif self._t105_attempt == C.RETRY_BEFORE_108:
            self.log.warning("105 no response, sending 108")
            self._send_packet(self._build_108())
            self._t105 = float('inf')

    def _fire_107_retry(self, now):
        if self.is_107_responded:
            self._t107 = float('inf')
            return
        if self._t107_attempt <= C.RETRY_MAX:
            self.log.debug("107 retry %d/3", self._t107_attempt)
            self._send_packet(self._build_107())
            self._t107_attempt += 1
            self._t107 = now + C.RETRY_INTERVAL_MS / 1000.0
        elif self._t107_attempt < C.RETRY_BEFORE_108:
            self._t107_attempt += 1
            self._t107 = now + C.RETRY_INTERVAL_MS / 1000.0
        elif self._t107_attempt == C.RETRY_BEFORE_108:
            self.log.warning("107 no response, sending 108")
            self._send_packet(self._build_108())
            self._t107 = float('inf')

    # ── Message handlers ───────────────────────────────────────────────

    def _handle_main_message(self, packet: bytes):
        """Handle a message from the train (via UDP)."""
        # Strip framing if present
        if packet[0] == C.FRAME_HEADER and packet[-1] == C.FRAME_TAIL:
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
            if len(self.adjustment_factors) >= C.ADJUSTMENT_WINDOW:
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
        # Lock around writes so the web API thread cannot interleave.
        prev_carry = self.carry_freq[-1]
        prev_mod = self.modulation_freq
        with self._lock:
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
            self._t102 = time.monotonic() + C.RETRY_INTERVAL_MS / 1000.0

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
        self.log.info("Starting TCR service...")

        # UDP socket for train communication
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.bind(self.udp_bind)
        self._udp_sock.setblocking(False)
        self.log.info("UDP: listening on %s:%d, target %s:%d",
                       self.udp_bind[0], self.udp_bind[1],
                       self.udp_target[0], self.udp_target[1])

        # Serial port for PXI communication (optional)
        if self.pxi_port is not None:
            self._pxi_serial = serial.Serial(self.pxi_port, self.pxi_baud, timeout=0)
            self.log.info("PXI serial: %s @ %d baud", self.pxi_port, self.pxi_baud)
        else:
            self.log.info("PXI serial: disabled")

        # Kick off periodic 101 timer
        self._t101 = time.monotonic() + C.SEND_101_INTERVAL_MS / 1000.0

        self._running = True

    def stop(self):
        """Close I/O and stop the event loop."""
        self.log.info("Stopping TCR service...")
        self._running = False
        if self._udp_sock:
            self._udp_sock.close()
            self._udp_sock = None
        if self._pxi_serial is not None and self._pxi_serial.is_open:
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
            if self._pxi_serial is not None and self._pxi_serial.in_waiting:
                self._feed_serial()

            # ── Fire due timers ──
            self._fire_timers(time.monotonic())

            time.sleep(0.05)

    # ── Web API ─────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a dict of public state for the dashboard API.

        Only locks around the frequency pair to avoid a torn read across the
        deque + float; every other attribute is atomically readable under
        the GIL.
        """
        with self._lock:
            carry = self.carry_freq[-1]
            mod = self.modulation_freq
        return {
            "carry_freq": carry,
            "modulation_freq": mod,
            "tcr_mode": self.tcr_mode,
            "tcr_up_down_locking": self.tcr_up_down_locking,
            "track_joint_nid": self.track_joint_nid,
            "is_102_responded": self.is_102_responded,
            "is_105_responded": self.is_105_responded,
            "is_107_responded": self.is_107_responded,
            "adjustment_count": len(self.adjustment_factors),
            "running": self._running,
            "allow_101_without_lock": self.allow_101_without_lock,
        }

    def update_frequencies(self, carry_freq: float, mod_freq: float):
        """Update carrier and modulation frequencies (called by web API).

        Acquires the same lock as _handle_pxi_message so the two writers
        cannot interleave.
        """
        with self._lock:
            self.carry_freq.append(carry_freq)
            self.modulation_freq = mod_freq

    _ALLOWED_SETTINGS = {"allow_101_without_lock"}

    def update_setting(self, key: str, value):
        """Update a runtime setting by name.

        Raises ``ValueError`` if *key* is not a known setting name.
        """
        if key not in self._ALLOWED_SETTINGS:
            raise ValueError(f"unknown setting: {key!r}")
        with self._lock:
            setattr(self, key, value)
