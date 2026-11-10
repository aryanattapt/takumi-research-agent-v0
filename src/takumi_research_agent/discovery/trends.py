from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

import httpx

from takumi_research_agent.config import RSSFeedConfig
from takumi_research_agent.domain.models import TopicField
from takumi_research_agent.research.url_policy import PublicSourceUrlPolicy

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ARXIV_API = "https://export.arxiv.org/api/query"
MAX_RSS_BYTES = 2_000_000
RSS_CACHE_SECONDS = 3_600


@dataclass(frozen=True, slots=True)
class TrendCandidate:
    title: str
    url: str
    field: TopicField
    source: str
    source_item_id: str
    observed_at: datetime
    published_at: datetime | None
    raw_metrics: dict[str, Any]
    score: float
    score_details: dict[str, Any]


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _entry_text(entry: ET.Element, names: set[str]) -> str:
    for child in list(entry):
        if _xml_local_name(child.tag) in names:
            value = " ".join("".join(child.itertext()).split())
            if value:
                return value
    return ""


def _entry_link(entry: ET.Element) -> str:
    links = [child for child in list(entry) if _xml_local_name(child.tag) == "link"]
    for child in links:
        relation = str(child.attrib.get("rel", "alternate")).casefold()
        link = str(child.attrib.get("href") or child.text or "").strip()
        if link and relation == "alternate":
            return link
    return ""


