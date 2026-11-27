"""The Scout runner's pure helpers, tested without Playwright."""

import base64
import hashlib
import io
import json
import sys

import pytest

from app import swarm
from workers import scout_runner as sr

ALLOW = ["www.notion.so", "notion.so"]


def test_runner_imports_without_playwright():
    assert "playwright" not in sys.modules or sr.crawl  # imported lazily inside crawl()


def test_normalise_text_is_stable():
    raw = "  Terms of   Service \n\n“You” agree’s\n\t\n"
    assert sr.normalise_text(raw) == "Terms of Service\n\"You\" agree's"
    assert sr.normalise_text(sr.normalise_text(raw)) == sr.normalise_text(raw)


def test_digest_is_sha256_of_text():
    assert sr.digest("abc") == hashlib.sha256(b"abc").hexdigest()


def test_policy_links_are_classified_and_kept_on_the_locked_site():
    anchors = [
        {"href": "/privacy", "text": "Privacy Policy"},
        {"href": "/legal/ai-supplementary-terms", "text": "AI Supplementary Terms"},
        {"href": "https://www.notion.so/dpa#top", "text": "Data Processing Addendum"},
        {"href": "https://help.notion.so/billing", "text": "Billing and payment terms"},
        {"href": "https://evil.example/privacy", "text": "Privacy Policy"},
        {"href": "javascript:alert(1)", "text": "Privacy"},
        {"href": "/privacy", "text": "Privacy again"},
        {"href": "/terms", "text": "Terms of Service"},  # the page itself
        {"href": "/blog", "text": "Blog"},
    ]
    links = sr.extract_policy_links(anchors, "https://www.notion.so/terms", ALLOW)
    assert [(link["url"], link["role"]) for link in links] == [
        ("https://www.notion.so/privacy", "privacy"),
        ("https://www.notion.so/legal/ai-supplementary-terms", "ai_terms"),
        ("https://www.notion.so/dpa", "dpa"),
        ("https://help.notion.so/billing", "billing"),
    ]


def test_host_allowed_is_exact_while_link_reporting_accepts_subdomains():
    assert sr.host_allowed("notion.so", ALLOW) and not sr.host_allowed("cdn.notion.so", ALLOW)
    assert not sr.host_allowed("evilnotion.so", ALLOW) and not sr.host_allowed("notion.so.evil.com", ALLOW)
    for host in ("notion.so", "cdn.notion.so", "x.notion.so"):
        assert sr.host_allowed(host, ALLOW) == swarm.host_allowed(host, ALLOW)
    assert sr.on_site("help.notion.so", ALLOW) and not sr.on_site("evilnotion.so", ALLOW)
    assert not sr.on_site("notion.so.evil.com", ALLOW) and not sr.on_site("help.notion.so.", ALLOW)


def test_parse_job_requires_an_explicit_lock():
    good = sr.parse_job(json.dumps({"url": "https://notion.so/terms", "allow_hosts": ALLOW}))
    assert good["screenshot"] is False
    for bad in (
        {"url": "https://notion.so/terms"},
        {"url": "https://notion.so/terms", "allow_hosts": ["*"]},
        {"url": "https://evil.example/", "allow_hosts": ALLOW},
        {"url": "file:///etc/passwd", "allow_hosts": ALLOW},
    ):
        with pytest.raises(ValueError):
            sr.parse_job(json.dumps(bad))


def test_result_round_trips_through_the_gateway_decoder():
    line = sr.encode_result({"ok": True, "sha256": "x"})
    assert swarm.extract_result("noise\n" + line + "\n") == {"ok": True, "sha256": "x"}
    assert line.startswith(swarm.RESULT_START) and line.endswith(swarm.RESULT_END)


def test_main_reports_bad_jobs_as_results(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"url": "https://x.example/"}')))
    assert sr.main() == 0
    result = swarm.extract_result(capsys.readouterr().out)
    assert result["ok"] is False and result["error"].startswith("bad_job")


def test_main_turns_crawl_errors_into_labels(monkeypatch, capsys):
    async def broken(job):
        raise TimeoutError("https://notion.so/secret-path")

    monkeypatch.setattr(sr, "crawl", broken)
    job = json.dumps({"url": "https://notion.so/terms", "allow_hosts": ALLOW}).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(job)))
    sr.main()
    out = capsys.readouterr().out
    assert swarm.extract_result(out) == {"ok": False, "error": "timeout_error"}
    assert "secret-path" not in base64.b64decode(out.split(swarm.RESULT_START)[1].split(swarm.RESULT_END)[0]).decode()


def test_only_exact_hosts_are_allowed_and_dotted_hosts_are_refused():
    allow = ["notion.so", "www.notion.so"]
    assert sr.host_allowed("notion.so", allow) and sr.host_allowed("WWW.notion.so", allow)
    for host in ("cdn.notion.so", "notion.so.", "www.notion.so.", ".notion.so", ""):
        assert not sr.host_allowed(host, allow), host


def test_wildcard_allow_hosts_are_rejected():
    for allow in (["*.notion.so"], ["notion.so", "*.notion.so"], ["*"]):
        with pytest.raises(ValueError):
            sr.parse_job(json.dumps({"url": "https://notion.so/terms", "allow_hosts": allow}))


def test_resolver_rules_pin_each_host_and_refuse_every_other_name():
    rules = sr.resolver_rules({"notion.so": "203.0.113.7", "www.notion.so": "2001:db8::1"})
    assert rules == "MAP notion.so 203.0.113.7, MAP www.notion.so [2001:db8::1], MAP * ~NOTFOUND"
    assert sr.resolver_rules({}) == "MAP * ~NOTFOUND"


async def test_public_addresses_refuse_any_private_answer(monkeypatch):
    answers = {
        "public.test": ["1.1.1.1", "8.8.8.8"],
        "mixed.test": ["1.1.1.1", "10.0.0.5"],  # one inside address is enough to refuse
        "docs.test": ["203.0.113.7"],  # reserved documentation range: not global
        "loop.test": ["127.0.0.1"],
        "dual.test": ["2606:4700::1111", "1.1.1.1"],  # IPv6 answered first
    }

    class Loop:
        async def getaddrinfo(self, host, port, type=0):
            if host not in answers:
                raise OSError("nxdomain")
            return [(0, 0, 0, "", (ip, 0)) for ip in answers[host]]

    monkeypatch.setattr(sr.asyncio, "get_running_loop", lambda: Loop())
    assert await sr.public_addresses("public.test") == ["1.1.1.1", "8.8.8.8"]
    assert await sr.public_addresses("mixed.test") == []
    assert await sr.public_addresses("docs.test") == []
    assert await sr.public_addresses("loop.test") == []
    assert await sr.public_addresses("missing.test") == []
    # Only hosts whose every address is public are pinned; the rest can't be reached at all.
    allow = ["public.test", "mixed.test", "loop.test", "missing.test", "PUBLIC.test"]
    assert await sr.pin_hosts(allow) == {"public.test": "1.1.1.1"}
    # IPv4 is preferred for the pin (worker Sprites may have no IPv6 route); IPv6-only hosts keep IPv6.
    answers["v6only.test"] = ["2606:4700::1111"]
    assert await sr.pin_hosts(["dual.test", "v6only.test"]) == {"dual.test": "1.1.1.1", "v6only.test": "2606:4700::1111"}
