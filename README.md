# Simple TCR

Headless TCR (Track Circuit Reader) protocol simulator — communicates with the
train over UDP and a PXI over a serial port.

## Requirements

- Python 3.8+
- `pyserial` (`pip install pyserial`)

## Quick Start

```bash
pip install pyserial
python simple_tcr.py --pxi-port COM1 --udp-bind 127.0.0.1:19010 --udp-target 127.0.0.1:19011
```

## Command-Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--pxi-port` | `/dev/ttyUSB0` | Serial port for PXI (track-side) |
| `--pxi-baud` | `9600` | PXI serial baud rate |
| `--udp-bind` | `0.0.0.0:9000` | UDP address to listen on (train channel) |
| `--udp-target` | `127.0.0.1:9001` | UDP address to send responses to |
| `--crc-size` | `32` | CRC size in bits: `32` (CRC-32C) or `48` (railway poly) |
| `--carry-freq` | `1698.7` | Initial carrier frequency in MHz, before first PXI packet |
| `--mod-freq` | `11.4` | Initial modulation frequency in Hz, before first PXI packet |
| `--log-level` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

## Testing

```bash
pip install pyserial
python3 test_tcr.py
```

The test script creates a virtual serial port (PTY), starts the service, feeds
it PXI and UDP packets, and verifies the output.
