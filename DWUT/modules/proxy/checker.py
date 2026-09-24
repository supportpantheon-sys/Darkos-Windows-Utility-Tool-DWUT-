"""
modules/proxy/checker.py

Real 9-stage proxy validation pipeline.

The original check_proxies() did:
    requests.get("http://google.com", proxies={...})
That's a reachability test, not a proxy validator.

This pipeline tests:
    PARSE → TCP CONNECT → PROTOCOL HANDSHAKE → EXIT-IP TEST →
    GEOLOCATION → ANONYMITY TEST → POOL ANALYSIS → SCORING → CLASSIFY

All results are factual.  If we can't determine a property we say so
("unknown") rather than guessing or claiming false certainty.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from core.events import Events, bus
from core.logger import get_logger
from modules.proxy.sources import ProxyRecord

_log = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Per-thread requests.Session — a bare requests.get() builds and tears down
# a whole connection-pool adapter on every single call, which is wasted work
# at the volume this checker runs at (tens of thousands of calls). A shared
# session per worker thread reuses connections where possible and — more
# importantly — puts an explicit, small cap on how many connections any one
# worker thread can keep open at once, instead of leaving it uncapped.
# ──────────────────────────────────────────────────────────────────────────────

_thread_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _thread_local.session = s
    return s

# ──────────────────────────────────────────────────────────────────────────────
# Known datacenter ASNs (partial list — proxies on these are flagged as pooled)
# ──────────────────────────────────────────────────────────────────────────────

_DATACENTER_ASNS = {
    "AS14618",  # Amazon AWS
    "AS16509",  # Amazon AWS
    "AS15169",  # Google Cloud
    "AS8075",   # Microsoft Azure
    "AS20473",  # Vultr
    "AS14061",  # DigitalOcean
    "AS16276",  # OVH
    "AS24940",  # Hetzner
    "AS63949",  # Linode / Akamai
    "AS9009",   # M247 (common VPN provider)
    "AS60781",  # LeaseWeb
    "AS197540", # Netcup
}

# ──────────────────────────────────────────────────────────────────────────────
# Anonymity-revealing headers that transparent/anonymous proxies inject
# ──────────────────────────────────────────────────────────────────────────────

_REVEALING_HEADERS = {
    "x-forwarded-for",
    "x-real-ip",
    "via",
    "proxy-connection",
    "x-proxy-id",
    "forwarded",
}

# ──────────────────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    proxy: ProxyRecord

    # Stage results
    tcp_ok: bool = False
    tcp_latency_ms: Optional[float] = None

    protocol_ok: bool = False

    exit_ip: Optional[str] = None
    is_transparent: bool = False      # exit IP == your real IP
    is_pooled_candidate: bool = False # exit IP differs from proxy IP

    country: Optional[str] = None
    country_code: Optional[str] = None
    isp: Optional[str] = None
    asn: Optional[str] = None
    timezone: Optional[str] = None

    anonymity: Optional[str] = None   # "elite" | "anonymous" | "transparent" | None
    revealing_headers: list[str] = field(default_factory=list)

    pool_flags: list[str] = field(default_factory=list)  # reasons it was flagged as pooled

    score: int = 0
    tier: str = "dead"   # "excellent" | "good" | "fair" | "rejected" | "dead"
    reject_reason: Optional[str] = None

    def update_proxy(self) -> ProxyRecord:
        """Return a new ProxyRecord with check results filled in."""
        from dataclasses import replace
        return replace(
            self.proxy,
            status=self.tier,
            latency_ms=self.tcp_latency_ms,
            country=self.country,
            country_code=self.country_code,
            isp=self.isp,
            asn=self.asn,
            anonymity=self.anonymity,
            exit_ip=self.exit_ip,
            is_pooled=bool(self.pool_flags),
            score=self.score,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Stage helpers
# ──────────────────────────────────────────────────────────────────────────────

def _my_real_ip(timeout: int = 5) -> Optional[str]:
    """Get our real (non-proxied) external IP for transparent proxy detection."""
    for url in ("https://api.ipify.org", "https://api.my-ip.io/ip"):
        try:
            r = _session().get(url, timeout=timeout)
            ip = r.text.strip()
            ipaddress.ip_address(ip)  # validate
            return ip
        except Exception:
            continue
    return None


_real_ip_cache: Optional[str] = None


def get_my_ip() -> Optional[str]:
    global _real_ip_cache
    if _real_ip_cache is None:
        _real_ip_cache = _my_real_ip()
    return _real_ip_cache


def _stage_tcp(proxy: ProxyRecord, timeout: float = 2.0) -> tuple[bool, Optional[float]]:
    """Raw TCP connect — fastest fail-fast stage."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((proxy.ip, proxy.port), timeout=timeout):
            pass
        latency = (time.monotonic() - t0) * 1000
        return True, latency
    except Exception:
        return False, None


