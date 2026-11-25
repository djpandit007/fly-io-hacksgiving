import json

import httpx
import pytest
import respx

from app import agent, discover, netguard
from app.exa import Result

SEARCH = "https://exa.test/search"
CONTENTS = "https://exa.test/contents"
LONG_TERMS = "## Terms of Service\n" + "You agree to these terms. " * 40


def test_pick_prefers_terms_pages_on_the_domain():
    results = [
        Result(url="https://www.notion.so/blog/terms-update", title="Our new terms"),
        Result(url="https://evil.test/notion-terms", title="Notion terms of service"),
        Result(url="https://www.notion.so/privacy", title="Privacy Policy"),
        Result(url="https://www.notion.so/notion/terms-and-privacy", title="Terms of Service"),
    ]
    assert discover.pick_terms_result(results, "notion.so") == "https://www.notion.so/notion/terms-and-privacy"
    assert discover.pick_terms_result(results[1:2], "notion.so") is None  # off-domain rejected


def test_name_only_results_must_be_on_the_names_own_domain():
    results = [
        Result(url="https://youtube-terms.evil.test/terms", title="YouTube Terms of Service"),
        Result(url="https://someone.github.io/youtube/terms", title="YouTube terms"),
        Result(url="https://www.youtube.com/t/terms", title="Terms of Service - YouTube"),
    ]
    assert discover.pick_terms_result(results, None, "youtube") == "https://www.youtube.com/t/terms"
    assert discover.pick_terms_result(results[:2], None, "youtube") is None
    assert discover.pick_terms_result(results, None, None) is None  # no name, no domain: accept nothing


def test_user_content_hosts_are_rejected_even_on_matching_domain():
    assert discover.is_user_content("https://acme.github.io/terms")
    assert discover.is_user_content("https://medium.com/@x/acme-terms")
    assert not discover.is_user_content("https://docs.github.com/en/site-policy/github-terms")


@respx.mock
async def test_name_only_discovery_uses_search():
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json={"results": [
        {"url": "https://www.notion.com/notion/terms", "title": "Terms and Conditions"}]}))
    cl = agent.Clients()
    url = await discover.find_terms_url("notion", cl)
    await cl.aclose()
    assert url == "https://www.notion.com/notion/terms"
    body = json.loads(route.calls[0].request.content)
    assert body["query"] == "notion terms of service" and "includeDomains" not in body


@respx.mock
async def test_domain_query_restricts_search_and_falls_back_to_scraper(monkeypatch):
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))
    scraped = []

    async def fake_scrape(domain, client=None):
        scraped.append(domain)
        return f"https://{domain}/terms"

    monkeypatch.setattr(discover, "_scrape_terms_url", fake_scrape)
    cl = agent.Clients()
    url = await discover.find_terms_url("acme.io", cl)
    await cl.aclose()
    assert json.loads(route.calls[0].request.content)["includeDomains"] == ["acme.io"]
    assert url == "https://acme.io/terms" and scraped == ["acme.io"]


@respx.mock
async def test_search_failure_falls_back_to_scraper(monkeypatch):
    respx.post(SEARCH).mock(return_value=httpx.Response(400))

    async def fake_scrape(domain, client=None):
        return f"https://{domain}/tos"

    monkeypatch.setattr(discover, "_scrape_terms_url", fake_scrape)
    cl = agent.Clients()
    assert await discover.find_terms_url("notion", cl) == "https://notion.com/tos"
    await cl.aclose()


@respx.mock
async def test_blocked_page_is_read_through_exa_contents(monkeypatch):
    async def refused(url, client=None):
        raise httpx.HTTPStatusError("403", request=httpx.Request("GET", url), response=httpx.Response(403))

    monkeypatch.setattr(agent, "fetch", refused)
    route = respx.post(CONTENTS).mock(return_value=httpx.Response(200, json={"results": [
        {"url": "https://acme.test/terms", "text": LONG_TERMS}]}))
    cl = agent.Clients()
    cl.llama = None
    text = await agent.page_text(cl, "https://acme.test/terms")
    await cl.aclose()
    assert text.startswith("## Terms of Service") and json.loads(route.calls[0].request.content)["urls"] == ["https://acme.test/terms"]


@respx.mock
async def test_js_only_page_uses_exa_contents(monkeypatch):
    async def empty(url, client=None):
        return "text/html", "<html><body><div id=root></div></body></html>"

    monkeypatch.setattr(agent, "fetch", empty)
    respx.post(CONTENTS).mock(return_value=httpx.Response(200, json={"results": [{"url": "u", "text": LONG_TERMS}]}))
    cl = agent.Clients()
    cl.llama = None
    assert "You agree to these terms" in await agent.page_text(cl, "https://acme.test/terms")
    await cl.aclose()


async def test_private_url_is_never_fetched_or_handed_to_fallbacks(monkeypatch):
    # No respx here: it would replace the network layer below the netguard check.
    called = []

    async def fake_contents(gw, url):
        called.append(url)
        return LONG_TERMS

    monkeypatch.setattr(agent.exa, "contents", fake_contents)
    cl = agent.Clients()
    with pytest.raises(netguard.BlockedURL):
        await agent.page_text(cl, "http://169.254.169.254/latest/meta-data/")
    await cl.aclose()
    assert called == []
