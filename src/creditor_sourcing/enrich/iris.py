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

Why no runner can reach IRIS, ever
----------------------------------
IRIS sits behind Cloudflare with a network rule, and the rule says so in
plain words. Measured from a GitHub Actions runner, with no credentials sent:

    HTTP 403, 51 bytes, server: cloudflare, cf-ray: ...-DFW
    body: "Please connect to the NCI VPN before accessing Iris"

Byte-identical for the project's user agent and a browser's, so this is about
where the request comes from, not who is asking. The block is at Cloudflare,
before IRIS sees anything, which rules out the obvious workarounds: a session
cookie cannot help a request that never arrives, and neither can driving a
headless browser in CI. This session's own container is refused a connection
to iris.nci.com.au outright by its egress proxy, one layer earlier again.

So there are exactly three ways an IRIS signal can reach this pipeline:

  1. An export. Debtor ABN plus limit activity, dropped in periodically, the
     same shape as the PolicyList input. No credentials in CI, nothing to
     scrape, survives any IRIS UI change. The recommended route.
  2. A self-hosted GitHub Actions runner on an NCI machine inside the VPN.
     The weekly job would then do the lookups itself.
  3. A person, or an agent running in a person's own browser on a machine
     already on the VPN, reading the screen. Not a scheduled pipeline step -
     it only happens when someone is at that machine.

What is left for the workbook is the search screen plus the ABN to paste,
which is what this module builds.
"""

from __future__ import annotations

BASE = "https://iris.nci.com.au/index.html"
# The stable prefix: the debtor search screen, with no instance id.
SEARCH_ROUTE = f"{BASE}#oDashboard/oSelectDebtor"


def debtor_search_url() -> str:
    """The IRIS debtor search screen. The ABN is pasted in by hand."""
    return SEARCH_ROUTE
