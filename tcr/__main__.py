"""
Entry point for ``python -m tcr``.
"""

import argparse
import logging
import pathlib
import signal
import sys
import threading

from . import constants as C
from .engine import TcrEngine
from .settings import DEFAULT_PATH, load_settings


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
            "  %(prog)s --web-port 8080                       # enable web dashboard\n"
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
            "config file:  search order: ./simple-tcr.json, ~/.config/simple-tcr/settings.json\n"
            "              CLI args override config file values\n"
            "\n"
            "web dashboard:  open http://127.0.0.1:8080 when --web-port is set\n"
            "test:  python3 tests/test_tcr.py  (uses a PTY virtual serial port)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config",
                   default=None,
                   metavar="PATH",
                   help="path to JSON config file; if not specified, searches "
                        "./simple-tcr.json then ~/.config/simple-tcr/settings.json")
    p.add_argument("--udp-bind",
                   default=None,
                   metavar="HOST:PORT",
                   help="UDP address the service listens on for train messages "
                        "(default: %(default)s)")
    p.add_argument("--udp-target",
                   default=None,
                   metavar="HOST:PORT",
                   help="UDP address to send TCR replies to (train side) "
                        "(default: %(default)s)")
    p.add_argument("--pxi-port",
                   default=None,
                   metavar="DEV",
                   help="serial port device for the PXI track-side equipment, "
                        "e.g. /dev/ttyUSB0 or COM3; omit to skip serial")
    p.add_argument("--pxi-baud",
                   type=int, default=None,
                   metavar="N",
                   help="baud rate for the PXI serial port (default: %(default)s)")
    p.add_argument("--crc-size",
                   type=int, default=None, choices=[32, 48],
                   metavar="{32,48}",
                   help="CRC width in bits: 32 = CRC-32C (Castagnoli), "
                        "48 = railway polynomial (default: %(default)s)")
    p.add_argument("--carry-freq",
                   type=float, default=None,
                   metavar="MHz",
                   help="initial carrier frequency in MHz, used until the first "
                        "PXI packet arrives (default: %(default)s)")
    p.add_argument("--mod-freq",
                   type=float, default=None,
                   metavar="Hz",
                   help="initial modulation frequency in Hz, used until the first "
                        "PXI packet arrives (default: %(default)s)")
    p.add_argument("--log-level",
                   default=None,
                   choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                   metavar="LEVEL",
                   help="logging verbosity for stdout "
                        "(default: %(default)s)")
    p.add_argument("--web-port",
                   type=int, default=None,
                   metavar="PORT",
                   help="HTTP port for the web dashboard; 0 disables "
                        "(default: %(default)s)")
    p.add_argument("--web-host",
                   default=None,
                   metavar="HOST",
                   help="address to bind the web dashboard to "
                        "(default: %(default)s)")
    p.add_argument("--allow-101-without-lock",
                   action="store_true", default=None,
                   help="send periodic 101 messages even before "
                        "receiving 011 locking (default: off)")
    return p.parse_args()


def _parse_addr(s: str) -> tuple:
    host, port = s.rsplit(":", 1)
    return host, int(port)


def _find_config_file():
    """Search for config file in standard locations."""
    # 1. Current directory
    local = pathlib.Path("simple-tcr.json")
    if local.exists():
        return str(local)
    # 2. User config directory
    return str(DEFAULT_PATH)


def _load_config_with_defaults(args):
    """Load config file and merge with CLI arguments. CLI overrides config."""
    # Determine config file path
    if args.config:
        config_path = args.config
    else:
        config_path = _find_config_file()
    
    # Load config file
    settings = load_settings(config_path)
    
    # Apply CLI overrides (only if explicitly provided)
    cli_overrides = {}
    if args.udp_bind is not None:
        cli_overrides["udp_bind"] = args.udp_bind
    if args.udp_target is not None:
        cli_overrides["udp_target"] = args.udp_target
    if args.pxi_port is not None:
        cli_overrides["pxi_port"] = args.pxi_port
    if args.pxi_baud is not None:
        cli_overrides["pxi_baud"] = args.pxi_baud
    if args.crc_size is not None:
        cli_overrides["crc_size"] = args.crc_size
    if args.carry_freq is not None:
        cli_overrides["carry_freq"] = args.carry_freq
    if args.mod_freq is not None:
        cli_overrides["mod_freq"] = args.mod_freq
    if args.log_level is not None:
        cli_overrides["log_level"] = args.log_level
    if args.web_port is not None:
        cli_overrides["web_port"] = args.web_port
    if args.web_host is not None:
        cli_overrides["web_host"] = args.web_host
    if args.allow_101_without_lock is not None:
        cli_overrides["allow_101_without_lock"] = args.allow_101_without_lock
    
    settings.update(cli_overrides)
    return settings, config_path


def main():
    args = parse_args()
    
    # Load config file and apply CLI overrides
    settings, config_path = _load_config_with_defaults(args)
    
    setup_logging(settings["log_level"])

    udp_bind = _parse_addr(settings["udp_bind"])
    udp_target = _parse_addr(settings["udp_target"])

    log = logging.getLogger("tcr")
    pxi_label = settings["pxi_port"] if settings["pxi_port"] else "disabled"
    log.info("TCR service starting (UDP %s:%d → %s:%d, PXI %s)",
             udp_bind[0], udp_bind[1],
             udp_target[0], udp_target[1],
             pxi_label)
    log.info("Config: %s", config_path)

    engine = TcrEngine(udp_bind, udp_target, settings["pxi_port"], settings["pxi_baud"],
                       crc_size=settings["crc_size"],
                       carry_freq=settings["carry_freq"],
                       mod_freq=settings["mod_freq"])

    engine.allow_101_without_lock = settings["allow_101_without_lock"]

    def _shutdown(signum, frame):
        log.info("Received signal %d, shutting down", signum)
        engine.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Web dashboard (background thread) ──
    if settings["web_port"] != 0:
        from . import web as _web
        _web_thread = threading.Thread(
            target=_web.start_web_server,
            args=(engine, settings["web_host"], settings["web_port"]),
            kwargs={"settings_path": config_path},
            daemon=True,
        )
        _web_thread.start()

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
