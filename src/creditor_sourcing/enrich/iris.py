"""Links into IRIS, NCI's own debtor and limit system.

What IRIS can and cannot give this pipeline
-------------------------------------------
Checking whether NCI already had limit activity on an insolvent debtor would
narrow down which Form 5604s are worth paying for. It cannot be automated from
here, and the URL structure is why.

IRIS is a single-page app. Everything after the "#" is a client-side route and
is never sent to the server, so it is in-app navigation state, not an address:

    .../index.html#oDashboard/oSelectDebtor/oSelectDebtorSearch-1644824
    .../index.html#oDashboard/oSelectDebtor/oSelectDebtorSearch-1644824/oZoomDebtor1-1122

The second is the first after zooming into a debtor. The search segment,
oSelectDebtorSearch-1644824, is byte-identical in both - it was already there
before any debtor was chosen - so that number is a screen instance, not a
company. Nothing in either URL carries an ABN, an ACN or a company name.

So a per-debtor deep link cannot be constructed. Substituting a number would
open whichever record that instance happens to resolve to in the viewer's own
session, which is worse than no link at all: a rep would read one company's
limit history under another company's name.

What is left is the search screen plus the ABN to paste, which is what this
module builds. A real IRIS signal has to come from an export - the runner
cannot reach IRIS either, which returns HTTP 403 to it.
"""

from __future__ import annotations

BASE = "https://iris.nci.com.au/index.html"
# The stable prefix: the debtor search screen, with no instance id.
SEARCH_ROUTE = f"{BASE}#oDashboard/oSelectDebtor"


def debtor_search_url() -> str:
    """The IRIS debtor search screen. The ABN is pasted in by hand."""
    return SEARCH_ROUTE
