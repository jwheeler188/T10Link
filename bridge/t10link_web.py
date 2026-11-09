"""
T10Link web page, served by the bridge on web_port (2401): each thermostat's status; finding,
adding, pairing, naming, re-addressing, unpairing and removing thermostats; letting a refused Savant
host connect; and the Blueprint profile to download.

Plain HTTP/1.1 on asyncio, one request per connection; no extra packages.

    GET  /                  the page
    GET  /api/status        JSON: every thermostat
    POST /api/pair          {"n": 2, "code": "12345678"}
    POST /api/add           {"ip": "192.168.1.51", "name": "Upstairs"}   (name optional)
    POST /api/name          {"n": 2, "name": "Upstairs"}   (blank = the thermostat's HomeKit name)
    POST /api/ip            {"n": 2, "ip": "192.168.1.61"}   (it moved; the pairing is kept)
    POST /api/unpair        {"n": 2, "force": false}   (force: delete the keys even if it doesn't answer)
    POST /api/remove        {"n": 2, "force": false}   (unpairs first; later thermostats move up one)
    POST /api/scan          {}   search the network for thermostats (6-25 s)
    POST /api/allow         {"ip": "192.168.1.20"}   add a refused Savant host to allowed_clients
    GET  /profile.zip       the Blueprint profile (import folder)

Who may open it: web_clients ("lan" = private addresses by default) plus allowed_clients and this
machine. POSTs need the header X-T10Link: 1 (a page on another site can't send it), and
web_password, if set, asks for HTTP Basic auth.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import io
import ipaddress
import json
import logging
import zipfile

log = logging.getLogger("t10link")
MAX_HEAD, MAX_BODY = 16384, 4096
POSTS = ("/api/pair", "/api/add", "/api/remove", "/api/name", "/api/ip", "/api/unpair", "/api/scan", "/api/allow")


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
        if method == "GET" and path == "/profile.zip":
            await self.profile_zip(writer)
            return
        if method != "POST" or path not in POSTS:
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
        unreachable = False
        try:
            if path == "/api/pair":
                msg = await b.pair(int(req.get("n")), str(req.get("code") or ""))
            elif path == "/api/add":
                t = await b.add_thermostat(req.get("ip") or "", req.get("name") or "")
                msg = (f"Added thermostat {t.n}{' (' + t.name + ')' if t.name else ''} at {t.ip}. Pair it next; in "
                       f"Blueprint its HVAC data table row gets ThermostatAddress {t.n}.")
            elif path == "/api/name":
                n = int(req.get("n"))
                name = await b.set_name(n, req.get("name") or "")
                msg = f"Thermostat {n} is now named {name}." if name else f"Thermostat {n} uses its HomeKit name again."
            elif path == "/api/ip":
                msg = await b.set_ip(req.get("n"), req.get("ip") or "")
            elif path == "/api/unpair":
                msg = await b.unpair(req.get("n"), bool(req.get("force")))
            elif path == "/api/remove":
                msg = await b.remove_thermostat(req.get("n"), bool(req.get("force")))
            elif path == "/api/allow":
                msg = b.allow_client(req.get("ip") or "")
            else:
                res = await b.discover()
                await self.json(writer, 200, {"ok": True, **res,
                                              "message": f"Found {len(res['found'])} thermostat(s)."})
                return
        except (TypeError, ValueError) as e:
            await self.json(writer, 400, {"ok": False, "message": str(e) or "bad request"})
            return
        except Exception as e:                      # PairError and friends: show the message
            unreachable = type(e).__name__ == "Unreachable"
            await self.json(writer, 400, {"ok": False, "message": str(e), "unreachable": unreachable})
            return
        await self.json(writer, 200, {"ok": True, "message": msg})

    async def profile_zip(self, writer):
        d = self.bridge.profile_dir()
        if d is None:
            await self.send(writer, 404, "text/plain", b"The Blueprint profile isn't installed with this bridge.\n")
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(d.rglob("*")):
                if f.is_file() and not f.name.startswith("."):
                    z.write(f, "T10Link Blueprint profile/" + f.relative_to(d).as_posix())
        await self.send(writer, 200, "application/zip", buf.getvalue(),
                        {"Content-Disposition": 'attachment; filename="T10Link-Blueprint-profile.zip"'})

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
details { font-size:13px; } summary { cursor:pointer; color:var(--accent); width:max-content; }
details form { margin-top:8px; }
ol { margin:4px 0 0 18px; padding:0; }
.notes { max-width:980px; margin:0 auto; padding:0 16px; display:grid; gap:10px; }
.note { background:var(--card); border:1px solid var(--line); border-left:5px solid var(--accent); border-radius:10px; padding:12px 14px; display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; justify-content:space-between; }
.note.warn { border-left-color:var(--warn); }
.callout { border:2px solid var(--warn); background:color-mix(in srgb, var(--warn) 10%, var(--card)); border-radius:10px; padding:12px 14px; }
.callout .head { font-weight:700; font-size:15px; display:flex; gap:8px; align-items:center; }
.callout p { margin:6px 0 0; font-size:13px; }
.step { font-weight:600; margin-top:4px; }
.step .k { display:inline-block; width:22px; height:22px; border-radius:50%; background:var(--accent); color:#fff; text-align:center; line-height:22px; font-size:12px; margin-right:6px; }
.callout .step .k { background:var(--warn); }
.found { display:grid; gap:6px; }
.hit { display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; justify-content:space-between; padding:8px 10px; border:1px solid var(--line); border-radius:8px; font-size:13px; }
.hit .what { display:flex; flex-direction:column; }
.mono { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12.5px; }
label.chk { display:flex; gap:8px; align-items:flex-start; font-size:13px; flex:1 1 100%; cursor:pointer; }
label.chk input { flex:none; margin-top:3px; }
button.danger { background:var(--bad); }
.svc { display:grid; gap:10px; margin-top:8px; }
.svc .line { display:flex; flex-wrap:wrap; gap:8px; }
.setup { grid-column:1/-1; }
.kv { display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; font-size:13px; }
.kv b { font-weight:600; }
a { color:var(--accent); }
</style>
</head>
<body>
<header>
<div class="brand"><svg xmlns="http://www.w3.org/2000/svg" class="logo" aria-hidden="true" viewBox="0 0 1024 1024"><defs><linearGradient id="lgBody" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f4f5f6"/><stop offset="1" stop-color="#c9ccd0"/></linearGradient><linearGradient id="lgScreen" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#20252b"/><stop offset="1" stop-color="#0d1013"/></linearGradient></defs><rect x="112" y="112" width="800" height="800" rx="140" fill="url(#lgBody)" stroke="#8d9297" stroke-width="10"/><rect x="192" y="192" width="640" height="640" rx="90" fill="url(#lgScreen)"/><circle cx="512" cy="512" r="250" fill="none" stroke="#2c3a46" stroke-width="34"/><path d="M 335 689 A 250 250 0 0 1 512 262" fill="none" stroke="#ff8a2a" stroke-width="34" stroke-linecap="round"/><path d="M 512 262 A 250 250 0 0 1 689 335" fill="none" stroke="#3aa0ff" stroke-width="34" stroke-linecap="round"/><text x="512" y="575" font-family="Helvetica, Arial, sans-serif" font-size="190" font-weight="300" fill="#ffffff" text-anchor="middle">72&#176;</text><text x="512" y="660" font-family="Helvetica, Arial, sans-serif" font-size="56" fill="#9aa6b0" text-anchor="middle">LOCAL</text></svg><div><h1>T10Link</h1><div class="by">by James Wheeler</div>
<div class="tagline">HomeKit-speaking, cloud-ignoring, outage-shrugging.</div></div></div>
<span class="sub" id="sub">loading</span>
</header>
<div class="notes" id="notes"></div>
<main id="cards"></main>
<script>
"use strict";
const cards = document.getElementById("cards"), sub = document.getElementById("sub"), notes = document.getElementById("notes");
const built = {};          // n -> {el, key}
let addCard = null, setupCard = null, last = null, hits = null;

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, ""); else if (v !== false && v !== null) e.setAttribute(k, v);
  }
  for (const k of kids) if (k !== null && k !== undefined && k !== false) e.append(k);
  return e;
}
async function post(path, body) {
  try {
    const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-T10Link": "1"}, body: JSON.stringify(body || {})});
    let j = {}; try { j = await r.json(); } catch (e) {}
    return Object.assign({}, j, {ok: r.ok && j.ok, message: j.message || ("HTTP " + r.status)});
  } catch (e) { return {ok: false, message: "Bridge not answering"}; }
}
function say(msg, r) { msg.className = "msg " + (r.ok ? "ok" : "bad"); msg.textContent = r.message; }
function deg(v, u) { return v === "" ? "--" : v + "°" + (u || ""); }
function label(t) { return "thermostat " + t.n + (t.name ? " (" + t.name + ")" : ""); }

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

// unpair / remove: when the thermostat doesn't answer, offer to delete the bridge's keys anyway
async function withForce(path, n, msg) {
  let r = await post(path, {n: n});
  if (!r.ok && r.unreachable && confirm(r.message + "\n\nDelete the bridge's pairing keys anyway? The thermostat will still list this bridge, " +
      "so before pairing it again you'd do Menu > Advanced Setup > Reset > HomeKit Reset on the thermostat.")) {
    r = await post(path, {n: n, force: true});
  }
  say(msg, r); refresh(); return r;
}

function actions(t) {
  const box = el("div", {class: "actions"});
  const msg = el("div", {class: "msg"});
  if (!t.paired) {
    const code = el("input", {placeholder: "Code, e.g. 123-45-678", inputmode: "numeric", autocomplete: "off", maxlength: "12"});
    const btn = el("button", {type: "submit"}, "Pair");
    box.append(el("ol", {class: "hint"},
      el("li", {}, "If it's in Apple Home (or another HomeKit app), remove it there first."),
      el("li", {}, "On this thermostat open Menu > Connect HomeKit."),
      el("li", {}, "Type the code it shows (it changes each time that screen opens).")));
    box.append(el("form", {onsubmit: async (e) => {
      e.preventDefault(); btn.disabled = true; msg.className = "msg"; msg.textContent = "Pairing... up to a minute (the thermostat's Wi-Fi may need to wake).";
      const r = await post("/api/pair", {n: t.n, code: code.value});
      btn.disabled = false; say(msg, r);
      if (r.ok) code.value = "";
      refresh();
    }}, code, btn));
  } else {
    box.append(el("div", {class: "hint"}, "Blueprint: HVAC data table row with ThermostatAddress " + t.n + "."));
  }
  const name = el("input", {placeholder: t.homekit_name ? "Name, e.g. Living Room (HomeKit: " + t.homekit_name + ")" : "Name, e.g. Living Room",
                             maxlength: "40", autocomplete: "off"});
  name.value = t.custom_name || "";
  const rmsg = el("div", {class: "msg"});
  box.append(el("details", {}, el("summary", {}, "Rename"),
    el("form", {onsubmit: async (e) => {
      e.preventDefault();
      say(rmsg, await post("/api/name", {n: t.n, name: name.value})); refresh();
    }}, name, el("button", {type: "submit"}, "Save")),
    el("div", {class: "hint"}, "Shown here and in Savant (T_NAME). Blank = the thermostat's own HomeKit name, which can't be changed on a T10."),
    rmsg));

  // service: new IP, unpair, remove
  const smsg = el("div", {class: "msg"});
  const ip = el("input", {placeholder: "New IP address", autocomplete: "off", value: t.ip});
  const svc = el("div", {class: "svc"},
    el("div", {class: "hint"}, "IP " + t.ip + (t.mac ? "  ·  MAC " : ""), t.mac ? el("span", {class: "mono"}, t.mac) : null,
       t.mac ? " (for the router's DHCP reservation)" : null),
    el("form", {onsubmit: async (e) => {
      e.preventDefault();
      say(smsg, await post("/api/ip", {n: t.n, ip: ip.value})); refresh();
    }}, ip, el("button", {class: "plain", type: "submit"}, "Change IP")),
    el("div", {class: "hint"}, "Change IP: the same thermostat got a new address. A replacement thermostat: Unpair first, change the IP, then pair the new one."),
    el("div", {class: "line"},
      t.paired ? el("button", {class: "plain", type: "button", onclick: async () => {
        if (!confirm("Unpair " + label(t) + "?\n\nThe thermostat forgets this bridge and Savant can't control it until it's paired again " +
                     "(here, or with Apple Home). It stays in the list as number " + t.n + ".")) return;
        smsg.className = "msg"; smsg.textContent = "Unpairing...";
        await withForce("/api/unpair", t.n, smsg);
      }}, "Unpair") : null,
      el("button", {class: "danger", type: "button", onclick: async () => {
        const total = last ? last.thermostats.length : t.n;
        let text = "Remove " + label(t) + " from T10Link?";
        if (t.paired) text += "\n\nIt is unpaired first, so the thermostat forgets this bridge.";
        if (t.n < total) text += "\n\nThermostats " + (t.n + 1) + "-" + total + " move up one number (" + (t.n + 1) + " becomes " + t.n +
                                 "...), so change their ThermostatAddress in Blueprint's HVAC data table to match.";
        if (!confirm(text)) return;
        smsg.className = "msg"; smsg.textContent = "Removing...";
        await withForce("/api/remove", t.n, smsg);
      }}, "Remove")),
    smsg);
  box.append(el("details", {}, el("summary", {}, "Service: IP, unpair, remove"), svc));
  box.append(msg);
  return box;
}

function render(st) {
  last = st;
  const n = st.thermostats.length;
  sub.textContent = "Bridge " + st.version + "  ·  " + n + " thermostat" + (n === 1 ? "" : "s") +
    "  ·  Savant " + (st.savant_connected ? "connected" : "not connected") + " (port " + st.port + ")";
  // notes: a refused Savant host, or nothing set up yet
  notes.replaceChildren();
  if (st.rejected) {
    const m = el("div", {class: "msg"});
    notes.append(el("div", {class: "note warn"},
      el("div", {}, el("b", {}, "Savant at " + st.rejected + " was refused. "), "Only allowed addresses may connect on port " + st.port + "."),
      el("button", {type: "button", onclick: async () => { say(m, await post("/api/allow", {ip: st.rejected})); refresh(); }}, "Allow " + st.rejected), m));
  }
  if (!n) {
    notes.append(el("div", {class: "note"}, el("div", {},
      el("b", {}, "Welcome. "), "Three steps: reserve each thermostat's IP in the router, find and add it below, then pair it with the code it shows. " +
      "Then import the Blueprint profile (bottom of this page).")));
  }
  const seen = new Set();
  for (const t of st.thermostats) {
    seen.add(t.n);
    let b = built[t.n];
    const key = [t.paired, t.ip, t.mac, t.homekit_name].join("|");
    if (!b || b.key !== key) {
      const card = el("section", {class: "card"});
      card.append(el("div", {class: "top"}, el("div", {}, el("div", {class: "num"}, "Thermostat " + t.n),
        el("div", {class: "title"}), el("div", {class: "hint ipline"})), el("span", {class: "badge"})));
      card.append(el("div", {class: "info"}), actions(t));
      if (b) b.el.replaceWith(card); else cards.insertBefore(card, addCard);
      built[t.n] = b = {el: card, key: key};
    }
    const top = b.el.querySelector(".top");
    top.querySelector(".title").textContent = t.name || "Honeywell T9 / T10";
    top.querySelector(".ipline").textContent = t.ip + (t.custom_name && t.homekit_name ? "  ·  HomeKit: " + t.homekit_name : "");
    top.querySelector(".badge").replaceWith(badge(t));
    b.el.querySelector(".info").replaceWith(info(t));
  }
  for (const k of Object.keys(built)) if (!seen.has(+k)) { built[k].el.remove(); delete built[k]; }
  addCard.querySelector("button[type=submit]").disabled = n >= st.max || !addCard.querySelector("#reserved").checked;
  if (hits) showHits();
  renderSetup(st);
}

// ---- add a thermostat: 1 reserve the IP, 2 find it, 3 add it --------------------------------
let ipIn, nmIn, foundBox;
function showHits() {
  foundBox.replaceChildren();
  if (!hits.length) { foundBox.append(el("div", {class: "hint"}, "No thermostats answered. Touch the thermostat's screen to wake it, check it's on Wi-Fi and on the same network as this bridge, then search again, or type its IP below.")); return; }
  const byIp = {}; for (const t of (last ? last.thermostats : [])) byIp[t.ip] = t.n;
  for (const h of hits) {
    const at = byIp[h.ip];
    let act;
    if (at) act = el("span", {class: "badge ok"}, "Thermostat " + at);
    else if (h.moved) act = el("button", {type: "button", class: "plain", onclick: async () => {
        const r = await post("/api/ip", {n: h.moved, ip: h.ip}); alert(r.message); refresh(); }}, "Thermostat " + h.moved + " moved here: use this IP");
    else if (h.paired_elsewhere) act = el("span", {class: "badge warn", title: "Remove it from Apple Home (or the other HomeKit app) first, or do a HomeKit Reset on the thermostat"}, "Paired to another HomeKit app");
    else act = el("button", {type: "button", onclick: () => { ipIn.value = h.ip; nmIn.focus(); }}, "Use");
    foundBox.append(el("div", {class: "hit"}, el("div", {class: "what"},
      el("b", {}, h.ip + "  ·  " + (h.name || "thermostat")),
      el("span", {class: "hint"}, (h.model || "HomeKit thermostat") + (h.mac ? "  ·  MAC " : ""), h.mac ? el("span", {class: "mono"}, h.mac) : null)), act));
  }
}

function makeAdd() {
  ipIn = el("input", {placeholder: "Thermostat IP address, e.g. 192.168.1.51", autocomplete: "off"});
  nmIn = el("input", {placeholder: "Name (optional), e.g. Upstairs", maxlength: "40", autocomplete: "off"});
  const reserved = el("input", {type: "checkbox", id: "reserved"});
  const msg = el("div", {class: "msg"}), smsg = el("div", {class: "msg"});
  const btn = el("button", {type: "submit", disabled: true}, "Add thermostat");
  reserved.addEventListener("change", () => { btn.disabled = !reserved.checked || (last && last.thermostats.length >= last.max); });
  foundBox = el("div", {class: "found"});
  const scan = el("button", {type: "button", class: "plain", onclick: async () => {
    scan.disabled = true; smsg.className = "msg"; smsg.textContent = "Searching the network... up to 30 seconds.";
    const r = await post("/api/scan");
    scan.disabled = false; say(smsg, r);
    if (r.ok) { hits = r.found; showHits(); if (r.networks && r.networks.length) smsg.textContent += " Searched " + r.networks.join(", ") + "."; }
  }}, "Search the network");
  addCard = el("section", {class: "card add"}, el("div", {class: "title"}, "Add a thermostat"),
    el("div", {class: "callout"},
      el("div", {class: "head"}, "⚠️ Step 1: give the thermostat a DHCP reservation first"),
      el("p", {}, "T10Link reaches each thermostat by its IP address. If the router ever hands it a different one, Savant loses that thermostat until you fix it here. " +
                  "So in the router (or DHCP server), reserve its current IP for its MAC address. This is also called a static lease, fixed IP or address reservation."),
      el("p", {}, "The search below shows each thermostat's IP and MAC. On the thermostat, Menu > Wi-Fi shows its IP too.")),
    el("div", {class: "step"}, el("span", {class: "k"}, "2"), "Find it"),
    el("div", {class: "line"}, scan), smsg, foundBox,
    el("div", {class: "step"}, el("span", {class: "k"}, "3"), "Add it"),
    el("form", {onsubmit: async (e) => {
      e.preventDefault(); btn.disabled = true;
      const r = await post("/api/add", {ip: ipIn.value, name: nmIn.value});
      btn.disabled = !reserved.checked; say(msg, r);
      if (r.ok) { ipIn.value = ""; nmIn.value = ""; reserved.checked = false; btn.disabled = true; }
      refresh();
    }}, ipIn, nmIn,
      el("label", {class: "chk"}, reserved, el("span", {}, el("b", {}, "This IP is reserved in the router"), " (DHCP reservation for the thermostat's MAC address)")),
      btn),
    el("div", {class: "hint"}, "It gets the next number, which is its ThermostatAddress in Blueprint. Then pair it on its card."), msg);
  cards.append(addCard);
}

// ---- Savant / Blueprint setup ------------------------------------------------------------------
function renderSetup(st) {
  let host = location.hostname;
  if (/^(localhost|127\.|\[?::1\]?$)/.test(host) && st.bridge_ips && st.bridge_ips.length) host = st.bridge_ips.join(" or ");
  const kv = el("div", {class: "kv"},
    el("span", {}, "Device IP Address"), el("b", {class: "mono"}, host),
    el("span", {}, "Port"), el("b", {class: "mono"}, String(st.port)),
    el("span", {}, "ThermostatAddress"), el("b", {}, "the thermostat's number on this page, one HVAC data table row each"),
    el("span", {}, "Savant connection"), el("b", {}, st.savant_connected ? "connected from " + st.savant_clients.join(", ") : "not connected yet"));
  setupCard.replaceChildren(el("div", {class: "title"}, "Savant setup"), kv,
    st.profile ? el("div", {}, el("a", {href: "/profile.zip"}, "Download the Blueprint profile"),
                     el("span", {class: "hint"}, "  (unzip it and import honeywell_t10link.xml in Blueprint, keeping the files beside it together)")) : null);
}

async function refresh() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    if (r.ok) render(await r.json()); else sub.textContent = "Bridge answered HTTP " + r.status;
  } catch (e) { sub.textContent = "Bridge not answering"; }
}
makeAdd();
setupCard = el("section", {class: "card setup"}); cards.append(setupCard);
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""
