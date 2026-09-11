"""Shared HTTP session.

Every upstream here is a government or practitioner portal that will rate-limit
or block an impolite client. One session, one identity, one throttle.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "NCI-CreditorSourcing/0.1 (+https://github.com/rgrgfdgdfggergereh/"
    "creditor-list-sourcing; trade credit insurance prospecting)"
)


class Client:
    def __init__(self, throttle_ms: int = 400, timeout: int = 45):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.throttle = throttle_ms / 1000.0
        self.timeout = timeout
        self._last = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.throttle:
            time.sleep(self.throttle - elapsed)
        self._last = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        self._wait()
        log.debug("GET %s", url)
        response = self.session.get(url, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response

    def post(self, url: str, **kwargs) -> requests.Response:
        self._wait()
        log.debug("POST %s", url)
        response = self.session.post(url, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response
