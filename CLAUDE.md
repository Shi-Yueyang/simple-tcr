# CLAUDE.md

This file provides context for Claude Code when working in this repository.

## Project overview

A headless TCR (Track Circuit Reader) protocol simulator for railway signaling.
It replaces a PyQt6 GUI application. The program sits between two peers:

- **Train** (European Vital Computer) — communicates over UDP with framed,
  byte-stuffed packets.
- **PXI** (track-side equipment) — communicates over a serial port with raw
  13-byte frames.

The only runtime dependency beyond stdlib is `pyserial`. The test suite uses
only stdlib (`os.openpty`, `subprocess`, `socket`).

## Files

```
simple-tcr/
├── tcr/
│   ├── __init__.py      # Public API: exports TcrEngine, pack, decode_message, …
│   ├── constants.py     # All configuration defaults and protocol lookup tables
│   ├── protocol.py      # CRC, byte-stuffing, packet builder, message decoders
│   ├── engine.py        # TcrEngine class — the protocol state machine
│   ├── web.py            # HTTP dashboard: stdlib server, JSON API, inline HTML
│   └── __main__.py      # argparse, logging setup, signal handlers, main()
├── tests/
│   └── test_tcr.py      # End-to-end test via PTY + subprocess
├── README.md
├── CLAUDE.md
└── pyproject.toml
```

## Running

```bash
python -m tcr
python -m tcr --udp-bind 0.0.0.0:9000 --udp-target 192.168.1.1:9001
python -m tcr --pxi-port /dev/ttyUSB0 --pxi-baud 115200
python -m tcr --crc-size 48
python -m tcr --carry-freq 1701.4 --mod-freq 29.0
```

## Testing

```bash
python3 tests/test_tcr.py
```

Uses an OS-created PTY pair so no physical serial hardware is needed. The test
imports `tcr.protocol.escape` and `tcr.protocol.calc_crc` for packet building
instead of duplicating them.

## Code structure

### `tcr/constants.py`

All configuration and protocol constants. No imports, no logic — pure data.
Contains: UDP/serial defaults, framing bytes, CRC parameters, timing intervals,
TCR mode constants, message type mappings, frequency lookup tables (carrier,
modulation, and shift-remap tables).

### `tcr/protocol.py`

Wire-protocol codec. Imports from `tcr.constants`. Contains:

- `reflect_bits()`, `calc_crc()` — generic CRC engine (32-bit CRC-32C or
  48-bit railway polynomial).
- `escape()`, `unescape()` — byte-stuffing for 0x7D/0x7E/0x7F.
- `pack()` — builds a complete framed TCR packet (header + escaped data +
  escaped CRC + tail).
- `decode_message()` + per-type `decode_*()` helpers — human-readable packet
  logging. Used for debug output only, not for protocol logic.
- `DECODERS` dispatch dict, `_parse_fields()` helper.

### `tcr/engine.py`

The `TcrEngine` class — the protocol state machine. Imports from both
`tcr.constants` and `tcr.protocol`.

Key internal methods:

| Method | Role |
|--------|------|
| `start()` / `stop()` / `run()` | Lifecycle: open I/O, poll-loop, close I/O |
| `run()` | Polls UDP (non-blocking), checks serial `in_waiting`, fires due timers, sleeps 50 ms |
| `_handle_main_message()` | Dispatches train messages (011–016) |
| `_handle_pxi_message()` | Processes PXI frames, updates frequencies, triggers 102 retry if locked |
| `_fire_timers()` | Drives periodic 101 and 102/105/107 retries |
| `_build_101()` … `_build_108()` | Per-message-type packet builders |
| `_feed_serial()` | Reads serial, syncs to 0x9A markers, extracts 13-byte PXI frames |

### `tcr/__main__.py`

CLI entry point for `python -m tcr`. Contains `setup_logging()`,
`parse_args()`, `_parse_addr()`, and `main()`. Handles SIGINT/SIGTERM for
graceful shutdown.

### `tcr/__init__.py`

Re-exports the public API: `TcrEngine`, `pack`, `decode_message`, `escape`,
`unescape`, `calc_crc`.

## State machine

Starts **idle** (no locking, all timers inactive). All behavior is
message-driven:

| In (train) | Out (TCR) | Trigger | Description |
|----------|-----------|---------|-------------|
| 011 | 103 | Immediate | Track circuit code → locking confirmation |
| 012 | 106 | After 16 samples | Time calibration → feedback |
| 013 | 104 | Immediate | Self-test request → result (always OK) |
| 014 | — | — | ACK of 102 (cancels retry) |
| 015 | — | — | ACK of 105 (cancels retry) |
| 016 | — | — | ACK of 107 (cancels retry) |
| — | 101 | Every 200 ms | Periodic track info (only when locked) |
| — | 102 | PXI packet + locked | Track joint transition (retry ×3 → 108) |
| — | 105/107 | train request | Carrier frequency report (retry ×3 → 108) |
| — | 108 | Retry exhausted | Failure report |

