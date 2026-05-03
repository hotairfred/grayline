"""
QRZ XML API Client

Looks up amateur radio callsigns via the QRZ.com XML API to retrieve
grid squares.  Results are cached to disk (JSON) to minimize API calls.

Requires a QRZ XML Logbook Data subscription.
"""

import asyncio
import json
import logging
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import quote

log = logging.getLogger(__name__)

_QRZ_URL = 'https://xmldata.qrz.com/xml/current/'
_NOT_FOUND = ''     # cached: QRZ confirmed no grid
_LOOKUP_FAILED = None  # not cached: transient error, retry later


class QRZLookup:
    """Async QRZ XML API client with disk-backed grid cache."""

    def __init__(self, username: str, password: str,
                 cache_file: str = 'qrz_cache.json'):
        self.username = username
        self.password = password
        self.cache_file = cache_file
        self._session_key = None
        self._cache = {}
        self._sem = asyncio.Semaphore(1)  # serialize lookups
        self._last_lookup = 0.0  # timestamp of last API call
        self._min_interval = 2.0  # seconds between API calls
        self._load_cache()

    # ------------------------------------------------------------------ #
    #  Cache                                                               #
    # ------------------------------------------------------------------ #

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file) as f:
                    self._cache = json.load(f)
                log.info("[QRZ] Loaded %d cached grids from %s",
                         len(self._cache), self.cache_file)
            except Exception as e:
                log.warning("[QRZ] Could not load cache: %s", e)
                self._cache = {}

    def _save_cache(self):
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(self._cache, f, indent=1, sort_keys=True)
        except Exception as e:
            log.warning("[QRZ] Could not save cache: %s", e)

    def update_cache(self, callsign: str, grid: str):
        """Update cache with a cluster-provided grid (authoritative)."""
        call = callsign.upper()
        if grid and self._cache.get(call) != grid:
            self._cache[call] = grid
            self._save_cache()
            log.debug("[QRZ] Cache updated from cluster: %s -> %s", call, grid)

    # ------------------------------------------------------------------ #
    #  QRZ XML API (synchronous, run via asyncio.to_thread)                #
    # ------------------------------------------------------------------ #

    def _parse_xml(self, text: str) -> ET.Element:
        """Parse QRZ XML, stripping the namespace for easier access."""
        return ET.fromstring(
            text.replace(' xmlns="http://xmldata.qrz.com"', ''))

    def _login(self):
        """Obtain a session key from QRZ."""
        url = (f'{_QRZ_URL}?username={quote(self.username)}'
               f';password={quote(self.password)};agent=gtbridge')
        try:
            resp = urllib.request.urlopen(url, timeout=10)
            root = self._parse_xml(resp.read().decode())
            err = root.findtext('.//Session/Error')
            if err:
                log.error("[QRZ] Login failed: %s", err)
                self._session_key = None
                return
            key = root.findtext('.//Session/Key')
            if key:
                self._session_key = key
                log.info("[QRZ] Logged in (session key obtained)")
            else:
                log.error("[QRZ] Login response missing session key")
        except Exception as e:
            log.error("[QRZ] Login error: %s", e)
            self._session_key = None

    def _fetch_grid(self, callsign: str) -> Optional[str]:
        """Blocking QRZ lookup.

        Returns:
            grid string  — found in QRZ
            ''           — QRZ confirmed call exists but has no grid (cache it)
            None         — transient failure, do NOT cache (retry later)
        """
        if not self._session_key:
            self._login()
        if not self._session_key:
            return _LOOKUP_FAILED  # can't reach QRZ — don't cache

        url = f'{_QRZ_URL}?s={self._session_key};callsign={quote(callsign)}'
        try:
            resp = urllib.request.urlopen(url, timeout=10)
            root = self._parse_xml(resp.read().decode())

            err = root.findtext('.//Session/Error')
            if err:
                if 'session' in err.lower() or 'timeout' in err.lower():
                    log.info("[QRZ] Session expired, re-logging in")
                    self._session_key = None
                    self._login()
                    if self._session_key:
                        return self._fetch_grid(callsign)
                    return _LOOKUP_FAILED  # still can't log in
                elif 'not found' in err.lower():
                    log.debug("[QRZ] %s not found", callsign)
                    return _NOT_FOUND  # definitively not in QRZ — cache it
                else:
                    log.warning("[QRZ] Lookup error for %s: %s", callsign, err)
                    return _LOOKUP_FAILED  # unknown error — don't cache

            grid = root.findtext('.//Callsign/grid')
            if grid:
                log.info("[QRZ] %s -> %s", callsign, grid)
                return grid
            log.debug("[QRZ] %s has no grid in QRZ", callsign)
            return _NOT_FOUND  # call exists but no grid — cache it

        except Exception as e:
            log.warning("[QRZ] Lookup error for %s: %s", callsign, e)
            return _LOOKUP_FAILED  # network error — don't cache

    # ------------------------------------------------------------------ #
    #  Async interface                                                     #
    # ------------------------------------------------------------------ #

    async def lookup_grid(self, callsign: str) -> Optional[str]:
        """Look up grid for *callsign*.  Cache hit is instant; cache miss
        queries QRZ in a background thread (serialized, one at a time).
        """
        call = callsign.upper()

        # Fast path: cache hit
        if call in self._cache:
            cached = self._cache[call]
            return cached if cached else None

        # Slow path: QRZ API (serialized + rate-limited)
        async with self._sem:
            # Re-check after acquiring lock
            if call in self._cache:
                cached = self._cache[call]
                return cached if cached else None

            # Rate limit: wait if we queried too recently
            elapsed = time.monotonic() - self._last_lookup
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)

            grid = await asyncio.to_thread(self._fetch_grid, call)
            self._last_lookup = time.monotonic()
            if grid is not _LOOKUP_FAILED:
                # Cache both grids and confirmed "not found" — but NOT transient failures
                self._cache[call] = grid or _NOT_FOUND
                self._save_cache()
            return grid if grid else None
