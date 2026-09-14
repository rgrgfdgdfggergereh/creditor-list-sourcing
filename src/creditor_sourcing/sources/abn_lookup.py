"""Resolve a company's ABN from its ACN or name.

An ABN identifies a debtor unambiguously, which an ACN alone stops doing as
soon as a company trades under a business name, and it is the key most
Australian systems are searched by. This pipeline holds names and ACNs, so
something has to bridge the two, and ABN Lookup (abr.business.gov.au) is the
public register that does it.

Why the title and not the table
-------------------------------
A search on an ACN redirects to the matching record and the page title reads
"Current details for ABN 50 683 236 259 | ABN Lookup". That is the one place
the ABN appears in a fixed, unambiguous form. The body puts it in a table whose
header cells are reused - measured live, reading the cell after the "ABN"
header returns "Active from 19 Dec 2024", the status, not the number. So the
title is the primary read and the body is a fallback, not the other way round.

The ABN check digit is verified before anything is returned. A page that does
not carry a real ABN yields None rather than a number that will send a rep to
the wrong debtor.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .http import Client

log = logging.getLogger(__name__)

SEARCH_URL = "https://abr.business.gov.au/Search/ResultsActive"

# "Current details for ABN 50 683 236 259 | ABN Lookup" - verbatim from live.
TITLE_ABN = re.compile(r"ABN\s+((?:\d[\s ]*){11})", re.IGNORECASE)
ANY_ABN = re.compile(r"\b(\d{2}\s?\d{3}\s?\d{3}\s?\d{3})\b")

# The ABN checksum, from the ATO's published algorithm: subtract 1 from the
# first digit, multiply by the positional weights, and the total must divide
# by 89. It is what separates a real ABN from any other eleven digits on the
# page - a phone number, a postcode run, an unrelated registration.
WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)


def digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text or "")


def is_valid_abn(abn: str) -> bool:
    bare = digits(abn)
    if len(bare) != 11:
        return False
    values = [int(ch) for ch in bare]
    values[0] -= 1
    return sum(v * w for v, w in zip(values, WEIGHTS, strict=True)) % 89 == 0


def format_abn(abn: str) -> str:
    """11 digits as ABN Lookup prints them: 50 683 236 259."""
    bare = digits(abn)
    if len(bare) != 11:
        return abn
    return f"{bare[:2]} {bare[2:5]} {bare[5:8]} {bare[8:]}"


def find_abn(html: str) -> str | None:
    """Pull a valid ABN out of an ABN Lookup result page."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""

    match = TITLE_ABN.search(title)
    if match and is_valid_abn(match.group(1)):
        return digits(match.group(1))

    # Fallback: the first checksum-valid ABN in the body. Only reached when
    # the title changes shape, and still refuses anything that is not an ABN.
    for candidate in ANY_ABN.findall(soup.get_text(" ", strip=True)):
        if is_valid_abn(candidate):
            return digits(candidate)
    return None


def search_url(term: str) -> str:
    """The ABN Lookup search a human can click, for an ACN or a company name."""
    from urllib.parse import quote_plus

    return f"{SEARCH_URL}?SearchText={quote_plus(term)}"


def resolve(acn_or_name: str, client: Client | None = None) -> str | None:
    """Look up one company's ABN. Returns 11 digits, or None."""
    term = (acn_or_name or "").strip()
    if not term:
        return None
    client = client or Client()
    try:
        html = client.get(search_url(digits(term) or term)).text
    except Exception as exc:  # noqa: BLE001 - one bad company must not stop a run
        log.warning("ABN Lookup failed for %s: %s", term, exc)
        return None
    return find_abn(html)
