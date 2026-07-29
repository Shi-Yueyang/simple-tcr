# TCR Daemon

Headless TCR (Track Circuit Reader) protocol simulator — communicates with the
train over UDP and a PXI over a serial port. Extracted and simplified from a
PyQt6 GUI application.

## Requirements

- Python 3.8+
- `pyserial` (`pip install pyserial`)

## Quick Start

```bash
pip install pyserial
python tcr_daemon.py --pxi-port COM1 --udp-bind 127.0.0.1:19010 --udp-target 127.0.0.1:19011
```

## Command-Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--pxi-port` | `/dev/ttyUSB0` | Serial port for PXI (track-side) |
| `--pxi-baud` | `9600` | PXI serial baud rate |
| `--udp-bind` | `0.0.0.0:9000` | UDP address to listen on (train channel) |
| `--udp-target` | `127.0.0.1:9001` | UDP address to send responses to |
| `--crc-size` | `32` | CRC size in bits: `32` (CRC-32C) or `48` (railway poly) |
| `--log-level` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

## Testing

The test script creates a virtual serial port (PTY), starts the daemon, feeds
it PXI and UDP packets, and verifies the output:

```bash
python3 test_daemon.py
```

## Protocol Summary

The daemon acts as the TCR (Track Circuit Reader) in a railway signaling system.
It sits between two peers: the **train** (European Vital Computer, reached over UDP)
and the **PXI** (track-side equipment, reached over a serial port).

### Packet Format

All UDP packets use a framed, byte-stuffed format:

```
| 0x7E | MsgType | Length | EscapedData | EscapedCRC | 0x7F |
```

- **0x7E / 0x7F** — frame delimiters
- **MsgType** — 1-byte message identifier (0x0B–0x10 for incoming, 0x65–0x6C for outgoing)
- **Length** — 1-byte count of MsgType + Length + Data (excludes CRC and tail)
- **EscapedData** — payload with byte-stuffing applied (0x7D→0x7D 0x5D, 0x7E→0x7D 0x5E, 0x7F→0x7D 0x5F)
- **EscapedCRC** — CRC over `0x7E + MsgType + Length + EscapedData`, also byte-stuffed
- CRC defaults to 32-bit (CRC-32C); `--crc-size 48` switches to the 48-bit railway polynomial

PXI packets are fixed-length 13-byte raw serial frames, no framing or CRC:

```
| 0x9A | Mode | 0x00 0x00 0x00 | Upper[3] | Lower[3] | Mod[2] |
```

### State Machine

The daemon starts **idle** — no locking, no timers active. All behavior is
driven by incoming messages.

#### 1. Locking — 011 → 103

When the train sends an **011** (track circuit code info), the daemon reads the
locking mode and direction from the payload, stores them, and immediately
responds with a **103** confirmation. From this point on the daemon is
**locked** and begins sending periodic 101 packets.

#### 2. Periodic update — 101 (every 200 ms)

While locked, the daemon sends a **101** packet every 200 ms containing the
current track-side frequency data (carrier and modulation frequencies taken
from the most recent PXI packet). If no PXI packet has arrived yet, frequencies
default to zero.

#### 3. Track joint transition — PXI → 102 (with retry)

When a PXI packet arrives and the daemon is locked, a **102** packet is
scheduled after one retry interval (300 ms). The daemon sends 102 with retry
logic:

| Attempt | Action |
|---------|--------|
| 1–3 | Send 102, wait 300 ms |
| 4 | Silent gap (wait 300 ms, no send) |
| 5 | Send **108** (failure report), stop |

If the train sends an **014** ACK at any point during retries, the 102 sequence
is cancelled and the train is marked as responded.

#### 4. Clock synchronization — 012 → 106 (after 16 samples)

The train periodically sends **012** (time calibration) packets. Each packet
carries a train-side period ID. The daemon computes an adjustment factor
(`train_period_id − local_time`) and appends it to a rolling window. Once 16
samples have accumulated, the daemon responds with a **106** (time feedback).
The most recent adjustment factor is used to correct the period IDs in all
outgoing packets.

#### 5. Self-test — 013 → 104

On receiving a **013**, the daemon immediately responds with a **104**
containing a self-test result (always OK) and protocol version.

#### 6. ACKs — 014 / 015 / 016

These acknowledge 102, 105, and 107 respectively. They simply cancel the
corresponding retry sequence. Messages 105 and 107 (carrier frequency requests)
follow the same retry pattern as 102 but are triggered by the train, not by PXI.

#### 7. Failure — 108

Sent automatically when a 102, 105, or 107 retry sequence exhausts all attempts
without receiving the corresponding ACK.

### Message Table

| In (train) | Out (TCR) | Trigger | Description |
|----------|-----------|---------|-------------|
| 011 | 103 | Immediate | Track circuit code → locking confirmation |
| 012 | 106 | After 16 samples | Time calibration → feedback |
| 013 | 104 | Immediate | Self-test request → result |
| 014 | — | — | ACK of 102 (cancels retry) |
| 015 | — | — | ACK of 105 (cancels retry) |
| 016 | — | — | ACK of 107 (cancels retry) |
| — | 101 | Every 200 ms | Periodic track info (when locked) |
| — | 102 | PXI packet + locked | Track joint transition (retry ×3 → 108) |
| — | 105/107 | train request | Carrier frequency report (retry ×3 → 108) |
| — | 108 | Retry exhausted | Failure report |