def _parse_feed_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_rss_entries(payload: bytes, feed: RSSFeedConfig) -> list[dict[str, Any]]:
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", payload, re.IGNORECASE):
        raise ValueError("RSS feeds with DTD/entity declarations are not accepted")
    root = ET.fromstring(payload)
    entries: list[dict[str, Any]] = []
    for node in root.iter():
        if _xml_local_name(node.tag) not in {"item", "entry"}:
            continue
        title = _entry_text(node, {"title"})
        link = _entry_link(node)
        published = _parse_feed_date(_entry_text(node, {"published", "updated", "pubdate", "date", "issued"}))
        identity = _entry_text(node, {"id", "guid"}) or link
        if not title or not link or published is None:
            continue
        item_url = urljoin(feed.url, link)
        parsed_url = urlsplit(item_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            continue
        item_key = hashlib.sha256(f"{feed.url}\0{identity}".encode("utf-8")).hexdigest()[:32]
        entries.append({"title": title[:1_000], "url": item_url[:2_000], "source_item_id": f"{feed.name}:{item_key}", "published_at": published})
        if len(entries) >= 1_000:
            break
    return entries


def heuristic_score(*, age_hours: float | None, points: float | None, comments: float | None, recent_count: float | None, baseline_count: float | None) -> tuple[float, dict[str, Any]]:
    """Transparent ranking signal; it is not a universal or platform-calibrated hype metric."""
    signals: list[tuple[float, float]] = []
    details: dict[str, Any] = {}
    if age_hours is not None:
        freshness = max(0.0, min(1.0, 1 - age_hours / 72))
        signals.append((freshness, 0.20))
        details["freshness_72h"] = round(freshness, 4)
    if points is not None or comments is not None:
        discussion = max(0.0, min(1.0, math.log1p(max(0.0, points or 0) + 2 * max(0.0, comments or 0)) / math.log(501)))
        signals.append((discussion, 0.35))
        details["discussion_activity"] = round(discussion, 4)
    if recent_count is not None and baseline_count is not None:
        velocity = max(0.0, min(1.0, recent_count / max(recent_count + baseline_count / 2, 1)))
        signals.append((velocity, 0.45))
        details["relative_velocity"] = round(velocity, 4)
    if not signals:
        return 0.0, {"available_signals": 0, "label": "unranked"}
    weight_total = sum(weight for _, weight in signals)
    score = sum(value * weight for value, weight in signals) / weight_total
    details.update({"available_signals": len(signals), "score": round(score, 4), "label": "heuristic_only"})
    return score, details


class TrendCollector:
    """Free public trend signals; network collection is separate from model inference."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15,
        rss_feeds: tuple[RSSFeedConfig, ...] = (),
        allowed_domains: tuple[str, ...] = (),
        feed_url_policy: PublicSourceUrlPolicy | None = None,
    ):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=8),
            follow_redirects=False,
            headers={"User-Agent": "TakumiResearchAgent/0.2 (+trend-discovery)"},
            transport=transport,
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._rss_cache: dict[str, tuple[float, list[dict[str, Any]], str | None, str | None]] = {}
        self.rss_feeds = rss_feeds
        self.feed_url_policy = feed_url_policy or PublicSourceUrlPolicy(allowed_domains)
        self._rss_semaphore = asyncio.Semaphore(4)
        self.last_coverage: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_json_cached(self, url: str, *, ttl: int = 3_600) -> dict[str, Any]:
        async with self._lock:
            cache = self._cache.get(url)
            if cache and cache[0] > time.monotonic():
                return cache[1]
        response = await self.client.get(url)
        response.raise_for_status()
        body = response.json()
        async with self._lock:
            self._cache[url] = (time.monotonic() + ttl, body)
        return body

    async def _hn_search(self, query: str, since: datetime) -> list[dict[str, Any]]:
        bucket_start = int(since.timestamp()) // 3_600 * 3_600
        url = f"{HN_SEARCH}?query={quote_plus(query)}&numericFilters=created_at_i%3E{bucket_start}&hitsPerPage=100"
        body = await self._get_json_cached(url)
        hits = body.get("hits", [])
        return [
            item for item in hits
            if isinstance(item, dict) and int(item.get("created_at_i", 0)) >= int(since.timestamp())
        ]

    async def _arxiv_recent(self, category: str, since: datetime) -> list[dict[str, Any]]:
        bucket_start = int(since.timestamp()) // 3_600 * 3_600
        cache_key = f"arxiv:{category}:{bucket_start}"
        async with self._lock:
            cache = self._cache.get(cache_key)
            if cache and cache[0] > time.monotonic():
                records = cache[1]
            else:
                records = None
        if records is None:
            response = await self.client.get(
                ARXIV_API,
                params={"search_query": f"cat:{category}", "start": 0, "max_results": 50, "sortBy": "submittedDate", "sortOrder": "descending"},
            )
            response.raise_for_status()
            root = ET.fromstring(response.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            records = []
            for entry in root.findall("atom:entry", ns):
                title = re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=ns)).strip()
                link = next((item.attrib.get("href", "") for item in entry.findall("atom:link", ns) if item.attrib.get("rel") == "alternate"), "")
                published_raw = entry.findtext("atom:published", default="", namespaces=ns)
                try:
                    published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                identifier = entry.findtext("atom:id", default="", namespaces=ns)
                records.append({"title": title, "url": link, "source_item_id": identifier, "published_at": published})
            async with self._lock:
                self._cache[cache_key] = (time.monotonic() + 3_600, records)
        return [item for item in records if item["published_at"].astimezone(timezone.utc) >= since]

    async def _rss_recent(self, feed: RSSFeedConfig, since: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            await self.feed_url_policy.validate(feed.url)
            async with self._lock:
                cached = self._rss_cache.get(feed.url)
            headers: dict[str, str] = {}
            if cached:
                if cached[2]:
                    headers["If-None-Match"] = cached[2]
                if cached[3]:
                    headers["If-Modified-Since"] = cached[3]
            async with self.client.stream("GET", feed.url, headers=headers) as response:
                if response.status_code == 304 and cached:
                    records = cached[1]
                    async with self._lock:
                        self._rss_cache[feed.url] = (time.monotonic() + RSS_CACHE_SECONDS, records, cached[2], cached[3])
                    status = "not_modified"
                else:
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_RSS_BYTES:
                        raise ValueError("RSS feed exceeds the 2 MB response limit")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(payload) + len(chunk) > MAX_RSS_BYTES:
                            raise ValueError("RSS feed exceeds the 2 MB response limit")
                        payload.extend(chunk)
                    records = _parse_rss_entries(bytes(payload), feed)
                    etag = response.headers.get("etag")
                    last_modified = response.headers.get("last-modified")
                    async with self._lock:
                        self._rss_cache[feed.url] = (time.monotonic() + RSS_CACHE_SECONDS, records, etag, last_modified)
                    status = "ok"
            recent = [item for item in records if item["published_at"] >= since]
            return recent, {"name": feed.name, "status": status, "items": len(recent)}
        except Exception as exc:
            return [], {"name": feed.name, "status": "failed", "items": 0, "error": type(exc).__name__}

    async def collect(
        self,
        *,
        now: datetime | None = None,
        rss_feeds: tuple[RSSFeedConfig, ...] | None = None,
        coverage_sink: dict[str, Any] | None = None,
    ) -> list[TrendCandidate]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        effective_rss_feeds = self.rss_feeds if rss_feeds is None else rss_feeds
        recent_cutoff = now - timedelta(hours=24)
        baseline_cutoff = now - timedelta(hours=72)
        query_fields: dict[str, tuple[TopicField, ...]] = {
            "AI agents": ("technology",),
            "software engineering": ("technology",),
            "cloud computing": ("technology",),
            "physics": ("physics",),
            "chemistry": ("chemistry",),
            "biology": ("biology",),
        }
        fetched = await asyncio.gather(*(self._hn_search(query, baseline_cutoff) for query in query_fields), return_exceptions=True)
        hn_succeeded = sum(not isinstance(value, BaseException) for value in fetched)
        hn_hits = sum(len(value) for value in fetched if not isinstance(value, BaseException))
        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        query_hit_counts: dict[str, tuple[int, int]] = {}
        for (query, fields), value in zip(query_fields.items(), fetched):
            if isinstance(value, BaseException):
                query_hit_counts[query] = (0, 0)
                continue
            recent_count = sum(int(item.get("created_at_i", 0)) >= int(recent_cutoff.timestamp()) for item in value)
            baseline_count = max(0, len(value) - recent_count)
            query_hit_counts[query] = (recent_count, baseline_count)
            for hit in value:
                title = str(hit.get("title") or hit.get("story_title") or "").strip()
                url = str(hit.get("url") or hit.get("story_url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}")
                if not title or not hit.get("objectID"):
                    continue
                key = ("hackernews", str(hit["objectID"]))
                if key not in candidates:
                    try:
                        published = datetime.fromtimestamp(int(hit["created_at_i"]), timezone.utc)
                    except (KeyError, TypeError, ValueError):
                        published = None
                    candidates[key] = {"title": title, "url": url, "published": published, "fields": set(), "points": 0, "comments": 0, "queries": set()}
                candidates[key]["fields"].update(fields)
                candidates[key]["points"] = max(candidates[key]["points"], float(hit.get("points") or 0))
                candidates[key]["comments"] = max(candidates[key]["comments"], float(hit.get("num_comments") or 0))
                candidates[key]["queries"].add(query)

        arxiv_specs: dict[str, tuple[TopicField, ...]] = {
            "cs.AI": ("technology",),
            "cs.SE": ("technology",),
            "cs.DC": ("technology",),
            "physics": ("physics",),
            "physics.chem-ph": ("chemistry",),
            "q-bio": ("biology",),
        }
        arxiv_results = await asyncio.gather(*(self._arxiv_recent(category, recent_cutoff) for category in arxiv_specs), return_exceptions=True)
        arxiv_succeeded = sum(not isinstance(value, BaseException) for value in arxiv_results)
        arxiv_items = sum(len(value) for value in arxiv_results if not isinstance(value, BaseException))
        for (category, fields), value in zip(arxiv_specs.items(), arxiv_results):
            if isinstance(value, BaseException):
                continue
            for item in value:
                key = ("arxiv", item["source_item_id"])
                candidates[key] = {
                    "title": item["title"],
                    "url": item["url"],
                    "published": item["published_at"],
                    "fields": set(fields),
                    "points": None,
                    "comments": None,
                    "queries": {category},
                }

        async def collect_rss_feed(feed: RSSFeedConfig):
            async with self._rss_semaphore:
                return await self._rss_recent(feed, recent_cutoff)

        rss_results = await asyncio.gather(*(collect_rss_feed(feed) for feed in effective_rss_feeds))
        rss_feed_reports = [report for _, report in rss_results]
        rss_items = sum(len(items) for items, _ in rss_results)
        for feed, (items, _) in zip(effective_rss_feeds, rss_results):
            for item in items:
                key = (f"rss:{feed.name}", item["source_item_id"])
                candidates[key] = {
                    "title": item["title"],
                    "url": item["url"],
                    "published": item["published_at"],
                    "fields": {feed.field},
                    "points": None,
                    "comments": None,
                    "queries": set(),
                }

        results: list[TrendCandidate] = []
        for (source, source_id), item in candidates.items():
            published = item["published"]
            age = (now - published.astimezone(timezone.utc)).total_seconds() / 3600 if published else None
            available_metrics = [query_hit_counts[query] for query in sorted(item["queries"]) if query in query_hit_counts]
            recent_count, baseline_count = (
                max(
                    available_metrics,
                    key=lambda counts: (counts[0] / max(counts[0] + counts[1] / 2, 1), counts[0]),
                )
                if available_metrics
                else (None, None)
            )
            score, details = heuristic_score(
                age_hours=age,
                points=item["points"],
                comments=item["comments"],
                recent_count=recent_count,
                baseline_count=baseline_count,
            )
            for field in item["fields"]:
                results.append(
                    TrendCandidate(
                        title=item["title"],
                        url=item["url"],
                        field=field,
                        source=source,
                        source_item_id=source_id,
                        observed_at=now,
                        published_at=published,
                        raw_metrics={"points": item["points"], "comments": item["comments"], "queries": sorted(item["queries"]), "selected_query_recent_count": recent_count, "selected_query_baseline_count": baseline_count},
                        score=score,
                        score_details=details,
                    )
                )
        results.sort(key=lambda item: (item.score, item.published_at or now), reverse=True)
        rss_succeeded = sum(report["status"] in {"ok", "not_modified"} for report in rss_feed_reports)
        coverage = {
            "window_start": recent_cutoff.isoformat(),
            "baseline_start": baseline_cutoff.isoformat(),
            "observed_at": now.isoformat(),
            "sources": {
                "hackernews": {"configured": len(query_fields), "successful": hn_succeeded, "failed": len(query_fields) - hn_succeeded, "hits": hn_hits},
                "arxiv": {"configured": len(arxiv_specs), "successful": arxiv_succeeded, "failed": len(arxiv_specs) - arxiv_succeeded, "items": arxiv_items},
                "rss": {"configured": len(effective_rss_feeds), "successful": rss_succeeded, "failed": len(effective_rss_feeds) - rss_succeeded, "items": rss_items, "feeds": rss_feed_reports},
            },
            "candidate_count": sum(
                item.published_at is not None
                and recent_cutoff <= item.published_at.astimezone(timezone.utc) <= now + timedelta(minutes=10)
                for item in results
            ),
        }
        self.last_coverage = coverage
        if coverage_sink is not None:
            coverage_sink.clear()
            coverage_sink.update(coverage)
        return results
