"""Policy Bundle discovery: companion legal documents on the service's own domain, public addresses only."""

import httpx
import pytest
import respx

from app import discover
from app.netguard import BlockedURL

TERMS = "https://www.acme.com/legal/terms"

TERMS_PAGE = """
<html><body><main><h1>Terms of Service</h1><p>These terms apply.</p></main>
<footer>
  <a href="/legal/terms">Terms of Service</a>
  <a href="/legal/privacy-policy?ref=footer#top">Privacy Policy</a>
  <a href="https://docs.acme.com/ai/supplementary-terms">AI Supplementary Terms</a>
  <a href="/legal/dpa">Data Processing Addendum</a>
  <a href="/billing-terms">Billing</a>
  <a href="/legal/cookies">Cookie Policy</a>
  <a href="https://acme-legal.evil.com/privacy">Privacy Policy (mirror)</a>
  <a href="https://acme.github.io/privacy-policy">Privacy notice</a>
  <a href="mailto:legal@acme.com">Email legal</a>
</footer></body></html>
"""


@pytest.fixture
def public(monkeypatch):
    """Every host resolves to a public address, except the ones listed as private."""
    private: set[str] = set()

    async def resolve(host):
        if host in private or host in ("localhost", "127.0.0.1"):
            raise BlockedURL(f"{host} resolves to a non-public address")
        return ["93.184.216.34"]

    monkeypatch.setattr(discover, "resolve_public", resolve)
    return private


@respx.mock
async def test_companions_on_the_services_own_domain_are_found(public):
    respx.get(TERMS).mock(return_value=httpx.Response(200, text=TERMS_PAGE, headers={"content-type": "text/html"}))
    found = await discover.discover_companions(TERMS)
    assert found == {
        "privacy": "https://www.acme.com/legal/privacy-policy",       # query and fragment dropped
        "ai_terms": "https://docs.acme.com/ai/supplementary-terms",   # a subdomain of the same registrable domain
        "billing": "https://www.acme.com/billing-terms",
        "dpa": "https://www.acme.com/legal/dpa",
    }
    # Off-domain look-alikes, user content, cookie notices and the terms page itself are never companions.
    assert not any("evil" in u or "github.io" in u or "cookie" in u for u in found.values())


@respx.mock
async def test_companion_on_a_private_address_is_skipped_and_never_fetched(public):
    page = TERMS_PAGE.replace("https://docs.acme.com/ai/supplementary-terms", "https://intranet.acme.com/ai-terms")
    respx.get(TERMS).mock(return_value=httpx.Response(200, text=page, headers={"content-type": "text/html"}))
    internal = respx.get("https://intranet.acme.com/ai-terms").mock(return_value=httpx.Response(200, text="secret"))
    public.add("intranet.acme.com")
    found = await discover.discover_companions(TERMS)
    assert "ai_terms" not in found and "privacy" in found
    assert not internal.called


@pytest.mark.parametrize("url", ["https://127.0.0.1/privacy", "https://10.0.0.8/privacy", "https://localhost/privacy",
                                 "https://[::1]/privacy"])
async def test_loopback_and_private_links_fail_the_public_address_check(url):
    # The real netguard resolver: IP literals and localhost resolve without the network.
    assert await discover._is_public(url) is False


@respx.mock
async def test_legal_hub_linked_from_the_terms_page_is_read_too(public):
    terms_page = '<html><body><p>Terms.</p><a href="/legal">Legal</a></body></html>'
    hub = '<html><body><a href="/legal/privacy">Privacy Notice</a><a href="/legal/ai-policy">AI policy</a></body></html>'
    respx.get(TERMS).mock(return_value=httpx.Response(200, text=terms_page, headers={"content-type": "text/html"}))
    respx.get("https://www.acme.com/legal").mock(return_value=httpx.Response(200, text=hub, headers={"content-type": "text/html"}))
    found = await discover.discover_companions(TERMS)
    assert found == {"privacy": "https://www.acme.com/legal/privacy", "ai_terms": "https://www.acme.com/legal/ai-policy"}


@respx.mock
async def test_terms_page_redirected_off_domain_yields_no_companions(public):
    respx.get(TERMS).mock(return_value=httpx.Response(302, headers={"Location": "https://other.test/terms"}))
    respx.get("https://other.test/terms").mock(return_value=httpx.Response(200, text=TERMS_PAGE))
    assert await discover.discover_companions(TERMS) == {}


@respx.mock
async def test_bundle_with_only_a_terms_page(public):
    respx.get(TERMS).mock(return_value=httpx.Response(200, text="<p>Terms only.</p>", headers={"content-type": "text/html"}))
    assert await discover.discover_bundle(TERMS) == {"terms": TERMS}


@respx.mock
async def test_bundle_survives_an_unreachable_terms_page(public):
    respx.get(TERMS).mock(side_effect=httpx.ConnectError("down"))
    assert await discover.discover_bundle(TERMS) == {"terms": TERMS}


async def test_bundle_from_a_name_uses_the_terms_finder(monkeypatch, public):
    async def find(query, cl=None, client=None):
        return None

    monkeypatch.setattr(discover, "find_terms_url", find)
    assert await discover.discover_bundle("nobody") is None