## Retry logic (102, 105, 107)

All three follow the same pattern:

| Attempt | Action |
|---------|--------|
| 1–3 | Send packet, wait 300 ms |
| 4 | Silent gap (wait 300 ms, no send) |
| 5 | Send **108** (failure report), stop |

Receiving the matching ACK (014/015/016) at any point cancels the sequence.

## Clock adjustment

012 packets carry a train-side `PeriodID`. The engine computes
`train_period_id − local_time` and appends it to a rolling deque (max 16
entries). Once 16 samples accumulate, a **106** (time feedback) is sent. The
most recent adjustment factor corrects period IDs in all outgoing packets.

## Protocol details

### UDP packet format

```
| 0x7E | MsgType | Length | EscapedData | EscapedCRC | 0x7F |
```

- **0x7E / 0x7F** — frame delimiters.
- **MsgType** — 1-byte: 0x0B–0x10 for incoming (train→TCR), 0x65–0x6C for
  outgoing (TCR→train).
- **Length** — 1-byte count of MsgType + Length + EscapedData (excludes CRC
  and tail).
- **EscapedData** — payload with byte-stuffing (0x7D→0x7D 0x5D,
  0x7E→0x7D 0x5E, 0x7F→0x7D 0x5F).
- **EscapedCRC** — CRC over `0x7E + MsgType + Length + EscapedData`, also
  byte-stuffed.
- CRC defaults to 32-bit (CRC-32C); `--crc-size 48` switches to a 48-bit
  railway polynomial.

**Important for decoding**: the Length byte counts the escaped bytes
(MsgType + Length + EscapedData), so you unescape _after_ slicing by Length.
The CRC follows the data area and must also be unescaped before validation.

### PXI serial format

Fixed 13-byte raw frames, no framing or CRC:

```
| 0x9A | Mode | 0x00 0x00 0x00 | Upper[3] | Lower[3] | Mod[2] |
```

The serial reader syncs on the 0x9A byte marker; bytes before the marker are
discarded.

## Key internal details

- **Web dashboard**: `--web-port` (default 8080) starts a stdlib
  `ThreadingHTTPServer` in a daemon thread. Routes: `GET /` (HTML dashboard),
  `GET /api/state` (JSON snapshot), `POST /api/state` (update frequencies).
  Pass `--web-port 0` to disable.
- **Thread safety**: `TcrEngine._lock` (`threading.Lock`) guards concurrent
  writes to `carry_freq` / `modulation_freq` from the PXI handler (engine
  thread) and `update_frequencies()` (web thread). Reads use `snapshot()`
  which acquires the lock only around the frequency pair copy.
- **Initial frequencies**: `--carry-freq` (default 1698.7 MHz) and `--mod-freq`
  (default 11.4 Hz) set the carrier and modulation frequencies used until the
  first PXI packet arrives. After that, PXI data overrides them.
- **Thread safety**: `TcrEngine.run()` is synchronous and single-threaded. To
  read or write engine state from another thread (e.g. a web API), use a
  `threading.Lock` and guard the public state attributes.
- **Shutdown**: `_running` boolean flag — set by `stop()`, checked by `run()`
  loop. SIGINT/SIGTERM → `engine.stop()`.
- **Send rate limiting**: `SEND_GAP_MIN_MS = 100` — enforces a minimum gap
  between UDP sends.
- **Frequency change logging**: PXI frequency updates log at INFO when
  carrier or modulation changes, DEBUG otherwise.
- **Message logging**: incoming 011 logs at INFO; all other incoming messages
  log at DEBUG. All outgoing messages log at DEBUG.
- **Logger name**: `"tcr"` — all logging goes through
  `logging.getLogger("tcr")`.
- **Period ID**: computed as `(current_time_ms - start_time_ms) +
  latest_adjustment_factor`, clamped to 32-bit.
- **Carrier frequency codes differ** between 101/102 messages and 105/107
  messages — see `CARRY_FREQ_MAP_101_102` vs `CARRY_FREQ_MAP_015_016_105_107`.
- **Switching frequencies** (550–850 Hz) trigger a remapping of low-frequency
  codes via `LOW_FREQ_MAP_WHEN_SHIFT`.
- **Module dependency order**: `constants` (zero deps) → `protocol` (imports
  constants) → `engine` (imports both) → `__main__` (imports engine +
  constants). `__init__.py` re-exports from engine and protocol for
  convenience.

## Dependencies

- `pyserial` (runtime; `pip install pyserial`)
- Python 3.8+ stdlib only for tests (`os.openpty`, `subprocess`, `socket`,
  `struct`, `tempfile`)