def _stage_protocol(proxy: ProxyRecord, timeout: int = 3) -> bool:
    """Verify the proxy speaks its claimed protocol."""
    proxies = {
        "http": proxy.uri(),
        "https": proxy.uri(),
    }
    try:
        r = _session().get(
            "http://httpbin.org/get",
            proxies=proxies,
            timeout=timeout,
        )
        return r.status_code == 200
    except Exception:
        return False


def _stage_exit_ip(proxy: ProxyRecord, timeout: int = 5) -> Optional[str]:
    """Route through proxy to determine exit IP."""
    proxies = {"http": proxy.uri(), "https": proxy.uri()}
    for url in ("https://api.ipify.org", "https://api.my-ip.io/ip"):
        try:
            r = _session().get(url, proxies=proxies, timeout=timeout)
            ip = r.text.strip()
            ipaddress.ip_address(ip)
            return ip
        except Exception:
            continue
    return None


def _stage_geolocation(ip: str, timeout: int = 3) -> dict:
    """Look up geolocation data for the exit IP."""
    try:
        r = _session().get(
            f"http://ip-api.com/json/{ip}?fields=country,countryCode,isp,org,as,timezone",
            timeout=timeout,
        )
        data = r.json()
        if data.get("status") == "success":
            return data
    except Exception:
        pass
    return {}


def _stage_anonymity(proxy: ProxyRecord, timeout: int = 5) -> tuple[str, list[str]]:
    """
    Check which identifying headers the proxy injects.

    elite      → none of the revealing headers present
    anonymous  → Via / Proxy-Connection present but NOT X-Forwarded-For
    transparent→ X-Forwarded-For present (leaks real IP)
    """
    proxies = {"http": proxy.uri(), "https": proxy.uri()}
    found: list[str] = []
    try:
        r = _session().get("http://httpbin.org/headers", proxies=proxies, timeout=timeout)
        headers_sent = {k.lower(): v for k, v in r.json().get("headers", {}).items()}
        for h in _REVEALING_HEADERS:
            if h in headers_sent:
                found.append(h)
    except Exception:
        return "unknown", []

    if "x-forwarded-for" in found:
        return "transparent", found
    if found:
        return "anonymous", found
    return "elite", found


def _stage_pool_analysis(
    proxy: ProxyRecord,
    exit_ip: Optional[str],
    geo: dict,
    timeout: int = 4,
) -> list[str]:
    """
    Detect indicators of shared pool infrastructure.

    Returns a list of flag strings (non-empty = pooled candidate).
    We make honest claims: "likely pooled" not "definitely unique".
    """
    flags: list[str] = []

    if exit_ip and exit_ip != proxy.ip:
        flags.append(f"exit IP ({exit_ip}) differs from proxy IP ({proxy.ip})")

    asn = geo.get("as", "")
    if any(dc in asn for dc in _DATACENTER_ASNS):
        flags.append(f"datacenter ASN detected ({asn})")

    org = geo.get("org", "").lower()
    dc_keywords = ["hosting", "datacenter", "cloud", "server", "vps", "colocation", "colo"]
    for kw in dc_keywords:
        if kw in org:
            flags.append(f"datacenter org keyword: '{kw}'")
            break

    # Multi-request exit-IP test: run 3 requests, if exit IPs differ → pool
    if exit_ip is None:
        return flags

    proxies = {"http": proxy.uri(), "https": proxy.uri()}
    exit_ips: set[str] = {exit_ip}
    for _ in range(2):
        try:
            r = _session().get("https://api.ipify.org", proxies=proxies, timeout=timeout)
            candidate = r.text.strip()
            ipaddress.ip_address(candidate)
            exit_ips.add(candidate)
        except Exception:
            pass

    if len(exit_ips) > 1:
        flags.append(f"multiple exit IPs observed ({', '.join(exit_ips)}) — confirmed pool")

    return flags


