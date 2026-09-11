"""The three records this pipeline moves around."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

_AMPERSAND = re.compile(r"\s*&\s*")
_PUNCT = re.compile(r"[^a-z0-9 ]+")
# Legal suffixes, corporate-family words and the joining "and" all vary freely
# between a PDF, a CRM record and a policy schedule for the same company, so
# none of them may contribute to the comparison key.
_SUFFIXES = re.compile(
    r"\b(pty|ltd|limited|proprietary|inc|incorporated|nl|co|company|"
    r"holdings|group|australia|aust|the|and)\b"
)
_SPACE = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """Fold a company name to a comparison key.

    Creditor names arrive from PDFs, portals and CRMs with different casing,
    punctuation and legal suffixes. Matching on the raw string produces
    duplicates that look like separate prospects but are one company.
    """
    folded = _AMPERSAND.sub(" and ", (name or "").lower())
    folded = _PUNCT.sub(" ", folded)
    folded = _SUFFIXES.sub(" ", folded)
    return _SPACE.sub(" ", folded).strip()


def _key(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass
class Matter:
    """One insolvent company - an external administration we are watching."""

    source: str                      # "asic" | "worrells"
    company_name: str
    acn: str | None = None
    appointment_type: str | None = None
    appointment_date: str | None = None      # ISO date
    practitioner: str | None = None
    practitioner_firm: str | None = None
    source_url: str | None = None
    source_id: str | None = None             # Worrells 32-hex id, ASIC notice id
    # Populated by the ASIC Connect watcher.
    form_5604_lodged: bool = False
    form_5604_date: str | None = None
    form_5604_doc_number: str | None = None
    form_5604_purchased: bool = False
    creditors_captured: bool = False
    first_seen: str | None = None
    last_checked: str | None = None

    @property
    def matter_id(self) -> str:
        return _key(self.source, self.acn or normalise_name(self.company_name))

    def to_dict(self) -> dict[str, Any]:
        return {"matter_id": self.matter_id, **asdict(self)}


@dataclass
class Creditor:
    """One company owed money by one Matter. The raw prospect signal."""

    creditor_name: str
    debtor_company: str                       # who they lost money to
    matter_id: str
    amount_aud: float = 0.0
    address: str | None = None
    related_party: bool = False
    creditor_type: str | None = None
    source: str = "asic"
    source_document: str | None = None

    @property
    def name_key(self) -> str:
        return normalise_name(self.creditor_name)

    def to_dict(self) -> dict[str, Any]:
        return {"name_key": self.name_key, **asdict(self)}


@dataclass
class Prospect:
    """One creditor company aggregated across every matter it appears in."""

    name_key: str
    display_name: str
    total_exposure_aud: float = 0.0
    matter_count: int = 0
    matters: list[dict[str, Any]] = field(default_factory=list)
    # Qualification.
    qualified: bool = True
    disqualified_reason: str | None = None
    # Enrichment.
    abn: str | None = None
    state: str | None = None
    website: str | None = None
    policylist_match: str | None = None
    pipedrive_org_id: int | None = None
    pipedrive_org_name: str | None = None
    pipedrive_owner: str | None = None
    pipedrive_url: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_source: str | None = None
    score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
