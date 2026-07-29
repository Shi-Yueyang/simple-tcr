# CLAUDE.md

This file provides context for Claude Code when working in this repository.

## Project overview

`simple_tcr.py` is a headless TCR (Track Circuit Reader) protocol simulator for
railway signaling. It replaces a PyQt6 GUI application. The program sits between
two peers:

- **Train** (European Vital Computer) — communicates over UDP with framed,
  byte-stuffed packets.
- **PXI** (track-side equipment) — communicates over a serial port with raw
  13-byte frames.

It is a single-file Python program with no framework dependencies beyond
`pyserial`. The test suite (`test_tcr.py`) uses only stdlib (`os.openpty`,
`subprocess`, `socket`).

## Files

| File | Purpose |
|------|---------|
| `simple_tcr.py` | Main program: CLI, protocol engine, packet codec, I/O loop |
| `test_tcr.py` | End-to-end test: creates PTY, spawns the program, sends PXI/UDP packets, checks log output |

## Running

```bash
python simple_tcr.py
python simple_tcr.py --udp-bind 0.0.0.0:9000 --udp-target 192.168.1.1:9001
python simple_tcr.py --pxi-port /dev/ttyUSB0 --pxi-baud 115200
python simple_tcr.py --crc-size 48
python simple_tcr.py --carry-freq 1701.4 --mod-freq 29.0
```

## Testing

```bash
python3 test_tcr.py
```

Uses an OS-created PTY pair so no physical serial hardware is needed.

## Code structure

### `simple_tcr.py`

All logic lives in one file, organised in sections:

1. **Constants** (lines ~25–88) — defaults for UDP, serial, CRC polynomials,
   timing intervals, message type mappings, and frequency lookup tables.
2. **CRC** (`calc_crc`, `reflect_bits`) — generic CRC calculator supporting
   32-bit (CRC-32C) and 48-bit (railway polynomial).
3. **Byte-stuffing** (`escape`, `unescape`) — byte-level framing escape
   (0x7D/0x7E/0x7F → 0x7D + mapped byte).
4. **Packet builder** (`pack`) — assembles a framed TCR packet: header +
   escaped data + escaped CRC + tail.
5. **Message decoder** (`decode_message` + per-type `decode_*` helpers) —
   human-readable logging for every message type. Used only for debug output,
   not for protocol logic.
6. **`TcrEngine` class** — the protocol state machine:
   - `start()` / `stop()` / `run()` — lifecycle: open I/O, poll-loop, close I/O.
   - `run()` polls UDP (non-blocking `recvfrom`), checks serial `in_waiting`,
     then fires due timers, sleeping 50 ms per iteration.
   - `_handle_main_message()` — dispatches train messages (011–016).
   - `_handle_pxi_message()` — processes PXI frames, updates frequencies,
     triggers 102 retry sequence if locked.
   - `_fire_timers()` — drives periodic 101 sends and 102/105/107 retries.
   - `_build_101()` through `_build_108()` — per-message-type packet builders.
7. **CLI** (`parse_args`, `main`) — argparse with `host:port` address parsing,
   signal handlers (SIGINT/SIGTERM → graceful shutdown).

### `TcrEngine` state machine

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

### Retry logic (102, 105, 107)

All three follow the same pattern:

| Attempt | Action |
|---------|--------|
| 1–3 | Send packet, wait 300 ms |
| 4 | Silent gap (wait 300 ms, no send) |
| 5 | Send **108** (failure report), stop |

Receiving the matching ACK (014/015/016) at any point cancels the sequence.

### Clock adjustment

012 packets carry a train-side `PeriodID`. The engine computes
`train_period_id − local_time` and appends it to a rolling deque (max 16
entries). Once 16 samples accumulate, a **106** (time feedback) is sent. The
most recent adjustment factor is used to correct period IDs in all outgoing
packets.

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

- **Initial frequencies**: `--carry-freq` (default 1698.7 MHz) and `--mod-freq`
  (default 11.4 Hz) set the carrier and modulation frequencies used until the
  first PXI packet arrives. After that, PXI data overrides them.
- **Documented signal**: `_running` boolean flag — set by `stop()`, checked by
  `run()` loop. Signal handlers set it via `engine.stop()`.
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

## Dependencies

- `pyserial` (runtime; `pip install pyserial`)
- Python 3.8+ stdlib only for tests (`os.openpty`, `subprocess`, `socket`,
  `struct`, `tempfile`)
