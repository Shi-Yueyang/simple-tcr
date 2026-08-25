"""
Web dashboard for the TCR engine.

Exposes a read-write JSON API (GET/POST /api/state) and a human-readable
HTML page (GET /) that auto-polls the API.  Uses only stdlib — no framework
dependency.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .settings import load_settings, save_settings

log = logging.getLogger("tcr")

_DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TCR Dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }
  h2 { font-size: 1rem; margin: 1.5rem 0 0.5rem; color: #999; }
  table { border-collapse: collapse; width: 100%; max-width: 480px; }
  td { padding: 6px 12px; border-bottom: 1px solid #333; }
  td:first-child { color: #999; white-space: nowrap; }
  td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
  input { width: 110px; text-align: right; font: inherit; font-variant-numeric: tabular-nums;
          background: #222; color: #eee; border: 1px solid #555; border-radius: 4px; padding: 3px 8px; }
  input:focus { border-color: #4af; outline: none; }
  button { margin-top: 8px; padding: 4px 18px; font: inherit; background: #1a4; color: #fff;
           border: none; border-radius: 4px; cursor: pointer; }
  button:hover { background: #2b6; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.85rem; }
  .badge-locked { background: #1a4; color: #fff; }
  .badge-idle { background: #555; color: #ccc; }
  .feedback { color: #4af; font-size: 0.9rem; margin-left: 12px; opacity: 0; transition: opacity 0.2s; }
  .feedback.show { opacity: 1; }
  .hint { font-size: 0.75rem; color: #666; margin-top: 2px; line-height: 1.4; }
</style>
</head>
<body>
<h1>TCR Dashboard</h1>

<h2>Set Frequencies</h2>
<table>
  <tr>
    <td>Carrier Freq (MHz)</td>
    <td>
      <input id="cf-input" value="1698.7" autocomplete="off">
      <div class="hint">550.0, 650.0, 750.0, 850.0,<br>1698.7, 1701.4, 1998.7, 2001.4,<br>2298.7, 2301.4, 2598.7, 2601.4</div>
    </td>
  </tr>
  <tr>
    <td>Modulation Freq (Hz)</td>
    <td>
      <input id="mf-input" value="11.4" autocomplete="off">
      <div class="hint">10.3, 11.4, 12.5, 13.6, 14.7, 15.8,<br>16.9, 18.0, 19.1, 20.2, 21.3, 22.4,<br>23.5, 24.6, 25.7, 26.8, 27.9, 29.0</div>
    </td>
  </tr>
</table>
<button id="confirm-btn">Apply</button>
<span class="feedback" id="feedback">&#10003; applied</span>

<h2>Settings</h2>
<table>
  <tr>
    <td>Allow 101 without lock</td>
    <td><input type="checkbox" id="allow-101"></td>
  </tr>
</table>

<h2>Current State</h2>
<table id="state"></table>

<script>
const EL = id => document.getElementById(id);

function badge(text, cls) {
  return '<span class="badge ' + cls + '">' + text + '</span>';
}

function modeLabel(mode, dir) {
  if (mode == null) return badge('idle', 'badge-idle');
  var s = (mode === 90 ? 'Auto' : mode === 165 ? 'Balise' : '0x' + (mode||0).toString(16));
  s += ' / ';
  s += (dir === 211 ? 'Down' : dir === 226 ? 'Up' : '?');
  return badge(s, 'badge-locked');
}

// ── Confirm button — send both values to the engine ──

EL('confirm-btn').addEventListener('click', function() {
  var cf = parseFloat(EL('cf-input').value);
  var mf = parseFloat(EL('mf-input').value);
  if (isNaN(cf) || isNaN(mf)) return;
  fetch('/api/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({carry_freq: cf, modulation_freq: mf}),
  }).then(function() {
    var fb = EL('feedback');
    fb.classList.add('show');
    setTimeout(function() { fb.classList.remove('show'); }, 1500);
  });
});

// ── Checkbox — send setting change to the engine ──

EL('allow-101').addEventListener('change', function() {
  fetch('/api/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({allow_101_without_lock: EL('allow-101').checked}),
  });
});

// ── Polling — only updates the read-only state table ──

function renderState(state) {
  // Seed the inputs with initial values on first load.
  if (!EL('cf-input').dataset.seeded) {
    EL('cf-input').value = state.carry_freq.toFixed(1);
    EL('mf-input').value = state.modulation_freq.toFixed(1);
    EL('cf-input').dataset.seeded = '1';
  }
  if (!EL('allow-101').dataset.seeded) {
    EL('allow-101').checked = state.allow_101_without_lock;
    EL('allow-101').dataset.seeded = '1';
  }

  EL('state').innerHTML =
    '<tr><td>Status</td><td>' + modeLabel(state.tcr_mode, state.tcr_up_down_locking) + '</td></tr>' +
    '<tr><td>Carrier Freq (MHz)</td><td>' + state.carry_freq.toFixed(1) + '</td></tr>' +
    '<tr><td>Modulation Freq (Hz)</td><td>' + state.modulation_freq.toFixed(1) + '</td></tr>' +
    '<tr><td>Track Joint NID</td><td>' + state.track_joint_nid + '</td></tr>' +
    '<tr><td>102 responded</td><td>' + state.is_102_responded + '</td></tr>' +
    '<tr><td>105 responded</td><td>' + state.is_105_responded + '</td></tr>' +
    '<tr><td>107 responded</td><td>' + state.is_107_responded + '</td></tr>' +
    '<tr><td>Time samples</td><td>' + state.adjustment_count + ' / 16</td></tr>' +
    '<tr><td>Running</td><td>' + state.running + '</td></tr>';
}

function poll() {
  fetch('/api/state')
    .then(r => r.json())
    .then(renderState)
    .catch(function() {});
}

setInterval(poll, 200);
poll();
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the TCR dashboard and API.

    Set ``DashboardHandler.engine`` before starting the server.
    """
    engine = None  # type: object  # TcrEngine — set by start_web_server()
    settings_path = None  # type: object  # pathlib.Path — set by start_web_server()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json({"error": message}, status)

    # ── Routes ───────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/":
            return self._html(_DASHBOARD_HTML)
        if self.path == "/api/state":
            if self.engine is None:
                return self._error(503, "engine not attached")
            return self._json(self.engine.snapshot())
        return self._error(404, "not found")

    def do_POST(self):
        if self.path != "/api/state":
            return self._error(404, "not found")
        if self.engine is None:
            return self._error(503, "engine not attached")

        # ── Read & parse body ──
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return self._error(400, "empty body")
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError, TypeError):
            return self._error(400, "invalid JSON")

        # ── Frequency update (requires both fields) ──
        if "carry_freq" in body and "modulation_freq" in body:
            try:
                carry = float(body["carry_freq"])
                mod = float(body["modulation_freq"])
            except (ValueError, TypeError):
                return self._error(400, "carry_freq and modulation_freq must be numbers")
            self.engine.update_frequencies(carry, mod)
            log.info("Web: set carry=%.1f MHz, mod=%.1f Hz", carry, mod)

        # ── Setting update ──
        if "allow_101_without_lock" in body:
            val = bool(body["allow_101_without_lock"])
            self.engine.update_setting("allow_101_without_lock", val)
            if self.settings_path is not None:
                settings = load_settings(self.settings_path)
                settings["allow_101_without_lock"] = val
                save_settings(self.settings_path, settings)
            log.info("Web: set allow_101_without_lock=%s", val)

        return self._json(self.engine.snapshot())

    # ── Suppress request log ─────────────────────────────────────────────

    def log_message(self, fmt, *args):
        # Route to the 'tcr' logger at DEBUG so --log-level controls it.
        log.debug("HTTP %s", fmt % args)


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════════

def start_web_server(engine, host="127.0.0.1", port=8080, settings_path=None):
    """Start the dashboard HTTP server.  Blocks — run in a daemon thread."""
    DashboardHandler.engine = engine
    DashboardHandler.settings_path = settings_path
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    log.info("Dashboard: http://%s:%d", host, port)
    try:
        server.serve_forever()
    except Exception:
        log.exception("Web server error")
