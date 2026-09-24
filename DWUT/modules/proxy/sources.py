"""
modules/proxy/sources.py

Proxy fetcher — ported from the original ProxyGenerator.fetch_proxies().

The original's parallel ThreadPoolExecutor fetch across 22 sources is the
one piece of the proxy subsystem that genuinely works.  This module keeps
that logic, cleans up the URL list, adds deduplication, and returns typed
ProxyRecord objects instead of raw strings.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import requests

from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Proxy source URLs
# ──────────────────────────────────────────────────────────────────────────────

_SOURCES: list[dict] = [
    # Format: {url, protocol (hint), notes}
    # HTTP/HTTPS sources
    {"url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",  "protocol": "http"},
    {"url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt","protocol": "socks4"},
    {"url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt","protocol": "socks5"},
    {"url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",   "protocol": "http"},
    {"url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt","protocol": "socks4"},
    {"url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt","protocol": "socks5"},
    {"url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "protocol": "http"},
    {"url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt","protocol": "socks4"},
    {"url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt","protocol": "socks5"},
    {"url": "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",  "protocol": "socks5"},
    {"url": "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt","protocol": "http"},
    {"url": "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt","protocol": "http"},
    {"url": "https://www.proxy-list.download/api/v1/get?type=http",  "protocol": "http"},
    {"url": "https://www.proxy-list.download/api/v1/get?type=socks4","protocol": "socks4"},
    {"url": "https://www.proxy-list.download/api/v1/get?type=socks5","protocol": "socks5"},
    {"url": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http",   "protocol": "http"},
    {"url": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4", "protocol": "socks4"},
    {"url": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5", "protocol": "socks5"},
    {"url": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt","protocol": "http"},
    {"url": "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt","protocol": "http"},
    {"url": "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt", "protocol": "http"},
    {"url": "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt","protocol": "socks5"},
]

# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ProxyRecord:
    ip: str
    port: int
    protocol: str          # "http" | "socks4" | "socks5"
    credentials: Optional[str] = None  # "user:pass" if present

    # Filled in by checker
    status: str = "unchecked"    # "unchecked"|"checking"|"ok"|"dead"|"rejected"
    latency_ms: Optional[float] = None
    country: Optional[str] = None
    country_code: Optional[str] = None
    isp: Optional[str] = None
    asn: Optional[str] = None
    anonymity: Optional[str] = None   # "elite"|"anonymous"|"transparent"
    exit_ip: Optional[str] = None
    is_pooled: Optional[bool] = None
    score: Optional[int] = None

    def address(self) -> str:
        return f"{self.ip}:{self.port}"

    def uri(self) -> str:
        if self.credentials:
            return f"{self.protocol}://{self.credentials}@{self.ip}:{self.port}"
        return f"{self.protocol}://{self.ip}:{self.port}"

    def __hash__(self):
        return hash((self.ip, self.port, self.protocol))

    def __eq__(self, other):
        if not isinstance(other, ProxyRecord):
            return False
        return self.ip == other.ip and self.port == other.port and self.protocol == other.protocol


# ──────────────────────────────────────────────────────────────────────────────
# Parsers
# ──────────────────────────────────────────────────────────────────────────────

_IP_PORT_RE = re.compile(
    r"(?:(?P<proto>https?|socks[45])://)?(?:(?P<creds>[^@\s]+)@)?"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d{2,5})"
)


def parse_proxy_line(line: str, default_protocol: str = "http") -> Optional[ProxyRecord]:
    """
    Parse a proxy from any common format:
      - "1.2.3.4:8080"
      - "socks5://1.2.3.4:1080"
      - "user:pass@1.2.3.4:3128"
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    m = _IP_PORT_RE.search(line)
    if not m:
        return None

    ip   = m.group("ip")
    port = int(m.group("port"))
    if port < 1 or port > 65535:
        return None

    # Validate IP octets
    octets = [int(o) for o in ip.split(".")]
    if any(o > 255 for o in octets):
        return None

    proto = m.group("proto") or default_protocol
    proto = proto.lower().replace("https", "http")

    creds = m.group("creds") or None

    return ProxyRecord(ip=ip, port=port, protocol=proto, credentials=creds)


def parse_proxy_text(text: str, default_protocol: str = "http") -> list[ProxyRecord]:
    """Parse all valid proxies from a newline-separated text block."""
    results = []
    for line in text.splitlines():
        rec = parse_proxy_line(line, default_protocol)
        if rec:
            results.append(rec)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Fetcher
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_one(source: dict, timeout: int = 10) -> tuple[str, list[ProxyRecord]]:
    """Fetch and parse one source URL.  Returns (url, records)."""
    url  = source["url"]
    proto = source.get("protocol", "http")
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        records = parse_proxy_text(resp.text, default_protocol=proto)
        return url, records
    except Exception as exc:
        _log.debug("Source fetch failed [%s]: %s", url, exc)
        return url, []


def fetch_all_sources(
    max_workers: int = 20,
    timeout_s: int = 10,
    sources: Optional[list[dict]] = None,
) -> list[ProxyRecord]:
    """
    Fetch proxies from all sources in parallel.

    Publishes Events.PROXY_FETCH_PROGRESS after each source completes.
    Publishes Events.PROXY_FETCH_DONE with the final deduplicated list.

    Returns the deduplicated list of ProxyRecord objects.
    """
    source_list = sources or _SOURCES
    total = len(source_list)
    completed = 0
    all_records: list[ProxyRecord] = []
    seen: set[tuple] = set()

    _log.info("Fetching proxies from %d sources (%d workers)", total, max_workers)
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, src, timeout_s): src for src in source_list}

        for future in as_completed(futures):
            url, records = future.result()
            completed += 1

            # Deduplicate by (ip, port, protocol)
            new_count = 0
            for rec in records:
                key = (rec.ip, rec.port, rec.protocol)
                if key not in seen:
                    seen.add(key)
                    all_records.append(rec)
                    new_count += 1

            bus.publish(Events.PROXY_FETCH_PROGRESS, {
                "completed": completed,
                "total": total,
                "url": url,
                "new_proxies": new_count,
                "running_total": len(all_records),
            })

    elapsed = time.monotonic() - t_start
    _log.info(
        "Fetch complete: %d unique proxies from %d sources in %.1fs",
        len(all_records), total, elapsed,
    )

    bus.publish(Events.PROXY_FETCH_DONE, all_records)
    return all_records
