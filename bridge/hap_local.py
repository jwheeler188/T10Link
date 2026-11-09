"""
Local HomeKit (HAP over IP) helpers for T10Link, on top of aiohomekit.

The T10's multicast mDNS announcements don't reliably reach the LAN (Wi-Fi power-save), so
nothing here depends on zeroconf discovery. The accessory is reached by IP (config.json
thermostat_ip). Its HAP record (device id, feature flags, ...) is read with a *unicast* mDNS
query straight to ip:5353, which it does answer.

Standard library only, apart from aiohomekit (imported lazily so tests run without it).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
from pathlib import Path

HAP_PTR = "_hap._tcp.local"
PIN_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")


# ---------------------------------------------------------------------------
# unicast mDNS (RFC 6762 section 5.5: QU question sent straight to the device)
# ---------------------------------------------------------------------------
def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split(".") if p) + b"\0"


def _read_name(data: bytes, off: int):
    labels, jumped, end = [], False, off
    for _ in range(64):                    # loop guard against malformed pointers
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | data[off + 1]
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            continue
        labels.append(data[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


def parse_mdns(data: bytes) -> list:
    """-> [(name, type, value)] for every record in a DNS message. value: PTR/SRV target names,
    TXT dict, A/AAAA address string, SRV (priority, weight, port, target)."""
    if len(data) < 12:
        return []
    qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
    off, out = 12, []
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    for _ in range(an + ns + ar):
        name, off = _read_name(data, off)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rd = data[off:off + rdlen]
        if rtype == 12:                                   # PTR
            val = _read_name(data, off)[0]
        elif rtype == 16:                                 # TXT
            val, i = {}, 0
            while i < len(rd):
                ln = rd[i]
                item = rd[i + 1:i + 1 + ln].decode("utf-8", "replace")
                k, _, v = item.partition("=")
                val[k.lower()] = v
                i += 1 + ln
        elif rtype == 33:                                 # SRV
            pr, wt, port = struct.unpack(">HHH", rd[:6])
            val = (pr, wt, port, _read_name(data, off + 6)[0])
        elif rtype == 1 and rdlen == 4:
            val = socket.inet_ntop(socket.AF_INET, rd)
        elif rtype == 28 and rdlen == 16:
            val = socket.inet_ntop(socket.AF_INET6, rd)
        else:
            val = rd
        out.append((name, rtype, val))
        off += rdlen
    return out


def hap_query_packet(name=HAP_PTR) -> bytes:
    # id 0, standard query, 1 question, PTR, class IN with the unicast-response (QU) bit
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + _encode_name(name) + struct.pack(">HH", 12, 0x8001)


def hap_record_from_answers(records, ip: str) -> dict | None:
    """Pick the _hap._tcp instance out of parsed records -> dict of its TXT keys plus name/port."""
    inst = next((v for n, t, v in records if t == 12 and n.lower().rstrip(".") == HAP_PTR), None)
    if not inst:
        return None
    txt = next((v for n, t, v in records if t == 16 and n == inst), {})
    srv = next((v for n, t, v in records if t == 33 and n == inst), None)
    addrs = [v for _n, t, v in records if t == 1]
    rec = dict(txt)
    rec["name"] = inst[: -len("." + HAP_PTR)] if inst.lower().endswith("." + HAP_PTR) else inst
    rec["port"] = srv[2] if srv else 80
    rec["address"] = ip
    rec["addresses"] = [ip] + [a for a in addrs if a != ip]
    return rec


def query_hap_record(ip: str, tries=8, timeout=2.0) -> dict | None:
    """Unicast-query ip:5353 for its HAP record. Retries because the radio sleeps."""
    pkt = hap_query_packet()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        for _ in range(tries):
            try:
                s.sendto(pkt, (ip, 5353))
                while True:
                    data, src = s.recvfrom(9000)
                    if src[0] != ip:
                        continue
                    rec = hap_record_from_answers(parse_mdns(data), ip)
                    if rec:
                        return rec
            except socket.timeout:
                continue
            except OSError:
                # "No route to host" while ARP for a sleeping device is still unresolved
                import time
                time.sleep(timeout)
    return None


# ---------------------------------------------------------------------------
# pairing data (pairing.json, readable by the service user only)
# ---------------------------------------------------------------------------
def normalize_pin(text: str) -> str:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) != 8:
        raise ValueError("the HomeKit setup code has 8 digits (shown on the thermostat as 123-45-678)")
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def write_private_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def load_pairing(path: Path, ip: str, port: int | None = None) -> dict | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # config.json is the source of truth for where the thermostat lives
    data["AccessoryIP"] = ip
    data["AccessoryIPs"] = [ip]
    if port:
        data["AccessoryPort"] = int(port)
    return data


# ---------------------------------------------------------------------------
# aiohomekit glue (lazy imports)
# ---------------------------------------------------------------------------
async def make_controller():
    """An IP controller that is never started: no zeroconf browsing, connections by IP only."""
    from aiohomekit.characteristic_cache import CharacteristicCacheMemory
    from aiohomekit.controller.ip.controller import IpController
    from zeroconf.asyncio import AsyncZeroconf

    azc = AsyncZeroconf()
    return IpController(char_cache=CharacteristicCacheMemory(), zeroconf_instance=azc), azc


def service_from_record(rec: dict):
    from aiohomekit.model import Categories
    from aiohomekit.model.feature_flags import FeatureFlags
    from aiohomekit.model.status_flags import StatusFlags
    from aiohomekit.zeroconf import HomeKitService

    cat = int(rec.get("ci", 1) or 1)
    try:
        category = Categories(cat)
    except ValueError:
        category = Categories(1)
    return HomeKitService(
        name=rec.get("name", "T10"), id=rec["id"].lower(), model=rec.get("md", ""),
        feature_flags=FeatureFlags(int(rec.get("ff", 0) or 0)),
        status_flags=StatusFlags(int(rec.get("sf", 0) or 0)),
        config_num=int(rec.get("c#", 0) or 0), state_num=int(rec.get("s#", 0) or 0),
        category=category, protocol_version=rec.get("pv", "1.0"), type="_hap._tcp.local.",
        address=rec["address"], addresses=rec["addresses"], port=int(rec.get("port", 80)))


async def pair(ip: str, pin: str, alias="t10link", record: dict | None = None) -> dict:
    """Pair-Setup with the accessory at ip. Returns the long-term pairing data to save."""
    from aiohomekit.controller.ip.discovery import IpDiscovery

    rec = record or query_hap_record(ip)
    if not rec:
        raise RuntimeError(f"no HomeKit record from {ip} (is it awake and on this network?)")
    if str(rec.get("sf", "1")) == "0":
        raise RuntimeError("the thermostat is already paired to a HomeKit controller "
                           "(Apple Home or another bridge). Remove it there, or do Menu > Advanced Setup > Reset > HomeKit Reset")
    controller, azc = await make_controller()
    try:
        disc = IpDiscovery(controller, service_from_record(rec))
        try:
            finish = await disc.async_start_pairing(alias)
            p = await finish(normalize_pin(pin))
        finally:
            await disc.close()
        data = dict(p.pairing_data)
        await p.close()
        return data
    finally:
        await azc.async_close()
