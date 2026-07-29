#!/usr/bin/env python3
"""
End-to-end test for tcr_daemon.py.

Creates a virtual serial port (PTY) for PXI, starts the daemon,
feeds it PXI and UDP packets, and checks the output.

Usage:
    python3 test_daemon.py
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import time


# ── Packet helpers (mirror daemon internals) ──────────────────────────

CRC_SIZE = 32
CRC_POLY = 0x1EDC6F41  # CRC-32C


def _crc(data: bytes) -> int:
    """Generic CRC calculator."""
    mask = (1 << CRC_SIZE) - 1
    poly = CRC_POLY & mask
    topbit = 1 << (CRC_SIZE - 1)
    crc = 0
    for byte in data:
        crc ^= (byte & 0xFF) << (CRC_SIZE - 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) if crc & topbit else (crc << 1)
            crc &= mask
    return crc


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == 0x7D: out.extend([0x7D, 0x5D])
        elif b == 0x7E: out.extend([0x7D, 0x5E])
        elif b == 0x7F: out.extend([0x7D, 0x5F])
        else: out.append(b)
    return bytes(out)


def build_udp_packet(msg_type: int, fmt: str, *values) -> bytes:
    """Build a framed TCR packet for sending over UDP."""
    data = struct.pack("!" + fmt, *values)
    esc = _escape(data)
    hdr = bytes([0x7E, msg_type, 2 + len(esc)])
    crc = _escape(_crc(hdr + esc).to_bytes(CRC_SIZE // 8, "big"))
    return hdr + esc + crc + bytes([0x7F])


# ── PXI packet (carrier=2301.4 MHz, modulation=29.0 Hz) ──────────────

PXI_PACKET = bytes([
    0x9A, 0x01,
    0x00, 0x00, 0x00,
    0x07, 0xA1, 0x20,     # upper freq
    0x07, 0x73, 0xF0,     # lower freq
    0xC2, 0x0A,           # mod freq
])

UDP_BIND = ("127.0.0.1", 9000)
UDP_TARGET = ("127.0.0.1", 9001)


def main():
    ok = 0
    fail = 0

    def check(description, output, *substrings):
        nonlocal ok, fail
        for line in output.splitlines():
            if all(s in line for s in substrings):
                print(f"  ✓ {description}")
                ok += 1
                return
        print(f"  ✗ {description}")
        print(f"    expected: {' | '.join(substrings)}")
        fail += 1

    # ── Create PTY pair ───────────────────────────────────────────────
    master_fd, slave_fd = os.openpty()
    pty_name = os.ttyname(slave_fd)
    print(f"[*] Virtual serial port: {pty_name}")

    # ── Output goes to temp file ──────────────────────────────────────
    outfile = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log")
    print(f"[*] Daemon output: {outfile.name}")

    # ── Start daemon ──────────────────────────────────────────────────
    daemon = subprocess.Popen(
        [sys.executable, "-u", "tcr_daemon.py",
         "--pxi-port", pty_name,
         "--udp-bind", f"{UDP_BIND[0]}:{UDP_BIND[1]}",
         "--udp-target", f"{UDP_TARGET[0]}:{UDP_TARGET[1]}",
         "--crc-size", str(CRC_SIZE),
         "--log-level", "DEBUG"],
        stdout=outfile,
        stderr=subprocess.STDOUT,
    )
    print(f"[*] Daemon PID: {daemon.pid}")

    # ── Helper: read current output ───────────────────────────────────
    def read_output():
        outfile.flush()
        with open(outfile.name) as f:
            return f.read()

    # ── Send PXI packets ───────────────────────────────────────────────
    print("\n── Sending PXI packets ──")
    time.sleep(1.0)
    for _ in range(4):
        os.write(master_fd, PXI_PACKET)
        time.sleep(0.5)

    time.sleep(0.5)
    check("PXI packets received", read_output(),
          "TrackSide", "ModFreq=29.0Hz", "CarryFreq=2301.4MHz")

    # ── Lock TCR with 011 ──────────────────────────────────────────────
    print("\n── Sending 011 (lock) ──")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = build_udp_packet(0x0B, "IBB", 1000, 0x5A, 0xD3)
    sock.sendto(pkt, UDP_BIND)
    time.sleep(0.5)

    check("011 → 103 response", read_output(),
          "[SEND]", "103:", "AutoLock", "Down")

    # ── Check 101 ──────────────────────────────────────────────────────
    print("\n── Checking 101 periodic send ──")
    time.sleep(0.5)
    check("101 with correct frequencies", read_output(),
          "[SEND]", "101:", "LowFreq=29.0Hz", "CarryFreq=2301.4MHz")

    # ── Time sync (012 × 16) ───────────────────────────────────────────
    print("\n── Sending 012 × 16 (time sync) ──")
    for i in range(16):
        data = struct.pack("!IBBB", 5000 + i * 10, 0, 0, 0)
        data += bytes([0x00, 0x00, 0x01])
        data += struct.pack("!HB", 100, 5)
        esc = _escape(data)
        hdr = bytes([0x7E, 0x0C, 2 + len(esc)])
        crc = _escape(_crc(hdr + esc).to_bytes(CRC_SIZE // 8, "big"))
        sock.sendto(hdr + esc + crc + bytes([0x7F]), UDP_BIND)
    time.sleep(1.5)

    check("012 → 106 after 16 samples", read_output(), "[SEND]", "106:")

    # ── Self-test ──────────────────────────────────────────────────────
    print("\n── Sending 013 (self-test request) ──")
    pkt = build_udp_packet(0x0D, "IB", 6000, 79)
    sock.sendto(pkt, UDP_BIND)
    time.sleep(0.5)

    check("013 → 104 response", read_output(), "[SEND]", "104:", "SelfTest=OK")

    # ── 102 retry → 108 ────────────────────────────────────────────────
    print("\n── Checking 102 retry → 108 (no 014 ACK) ──")
    os.write(master_fd, PXI_PACKET)
    time.sleep(2.5)

    output = read_output()
    has_retry = "102 retry 1/3" in output
    has_108 = "sending 108" in output
    if has_retry and has_108:
        print("  ✓ 102 retries then sends 108")
        ok += 1
    else:
        print(f"  ✗ retry={has_retry}, 108={has_108}")
        fail += 1

    # ── 014 ACK + new 102 ──────────────────────────────────────────────
    print("\n── Sending 014 (ACK 102) + PXI ──")
    pkt = build_udp_packet(0x0E, "IB", 7000, 1)
    sock.sendto(pkt, UDP_BIND)
    os.write(master_fd, PXI_PACKET)
    time.sleep(0.8)

    check("New 102 triggered after ACK", read_output(), "102 retry 1/3")

    # ── Cleanup ────────────────────────────────────────────────────────
    sock.close()
    daemon.terminate()
    daemon.wait(timeout=3)
    os.close(master_fd)
    os.close(slave_fd)

    print(f"\n{'─'*40}")
    print(f"Results: {ok} passed, {fail} failed")
    if fail > 0:
        print(f"Full log: {outfile.name}")
        outfile.flush()
    else:
        outfile.close()
        os.unlink(outfile.name)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
