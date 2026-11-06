from __future__ import annotations

import asyncio
import math
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

import httpx

from takumi_research_agent.domain.models import TopicField

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ARXIV_API = "https://export.arxiv.org/api/query"


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

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 15):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=8),
            follow_redirects=False,
            headers={"User-Agent": "TakumiResearchAgent/0.2 (+trend-discovery)"},
            transport=transport,
        )
        self._cache: dict[str, tuple[float, Any]] = {}
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

    async def collect(self, *, now: datetime | None = None) -> list[TrendCandidate]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
        return results