def _score(result: CheckResult) -> int:
    """
    Compute 0–100 score.

    Latency:    ≤50ms=30,  ≤100ms=20,  ≤200ms=10,  >200ms=5
    Anonymity:  elite=25,  anonymous=15, transparent=0, unknown=5
    Pool flags: 0 flags=20, 1 flag=10, 2+=0
    Protocol:   socks5=15, socks4=10, http=5
    Country data available: +10
    """
    s = 0

    lat = result.tcp_latency_ms or 9999
    if lat <= 50:
        s += 30
    elif lat <= 100:
        s += 20
    elif lat <= 200:
        s += 10
    else:
        s += 5

    anon = result.anonymity or "unknown"
    if anon == "elite":
        s += 25
    elif anon == "anonymous":
        s += 15
    elif anon == "unknown":
        s += 5
    # transparent = 0

    if not result.pool_flags:
        s += 20
    elif len(result.pool_flags) == 1:
        s += 10

    proto = result.proxy.protocol
    if proto == "socks5":
        s += 15
    elif proto == "socks4":
        s += 10
    else:
        s += 5

    if result.country:
        s += 5

    return min(100, s)


def _tier(score: int, result: CheckResult) -> str:
    if result.is_transparent:
        return "rejected"
    if score >= 80:
        return "excellent"
    if score >= 60:
        return "good"
    if score >= 40:
        return "fair"
    return "rejected"


# ──────────────────────────────────────────────────────────────────────────────
# Full single-proxy check
# ──────────────────────────────────────────────────────────────────────────────

def check_one(proxy: ProxyRecord, my_ip: Optional[str] = None) -> CheckResult:
    """Run all 9 stages on a single proxy."""
    result = CheckResult(proxy=proxy)

    # Stage 1 — TCP connect
    ok, latency = _stage_tcp(proxy)
    result.tcp_ok = ok
    result.tcp_latency_ms = latency
    if not ok:
        result.tier = "dead"
        return result

    # Stage 2 — Protocol handshake
    result.protocol_ok = _stage_protocol(proxy)
    if not result.protocol_ok:
        result.tier = "dead"
        result.reject_reason = "protocol handshake failed"
        return result

    # Stage 3 — Exit IP
    result.exit_ip = _stage_exit_ip(proxy)

    # Stage 4 — Transparent check
    if result.exit_ip and my_ip and result.exit_ip == my_ip:
        result.is_transparent = True
        result.tier = "rejected"
        result.reject_reason = "transparent — leaks real IP"
        result.score = 0
        return result

    if result.exit_ip and result.exit_ip != proxy.ip:
        result.is_pooled_candidate = True

    # Stage 5 — Geolocation
    if result.exit_ip:
        geo = _stage_geolocation(result.exit_ip)
        result.country = geo.get("country")
        result.country_code = geo.get("countryCode")
        result.isp = geo.get("isp") or geo.get("org")
        result.asn = geo.get("as")
        result.timezone = geo.get("timezone")

    # Stage 6 — Anonymity
    result.anonymity, result.revealing_headers = _stage_anonymity(proxy)

    # Stage 7 — Pool analysis
    geo_data = {
        "as": result.asn or "",
        "org": result.isp or "",
    }
    result.pool_flags = _stage_pool_analysis(proxy, result.exit_ip, geo_data)

    # Stage 8 — Score
    result.score = _score(result)

    # Stage 9 — Classify
    result.tier = _tier(result.score, result)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Batch checker
# ──────────────────────────────────────────────────────────────────────────────

