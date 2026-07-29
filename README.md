# Simple TCR

Headless TCR (Track Circuit Reader) protocol simulator — communicates with the
train over UDP and a PXI over a serial port.

## Setup

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate.bat
pip install .
```

This installs `pyserial` and makes the `tcr` command available.

## Quick Start

### Without serial (train-only, UDP communication)

```bash
# Linux / macOS
tcr --udp-bind 127.0.0.1:19010 --udp-target 127.0.0.1:19011
```

### With serial

```bash
tcr --pxi-port /dev/ttyUSB0 --pxi-baud 115200 --udp-bind 127.0.0.1:19010 --udp-target 127.0.0.1:19011
tcr --pxi-port COM3 --udp-bind 127.0.0.1:19010 --udp-target 127.0.0.1:19011
```

### Run without installing (pure script)

```bash
python -m tcr
python -m tcr --pxi-port /dev/ttyUSB0
python -m tcr --udp-bind 0.0.0.0:9000 --udp-target 192.168.1.1:9001
```

### All defaults

```bash
tcr
```

Listens on `0.0.0.0:9000`, sends to `127.0.0.1:9001`, no serial, dashboard on
http://127.0.0.1:8080.

## Command-Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--pxi-port` | *(none)* | Serial port for PXI (track-side); omit to run without serial |
| `--pxi-baud` | `9600` | PXI serial baud rate |
| `--udp-bind` | `0.0.0.0:9000` | UDP address to listen on (train channel) |
| `--udp-target` | `127.0.0.1:9001` | UDP address to send responses to |
| `--crc-size` | `32` | CRC size in bits: `32` (CRC-32C) or `48` (railway poly) |
| `--carry-freq` | `1698.7` | Initial carrier frequency in MHz, before first PXI packet |
| `--mod-freq` | `11.4` | Initial modulation frequency in Hz, before first PXI packet |
| `--log-level` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `--web-port` | `8080` | HTTP port for the web dashboard; `0` disables |
| `--web-host` | `127.0.0.1` | Address to bind the web dashboard to |

A web dashboard is available by default at http://127.0.0.1:8080. It shows
live engine state and lets you edit carrier and modulation frequencies.

## Testing

```bash
python3 tests/test_tcr.py
```

The test script creates a virtual serial port (PTY), starts the service, feeds
it PXI and UDP packets, and verifies the output.
