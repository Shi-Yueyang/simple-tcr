"""
Entry point for ``python -m tcr``.
"""

import argparse
import logging
import signal
import sys

from . import constants as C
from .engine import TcrEngine


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
        prog="python -m tcr",
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
            "  UDP   train (European Vital Computer) channel. The service listens on\n"
            "        --udp-bind for train messages (011-016) and sends replies\n"
            "        (101-108) to --udp-target.\n"
            "  PXI   track-side equipment reached over a serial port. Receives\n"
            "        fixed 13-byte frames starting with 0x9A.\n"
            "\n"
            "messages (train -> TCR):  011 lock, 012 time sync, 013 self-test, 014-016 ACKs\n"
            "messages (TCR -> train):  101 periodic, 102 joint, 103 lock ack,\n"
            "                        104 self-test, 105/107 carrier, 106 time ack, 108 failure\n"
            "\n"
            "test:  python3 tests/test_tcr.py  (uses a PTY virtual serial port)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--udp-bind",
                   default=f"{C.UDP_BIND_HOST}:{C.UDP_BIND_PORT}",
                   metavar="HOST:PORT",
                   help="UDP address the service listens on for train messages "
                        "(default: %(default)s)")
    p.add_argument("--udp-target",
                   default=f"{C.UDP_TARGET_HOST}:{C.UDP_TARGET_PORT}",
                   metavar="HOST:PORT",
                   help="UDP address to send TCR replies to (train side) "
                        "(default: %(default)s)")
    p.add_argument("--pxi-port",
                   default=C.PXI_SERIAL_PORT,
                   metavar="DEV",
                   help="serial port device for the PXI track-side equipment, "
                        "e.g. /dev/ttyUSB0 or COM3 (default: %(default)s)")
    p.add_argument("--pxi-baud",
                   type=int, default=C.PXI_SERIAL_BAUD,
                   metavar="N",
                   help="baud rate for the PXI serial port (default: %(default)s)")
    p.add_argument("--crc-size",
                   type=int, default=C.CRC_SIZE, choices=[32, 48],
                   metavar="{32,48}",
                   help="CRC width in bits: 32 = CRC-32C (Castagnoli), "
                        "48 = railway polynomial (default: %(default)s)")
    p.add_argument("--carry-freq",
                   type=float, default=1698.7,
                   metavar="MHz",
                   help="initial carrier frequency in MHz, used until the first "
                        "PXI packet arrives (default: %(default)s)")
    p.add_argument("--mod-freq",
                   type=float, default=11.4,
                   metavar="Hz",
                   help="initial modulation frequency in Hz, used until the first "
                        "PXI packet arrives (default: %(default)s)")
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
    log.info("TCR service starting (UDP %s:%d → %s:%d, PXI %s @ %d)",
             udp_bind[0], udp_bind[1],
             udp_target[0], udp_target[1],
             args.pxi_port, args.pxi_baud)

    engine = TcrEngine(udp_bind, udp_target, args.pxi_port, args.pxi_baud,
                       crc_size=args.crc_size,
                       carry_freq=args.carry_freq,
                       mod_freq=args.mod_freq)

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
        log.info("TCR service stopped")


if __name__ == "__main__":
    main()