def check_all(
    proxies: list[ProxyRecord],
    max_workers: int = 50,
    region_filter: Optional[str] = None,
    protocol_filter: Optional[str] = None,
    min_anonymity: Optional[str] = None,
    max_latency_ms: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
) -> list[CheckResult]:
    """
    Check a list of proxies concurrently.

    Filters (all optional, None/"any"/"Any" = no filter):
      protocol_filter — only check proxies of this protocol (applied
                         BEFORE checking starts, so filtered-out proxies
                         never cost a network round trip at all)
      region_filter    — keep only results whose geolocated exit country
                          matches (applied AFTER the geolocation stage,
                          since that's the only point the country is known)
      min_anonymity    — keep only results at or above this anonymity
                          level (elite > anonymous > transparent)
      max_latency_ms   — keep only results with TCP latency at or under
                          this threshold

    A proxy that fails a post-check filter still counts toward progress
    (it WAS checked) but is left out of `results` and the batch events —
    this is what actually filters dead/mismatched proxies out of the
    table, instead of just collecting everything and hoping the UI
    filters it back out cosmetically.

    `stop_event`, if given, lets the caller forcefully abort mid-run: once
    set, no new checks are submitted and any not-yet-started ones are
    cancelled immediately, so control returns to the caller within
    ~200ms instead of waiting for the whole batch. Checks that were
    already in-flight when the stop was requested simply finish quietly
    in the background and are discarded — Python can't kill a running
    thread outright, but nothing from them reaches the UI.

    Publishes Events.PROXY_CHECK_PROGRESS periodically.
    Publishes Events.PROXY_CHECK_DONE with the final results list.
    Publishes Events.PROXY_RESULT_BATCH with a list of newly-completed
    results roughly every 200ms — NOT one event per proxy. Publishing per
    proxy meant a check of thousands of proxies (most of which die
    instantly at the TCP stage, so huge waves complete in near lock-step)
    could flood the UI thread with thousands of individually-scheduled
    callbacks in a short burst, which was the real source of the checker
    feeling laggy — batching cuts that to at most ~5 events/sec no matter
    how many proxies are being checked.
    """
    _ANON_RANK = {"transparent": 0, "anonymous": 1, "elite": 2}

    def _passes_post_filters(result: "CheckResult") -> bool:
        if region_filter and region_filter.lower() != "any":
            if not result.country or result.country.lower() != region_filter.lower():
                return False
        if min_anonymity and min_anonymity.lower() != "any":
            want = _ANON_RANK.get(min_anonymity.lower())
            have = _ANON_RANK.get((result.anonymity or "").lower(), -1)
            if want is not None and have < want:
                return False
        if max_latency_ms is not None:
            if result.tcp_latency_ms is None or result.tcp_latency_ms > max_latency_ms:
                return False
        return True

    # Protocol filter — trim BEFORE checking starts so filtered-out
    # proxies never cost a network round trip at all
    if protocol_filter and protocol_filter.lower() != "any":
        proxies = [p for p in proxies if p.protocol.lower() == protocol_filter.lower()]

    my_ip = get_my_ip()
    total = len(proxies)
    completed = 0
    results: list[CheckResult] = []
    stopped = False

    _log.info("Checking %d proxies (%d workers)", total, max_workers)
    t_start = time.monotonic()

    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {pool.submit(check_one, p, my_ip): p for p in proxies}
        pending = set(futures)

        PROGRESS_EVERY = 10   # only publish progress every N completions
        while pending:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break

            # Poll in short slices so a stop request is noticed quickly
            # instead of blocking on the next single completion. This slice
            # also doubles as the batch window for PROXY_RESULT_BATCH below.
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue

            batch: list[CheckResult] = []
            for future in done:
                try:
                    result = future.result()
                except Exception as exc:
                    _log.warning("Proxy check task failed: %s", exc)
                    continue

                completed += 1
                if not _passes_post_filters(result):
                    continue
                results.append(result)
                batch.append(result)

            if batch:
                bus.publish(Events.PROXY_RESULT_BATCH, batch)

            # Throttle progress bar updates — no need to update every single proxy
            if batch and (completed % PROGRESS_EVERY == 0 or completed == total):
                bus.publish(Events.PROXY_CHECK_PROGRESS, {
                    "completed": completed,
                    "total": total,
                    "last_proxy": batch[-1].proxy.address(),
                    "last_tier": batch[-1].tier,
                })
    finally:
        # Cancel anything that hasn't started yet; in-flight checks are left
        # to finish on their own timeouts, but we don't wait around for them.
        pool.shutdown(wait=False, cancel_futures=True)

    elapsed = time.monotonic() - t_start
    ok = sum(1 for r in results if r.tier in ("excellent", "good", "fair"))
    if stopped:
        _log.info(
            "Check stopped by user: %d/%d completed (%d working) in %.1fs",
            completed, total, ok, elapsed,
        )
    else:
        _log.info(
            "Check complete: %d/%d working in %.1fs",
            ok, total, elapsed,
        )

    bus.publish(Events.PROXY_CHECK_DONE, results)
    return results
