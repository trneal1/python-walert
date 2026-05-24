#!/usr/bin/env python3
"""
weatheralert_python.py — Python port of the ESP32 Weather Alert display service.

Key differences from the Arduino sketch:
- Runs on any standard Python install using Python's HTTP server and a background fetch thread.
- Writes to a remote TFT Terminal device via tft_terminal.py instead of a local LCD.
- Preserves similar web interfaces and zone-management endpoints.
- Removes all local and remote RGB LED / NeoPixel functionality.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import queue
import re
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

try:
    from tft_terminal import TFTError, TFTTerminal
except Exception:  # pragma: no cover - allows web-only operation if TFT library is absent
    TFTTerminal = None  # type: ignore[assignment]

    class TFTError(Exception):
        pass


# ── Configuration ─────────────────────────────────────────────────────────────
MAX_ZONES = 15
ALERT_CYCLE_SECONDS = int(os.getenv("ALERT_CYCLE_SECONDS", "60"))
HTTP_BIND = os.getenv("WALERT_BIND", "0.0.0.0")
HTTP_PORT = int(os.getenv("WALERT_PORT", "8080"))

DEFAULT_ZONES = [
    {"id": "NCC183", "code": "NC W", "active": True, "type": "same", "lat": "", "lon": ""},
    {"id": "NCC063", "code": "NC D", "active": True, "type": "same", "lat": "", "lon": ""},
    {"id": "NCC069", "code": "NC F", "active": True, "type": "same", "lat": "", "lon": ""},
    {"id": "SCZ056", "code": "SC G", "active": True, "type": "same", "lat": "", "lon": ""},
    {"id": "MDC027", "code": "MD H", "active": True, "type": "same", "lat": "", "lon": ""},
    {"id": "NYZ072", "code": "NY M", "active": True, "type": "same", "lat": "", "lon": ""},
    {"id": "PAZ065", "code": "PA Y", "active": True, "type": "same", "lat": "", "lon": ""},
]

STATE_FIPS_TO_AREA = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}

CONFIG_PATH = Path(os.getenv("WALERT_CONFIG", "weatheralert_config.json"))
RUNTIME_PATH = Path(os.getenv("WALERT_RUNTIME", "weatheralert_runtime.json"))

NWS_USER_AGENT = os.getenv(
    "WALERT_USER_AGENT",
    "WeatherAlertPython/1.0 (set WALERT_USER_AGENT with your contact email)",
)
NWS_TIMEOUT = float(os.getenv("WALERT_NWS_TIMEOUT", "60"))

TFT_HOST = os.getenv("TFT_HOST", "")
TFT_PORT = int(os.getenv("TFT_PORT", "8888"))
TFT_DISPLAY = os.getenv("TFT_DISPLAY", "ili9341")
TFT_ROTATION = int(os.getenv("TFT_ROTATION", "1"))
TFT_TIMEOUT = float(os.getenv("TFT_TIMEOUT", "5.0"))

TFT2_HOST = os.getenv("TFT2_HOST", "")
TFT2_PORT = int(os.getenv("TFT2_PORT", "8888"))
TFT2_DISPLAY = os.getenv("TFT2_DISPLAY", TFT_DISPLAY)
TFT2_ROTATION = int(os.getenv("TFT2_ROTATION", str(TFT_ROTATION)))
TFT2_TIMEOUT = float(os.getenv("TFT2_TIMEOUT", str(TFT_TIMEOUT)))

LED_HOST = os.getenv("LED_HOST", os.getenv("WALERT_LED_HOST", ""))
LED_PORT = int(os.getenv("LED_PORT", os.getenv("WALERT_LED_PORT", "7777")))
LED_TIMEOUT = float(os.getenv("LED_TIMEOUT", os.getenv("WALERT_LED_TIMEOUT", "2.0")))
LED_COUNT = 8
LED_BLINK_SECONDS = 30
LED_BLINK_INTERVAL_MS = 500

LOG_LEVEL = os.getenv("WALERT_LOG_LEVEL", "INFO").upper()
LOG = logging.getLogger("weatheralert")


# ── Shared stylesheet / layout ────────────────────────────────────────────────
CSS = """
<style>
:root{
--bg:#0d0d0d;--surface:#161616;--border:#2a2a2a;--accent:#f5c400;--accent2:#ff6b35;
--text:#e8e8e8;--text-muted:#888;--green:#22c55e;--red:#ef4444;--yellow:#f5c400;
--blue:#3b82f6;--white:#ffffff;--magenta:#d946ef;--orange:#f97316
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,monospace;font-size:14px;min-height:100vh}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
nav{background:var(--surface);border-bottom:2px solid var(--accent);padding:0 20px;display:flex;align-items:stretch;gap:0}
.nav-brand{display:flex;align-items:center;gap:8px;padding:12px 20px 12px 0;border-right:1px solid var(--border);margin-right:8px}
.nav-brand span{color:var(--accent);font-size:18px;font-weight:700;letter-spacing:.5px}
.nav-links{display:flex;gap:0;align-items:stretch;flex-wrap:wrap}
.nav-links a{display:flex;align-items:center;padding:0 18px;color:var(--text-muted);font-size:13px;font-weight:500;border-bottom:2px solid transparent;margin-bottom:-2px}
.nav-links a:hover{color:var(--accent);text-decoration:none;border-bottom-color:var(--accent)}
.nav-links a.active{color:var(--accent);border-bottom-color:var(--accent)}
.page{padding:28px 24px;max-width:1180px;margin:0 auto}
.page-title{font-size:22px;font-weight:700;color:var(--accent);margin-bottom:4px;display:flex;align-items:center;gap:10px}
.page-subtitle{color:var(--text-muted);font-size:13px;margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;margin-bottom:20px}
.card-title{font-size:13px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.alert-table,.zone-list,.timing-table{width:100%;border-collapse:collapse}
.alert-table th,.zone-list th,.timing-table th{text-align:left;padding:8px 12px;font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--border)}
.alert-table td,.zone-list td,.timing-table td{padding:7px 12px;vertical-align:top;border-bottom:1px solid #1e1e1e;font-size:13px}
.alert-table tr:last-child td,.zone-list tr:last-child td,.timing-table tr:last-child td{border-bottom:none}
.zone-hdr td{background:#1a1a0a;color:var(--accent);font-weight:600;font-size:12px;padding:6px 12px;text-transform:uppercase;letter-spacing:.5px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;line-height:1.5}
.sev-tornado{background:#ffffff22;color:#fff;border:1px solid #fff}
.sev-warning{background:#ef444422;color:#ef4444;border:1px solid #ef4444}
.sev-watch{background:#f5c40022;color:#f5c400;border:1px solid #f5c400}
.sev-statement{background:#3b82f622;color:#3b82f6;border:1px solid #3b82f6}
.sev-advisory{background:#22c55e22;color:#22c55e;border:1px solid #22c55e}
.code-0{color:var(--magenta);font-weight:700;font-family:monospace}
.code-n{color:var(--green);font-weight:700;font-family:monospace}
.exp{color:var(--text-muted);font-size:12px;font-family:monospace}
.err{color:var(--orange);font-style:italic}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}
.stat-box{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.stat-label{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}
.stat-value{font-size:24px;font-weight:700;color:var(--accent);font-family:monospace}
.stat-unit{font-size:12px;color:var(--text-muted);margin-left:4px}
.zone-idx{color:var(--text-muted);font-family:monospace;font-size:12px}
.zone-id{font-family:monospace;font-weight:600;color:var(--accent)}
.zone-code{font-family:monospace;color:var(--green)}
.btn{display:inline-block;padding:7px 16px;border-radius:5px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:opacity .15s}
.btn:hover{opacity:.85}.btn-danger{background:#ef4444;color:#fff}.btn-primary{background:var(--accent);color:#000}
.btn-secondary{background:var(--border);color:var(--text)}.btn-sm{padding:4px 10px;font-size:12px}
input[type=text]{background:#1e1e1e;border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:5px;font-size:13px;font-family:monospace;outline:none}
input[type=text]:focus{border-color:var(--accent)}
.add-form{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-label{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px}
.help-text{font-size:11px;color:var(--text-muted);margin-top:3px}
.alert-banner{padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:16px}
.alert-success{background:#22c55e22;border:1px solid #22c55e;color:#22c55e}
.alert-error{background:#ef444422;border:1px solid #ef4444;color:#ef4444}
.alert-info{background:#3b82f622;border:1px solid #3b82f6;color:#3b82f6}
.zone-count{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text-muted);margin-bottom:14px}
.zone-count .num{font-size:16px;font-weight:700;color:var(--accent)}
.max-warn{color:var(--orange);font-weight:600}
.footer-bar{color:var(--text-muted);font-size:12px;margin-top:28px;padding-top:12px;border-top:1px solid var(--border);display:flex;gap:20px;flex-wrap:wrap}
pre.detail,pre.json{white-space:pre-wrap;word-break:break-word;font-family:monospace;font-size:12px;line-height:1.4;background:#111;border:1px solid var(--border);border-radius:6px;padding:16px;max-height:75vh;overflow:auto}
.inline-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.status-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600}
.status-active{background:#22c55e22;color:var(--green);border:1px solid var(--green)}
.status-inactive{background:#44444422;color:var(--text-muted);border:1px solid #444}
.type-same{color:var(--accent);font-size:11px;font-weight:600;background:#f5c40011;border:1px solid var(--accent);border-radius:3px;padding:1px 5px}
.type-latlon{color:var(--blue);font-size:11px;font-weight:600;background:#3b82f611;border:1px solid var(--blue);border-radius:3px;padding:1px 5px}
@media(max-width:760px){nav{padding:0 10px}.nav-links a{padding:10px 8px}.page{padding:18px 12px}.alert-table,.zone-list,.timing-table{display:block;overflow:auto}}
</style>
"""

SSE_SCRIPT = """
<script>
(function(){
  var es = new EventSource('/events');
  es.addEventListener('reload', function(){ es.close(); location.reload(); });
  es.onerror = function(){ setTimeout(function(){ location.reload(); }, 5000); };
})();
</script>
"""


# ── Helpers ───────────────────────────────────────────────────────────────────
def now_local() -> str:
    return datetime.now().strftime("%d %b %Y %H:%M:%S")


def short_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def clock_time() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


def clock_date() -> str:
    return datetime.now().strftime("%b %d, %Y")


def clock_dow() -> str:
    return datetime.now().strftime("%A")


def safe(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def short_expiry(expires: str) -> str:
    if not expires:
        return ""
    return expires[:16].replace("T", " ")


def tft_alert_text(zone_code: str, event: str, expires: str) -> str:
    clean_event = re.sub(r"\b(\w+)\s+\1\b", r"\1", event, flags=re.IGNORECASE)
    parts = [zone_code, clean_event]
    if expires:
        parts.append(expires)
    return " ".join(part for part in parts if part).strip()


def same_area_from_code(same_code: str) -> str:
    return STATE_FIPS_TO_AREA.get(same_code[1:3], "")


def zone_type_badge(zone: "Zone") -> str:
    if zone.type == "latlon":
        return (
            "<span class='type-latlon'>&#x1F4CD; Lat/Lon</span><br>"
            f"<span style='font-family:monospace;font-size:11px;color:var(--text-muted)'>{safe(zone.lat)}, {safe(zone.lon)}</span>"
        )
    if zone.type == "same6":
        return "<span class='type-same'>&#x1F4EF; SAME</span>"
    return "<span class='type-same'>&#x1F4EF; NWS Zone/County</span>"


def severity_for_event(event: str) -> tuple[str, str]:
    e = event or ""
    if "Tornado" in e and "Warning" in e:
        return "sev-tornado", "white"
    if "Warning" in e:
        return "sev-warning", "red"
    if "Watch" in e:
        return "sev-watch", "yellow"
    if "Statement" in e:
        return "sev-statement", "blue"
    return "sev-advisory", "green"


def led_color_for_event(event: str) -> str:
    e = event or ""
    if "Warning" in e:
        return "red"
    if "Advisory" in e:
        return "green"
    if "Watch" in e:
        return "yellow"
    if "Statement" in e:
        return "blue"
    return "green"


def qflag(query: dict[str, list[str]], key: str) -> bool:
    return key in query


def parse_float_in_range(value: str, low: float, high: float) -> bool:
    try:
        return low <= float(value) <= high
    except ValueError:
        return False


def redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    body = handler.rfile.read(length).decode("utf-8", "replace")
    parsed = parse_qs(body, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


# ── State ─────────────────────────────────────────────────────────────────────
@dataclass
class Zone:
    id: str
    code: str
    active: bool = True
    type: str = "same"  # legacy NWS zone/county ID; also supports "same6" and "latlon"
    lat: str = ""
    lon: str = ""

    def normalised(self) -> "Zone":
        zone_id = self.id.strip().upper()
        if self.type == "latlon":
            ztype = "latlon"
        elif self.type == "same6" or re.fullmatch(r"\d{6}", zone_id):
            ztype = "same6"
        else:
            ztype = "same"
        return Zone(
            id=zone_id,
            code=self.code.strip(),
            active=bool(self.active),
            type=ztype,
            lat=self.lat.strip(),
            lon=self.lon.strip(),
        )


@dataclass
class ZoneResult:
    fetched: bool = False
    http_code: int = 0
    alert_count: int = 0
    raw_json: str = ""
    load_ms: int | None = None
    parse_ms: int | None = None
    fetch_error: str = ""
    alerts: list[dict[str, str]] = field(default_factory=list)


@dataclass
class RuntimeStats:
    reboots: int = 0
    restarts: int = 0
    connects: int = 0
    badhttp: int = 0


class AppState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reload_cond = threading.Condition(self.lock)
        self.reload_generation = 0
        self.started_monotonic = time.monotonic()
        self.stats = self._load_runtime()
        self.stats.reboots += 1
        self._save_runtime()

        self.saved_at = ""
        self.zones = self._load_config_or_defaults()
        self.results: list[ZoneResult] = [ZoneResult() for _ in self.zones]
        self.display_page = self._display_placeholder()
        self.desc_text = ""
        self.last_updated = "pending"
        self.fetch_thread: threading.Thread | None = None
        self.tft2_clock_thread: threading.Thread | None = None
        self.tft_showing_alerts = False
        self.tft2_showing_area0_alerts = False
        self.shutdown_event = threading.Event()
        self.tft = RemoteTFT("TFT", TFT_HOST, TFT_PORT, TFT_DISPLAY, TFT_ROTATION, TFT_TIMEOUT)
        self.tft2 = RemoteTFT("TFT2", TFT2_HOST, TFT2_PORT, TFT2_DISPLAY, TFT2_ROTATION, TFT2_TIMEOUT)
        self.leds = RemoteLEDController(LED_HOST, LED_PORT, LED_TIMEOUT)
        self.tft.connect_if_configured()
        self.tft2.connect_if_configured()
        self.leds.clear_all()
        self._render_boot()

    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_monotonic)

    def _load_runtime(self) -> RuntimeStats:
        try:
            data = json.loads(RUNTIME_PATH.read_text())
            return RuntimeStats(
                reboots=int(data.get("reboots", 0)),
                restarts=int(data.get("restarts", 0)),
                connects=int(data.get("connects", 0)),
                badhttp=int(data.get("badhttp", 0)),
            )
        except Exception:
            return RuntimeStats()

    def _save_runtime(self) -> None:
        try:
            RUNTIME_PATH.write_text(json.dumps(asdict(self.stats), indent=2))
        except Exception as exc:
            LOG.warning("Could not persist runtime stats: %s", exc)

    def _load_config_or_defaults(self) -> list[Zone]:
        try:
            data = json.loads(CONFIG_PATH.read_text())
            zones = [Zone(**row).normalised() for row in data.get("zones", [])]
            if len(zones) > MAX_ZONES:
                zones = zones[:MAX_ZONES]
            self.saved_at = str(data.get("saved_at", ""))
            if zones:
                LOG.info("Loaded %d saved zones from %s", len(zones), CONFIG_PATH)
                return zones
        except FileNotFoundError:
            pass
        except Exception as exc:
            LOG.warning("Saved configuration ignored: %s", exc)
        LOG.info("Using built-in default zones")
        self.saved_at = ""
        return [Zone(**row).normalised() for row in DEFAULT_ZONES[:MAX_ZONES]]

    def saved_zone_ids(self) -> set[str]:
        try:
            data = json.loads(CONFIG_PATH.read_text())
            return {str(row.get("id", "")).upper() for row in data.get("zones", [])}
        except Exception:
            return set()

    def save_config(self) -> None:
        with self.lock:
            self.saved_at = now_local()
            payload = {
                "saved_at": self.saved_at,
                "zones": [asdict(z) for z in self.zones],
            }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2))
        LOG.info("Saved %d zones at %s", len(payload["zones"]), self.saved_at)

    def clear_saved_config(self) -> None:
        try:
            CONFIG_PATH.unlink()
        except FileNotFoundError:
            pass
        with self.lock:
            self.saved_at = ""
        LOG.info("Cleared saved configuration")

    def restore_saved_config(self) -> bool:
        try:
            data = json.loads(CONFIG_PATH.read_text())
            zones = [Zone(**row).normalised() for row in data.get("zones", [])][:MAX_ZONES]
            if not zones and data.get("zones"):
                return False
        except Exception:
            return False
        with self.lock:
            self.zones = zones
            self.results = [ZoneResult() for _ in zones]
            self.saved_at = str(data.get("saved_at", self.saved_at))
        self.notify_reload()
        return True

    def reset_stats(self) -> None:
        with self.lock:
            self.stats.reboots = 1
            self.stats.restarts = 0
            self.stats.connects = 0
            self.stats.badhttp = 0
            self._save_runtime()

    def zones_snapshot(self) -> list[Zone]:
        with self.lock:
            return [Zone(**asdict(z)) for z in self.zones]

    def result_snapshot(self) -> list[ZoneResult]:
        with self.lock:
            # JSON round-trip gives a cheap deep copy of nested alert structures.
            rows = []
            for r in self.results:
                rows.append(
                    ZoneResult(
                        fetched=r.fetched,
                        http_code=r.http_code,
                        alert_count=r.alert_count,
                        raw_json=r.raw_json,
                        load_ms=r.load_ms,
                        parse_ms=r.parse_ms,
                        fetch_error=r.fetch_error,
                        alerts=[dict(a) for a in r.alerts],
                    )
                )
            return rows

    def replace_results(
        self,
        results: list[ZoneResult],
        display_page: str,
        desc_text: str,
        tft_rows: list[tuple[str, str]],
        tft2_rows: list[tuple[str, str]],
    ) -> None:
        with self.lock:
            self.results = results
            self.display_page = display_page
            self.desc_text = desc_text
            self.last_updated = now_local()
            self.tft_showing_alerts = bool(tft_rows)
            self.tft2_showing_area0_alerts = bool(tft2_rows)
        self.tft.render_alerts(tft_rows, self.stats, self.uptime_seconds())
        self.tft2.render_area0_or_clock(tft2_rows)
        self.notify_reload()

    def notify_reload(self) -> None:
        with self.reload_cond:
            self.reload_generation += 1
            self.reload_cond.notify_all()

    def wait_for_reload(self, old_generation: int, timeout: float = 30.0) -> int:
        with self.reload_cond:
            if self.reload_generation == old_generation:
                self.reload_cond.wait(timeout)
            return self.reload_generation

    def _display_placeholder(self) -> str:
        return page_shell(
            "Display",
            "<div class='page'><div class='card'><div class='alert-banner alert-info'>"
            "Display not ready yet. The first NWS fetch will populate this page."
            "</div></div></div>",
            include_sse=True,
        )

    def _render_boot(self) -> None:
        suffix = "(saved config)" if self.saved_at else "(defaults)"
        self.tft.render_lines(
            [
                (f"Weather alerts {short_time()}", "yellow", 2),
                (f"{len(self.zones)} zones {suffix}", "green" if self.saved_at else "yellow", 1),
                (self.leds.status_line(), "green" if self.leds.connected else "red", 1),
                ("Waiting for first NWS fetch...", "white", 1),
            ]
        )
        self.tft2.render_clock("NO ACTIVE AREA 0 ALERTS")

    def start_fetch_loop(self) -> None:
        thread = threading.Thread(target=self._fetch_loop, name="alert-fetcher", daemon=True)
        self.fetch_thread = thread
        thread.start()
        tft2_thread = threading.Thread(target=self._clock_loop, name="tft-clock", daemon=True)
        self.tft2_clock_thread = tft2_thread
        tft2_thread.start()

    def _fetch_loop(self) -> None:
        while not self.shutdown_event.is_set():
            cycle_started = time.monotonic()
            try:
                build_cycle(self)
            except Exception:
                LOG.exception("Fetch cycle crashed")
                with self.lock:
                    self.stats.restarts += 1
                    self._save_runtime()
            elapsed = time.monotonic() - cycle_started
            delay = max(1.0, ALERT_CYCLE_SECONDS - elapsed)
            self.shutdown_event.wait(delay)

    def _clock_loop(self) -> None:
        while True:
            delay = 60.0 - (time.time() % 60.0)
            if delay < 0.05:
                delay = 60.0
            if self.shutdown_event.wait(delay):
                return
            with self.lock:
                tft_showing_alerts = self.tft_showing_alerts
                tft2_showing_alerts = self.tft2_showing_area0_alerts
            if not tft_showing_alerts:
                self.tft.render_clock("NO ACTIVE ALERTS")
            if not tft2_showing_alerts:
                self.tft2.render_clock("NO ACTIVE AREA 0 ALERTS")


class RemoteTFT:
    """Small resiliency wrapper around the uploaded TFT Terminal client."""

    def __init__(self, name: str, host: str, port: int, display: str, rotation: int, timeout: float) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.display = display
        self.rotation = rotation
        self.timeout = timeout
        self.lock = threading.RLock()
        self.term: Any = None
        self.last_error = ""
        self.configured = bool(host)
        self.clock_visible = False
        self.last_clock_key = ""

    def connect_if_configured(self) -> None:
        if not self.configured:
            LOG.info("%s output disabled: host is unset", self.name)
            return
        if TFTTerminal is None:
            self.last_error = "tft_terminal.py unavailable"
            LOG.warning("%s output disabled: %s", self.name, self.last_error)
            return
        with self.lock:
            if self.term is not None:
                return
            try:
                self.term = TFTTerminal(
                    self.host,
                    self.port,
                    display=self.display,
                    rotation=self.rotation,
                    timeout=self.timeout,
                    auto_connect=True,
                )
                try:
                    self.term.sync()
                except Exception:
                    pass
                LOG.info("Connected to %s terminal at %s:%d", self.name, self.host, self.port)
            except Exception as exc:
                self.term = None
                self.last_error = str(exc)
                LOG.warning("%s connection failed: %s", self.name, exc)

    def _with_terminal(self, fn) -> None:
        if not self.configured:
            return
        with self.lock:
            if self.term is None:
                try:
                    self.connect_if_configured()
                except Exception:
                    return
            if self.term is None:
                return
            try:
                fn(self.term)
            except Exception as exc:
                self.last_error = str(exc)
                LOG.warning("%s write failed: %s", self.name, exc)
                try:
                    self.term.disconnect()
                except Exception:
                    pass
                self.term = None

    def render_lines(self, rows: list[tuple[str, str, int]]) -> None:
        def draw(tft: Any) -> None:
            tft.fill_screen("black")
            y = 0
            for text, color, size in rows:
                clean = str(text).replace("\r", " ").replace("\n", " ")
                if not clean:
                    y += 10 * max(1, size)
                    continue
                # Match wrapping to the actual display width so short alert rows stay together.
                wrap = max(1, int(getattr(tft, "width", 320)) // (6 * max(1, size)))
                for chunk in split_visual(clean, wrap):
                    tft.text(0, y, chunk, color=color, size=size)
                    y += 9 * size + 3
                    if y > max(0, getattr(tft, "height", 240) - 12):
                        return
        self._with_terminal(draw)
        self.clock_visible = False

    def render_alerts(
        self,
        rows: list[tuple[str, str]],
        stats: RuntimeStats,
        uptime_seconds: int,
    ) -> None:
        if not rows:
            self.render_clock("NO ACTIVE ALERTS")
            return
        rendered: list[tuple[str, str, int]] = [
            (f"Weather alerts {short_time()}", "yellow", 2),
        ]
        for text, color in rows[:18]:
            rendered.append((text, color, 1))
        rendered.append((f"$$ t={uptime_seconds} c={stats.connects} h={stats.badhttp} b={stats.reboots} r={stats.restarts}", "green", 1))
        self.render_lines(rendered)

    def render_area0_or_clock(self, rows: list[tuple[str, str]]) -> None:
        if not rows:
            self.render_clock("NO ACTIVE AREA 0 ALERTS")
            return
        rendered: list[tuple[str, str, int]] = [(f"Area 0 {short_time()}", "magenta", 2)]
        for text, color in rows[:14]:
            rendered.append((text, color, 1))
        self.render_lines(rendered)

    def render_clock(self, subtitle: str = "NO ACTIVE ALERTS") -> None:
        time_text = clock_time()
        date_text = clock_date()
        dow_text = clock_dow().upper()
        clock_key = f"{time_text}|{date_text}|{dow_text}|{subtitle}"
        if self.clock_visible and self.last_clock_key == clock_key:
            return

        def draw(tft: Any) -> None:
            tft.fill_screen("black")
            width = max(1, int(getattr(tft, "width", 320)))
            height = max(1, int(getattr(tft, "height", 240)))

            def text_width(text: str, size: int) -> int:
                return len(text) * 6 * size

            def fit_size(text: str, max_width: int, preferred: int, minimum: int = 1) -> int:
                size = preferred
                while size > minimum and text_width(text, size) > max_width:
                    size -= 1
                return max(minimum, size)

            def centered(y: int, text: str, color: str, size: int) -> None:
                x = max(0, (width - text_width(text, size)) // 2)
                tft.text(x, y, text, color=color, size=size)

            time_size = fit_size(time_text, width - 8, 6 if width >= 300 else 5, 3)
            dow_size = fit_size(dow_text, width - 16, 3 if width >= 300 else 2, 2)
            date_size = fit_size(date_text, width - 16, 2, 1)
            subtitle_size = fit_size(subtitle, width - 16, 1, 1)

            total_height = (
                (8 * time_size)
                + 18
                + (8 * dow_size)
                + 14
                + (8 * date_size)
                + 18
                + (8 * subtitle_size)
            )
            y = max(8, (height - total_height) // 2)
            centered(y, time_text, "yellow", time_size)
            y += 8 * time_size + 18
            centered(y, dow_text, "white", dow_size)
            y += 8 * dow_size + 14
            centered(y, date_text, "cyan", date_size)
            y += 8 * date_size + 18
            centered(y, subtitle, "green", subtitle_size)

        self._with_terminal(draw)
        self.clock_visible = True
        self.last_clock_key = clock_key


class RemoteLEDController:
    """Sends alert colour programs to the ESP8266 LED JSON controller."""

    COLORS: dict[str, list[int]] = {
        "red": [255, 0, 0],
        "green": [0, 255, 0],
        "yellow": [255, 180, 0],
        "blue": [0, 0, 255],
        "white": [255, 255, 255],
    }

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.configured = bool(host)
        self.lock = threading.RLock()
        self.last_signatures: list[tuple[str, str, str, str, str] | None] = [None] * LED_COUNT
        self.off_confirmed: list[bool] = [False] * LED_COUNT
        self.blink_until: list[float] = [0.0] * LED_COUNT
        self.last_error = ""
        self.connected = False
        if not self.configured:
            LOG.info("LED output disabled: LED_HOST is unset")

    def clear_all(self) -> None:
        if not self.configured:
            return
        with self.lock:
            sent = 0
            for led in range(LED_COUNT):
                off_sent = self._send_off(led)
                if off_sent:
                    sent += 1
                self.off_confirmed[led] = off_sent
                self.last_signatures[led] = None
                self.blink_until[led] = 0.0
            self.connected = sent > 0

    def status_line(self) -> str:
        if not self.configured:
            return "LED host: not configured"
        if self.connected:
            return f"LED host: {self.host}:{self.port} connected"
        detail = f" ({self.last_error})" if self.last_error else ""
        return f"LED host: {self.host}:{self.port} not connected{detail}"

    def sync_area0_alerts(self, alerts: list[dict[str, str]]) -> None:
        if not self.configured:
            return
        now = time.monotonic()
        wanted = alerts[:LED_COUNT]
        with self.lock:
            wanted_signatures: list[tuple[str, str, str, str, str]] = []
            for led, alert in enumerate(wanted):
                signature = self._alert_signature(alert)
                wanted_signatures.append(signature)
                color_name = led_color_for_event(alert.get("event", ""))
                rgb = self.COLORS.get(color_name, self.COLORS["green"])
                changed = signature != self.last_signatures[led]

                if changed:
                    if self._send_alert(led, rgb, blink=True):
                        self.last_signatures[led] = signature
                        self.off_confirmed[led] = False
                        self.blink_until[led] = now + LED_BLINK_SECONDS
                elif now >= self.blink_until[led]:
                    self.blink_until[led] = 0.0

            wanted_signature_set = set(wanted_signatures)
            assigned_signature_set = {sig for sig in self.last_signatures[: len(wanted)] if sig is not None}
            for led in range(len(wanted), LED_COUNT):
                signature = self.last_signatures[led]
                if signature in wanted_signature_set and signature not in assigned_signature_set:
                    continue
                if signature is not None or not self.off_confirmed[led]:
                    self.off_confirmed[led] = self._send_off(led)
                    self.last_signatures[led] = None
                    self.blink_until[led] = 0.0

    def _alert_signature(self, alert: dict[str, str]) -> tuple[str, str, str, str, str]:
        return (
            alert.get("id", "") or alert.get("event", ""),
            alert.get("expires", ""),
            alert.get("effective", ""),
            alert.get("onset", ""),
            alert.get("description", ""),
        )

    def _send_off(self, led: int) -> bool:
        return self._send(
            {
                "led": led,
                "root": "off",
                "sequences": {"off": {"steps": [{"rgb": [0, 0, 0], "hold": 0}]}},
            }
        )

    def _send_alert(self, led: int, rgb: list[int], blink: bool) -> bool:
        if not blink:
            return self._send(
                {
                    "led": led,
                    "root": "steady",
                    "sequences": {"steady": {"steps": [{"rgb": rgb, "hold": 0}]}},
                }
            )

        blink_steps = []
        for step in range(LED_BLINK_SECONDS * 1000 // LED_BLINK_INTERVAL_MS):
            blink_steps.append(
                {
                    "rgb": rgb if step % 2 == 0 else [0, 0, 0],
                    "hold": LED_BLINK_INTERVAL_MS,
                }
            )
        blink_steps.append({"rgb": rgb, "hold": 0})
        return self._send({"led": led, "root": "alert", "sequences": {"alert": {"steps": blink_steps}}})

    def _send(self, payload: dict[str, Any]) -> bool:
        if not self.configured:
            return False
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(data)
                try:
                    response = sock.recv(512).decode("utf-8", "replace").strip()
                except socket.timeout:
                    response = ""
            if response:
                try:
                    status = json.loads(response).get("status", "")
                except ValueError:
                    status = ""
                if status and status != "ok":
                    LOG.warning("LED controller rejected command: %s", response)
                    self.connected = False
                    return False
            self.connected = True
            self.last_error = ""
            return True
        except OSError as exc:
            self.last_error = str(exc)
            self.connected = False
            LOG.warning("LED controller write failed: %s", exc)
            return False


def split_visual(text: str, width: int) -> list[str]:
    if width <= 0:
        return [text]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.extend(word[i:i+width] for i in range(0, len(word), width))
            continue
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines



# ── NWS fetch + page construction ─────────────────────────────────────────────
def build_cycle(state: AppState) -> None:
    zones = state.zones_snapshot()
    results: list[ZoneResult] = [ZoneResult() for _ in zones]
    desc_sections: list[str] = []
    display_rows: list[str] = []
    tft_rows: list[tuple[str, str]] = []
    tft2_rows: list[tuple[str, str]] = []
    last_tft_zone_idx: int | None = None

    for idx, zone in enumerate(zones):
        display_rows.append(zone_header_html(zone, idx))
        desc_sections.append("######################################")
        desc_sections.append(zone.id)
        desc_sections.append("######")

        if not zone.active:
            display_rows.append(
                f"<tr style='opacity:0.45'><td class='code-n'>{safe(zone.code)}</td>"
                "<td colspan='2' style='color:var(--text-muted);font-style:italic'>"
                "Monitoring disabled — enable in Config</td></tr>"
            )
            desc_sections.append("(inactive)")
            desc_sections.append("-------------------------------------------------------------------")
            continue

        result = fetch_zone(zone)
        results[idx] = result

        if result.fetch_error:
            state.stats.badhttp += 1
            state._save_runtime()
            display_rows.append(
                f"<tr><td class='code-n'>{safe(zone.code)}</td>"
                f"<td colspan='2' class='err'>{safe(result.fetch_error)}</td></tr>"
            )
            desc_sections.append(result.fetch_error)
            desc_sections.append("-------------------------------------------------------------------")
            continue

        if not result.alerts:
            display_rows.append(
                f"<tr><td class='code-n'>{safe(zone.code)}</td>"
                "<td colspan='2' style='color:var(--text-muted);font-style:italic'>No active alerts</td></tr>"
            )
            desc_sections.append("No active alerts")
            desc_sections.append("-------------------------------------------------------------------")
            continue

        for alert in result.alerts:
            event = alert.get("event", "")
            expires = short_expiry(alert.get("expires", ""))
            sev_class, tft_color = severity_for_event(event)
            display_rows.append(
                "<tr>"
                f"<td class='{'code-0' if idx == 0 else 'code-n'}'>{safe(zone.code)}</td>"
                f"<td><span class='badge {sev_class}'>{safe(event) or '&nbsp;'}</span></td>"
                f"<td class='exp'>{safe(expires)}</td>"
                "</tr>"
            )
            tft_text = tft_alert_text(zone.code, event, expires)
            if last_tft_zone_idx is not None and last_tft_zone_idx != idx:
                tft_rows.append(("", "black"))
            tft_rows.append((tft_text, "magenta" if idx == 0 else tft_color))
            last_tft_zone_idx = idx
            if idx == 0:
                tft2_rows.append((tft_text, "magenta"))
            desc_sections.extend(
                [
                    event,
                    alert.get("headline", ""),
                    alert.get("description", ""),
                    alert.get("effective", ""),
                    alert.get("expires", ""),
                    "-------------------------------------------------------------------",
                ]
            )

    desc_sections.append("$$$$$")
    desc_text = "\n".join(section for section in desc_sections if section is not None)
    display_page = build_display_page(zones, display_rows)
    state.replace_results(results, display_page, desc_text, tft_rows, tft2_rows)
    sync_area0_leds(state, zones, results)


def sync_area0_leds(state: AppState, zones: list[Zone], results: list[ZoneResult]) -> None:
    if not zones or not results:
        state.leds.sync_area0_alerts([])
        return
    zone = zones[0]
    result = results[0]
    if not zone.active:
        state.leds.sync_area0_alerts([])
        return
    if result.fetched and not result.fetch_error:
        state.leds.sync_area0_alerts(result.alerts)


def fetch_zone(zone: Zone) -> ZoneResult:
    headers = {
        "User-Agent": NWS_USER_AGENT,
        "Accept": "application/geo+json, application/json",
    }
    if zone.type == "latlon":
        url = "https://api.weather.gov/alerts/active"
        params = {"point": f"{zone.lat},{zone.lon}"}
    elif zone.type == "same6":
        area = same_area_from_code(zone.id)
        if not area:
            return ZoneResult(fetched=True, fetch_error=f"Unknown SAME state code: {zone.id[1:3]}")
        url = "https://api.weather.gov/alerts/active"
        params = {"area": area}
    else:
        url = "https://api.weather.gov/alerts/active"
        params = {"zone": zone.id}

    started = time.perf_counter()
    try:
        STATE.stats.connects += 1
        STATE._save_runtime()
        response = requests.get(url, params=params, headers=headers, timeout=NWS_TIMEOUT)
        load_ms = int((time.perf_counter() - started) * 1000)
    except requests.RequestException as exc:
        return ZoneResult(
            fetched=True,
            http_code=-1,
            fetch_error=f"Connection failed: {exc}",
            load_ms=int((time.perf_counter() - started) * 1000),
        )

    raw_text = response.text
    if response.status_code != 200:
        return ZoneResult(
            fetched=True,
            http_code=response.status_code,
            raw_json=raw_text,
            load_ms=load_ms,
            fetch_error=f"HTTP error {response.status_code}",
        )

    parse_started = time.perf_counter()
    try:
        payload = response.json()
    except ValueError as exc:
        return ZoneResult(
            fetched=True,
            http_code=response.status_code,
            raw_json=raw_text,
            load_ms=load_ms,
            parse_ms=int((time.perf_counter() - parse_started) * 1000),
            fetch_error=f"JSON error: {exc}",
        )

    features = payload.get("features")
    if not isinstance(features, list):
        return ZoneResult(
            fetched=True,
            http_code=response.status_code,
            raw_json=raw_text,
            load_ms=load_ms,
            parse_ms=int((time.perf_counter() - parse_started) * 1000),
            fetch_error="Unexpected API response: missing features array",
        )

    if zone.type == "same6":
        features = [feature for feature in features if feature_matches_same(feature, zone.id)]
        payload = dict(payload)
        payload["features"] = features
        raw_text = json.dumps(payload)

    alerts: list[dict[str, str]] = []
    for feature in features:
        props = feature.get("properties", {}) if isinstance(feature, dict) else {}
        if not isinstance(props, dict):
            continue
        parameters = props.get("parameters", {})
        headline = ""
        if isinstance(parameters, dict):
            nws_headline = parameters.get("NWSheadline", [])
            if isinstance(nws_headline, list) and nws_headline:
                headline = str(nws_headline[0] or "")
            elif isinstance(nws_headline, str):
                headline = nws_headline
        alerts.append(
            {
                "id": str(feature.get("id", "") or props.get("id", "") or ""),
                "event": str(props.get("event", "") or ""),
                "headline": headline,
                "description": str(props.get("description", "") or ""),
                "effective": str(props.get("effective", "") or ""),
                "onset": str(props.get("onset", "") or ""),
                "expires": str(props.get("expires", "") or ""),
            }
        )

    return ZoneResult(
        fetched=True,
        http_code=response.status_code,
        alert_count=len(alerts),
        raw_json=raw_text,
        load_ms=load_ms,
        parse_ms=int((time.perf_counter() - parse_started) * 1000),
        alerts=alerts,
    )


def feature_matches_same(feature: Any, same_code: str) -> bool:
    props = feature.get("properties", {}) if isinstance(feature, dict) else {}
    if not isinstance(props, dict):
        return False
    geocode = props.get("geocode", {})
    if not isinstance(geocode, dict):
        return False
    same_values = geocode.get("SAME", [])
    if isinstance(same_values, str):
        same_values = [same_values]
    if not isinstance(same_values, list):
        return False
    return same_code in {str(value).zfill(6) for value in same_values}


# ── HTML builders ─────────────────────────────────────────────────────────────
def nav_html(active: str) -> str:
    def nav_item(path: str, label: str) -> str:
        cls = "active" if active == label else ""
        return f"<a href='{path}' class='{cls}'>{label}</a>"

    return (
        "<nav><div class='nav-brand'><span>&#x26A1; WeatherAlert</span></div>"
        "<div class='nav-links'>"
        f"{nav_item('/display', 'Display')}"
        f"{nav_item('/stats', 'Stats')}"
        f"{nav_item('/desc', 'Detail')}"
        f"{nav_item('/areas', 'Areas')}"
        f"{nav_item('/config', 'Config')}"
        "</div></nav>"
    )


def footer_html() -> str:
    s = STATE.stats
    return (
        "<div class='footer-bar'>"
        f"<span>&#x23F1; Uptime: <b>{STATE.uptime_seconds()} s</b></span>"
        f"<span>&#x1F4F6; Connects: <b>{s.connects}</b></span>"
        f"<span>&#x274C; HTTP errors: <b>{s.badhttp}</b></span>"
        f"<span>&#x1F504; Reboots: <b>{s.reboots}</b></span>"
        f"<span>&#x26A0; Restarts: <b>{s.restarts}</b></span>"
        "</div>"
    )


def page_shell(active: str, body: str, *, include_sse: bool = False, refresh_seconds: int | None = None) -> str:
    refresh = ""
    if refresh_seconds:
        refresh = f"<meta http-equiv='refresh' content='{int(refresh_seconds)}'>"
    script = SSE_SCRIPT if include_sse else ""
    return (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Weather Alerts — {safe(active)}</title>{refresh}{CSS}{script}</head><body>"
        f"{nav_html(active)}{body}</body></html>"
    )


def build_display_page(zones: list[Zone], rows: list[str]) -> str:
    subtitle = (
        f"Updated: {safe(now_local())} &nbsp;&mdash;&nbsp; "
        f"Monitoring {len(zones)} zone{'s' if len(zones) != 1 else ''} &nbsp;&mdash;&nbsp; "
        "Auto-refreshes on data update"
    )
    if not zones:
        content = "<div class='card'><div class='alert-banner alert-info'>No zones configured. Visit <a href='/config'>Config</a> to add zones.</div></div>"
    else:
        content = (
            "<div class='card'><table class='alert-table'><thead><tr>"
            "<th>Zone</th><th>Alert</th><th>Expires (UTC)</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )
    body = (
        "<div class='page'>"
        "<div class='page-title'><span>&#x1F4CB;</span>Active Weather Alerts</div>"
        f"<div class='page-subtitle'>{subtitle}</div>"
        f"{content}{footer_html()}</div>"
    )
    return page_shell("Display", body, include_sse=True)


def zone_header_html(zone: Zone, idx: int) -> str:
    if zone.active:
        if zone.type == "latlon":
            subtitle = f"&nbsp;<span class='type-latlon'>&#x1F4CD; {safe(zone.lat)}, {safe(zone.lon)}</span>"
            label = safe(zone.code)
        else:
            subtitle = f" &mdash; {safe(zone.id)}"
            label = safe(zone.code)
        return f"<tr class='zone-hdr'><td colspan='3'><span>&#9632;</span> {label}{subtitle}</td></tr>"
    return (
        "<tr class='zone-hdr' style='opacity:0.45'><td colspan='3'>"
        f"<span>&#9632;</span> {safe(zone.code)} &mdash; {safe(zone.id)} "
        "<span style='font-size:10px;font-weight:400;color:var(--text-muted);background:#222;border:1px solid #444;border-radius:3px;padding:1px 6px;vertical-align:middle'>INACTIVE</span>"
        "</td></tr>"
    )


def build_desc_page() -> str:
    desc = STATE.desc_text
    if not desc:
        card = (
            "<div class='card'><div class='alert-banner alert-info'>"
            f"No alert detail available yet. Data loads within {ALERT_CYCLE_SECONDS} seconds of boot."
            "</div></div>"
        )
    else:
        card = f"<div class='card'><div class='card-title'>Combined Alert Detail</div><pre class='detail'>{safe(desc)}</pre></div>"
    body = (
        "<div class='page'><div class='page-title'><span>&#x1F4DC;</span>Alert Detail</div>"
        f"<div class='page-subtitle'>Full text of all active alerts — {safe(STATE.last_updated)}</div>"
        f"{card}{footer_html()}</div>"
    )
    return page_shell("Detail", body, refresh_seconds=ALERT_CYCLE_SECONDS)


def build_areas_page() -> str:
    zones = STATE.zones_snapshot()
    results = STATE.result_snapshot()
    rows: list[str] = []
    for idx, zone in enumerate(zones):
        result = results[idx] if idx < len(results) else ZoneResult()
        if not zone.active:
            alerts_cell = "<span style='color:var(--text-muted)'>-</span>"
            size_cell = "<span style='color:var(--text-muted)'>-</span>"
            view_cell = "<span style='color:var(--text-muted);font-size:12px'>inactive</span>"
        else:
            if not result.fetched:
                alerts_cell = "<span style='color:var(--text-muted);font-style:italic'>pending</span>"
            elif result.alert_count == 0:
                alerts_cell = "<span style='color:var(--text-muted)'>None</span>"
            else:
                alerts_cell = f"<span style='color:var(--yellow);font-weight:700'>{result.alert_count}</span>"
            raw_len = len(result.raw_json.encode("utf-8"))
            size_cell = f"{raw_len} B" if raw_len else "<span style='color:var(--text-muted)'>-</span>"
            view_cell = (
                f"<a href='/area?num={idx}' target='_blank' style='font-size:12px;font-weight:600'>&#x1F4C4; View JSON</a>"
                if raw_len
                else "<span style='color:var(--text-muted);font-size:12px'>no data yet</span>"
            )
        opacity = "" if zone.active else "opacity:0.5"
        rows.append(
            f"<tr style='{opacity}'>"
            f"<td class='zone-idx'>{idx}</td>"
            f"<td class='zone-id'>{safe(zone.id)}</td>"
            f"<td class='zone-code'>{safe(zone.code)}</td>"
            f"<td>{zone_type_badge(zone)}</td>"
            f"<td>{alerts_cell}</td><td style='font-family:monospace;font-size:12px;color:var(--text-muted)'>{size_cell}</td>"
            f"<td>{view_cell}</td></tr>"
        )
    if not rows:
        content = "<div class='card'><div class='alert-banner alert-info'>No zones configured. Visit <a href='/config'>Config</a> to add zones.</div></div>"
    else:
        content = (
            "<div class='card'><div class='card-title'>Zone Raw Data</div>"
            "<table class='zone-list'><thead><tr>"
            "<th>#</th><th>Zone ID / Location</th><th>Label</th><th>Type</th><th>Alerts</th><th>Size</th><th>Raw JSON</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
            "<div class='help-text' style='margin-top:10px'>&#x1F4C4; View JSON opens the raw NWS response in a new tab. Timing and fetch status are on the <a href='/stats'>Stats</a> page.</div></div>"
        )
    body = (
        "<div class='page'><div class='page-title'><span>&#x1F4E1;</span>Zone Data</div>"
        f"<div class='page-subtitle'>Fetch status and raw NWS JSON viewer — {safe(STATE.last_updated)}</div>"
        f"{content}{footer_html()}</div>"
    )
    return page_shell("Areas", body)


def build_area_page(idx: int) -> str:
    zones = STATE.zones_snapshot()
    results = STATE.result_snapshot()
    zone = zones[idx]
    result = results[idx] if idx < len(results) else ZoneResult()
    raw_len = len(result.raw_json.encode("utf-8"))
    if not zone.active:
        status = "<span style='color:var(--text-muted)'>&#x25CB; Inactive</span>"
    elif not result.fetched:
        status = "<span style='color:var(--text-muted)'>Pending first fetch</span>"
    elif result.http_code == 200:
        plural = "" if result.alert_count == 1 else "s"
        status = f"<span style='color:var(--green)'>&#x2713; HTTP 200 &mdash; {result.alert_count} alert{plural}</span>"
    else:
        status = f"<span style='color:var(--red)'>&#x2717; HTTP {result.http_code}</span>"
    body = (
        "<div class='page'>"
        f"<div class='page-title'><span>&#x1F4C4;</span><span style='color:var(--accent)'>{safe(zone.code)}</span> &mdash; "
        f"<span style='color:var(--green)'>{safe(zone.id)}</span></div>"
        f"<div class='page-subtitle'>{status} &nbsp;|&nbsp; {raw_len} bytes "
        f"&nbsp;|&nbsp; <a href='/area?num={idx}&download=1' style='color:var(--accent)'>&#x2B07; Download JSON</a> "
        "&nbsp;|&nbsp; <a href='/stats' style='color:var(--text-muted)'>Timing &amp; stats &#x2192;</a></div>"
        "<div class='card'><div class='card-title'>Raw JSON</div>"
        f"<pre class='json' id='jv'>{safe(pretty_json(result.raw_json))}</pre>"
        "<div style='margin-top:12px'><button class='btn btn-secondary btn-sm' onclick='copyJson()' id='cpbtn'>&#x1F4CB; Copy</button></div>"
        "</div><div class='footer-bar'><span>&#x2190; <a href='/areas'>Back to Areas</a></span></div></div>"
        "<script>function copyJson(){var t=document.getElementById('jv').textContent;"
        "if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done).catch(function(){fallback(t);});}else{fallback(t);}"
        "function done(){var b=document.getElementById('cpbtn');b.textContent='Copied!';setTimeout(function(){b.textContent='Copy';},2000);}"
        "function fallback(x){var ta=document.createElement('textarea');ta.value=x;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.focus();ta.select();document.execCommand('copy');document.body.removeChild(ta);done();}}</script>"
    )
    return page_shell("Areas", body)


def pretty_json(raw_json: str) -> str:
    if not raw_json:
        return ""
    try:
        return json.dumps(json.loads(raw_json), indent=2)
    except Exception:
        return raw_json


def build_stats_page() -> str:
    zones = STATE.zones_snapshot()
    results = STATE.result_snapshot()
    stat_boxes = (
        "<div class='stats-grid'>"
        f"<div class='stat-box'><div class='stat-label'>Uptime</div><div class='stat-value'>{STATE.uptime_seconds()}<span class='stat-unit'>s</span></div></div>"
        f"<div class='stat-box'><div class='stat-label'>Connects</div><div class='stat-value'>{STATE.stats.connects}</div></div>"
        f"<div class='stat-box'><div class='stat-label'>HTTP Errors</div><div class='stat-value'>{STATE.stats.badhttp}</div></div>"
        f"<div class='stat-box'><div class='stat-label'>Reboots</div><div class='stat-value'>{STATE.stats.reboots}</div></div>"
        f"<div class='stat-box'><div class='stat-label'>Restarts</div><div class='stat-value'>{STATE.stats.restarts}</div></div>"
        "</div>"
    )
    rows: list[str] = []
    for idx, zone in enumerate(zones):
        result = results[idx] if idx < len(results) else ZoneResult()
        if not zone.active:
            status = "<span style='color:var(--text-muted)'>&#x25CB; Inactive</span>"
            alerts = "-"
            raw_len = "-"
            load_ms = "-"
            parse_ms = "-"
        elif not result.fetched:
            status = "<span style='color:var(--text-muted);font-style:italic'>pending</span>"
            alerts = "-"
            raw_len = "-"
            load_ms = "-"
            parse_ms = "-"
        elif result.fetch_error:
            status = f"<span style='color:var(--red)'>{safe(result.fetch_error)}</span>"
            alerts = str(result.alert_count)
            raw_len = str(len(result.raw_json.encode("utf-8")))
            load_ms = "-" if result.load_ms is None else str(result.load_ms)
            parse_ms = "-" if result.parse_ms is None else str(result.parse_ms)
        else:
            status = f"<span style='color:var(--green)'>&#x2713; HTTP {result.http_code}</span>"
            alerts = str(result.alert_count)
            raw_len = str(len(result.raw_json.encode("utf-8")))
            load_ms = "-" if result.load_ms is None else str(result.load_ms)
            parse_ms = "-" if result.parse_ms is None else str(result.parse_ms)
        opacity = "" if zone.active else "opacity:0.5"
        rows.append(
            f"<tr style='{opacity}'><td>{idx}</td><td>{safe(zone.id)}</td><td>{safe(zone.code)}</td>"
            f"<td>{status}</td><td>{safe(alerts)}</td><td>{safe(raw_len)}</td><td>{safe(load_ms)}</td><td>{safe(parse_ms)}</td></tr>"
        )
    zone_table = (
        "<div class='card'><div class='card-title'>Per-Zone Fetch Status</div>"
        "<table class='timing-table'><thead><tr>"
        "<th>#</th><th>Zone</th><th>Label</th><th>Status</th><th>Alerts</th><th>JSON bytes</th><th>Load ms</th><th>Parse ms</th>"
        "</tr></thead><tbody>"
        + ("".join(rows) if rows else "<tr><td colspan='8'>No zones configured</td></tr>")
        + "</tbody></table></div>"
    )
    buffer_card = (
        "<div class='card'><div class='card-title'>Page / Detail Sizes</div>"
        "<table class='timing-table'><thead><tr><th>Object</th><th>Used bytes</th></tr></thead><tbody>"
        f"<tr><td>desc_text</td><td>{len(STATE.desc_text.encode('utf-8'))}</td></tr>"
        f"<tr><td>display_page</td><td>{len(STATE.display_page.encode('utf-8'))}</td></tr>"
        "</tbody></table></div>"
    )
    actions = (
        "<div class='card'><div class='card-title'>Actions</div>"
        "<a href='/flush' class='btn btn-danger' onclick=\"return confirm('Reset all counters?')\">&#x1F5D1; Reset Statistics</a>"
        "</div>"
    )
    body = (
        "<div class='page'><div class='page-title'><span>&#x1F4CA;</span>Runtime Statistics</div>"
        f"<div class='page-subtitle'>Last display build: {safe(STATE.last_updated)}</div>"
        f"{stat_boxes}{zone_table}{buffer_card}{actions}{footer_html()}</div>"
    )
    return page_shell("Stats", body, refresh_seconds=ALERT_CYCLE_SECONDS)


def build_config_page(query: dict[str, list[str]]) -> str:
    messages = [
        ("added", "success", "Zone added successfully."),
        ("exists", "error", "Zone already exists in the list."),
        ("full", "error", f"Maximum of {MAX_ZONES} zones already configured."),
        ("deleted", "success", "Zone removed successfully."),
        ("enabled", "success", "Zone enabled — will be fetched on next cycle."),
        ("disabled", "info", "Zone disabled — will be skipped on next cycle."),
        ("moved", "success", "Zone order updated."),
        ("saved", "success", "Configuration saved to disk. It will be restored on next service start."),
        ("cleared", "info", "Saved configuration cleared. Built-in defaults will be used on next start."),
        ("restored", "success", "Saved configuration restored. Running zones now match the saved configuration."),
        ("nosavedrestore", "error", "No saved configuration found — nothing to restore."),
        ("invalid", "error", "Invalid input. Use an NWS zone/county code, a 6-digit SAME code, or valid Lat/Lon values. Labels may use 1–7 characters."),
    ]
    banner = ""
    for flag, level, text in messages:
        if qflag(query, flag):
            banner = f"<div class='alert-banner alert-{level}'>{text}</div>"
            break

    zones = STATE.zones_snapshot()
    saved_ids = STATE.saved_zone_ids()
    default_ids = {row["id"].upper() for row in DEFAULT_ZONES}
    active_count = sum(1 for z in zones if z.active)
    rows: list[str] = []
    for idx, zone in enumerate(zones):
        in_default = zone.id.upper() in default_ids
        in_saved = zone.id.upper() in saved_ids
        status_badge = (
            "<span class='status-pill status-active'>&#x25CF; Active</span>"
            if zone.active
            else "<span class='status-pill status-inactive'>&#x25CB; Inactive</span>"
        )
        default_cell = "<span style='color:var(--accent);font-size:12px;font-weight:600'>&#x2713; Yes</span>" if in_default else "<span style='color:var(--text-muted)'>&#x2014;</span>"
        if not saved_ids:
            saved_cell = "<span style='color:var(--text-muted);font-size:12px'>none</span>"
        elif in_saved:
            saved_cell = "<span style='color:var(--blue);font-size:12px;font-weight:600'>&#x2713; Yes</span>"
        else:
            saved_cell = "<span style='color:var(--text-muted)'>&#x2014;</span>"
        toggle_text = "&#x23F8; Disable" if zone.active else "&#x25B6; Enable"
        toggle_style = (
            "background:#44444433;color:var(--text-muted);border:1px solid #555"
            if zone.active
            else "background:#22c55e22;color:var(--green);border:1px solid var(--green)"
        )
        row_style = "" if zone.active else "opacity:0.6"
        move_up_disabled = "disabled" if idx == 0 else ""
        move_down_disabled = "disabled" if idx == len(zones) - 1 else ""
        rows.append(
            f"<tr style='{row_style}'><td class='zone-idx'>{idx}</td>"
            f"<td class='zone-id'>{safe(zone.id)}</td><td class='zone-code'>{safe(zone.code)}</td>"
            f"<td>{zone_type_badge(zone)}</td><td>{status_badge}</td><td style='text-align:center'>{default_cell}</td>"
            f"<td style='text-align:center'>{saved_cell}</td>"
            "<td><div class='inline-actions'>"
            f"<form method='POST' action='/config/move'><input type='hidden' name='idx' value='{idx}'><input type='hidden' name='dir' value='up'><button type='submit' class='btn btn-secondary btn-sm' title='Move up' {move_up_disabled}>&#x25B2;</button></form>"
            f"<form method='POST' action='/config/move'><input type='hidden' name='idx' value='{idx}'><input type='hidden' name='dir' value='down'><button type='submit' class='btn btn-secondary btn-sm' title='Move down' {move_down_disabled}>&#x25BC;</button></form>"
            f"<form method='POST' action='/config/toggle'><input type='hidden' name='idx' value='{idx}'><button type='submit' class='btn btn-sm' style='{toggle_style}'>{toggle_text}</button></form>"
            f"<form method='POST' action='/config/delete' onsubmit=\"return confirm('Remove zone {safe(zone.id)}?')\"><input type='hidden' name='idx' value='{idx}'><button type='submit' class='btn btn-danger btn-sm'>&#x2715; Remove</button></form>"
            "</div></td></tr>"
        )
    at_max = len(zones) >= MAX_ZONES
    zone_rows = "".join(rows) if rows else "<tr><td colspan='8' style='color:var(--text-muted);font-style:italic;text-align:center;padding:16px'>No zones configured</td></tr>"
    max_note = " &nbsp;<span class='max-warn'>&#x26A0; Maximum reached</span>" if at_max else ""
    zone_card = (
        "<div class='card'><div class='card-title'>Configured Zones</div>"
        f"<div class='zone-count'><span class='num'>{len(zones)}</span> zones loaded &nbsp;&mdash;&nbsp; "
        f"<span class='num' style='color:var(--green)'>{active_count}</span> active &nbsp;&mdash;&nbsp; "
        f"<span class='num' style='color:var(--text-muted)'>{len(zones)-active_count}</span> inactive{max_note}</div>"
        "<table class='zone-list'><thead><tr><th>#</th><th>Zone ID / Location</th><th>Label</th><th>Type</th><th>Status</th><th>Default</th><th>Saved</th><th>Actions</th></tr></thead>"
        f"<tbody>{zone_rows}</tbody></table></div>"
    )
    add_card = build_add_zone_card(at_max)
    if STATE.saved_at:
        save_banner = f"<div class='alert-banner alert-success'>&#x2713; Saved configuration active — last saved: <b>{safe(STATE.saved_at)}</b></div>"
        disabled = ""
    else:
        save_banner = "<div class='alert-banner alert-info'>&#x2139; No saved configuration — built-in defaults will be used on next service start.</div>"
        disabled = "disabled"
    save_card = (
        "<div class='card'><div class='card-title'>&#x1F4BE; Save Configuration</div>"
        f"{save_banner}<div class='inline-actions'>"
        "<form method='POST' action='/config/save'><button type='submit' class='btn btn-primary'>&#x1F4BE; Save Current Zones</button></form>"
        f"<form method='POST' action='/config/restore' onsubmit=\"return confirm('Replace current zones with saved config?')\"><button type='submit' class='btn btn-secondary' {disabled}>&#x21BA; Restore from Saved</button></form>"
        f"<form method='POST' action='/config/clearsave' onsubmit=\"return confirm('Clear saved config? Defaults will load on next start.')\"><button type='submit' class='btn btn-secondary' {disabled}>&#x1F5D1; Clear Saved Config</button></form>"
        "</div><div class='help-text' style='margin-top:10px'><b>Save</b> writes the current zone list to disk. <b>Restore</b> replaces the running zones immediately. <b>Clear</b> removes the saved file.</div></div>"
    )
    body = (
        "<div class='page'><div class='page-title'><span>&#x2699;&#xFE0F;</span>Zone Configuration</div>"
        "<div class='page-subtitle'>Manage monitored alert areas — NWS zone/county codes, 6-digit SAME codes, or geographic Lat/Lon points.</div>"
        f"{banner}{zone_card}{add_card}{save_card}{footer_html()}</div>"
    )
    return page_shell("Config", body)


def build_add_zone_card(at_max: bool) -> str:
    if at_max:
        return (
            "<div class='card'><div class='card-title'>Add Zone</div>"
            f"<div class='alert-banner alert-error'>Maximum of {MAX_ZONES} zones reached. Remove a zone before adding a new one.</div></div>"
        )
    return (
        "<div class='card'><div class='card-title'>Add Alert Area</div>"
        "<div style='display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:16px'>"
        "<button onclick=\"showTab('same')\" id='tab-same' style='padding:8px 18px;background:none;border:none;border-bottom:2px solid var(--accent);color:var(--accent);font-size:13px;font-weight:600;cursor:pointer;margin-bottom:-1px'>&#x1F4EF; NWS / SAME Code</button>"
        "<button onclick=\"showTab('latlon')\" id='tab-latlon' style='padding:8px 18px;background:none;border:none;border-bottom:2px solid transparent;color:var(--text-muted);font-size:13px;font-weight:600;cursor:pointer;margin-bottom:-1px'>&#x1F4CD; Geographic Lat/Lon</button>"
        "</div>"
        "<div id='pane-same'><form method='POST' action='/config/add'><div class='add-form'>"
        "<div class='form-group'><label class='form-label'>NWS or SAME Code</label><input type='text' name='id' placeholder='e.g. NCC183 or 037183' maxlength='31' required><div class='help-text'>NWS zone/county ID or 6-digit SAME code.</div></div>"
        "<div class='form-group'><label class='form-label'>Short Label</label><input type='text' name='code' placeholder='e.g. NC W' maxlength='7' required><div class='help-text'>Display label (up to 7 chars)</div></div>"
        "<div class='form-group' style='justify-content:flex-end'><button type='submit' class='btn btn-primary'>&#x2B; Add Alert Area</button></div>"
        "</div></form></div>"
        "<div id='pane-latlon' style='display:none'><form method='POST' action='/config/addlatlon'><div class='add-form'>"
        "<div class='form-group'><label class='form-label'>Latitude</label><input type='text' name='lat' placeholder='e.g. 35.7796' maxlength='11' required><div class='help-text'>Decimal degrees (−90 to 90)</div></div>"
        "<div class='form-group'><label class='form-label'>Longitude</label><input type='text' name='lon' placeholder='e.g. -78.6382' maxlength='12' required><div class='help-text'>Decimal degrees (−180 to 180)</div></div>"
        "<div class='form-group'><label class='form-label'>Short Label</label><input type='text' name='code' placeholder='e.g. RDU' maxlength='7' required><div class='help-text'>Display label (up to 7 chars)</div></div>"
        "<div class='form-group' style='justify-content:flex-end'><button type='submit' class='btn btn-primary' style='background:var(--blue)'>&#x2B; Add Lat/Lon Zone</button></div>"
        "</div><div class='help-text' style='margin-top:8px;padding:8px 10px;background:#3b82f611;border-left:3px solid var(--blue);border-radius:3px'>&#x2139; Uses the NWS active-alert point query for the requested coordinate.</div></form></div>"
        "<script>function showTab(t){document.getElementById('pane-same').style.display=t==='same'?'':'none';document.getElementById('pane-latlon').style.display=t==='latlon'?'':'none';var ts=document.getElementById('tab-same');var tl=document.getElementById('tab-latlon');ts.style.borderBottomColor=t==='same'?'var(--accent)':'transparent';ts.style.color=t==='same'?'var(--accent)':'var(--text-muted)';tl.style.borderBottomColor=t==='latlon'?'var(--blue)':'transparent';tl.style.color=t==='latlon'?'var(--blue)':'var(--text-muted)';}</script>"
        "</div>"
    )


# ── HTTP handler ──────────────────────────────────────────────────────────────
class WeatherAlertHandler(BaseHTTPRequestHandler):
    server_version = "WeatherAlertPython/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def send_html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/":
            redirect(self, "/display")
            return
        if path == "/display":
            self.send_html(STATE.display_page)
            return
        if path == "/events":
            self.handle_events()
            return
        if path == "/desc":
            self.send_html(build_desc_page())
            return
        if path == "/areas":
            self.send_html(build_areas_page())
            return
        if path == "/area":
            self.handle_area(query)
            return
        if path == "/stats":
            self.send_html(build_stats_page())
            return
        if path == "/flush":
            STATE.reset_stats()
            redirect(self, "/stats")
            return
        if path == "/config":
            self.send_html(build_config_page(query))
            return
        self.send_text("Not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        form = read_form(self)

        if path == "/config/add":
            redirect(self, add_same_zone(form))
            return
        if path == "/config/addlatlon":
            redirect(self, add_latlon_zone(form))
            return
        if path == "/config/delete":
            redirect(self, delete_zone(form))
            return
        if path == "/config/toggle":
            redirect(self, toggle_zone(form))
            return
        if path == "/config/move":
            redirect(self, move_zone(form))
            return
        if path == "/config/save":
            try:
                STATE.save_config()
                redirect(self, "/config?saved")
            except Exception:
                LOG.exception("Save config failed")
                redirect(self, "/config?invalid")
            return
        if path == "/config/clearsave":
            STATE.clear_saved_config()
            redirect(self, "/config?cleared")
            return
        if path == "/config/restore":
            redirect(self, "/config?restored" if STATE.restore_saved_config() else "/config?nosavedrestore")
            return
        self.send_text("Not found", status=404)

    def handle_area(self, query: dict[str, list[str]]) -> None:
        try:
            idx = int(query.get("num", [""])[-1])
        except ValueError:
            self.send_text("Missing or invalid num parameter", status=400)
            return
        zones = STATE.zones_snapshot()
        results = STATE.result_snapshot()
        if idx < 0 or idx >= len(zones):
            self.send_text("num out of range", status=400)
            return
        result = results[idx] if idx < len(results) else ZoneResult()
        accepts = self.headers.get("Accept", "")
        if "application/json" in accepts or "text/plain" in accepts or qflag(query, "download"):
            content_type = "application/json; charset=utf-8"
            raw = result.raw_json or "{}"
            data = raw.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if qflag(query, "download"):
                self.send_header("Content-Disposition", f"attachment; filename={quote(zones[idx].id)}.json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_html(build_area_page(idx))

    def handle_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        generation = STATE.reload_generation
        try:
            self.wfile.write(b"event: status\ndata: connected\n\n")
            self.wfile.flush()
            while True:
                generation = STATE.wait_for_reload(generation, timeout=25.0)
                self.wfile.write(b"event: reload\ndata: reload\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return


# ── Config operations ─────────────────────────────────────────────────────────
def add_same_zone(form: dict[str, str]) -> str:
    zone_id = form.get("id", "").strip().upper()
    code = form.get("code", "").strip()
    is_same6 = bool(re.fullmatch(r"\d{6}", zone_id))
    is_nws_zone = bool(re.fullmatch(r"[A-Z0-9]{2,31}", zone_id))
    if not (is_same6 or is_nws_zone) or not (1 <= len(code) <= 7):
        return "/config?invalid"
    if is_same6 and not same_area_from_code(zone_id):
        return "/config?invalid"
    with STATE.lock:
        if len(STATE.zones) >= MAX_ZONES:
            return "/config?full"
        if any(z.id.upper() == zone_id for z in STATE.zones):
            return "/config?exists"
        STATE.zones.append(Zone(zone_id, code, True, "same6" if is_same6 else "same", "", ""))
        STATE.results.append(ZoneResult())
    STATE.notify_reload()
    return "/config?added"


def add_latlon_zone(form: dict[str, str]) -> str:
    lat = form.get("lat", "").strip()
    lon = form.get("lon", "").strip()
    code = form.get("code", "").strip()
    if not (1 <= len(lat) <= 11 and 1 <= len(lon) <= 12 and 1 <= len(code) <= 7):
        return "/config?invalid"
    if not parse_float_in_range(lat, -90.0, 90.0) or not parse_float_in_range(lon, -180.0, 180.0):
        return "/config?invalid"
    synthetic = f"LL:{lat[:5]},{lon[:6]}"
    synthetic = re.sub(r"[^A-Za-z0-9:,\-.]", "_", synthetic)[:31]
    with STATE.lock:
        if len(STATE.zones) >= MAX_ZONES:
            return "/config?full"
        if any(z.type == "latlon" and z.lat == lat and z.lon == lon for z in STATE.zones):
            return "/config?exists"
        STATE.zones.append(Zone(synthetic, code, True, "latlon", lat, lon))
        STATE.results.append(ZoneResult())
    STATE.notify_reload()
    return "/config?added"


def parse_idx(form: dict[str, str]) -> int | None:
    try:
        return int(form.get("idx", ""))
    except ValueError:
        return None


def delete_zone(form: dict[str, str]) -> str:
    idx = parse_idx(form)
    with STATE.lock:
        if idx is None or idx < 0 or idx >= len(STATE.zones):
            return "/config"
        STATE.zones.pop(idx)
        if idx < len(STATE.results):
            STATE.results.pop(idx)
    STATE.notify_reload()
    return "/config?deleted"


def toggle_zone(form: dict[str, str]) -> str:
    idx = parse_idx(form)
    with STATE.lock:
        if idx is None or idx < 0 or idx >= len(STATE.zones):
            return "/config"
        STATE.zones[idx].active = not STATE.zones[idx].active
        active = STATE.zones[idx].active
    STATE.notify_reload()
    return "/config?enabled" if active else "/config?disabled"


def move_zone(form: dict[str, str]) -> str:
    idx = parse_idx(form)
    direction = form.get("dir", "")
    offset = -1 if direction == "up" else 1 if direction == "down" else 0
    with STATE.lock:
        target = -1 if idx is None else idx + offset
        if offset == 0 or idx is None or idx < 0 or idx >= len(STATE.zones) or target < 0 or target >= len(STATE.zones):
            return "/config"
        STATE.zones[idx], STATE.zones[target] = STATE.zones[target], STATE.zones[idx]
        if idx < len(STATE.results) and target < len(STATE.results):
            STATE.results[idx], STATE.results[target] = STATE.results[target], STATE.results[idx]
    STATE.notify_reload()
    return "/config?moved"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weather alert web UI and remote TFT display service."
    )
    parser.add_argument("--alert-cycle-seconds", type=int, default=ALERT_CYCLE_SECONDS, help="Fetch interval in seconds. Env: ALERT_CYCLE_SECONDS")
    parser.add_argument("--bind", default=HTTP_BIND, help="HTTP bind address. Env: WALERT_BIND")
    parser.add_argument("--port", type=int, default=HTTP_PORT, help="HTTP port. Env: WALERT_PORT")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Zone configuration file. Env: WALERT_CONFIG")
    parser.add_argument("--runtime", type=Path, default=RUNTIME_PATH, help="Runtime statistics file. Env: WALERT_RUNTIME")
    parser.add_argument("--user-agent", default=NWS_USER_AGENT, help="NWS User-Agent header. Env: WALERT_USER_AGENT")
    parser.add_argument("--nws-timeout", type=float, default=NWS_TIMEOUT, help="NWS request timeout in seconds. Env: WALERT_NWS_TIMEOUT")
    parser.add_argument("--tft-host", default=TFT_HOST, help="Remote TFT host. Env: TFT_HOST")
    parser.add_argument("--tft-port", type=int, default=TFT_PORT, help="Remote TFT port. Env: TFT_PORT")
    parser.add_argument("--tft-display", default=TFT_DISPLAY, help="TFT display type. Env: TFT_DISPLAY")
    parser.add_argument("--tft-rotation", type=int, default=TFT_ROTATION, help="TFT rotation. Env: TFT_ROTATION")
    parser.add_argument("--tft-timeout", type=float, default=TFT_TIMEOUT, help="Remote TFT timeout in seconds. Env: TFT_TIMEOUT")
    parser.add_argument("--tft2-host", default=TFT2_HOST, help="Second remote TFT host. Env: TFT2_HOST")
    parser.add_argument("--tft2-port", type=int, default=TFT2_PORT, help="Second remote TFT port. Env: TFT2_PORT")
    parser.add_argument("--tft2-display", default=TFT2_DISPLAY, help="Second TFT display type. Env: TFT2_DISPLAY")
    parser.add_argument("--tft2-rotation", type=int, default=TFT2_ROTATION, help="Second TFT rotation. Env: TFT2_ROTATION")
    parser.add_argument("--tft2-timeout", type=float, default=TFT2_TIMEOUT, help="Second remote TFT timeout in seconds. Env: TFT2_TIMEOUT")
    parser.add_argument("--led-host", default=LED_HOST, help="Remote ESP8266 LED controller host. Env: LED_HOST or WALERT_LED_HOST")
    parser.add_argument("--led-port", type=int, default=LED_PORT, help="Remote ESP8266 LED controller TCP port. Env: LED_PORT or WALERT_LED_PORT")
    parser.add_argument("--led-timeout", type=float, default=LED_TIMEOUT, help="Remote ESP8266 LED controller timeout in seconds. Env: LED_TIMEOUT or WALERT_LED_TIMEOUT")
    parser.add_argument("--log-level", default=LOG_LEVEL, help="Python log level. Env: WALERT_LOG_LEVEL")
    return parser


def apply_cli_config(argv: list[str] | None = None) -> None:
    global ALERT_CYCLE_SECONDS, HTTP_BIND, HTTP_PORT
    global CONFIG_PATH, RUNTIME_PATH, NWS_USER_AGENT, NWS_TIMEOUT
    global TFT_HOST, TFT_PORT, TFT_DISPLAY, TFT_ROTATION, TFT_TIMEOUT, LOG_LEVEL
    global TFT2_HOST, TFT2_PORT, TFT2_DISPLAY, TFT2_ROTATION, TFT2_TIMEOUT
    global LED_HOST, LED_PORT, LED_TIMEOUT

    args = build_arg_parser().parse_args(argv)
    ALERT_CYCLE_SECONDS = args.alert_cycle_seconds
    HTTP_BIND = args.bind
    HTTP_PORT = args.port
    CONFIG_PATH = args.config
    RUNTIME_PATH = args.runtime
    NWS_USER_AGENT = args.user_agent
    NWS_TIMEOUT = args.nws_timeout
    TFT_HOST = args.tft_host
    TFT_PORT = args.tft_port
    TFT_DISPLAY = args.tft_display
    TFT_ROTATION = args.tft_rotation
    TFT_TIMEOUT = args.tft_timeout
    TFT2_HOST = args.tft2_host
    TFT2_PORT = args.tft2_port
    TFT2_DISPLAY = args.tft2_display
    TFT2_ROTATION = args.tft2_rotation
    TFT2_TIMEOUT = args.tft2_timeout
    LED_HOST = args.led_host
    LED_PORT = args.led_port
    LED_TIMEOUT = args.led_timeout
    LOG_LEVEL = str(args.log_level).upper()

    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s", force=True)


STATE: AppState


# ── Entrypoint ────────────────────────────────────────────────────────────────
def main() -> None:
    global STATE
    apply_cli_config()
    STATE = AppState()
    STATE.start_fetch_loop()
    httpd = ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), WeatherAlertHandler)
    LOG.info("WeatherAlert Python web UI listening on http://%s:%d", HTTP_BIND, HTTP_PORT)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("Shutdown requested")
    finally:
        STATE.shutdown_event.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
