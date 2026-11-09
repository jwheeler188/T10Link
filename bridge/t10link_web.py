"""
T10Link web page, served by the bridge on web_port (2401): each thermostat's status, pairing with
the code the thermostat shows, and adding a thermostat by IP. Unpairing stays on the command line.

Plain HTTP/1.1 on asyncio, one request per connection; no extra packages.

    GET  /                  the page
    GET  /api/status        JSON: every thermostat
    POST /api/pair          {"n": 2, "code": "12345678"}
    POST /api/add           {"ip": "192.168.1.51"}
    POST /api/remove        {"n": 3}   (last thermostat, not paired)

Who may open it: web_clients ("lan" = private addresses by default) plus allowed_clients and this
machine. POSTs need the header X-T10Link: 1 (a page on another site can't send it), and
web_password, if set, asks for HTTP Basic auth.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import logging

log = logging.getLogger("t10link")
MAX_HEAD, MAX_BODY = 16384, 4096


def parse_clients(value):
    """web_clients -> (any, lan, [networks])."""
    items = value if isinstance(value, list) else [value]
    allow_any, lan, nets = False, False, []
    for raw in items:
        item = str(raw).strip().lower()
        if item in ("any", "*", "all"):
            allow_any = True
        elif item in ("lan", "private", "local", ""):
            lan = True
        else:
            nets.append(ipaddress.ip_network(item, strict=False))
    return allow_any, lan, nets


class WebPage:
    def __init__(self, bridge):
        self.bridge = bridge
        self.any, self.lan, self.nets = parse_clients(bridge.cfg.get("web_clients", "lan"))
        self.password = str(bridge.cfg.get("web_password") or "")

    def client_allowed(self, ip):
        if self.any or self.bridge.client_allowed(ip):
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        if any(addr in n for n in self.nets):
            return True
        return self.lan and (addr.is_private or addr.is_link_local) and not addr.is_unspecified

    def authorized(self, headers):
        if not self.password:
            return True
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            _, _, pw = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace").partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(pw.encode(), self.password.encode())

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else ""
        try:
            if not self.client_allowed(ip):
                log.warning("web page: refused %s (not in web_clients)", ip)
                await self.send(writer, 403, "text/plain", b"Not allowed from this address (web_clients in config.json)\n")
                return
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return
            if len(head) > MAX_HEAD:
                await self.send(writer, 431, "text/plain", b"Headers too large\n")
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) < 2:
                await self.send(writer, 400, "text/plain", b"Bad request\n")
                return
            method, path = parts[0].upper(), parts[1].split("?", 1)[0]
            headers = {}
            for ln in lines[1:]:
                k, sep, v = ln.partition(":")
                if sep:
                    headers[k.strip().lower()] = v.strip()
            if not self.authorized(headers):
                await self.send(writer, 401, "text/plain", b"Password required\n",
                                {"WWW-Authenticate": 'Basic realm="T10Link", charset="UTF-8"'})
                return
            body = b""
            n = int(headers.get("content-length") or 0)
            if n > MAX_BODY:
                await self.send(writer, 413, "text/plain", b"Too large\n")
                return
            if n:
                body = await asyncio.wait_for(reader.readexactly(n), 10)
            await self.route(writer, method, path, headers, body)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception as e:                      # never let the page take the bridge down
            log.warning("web page: %s: %s", type(e).__name__, e)
            try:
                await self.json(writer, 500, {"ok": False, "message": "internal error (see the bridge log)"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def route(self, writer, method, path, headers, body):
        b = self.bridge
        if method == "GET" and path in ("/", "/index.html"):
            await self.send(writer, 200, "text/html; charset=utf-8", PAGE.encode())
            return
        if method == "GET" and path == "/api/status":
            await self.json(writer, 200, b.status())
            return
        if method != "POST" or path not in ("/api/pair", "/api/add", "/api/remove"):
            await self.send(writer, 404, "text/plain", b"Not found\n")
            return
        # A page on another site can't add a custom header without a CORS preflight, which this
        # server never approves: so only this page can make changes.
        if headers.get("x-t10link") != "1" or "application/json" not in headers.get("content-type", ""):
            await self.json(writer, 403, {"ok": False, "message": "missing X-T10Link header"})
            return
        try:
            req = json.loads(body or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            await self.json(writer, 400, {"ok": False, "message": "bad JSON"})
            return
        try:
            if path == "/api/pair":
                msg = await b.pair(int(req.get("n")), str(req.get("code") or ""))
            elif path == "/api/add":
                t = await b.add_thermostat(req.get("ip") or "")
                msg = (f"Added thermostat {t.n} at {t.ip}. Pair it next; in Blueprint its HVAC data table row "
                       f"gets ThermostatAddress {t.n}.")
            else:
                n = int(req.get("n"))
                await b.remove_thermostat(n)
                msg = f"Removed thermostat {n}."
        except (TypeError, ValueError) as e:
            await self.json(writer, 400, {"ok": False, "message": str(e) or "bad request"})
            return
        except Exception as e:                      # PairError and friends: show the message
            await self.json(writer, 400, {"ok": False, "message": str(e)})
            return
        await self.json(writer, 200, {"ok": True, "message": msg})

    async def json(self, writer, code, obj):
        await self.send(writer, code, "application/json", json.dumps(obj).encode())

    async def send(self, writer, code, ctype, data, extra=None):
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                  413: "Payload Too Large", 431: "Request Header Fields Too Large", 500: "Internal Server Error"}[code]
        hdr = {"Content-Type": ctype, "Content-Length": str(len(data)), "Connection": "close",
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
               "Referrer-Policy": "no-referrer",
               "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"}
        hdr.update(extra or {})
        writer.write((f"HTTP/1.1 {code} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items())
                      + "\r\n").encode("latin-1") + data)
        await writer.drain()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>T10Link</title>
<style>
:root { --bg:#f5f5f7; --card:#fff; --text:#1d1d1f; --muted:#6e6e73; --line:#d9d9de; --accent:#0a6cdf;
        --ok:#1f8f4e; --warn:#b26a00; --bad:#c62828; --chip:#eef0f3; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#151517; --card:#232326; --text:#f2f2f4; --muted:#a1a1a8; --line:#3a3a40; --accent:#4c9bff;
          --ok:#4cc27e; --warn:#f0a63a; --bad:#ff6b6b; --chip:#2e2e33; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
header { padding:20px 16px 10px; max-width:980px; margin:0 auto; display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:10px 20px; }
.brand { display:flex; align-items:center; gap:12px; min-width:0; }
.logo { width:52px; height:52px; flex:none; }
h1 { font-size:22px; margin:0; line-height:1.1; }
.by { font-size:13px; color:var(--muted); }
.tagline { font-size:13px; font-style:italic; color:var(--muted); margin-top:2px; max-width:420px; }
.sub { color:var(--muted); font-size:13px; }
main { max-width:980px; margin:0 auto; padding:8px 16px 40px; display:grid; gap:14px; grid-template-columns:repeat(auto-fill, minmax(min(300px, 100%), 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; display:flex; flex-direction:column; gap:10px; }
.top { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.num { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.title { font-weight:600; font-size:16px; }
.badge { font-size:12px; padding:2px 8px; border-radius:99px; background:var(--chip); white-space:nowrap; }
.badge.ok { color:var(--ok); } .badge.warn { color:var(--warn); } .badge.bad { color:var(--bad); }
.big { font-size:34px; font-weight:600; line-height:1; }
.row { display:flex; flex-wrap:wrap; gap:6px 16px; color:var(--muted); font-size:13px; }
.row b { color:var(--text); font-weight:600; }
.problem { color:var(--bad); font-size:13px; }
.hint { color:var(--muted); font-size:13px; }
form { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
input { font:inherit; padding:7px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--text); min-width:0; flex:1 1 140px; }
button { font:inherit; padding:7px 14px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
button.plain { background:var(--chip); color:var(--text); }
button:disabled { opacity:.5; cursor:default; }
.msg { font-size:13px; } .msg.ok { color:var(--ok); } .msg.bad { color:var(--bad); }
.add { grid-column:1/-1; }
ol { margin:4px 0 0 18px; padding:0; }
</style>
</head>
<body>
<header>
<div class="brand"><svg xmlns="http://www.w3.org/2000/svg" class="logo" aria-hidden="true" viewBox="0 0 1024 1024"><defs><linearGradient id="lgBody" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f4f5f6"/><stop offset="1" stop-color="#c9ccd0"/></linearGradient><linearGradient id="lgScreen" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#20252b"/><stop offset="1" stop-color="#0d1013"/></linearGradient></defs><rect x="112" y="112" width="800" height="800" rx="140" fill="url(#lgBody)" stroke="#8d9297" stroke-width="10"/><rect x="192" y="192" width="640" height="640" rx="90" fill="url(#lgScreen)"/><circle cx="512" cy="512" r="250" fill="none" stroke="#2c3a46" stroke-width="34"/><path d="M 335 689 A 250 250 0 0 1 512 262" fill="none" stroke="#ff8a2a" stroke-width="34" stroke-linecap="round"/><path d="M 512 262 A 250 250 0 0 1 689 335" fill="none" stroke="#3aa0ff" stroke-width="34" stroke-linecap="round"/><text x="512" y="575" font-family="Helvetica, Arial, sans-serif" font-size="190" font-weight="300" fill="#ffffff" text-anchor="middle">72&#176;</text><text x="512" y="660" font-family="Helvetica, Arial, sans-serif" font-size="56" fill="#9aa6b0" text-anchor="middle">LOCAL</text></svg><div><h1>T10Link</h1><div class="by">by James Wheeler</div>
<div class="tagline">HomeKit-speaking, cloud-ignoring, outage-shrugging.</div></div></div>
<span class="sub" id="sub">loading</span>
</header>
<main id="cards"></main>
<script>
"use strict";
const cards = document.getElementById("cards"), sub = document.getElementById("sub");
const built = {};          // n -> {el, paired}
let addCard = null, last = null;

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
  }
  for (const k of kids) if (k !== null && k !== undefined) e.append(k);
  return e;
}
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-T10Link": "1"}, body: JSON.stringify(body)});
  let j = {}; try { j = await r.json(); } catch (e) {}
  return {ok: r.ok && j.ok, message: j.message || ("HTTP " + r.status)};
}
function deg(v, u) { return v === "" ? "--" : v + "°" + (u || ""); }

function badge(t) {
  if (!t.paired) return el("span", {class: "badge warn"}, "Not paired");
  if (t.connected && t.online) return el("span", {class: "badge ok"}, "Connected");
  if (t.online) return el("span", {class: "badge warn"}, "Reconnecting");
  return el("span", {class: "badge bad"}, "Offline");
}

function info(t) {
  const box = el("div", {class: "info"});
  if (t.paired && t.mode) {
    box.append(el("div", {class: "big"}, deg(t.temp, t.units)));
    const r = el("div", {class: "row"});
    r.append(el("span", {}, "Mode ", el("b", {}, t.mode)), el("span", {}, el("b", {}, t.state || "")));
    if (t.mode === "Heat" || t.mode === "Auto") r.append(el("span", {}, "Heat ", el("b", {}, deg(t.heat))));
    if (t.mode === "Cool" || t.mode === "Auto") r.append(el("span", {}, "Cool ", el("b", {}, deg(t.cool))));
    if (t.humidity !== "") r.append(el("span", {}, "Humidity ", el("b", {}, t.humidity + "%")));
    box.append(r);
    for (const s of t.sensors) box.append(el("div", {class: "row"}, el("span", {}, "Sensor " + s.n + ": ", el("b", {}, s.name)),
        el("span", {}, deg(s.temp)), s.humidity !== "" ? el("span", {}, s.humidity + "%") : null, s.motion ? el("span", {}, "motion") : null));
    if (t.pending) box.append(el("div", {class: "hint"}, "Changing: " + t.pending));
  }
  if (t.problem && t.paired) box.append(el("div", {class: "problem"}, t.problem));
  else if (t.error) box.append(el("div", {class: "problem"}, t.error));
  return box;
}

function actions(t, card) {
  const box = el("div", {class: "actions"});
  const msg = el("div", {class: "msg"});
  if (!t.paired) {
    const code = el("input", {placeholder: "Code, e.g. 123-45-678", inputmode: "numeric", autocomplete: "off", maxlength: "12"});
    const btn = el("button", {type: "submit"}, "Pair");
    box.append(el("ol", {class: "hint"},
      el("li", {}, "On this thermostat open Menu > Connect HomeKit."),
      el("li", {}, "Type the code it shows (it changes each time that screen opens).")));
    box.append(el("form", {onsubmit: async (e) => {
      e.preventDefault(); btn.disabled = true; msg.className = "msg"; msg.textContent = "Pairing... up to a minute (the thermostat's Wi-Fi may need to wake).";
      const r = await post("/api/pair", {n: t.n, code: code.value});
      btn.disabled = false; msg.className = "msg " + (r.ok ? "ok" : "bad"); msg.textContent = r.message;
      if (r.ok) code.value = "";
      refresh();
    }}, code, btn));
  } else {
    box.append(el("div", {class: "hint"}, "Blueprint: HVAC data table row with ThermostatAddress " + t.n + "."));
  }
  if (t.removable) {
    box.append(el("form", {onsubmit: async (e) => {
      e.preventDefault();
      if (!confirm("Remove thermostat " + t.n + " (" + t.ip + ") from the bridge?")) return;
      const r = await post("/api/remove", {n: t.n});
      msg.className = "msg " + (r.ok ? "ok" : "bad"); msg.textContent = r.message; refresh();
    }}, el("button", {class: "plain", type: "submit"}, "Remove")));
  }
  box.append(msg);
  return box;
}

function render(st) {
  last = st;
  const n = st.thermostats.length;
  sub.textContent = "Bridge " + st.version + "  ·  " + n + " thermostat" + (n === 1 ? "" : "s") +
    "  ·  Savant " + (st.savant_connected ? "connected" : "not connected") + " (port " + st.port + ")";
  const seen = new Set();
  for (const t of st.thermostats) {
    seen.add(t.n);
    let b = built[t.n];
    if (!b || b.paired !== t.paired || b.removable !== t.removable || b.ip !== t.ip) {
      const card = el("section", {class: "card"});
      const fresh = {el: card, paired: t.paired, removable: t.removable, ip: t.ip};
      card.append(el("div", {class: "top"}, el("div", {}, el("div", {class: "num"}, "Thermostat " + t.n),
        el("div", {class: "title"}, t.name || "Honeywell T9 / T10"), el("div", {class: "hint"}, t.ip)), el("span", {class: "badge"})));
      card.append(el("div", {class: "info"}), actions(t, card));
      if (b) b.el.replaceWith(card); else cards.insertBefore(card, addCard);
      built[t.n] = b = fresh;
    }
    const top = b.el.querySelector(".top");
    top.querySelector(".title").textContent = t.name || "Honeywell T9 / T10";
    top.querySelector(".badge").replaceWith(badge(t));
    b.el.querySelector(".info").replaceWith(info(t));
  }
  for (const k of Object.keys(built)) if (!seen.has(+k)) { built[k].el.remove(); delete built[k]; }
  addCard.querySelector("button").disabled = n >= st.max;
}

function makeAdd() {
  const ip = el("input", {placeholder: "Thermostat IP address, e.g. 192.168.1.51", autocomplete: "off"});
  const msg = el("div", {class: "msg"});
  const btn = el("button", {type: "submit"}, "Add");
  addCard = el("section", {class: "card add"}, el("div", {class: "title"}, "Add a thermostat"),
    el("div", {class: "hint"}, "Menu > Wi-Fi on the thermostat shows its IP. Give it a DHCP reservation first. It becomes the next number, which is its ThermostatAddress in Blueprint."),
    el("form", {onsubmit: async (e) => {
      e.preventDefault(); btn.disabled = true;
      const r = await post("/api/add", {ip: ip.value});
      btn.disabled = false; msg.className = "msg " + (r.ok ? "ok" : "bad"); msg.textContent = r.message;
      if (r.ok) ip.value = "";
      refresh();
    }}, ip, btn), msg);
  cards.append(addCard);
}

async function refresh() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    if (r.ok) render(await r.json()); else sub.textContent = "Bridge answered HTTP " + r.status;
  } catch (e) { sub.textContent = "Bridge not answering"; }
}
makeAdd(); refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""
