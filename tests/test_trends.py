from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from takumi_research_agent.config import RSSFeedConfig, Settings
from takumi_research_agent.discovery.trends import TrendCollector, _parse_rss_entries
from takumi_research_agent.research.url_policy import PublicSourceUrlPolicy


RSS_BODY = b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\"><channel><title>Research lab</title><item>
<title>Measured result announced</title><link>https://lab.example.org/posts/1</link>
<guid>lab-result-1</guid><pubDate>Tue, 10 Nov 2026 10:00:00 GMT</pubDate>
</item></channel></rss>"""


def test_atom_feed_uses_updated_date_and_alternate_link():
    atom = b"""<feed xmlns=\"http://www.w3.org/2005/Atom\"><entry>
    <id>tag:lab.example.org,2026:2</id><title>New observation</title>
    <link rel=\"self\" href=\"https://feeds.example.org/self\"/>
    <link rel=\"alternate\" href=\"/posts/2\"/><updated>2026-11-10T10:00:00Z</updated>
    </entry></feed>"""
    item = _parse_rss_entries(atom, RSSFeedConfig("lab", "https://feeds.example.org/rss", "physics"))[0]
    assert item["title"] == "New observation"
    assert item["url"] == "https://feeds.example.org/posts/2"
    assert item["published_at"] == datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


def test_rss_parser_rejects_entity_declarations():
    payload = b"<!DOCTYPE rss [<!ENTITY payload 'expanded'>]><rss><channel></channel></rss>"
    with pytest.raises(ValueError, match="DTD/entity"):
        _parse_rss_entries(payload, RSSFeedConfig("lab", "https://feeds.example.org/rss", "physics"))


def test_rss_feeds_are_loaded_from_strict_environment_config(monkeypatch):
    monkeypatch.setenv(
        "TREND_RSS_FEEDS",
        json.dumps([{"name": "lab", "url": "https://feeds.example.org/rss.xml", "field": "biology"}]),
    )
    settings = Settings.from_env(env_file=None)
    assert settings.trend_rss_feeds == (RSSFeedConfig("lab", "https://feeds.example.org/rss.xml", "biology"),)

    monkeypatch.setenv("TREND_RSS_FEEDS", json.dumps([{"name": "lab", "url": "http://feeds.example.org/rss.xml", "field": "biology"}]))
    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        Settings.from_env(env_file=None)


@pytest.mark.asyncio
async def test_rss_collector_parses_items_uses_conditional_cache_and_reports_coverage():
    feed = RSSFeedConfig("lab", "https://feeds.example.org/rss.xml", "biology")
    rss_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "feeds.example.org":
            rss_requests.append(request)
            if request.headers.get("If-None-Match") == '"feed-v1"':
                return httpx.Response(304, headers={"ETag": '"feed-v1"'})
            return httpx.Response(200, content=RSS_BODY, headers={"ETag": '"feed-v1"', "Content-Type": "application/rss+xml"})
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": []})
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text="<feed xmlns=\"http://www.w3.org/2005/Atom\"></feed>")
        return httpx.Response(404)

    collector = TrendCollector(
        transport=httpx.MockTransport(handler),
        rss_feeds=(feed,),
        feed_url_policy=PublicSourceUrlPolicy(("feeds.example.org",), resolve_dns=False),
    )
    now = datetime(2026, 11, 10, 10, 30, tzinfo=timezone.utc)
    try:
        candidates = await collector.collect(now=now)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.source == "rss:lab"
        assert candidate.field == "biology"
        assert candidate.url == "https://lab.example.org/posts/1"
        assert candidate.published_at == datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
        assert candidate.score_details["label"] == "heuristic_only"
        assert collector.last_coverage["sources"]["rss"] == {
            "configured": 1,
            "successful": 1,
            "failed": 0,
            "items": 1,
            "feeds": [{"name": "lab", "status": "ok", "items": 1}],
        }

        repeated = await collector.collect(now=now.replace(minute=35))
        assert len(repeated) == 1
        assert rss_requests[1].headers["If-None-Match"] == '"feed-v1"'
        assert collector.last_coverage["sources"]["rss"]["feeds"][0]["status"] == "not_modified"
    finally:
        await collector.close()


@pytest.mark.asyncio
async def test_rss_feed_outside_allowlist_fails_closed_and_is_visible_in_coverage():
    feed = RSSFeedConfig("blocked", "https://internal.example.org/rss.xml", "technology")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": []})
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text="<feed xmlns=\"http://www.w3.org/2005/Atom\"></feed>")
        return httpx.Response(200, content=RSS_BODY)

    collector = TrendCollector(
        transport=httpx.MockTransport(handler),
        rss_feeds=(feed,),
        feed_url_policy=PublicSourceUrlPolicy(("feeds.example.org",), resolve_dns=False),
    )
    try:
        candidates = await collector.collect(now=datetime(2026, 11, 10, 10, 30, tzinfo=timezone.utc))
        rss_status = collector.last_coverage["sources"]["rss"]
        assert candidates == []
        assert rss_status["failed"] == 1
        assert rss_status["feeds"][0]["status"] == "failed"
        assert rss_status["feeds"][0]["error"] == "UnsafeSourceUrl"
        assert not any(request.url.host == "internal.example.org" for request in requests)
    finally:
        await collector.close()
