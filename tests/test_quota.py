"""Per-member daily quotas for heavy worker actions."""

from datetime import date
from types import SimpleNamespace

import pytest

from app import agent, db, quota, users


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "quota.db")


def test_a_quota_allows_its_limit_then_refuses_until_the_next_day(conn):
    day = date(2026, 11, 27)
    assert [quota.take(conn, "scout", 7, 2, today=day) for _ in range(3)] == [True, True, False]
    assert quota.take(conn, "scout", 8, 2, today=day)  # another member has their own allowance
    assert quota.take(conn, "redline", 7, 2, today=day)  # and so does another action
    assert quota.take(conn, "scout", 7, 2, today=date(2026, 11, 28))  # a new day starts afresh


async def test_a_member_over_the_scout_allowance_gets_no_crawl(conn, monkeypatch):
    member = users.join_member(conn, "join-code", chat_id=31, name="Cy")
    crawls = []

    async def fetch(url):
        return "text/html", "<p>bot wall</p>"  # too little text: the Scout would be next

    async def scout(cl, url, public=None, links=None):
        crawls.append(url)
        return ""

    async def card(*a, **kw):
        return agent.Outgoing(text="card")

    monkeypatch.setattr(agent, "fetch", fetch)
    monkeypatch.setattr(agent, "scout_text", scout)
    monkeypatch.setattr(agent, "verdict_card", card)
    monkeypatch.setattr(quota, "SCOUT_CHECKS_PER_DAY", 1)
    cl = SimpleNamespace(exa=None, llama=None)
    for _ in range(3):
        try:
            await agent.check_url(cl, "https://acme.test/terms", conn=conn, user=member)
        except RuntimeError:
            pass
    assert len(crawls) == 1
