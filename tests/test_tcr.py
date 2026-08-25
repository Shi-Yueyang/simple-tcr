#!/usr/bin/env python3
"""
End-to-end test for the TCR engine.

Creates a virtual serial port (PTY) for PXI, starts the service,
feeds it PXI and UDP packets, and checks the output.

Usage:
    python3 tests/test_tcr.py
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import time


# ── Packet helpers (use the real protocol module) ──────────────────────

CRC_SIZE = 32

# Add project root so 'import tcr' works when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tcr.protocol import escape, calc_crc  # noqa: E402


def build_udp_packet(msg_type: int, fmt: str, *values) -> bytes:
    """Build a framed TCR packet for sending over UDP."""
    data = struct.pack("!" + fmt, *values)
    esc = escape(data)
    hdr = bytes([0x7E, msg_type, 2 + len(esc)])
    crc = escape(calc_crc(hdr + esc, size=CRC_SIZE).to_bytes(CRC_SIZE // 8, "big"))
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


def test_settings():
    """Unit test: settings load/save round-trip."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tcr.settings import load_settings, save_settings, DEFAULTS
    import json

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    tmp.close()
    path = tmp.name

    try:
        # Fresh load returns defaults
        s = load_settings(path)
        assert s == DEFAULTS, f"expected defaults, got {s}"

        # Save and reload
        s["allow_101_without_lock"] = True
        save_settings(path, s)
        s2 = load_settings(path)
        assert s2["allow_101_without_lock"] is True, f"expected True, got {s2}"

        # Corrupt file returns defaults
        with open(path, "w") as f:
            f.write("not json{{{")
        s3 = load_settings(path)
        assert s3 == DEFAULTS, f"expected defaults on corrupt file, got {s3}"

        print("  ✓ settings load/save round-trip")
        return 0
    except AssertionError as e:
        print(f"  ✗ settings: {e}")
        return 1
    finally:
        os.unlink(path)


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
    print(f"[*] Service output: {outfile.name}")

    # ── Start service ──────────────────────────────────────────────────
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    service = subprocess.Popen(
        [sys.executable, "-u", "-m", "tcr",
         "--pxi-port", pty_name,
         "--udp-bind", f"{UDP_BIND[0]}:{UDP_BIND[1]}",
         "--udp-target", f"{UDP_TARGET[0]}:{UDP_TARGET[1]}",
         "--crc-size", str(CRC_SIZE),
         "--log-level", "DEBUG"],
        stdout=outfile,
        stderr=subprocess.STDOUT,
        cwd=project_root,
    )
    print(f"[*] Service PID: {service.pid}")

    # ── Helper: read current output ───────────────────────────────────
    def read_output():
        outfile.flush()
        with open(outfile.name) as f:
            return f.read()

    # ── Send PXI packets ───────────────────────────────────────────────
    print("\n-- Sending PXI packets --")
    time.sleep(1.0)
    for _ in range(4):
        os.write(master_fd, PXI_PACKET)
        time.sleep(0.5)

    time.sleep(0.5)
    check("PXI packets received", read_output(),
          "TrackSide", "ModFreq=29.0Hz", "CarryFreq=2301.4MHz")

    # ── Lock TCR with 011 ──────────────────────────────────────────────
    print("\n-- Sending 011 (lock) --")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = build_udp_packet(0x0B, "IBB", 1000, 0x5A, 0xD3)
    sock.sendto(pkt, UDP_BIND)
    time.sleep(0.5)

    check("011 → 103 response", read_output(),
          "[SEND]", "103:", "AutoLock", "Down")

    # ── Check 101 ──────────────────────────────────────────────────────
    print("\n-- Checking 101 periodic send --")
    time.sleep(0.5)
    check("101 with correct frequencies", read_output(),
          "[SEND]", "101:", "LowFreq=29.0Hz", "CarryFreq=2301.4MHz")

    # ── Time sync (012 × 16) ───────────────────────────────────────
    print("\n-- Sending 012 × 16 (time sync) --")
    for i in range(16):
        data = struct.pack("!IBBB", 5000 + i * 10, 0, 0, 0)
        data += bytes([0x00, 0x00, 0x01])
        data += struct.pack("!HB", 100, 5)
        esc = escape(data)
        hdr = bytes([0x7E, 0x0C, 2 + len(esc)])
        crc = escape(calc_crc(hdr + esc, size=CRC_SIZE).to_bytes(CRC_SIZE // 8, "big"))
        sock.sendto(hdr + esc + crc + bytes([0x7F]), UDP_BIND)
    time.sleep(1.5)

    check("012 → 106 after 16 samples", read_output(), "[SEND]", "106:")

    # ── Self-test ──────────────────────────────────────────────────────
    print("\n-- Sending 013 (self-test request) --")
    pkt = build_udp_packet(0x0D, "IB", 6000, 79)
    sock.sendto(pkt, UDP_BIND)
    time.sleep(0.5)

    check("013 → 104 response", read_output(), "[SEND]", "104:", "SelfTest=OK")

    # ── 102 retry → 108 ────────────────────────────────────────────
    print("\n-- Checking 102 retry → 108 (no 014 ACK) --")
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
    print("\n-- Sending 014 (ACK 102) + PXI --")
    pkt = build_udp_packet(0x0E, "IB", 7000, 1)
    sock.sendto(pkt, UDP_BIND)
    os.write(master_fd, PXI_PACKET)
    time.sleep(0.8)

    check("New 102 triggered after ACK", read_output(), "102 retry 1/3")

    # ── Cleanup first service ─────────────────────────────────────────
    sock.close()
    service.terminate()
    service.wait(timeout=3)
    os.close(master_fd)
    os.close(slave_fd)

    # ── Test allow_101_without_lock ────────────────────────────────────
    print("\n-- Testing --allow-101-without-lock --")
    master_fd2, slave_fd2 = os.openpty()
    pty_name2 = os.ttyname(slave_fd2)
    outfile2 = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log")

    service2 = subprocess.Popen(
        [sys.executable, "-u", "-m", "tcr",
         "--pxi-port", pty_name2,
         "--udp-bind", "127.0.0.1:9002",
         "--udp-target", "127.0.0.1:9003",
         "--crc-size", str(CRC_SIZE),
         "--log-level", "DEBUG",
         "--allow-101-without-lock",
         "--web-port", "0"],
        stdout=outfile2,
        stderr=subprocess.STDOUT,
        cwd=project_root,
    )

    def read_output2():
        outfile2.flush()
        with open(outfile2.name) as f:
            return f.read()

    # Wait long enough for at least one 101 timer to fire (200ms interval)
    time.sleep(1.5)

    output2 = read_output2()
    has_101_no_lock = "[SEND]" in output2 and "101:" in output2
    if has_101_no_lock:
        print("  ✓ 101 sent without locking when flag is set")
        ok += 1
    else:
        print("  ✗ expected 101 without locking")
        fail += 1

    service2.terminate()
    service2.wait(timeout=3)
    os.close(master_fd2)
    os.close(slave_fd2)
    outfile2.close()
    os.unlink(outfile2.name)

    print(f"\n{'='*40}")
    print(f"Results: {ok} passed, {fail} failed")
    if fail > 0:
        print(f"Full log: {outfile.name}")
        outfile.flush()
    else:
        outfile.close()
        os.unlink(outfile.name)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    ret = test_settings()
    sys.exit(ret or main())
