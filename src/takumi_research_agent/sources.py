from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from takumi_research_agent.store import Store

CROSSREF = "https://api.crossref.org"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DOAJ = "https://doaj.org/api/v4"
DOAJ_POLICY = "https://doaj.org/apply/guide/"


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def clean_html(value: str) -> str:
    parser = _TextOnly()
    parser.feed(value)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def normalize_doi(value: str) -> str:
    doi = value.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.removeprefix("doi:").strip().lower()


def _date_interval(value: str) -> tuple[date, date] | None:
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", value.strip())
    if not match:
        return None
    year, month, day = match.groups()
    try:
        if day:
            parsed = date(int(year), int(month), int(day))
            return parsed, parsed
        if month:
            start = date(int(year), int(month), 1)
            end = date(int(year) + (int(month) == 12), int(month) % 12 + 1, 1)
            from datetime import timedelta

            return start, end - timedelta(days=1)
        return date(int(year), 1, 1), date(int(year), 12, 31)
    except ValueError:
        return None


def _published_date(parts: list[list[int]] | None) -> tuple[str, str]:
    if not parts or not parts[0]:
        return "", ""
    item = parts[0]
    precision = {1: "year", 2: "month", 3: "day"}.get(len(item), "")
    formatted = f"{item[0]:04d}"
    if len(item) > 1:
        formatted += f"-{item[1]:02d}"
    if len(item) > 2:
        formatted += f"-{item[2]:02d}"
    return formatted, precision


def _crossref_record(item: dict[str, Any]) -> dict[str, Any] | None:
    doi = normalize_doi(str(item.get("DOI", "")))
    title = next(iter(item.get("title", [])), "").strip()
    if not doi or not title:
        return None
    published, precision = _published_date(
        item.get("published", {}).get("date-parts")
        or item.get("published-print", {}).get("date-parts")
        or item.get("published-online", {}).get("date-parts")
    )
    authors = [
        " ".join(part for part in (person.get("given", ""), person.get("family", "")) if part)
        for person in item.get("author", [])
    ]
    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "publication_date": published,
        "date_precision": precision,
        "venue": next(iter(item.get("container-title", [])), ""),
        "issns": item.get("ISSN", []),
        "type": item.get("type", ""),
        "url": item.get("URL", f"https://doi.org/{doi}"),
        "abstract": clean_html(item.get("abstract", "")),
        "source": "crossref",
        "review_status": "unknown",
        "review_evidence": [],
        "retracted": any(
            "retract" in f"{key} {value}".lower()
            for key, value in item.get("relation", {}).items()
        ),
        "crossref_updated": item.get("updated", {}).get("date-time", ""),
    }


def _europe_pmc_record(item: dict[str, Any]) -> dict[str, Any] | None:
    doi = normalize_doi(str(item.get("doi", "")))
    title = clean_html(str(item.get("title", "")))
    if not doi or not title:
        return None
    published = item.get("firstPublicationDate", "")
    return {
        "doi": doi,
        "title": title,
        "authors": [item.get("authorString", "")] if item.get("authorString") else [],
        "publication_date": published,
        "date_precision": "day" if len(published) == 10 else ("month" if len(published) == 7 else "year"),
        "venue": item.get("journalTitle", ""),
        "issns": [value for value in (item.get("journalIssn", "") or "").split(",") if value],
        "type": "journal-article"
        if any(
            value.lower().replace("-", " ") in {"journal article", "research article"}
            for value in item.get("pubTypeList", {}).get("pubType", [])
        )
        else "",
        "url": f"https://doi.org/{doi}",
        "abstract": clean_html(str(item.get("abstractText", ""))),
        "source": "europe-pmc",
        "pmcid": item.get("pmcid", ""),
        "pmid": item.get("pmid", ""),
        "open_access": item.get("isOpenAccess") == "Y",
        "publication_types": item.get("pubTypeList", {}).get("pubType", []),
        "review_status": "unknown",
        "review_evidence": [],
        "retracted": bool(item.get("isRetracted", False)),
    }


class LiteratureSources:
    def __init__(self, store: Store, timezone: str = "Asia/Jakarta", transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self.timezone = timezone
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(25, connect=10),
            follow_redirects=True,
            headers={"User-Agent": "TakumiResearchAgent/0.1"},
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.timezone)).date()

    async def search(self, query: str, source: str = "both", rows: int = 10) -> list[dict[str, Any]]:
        terms = query.strip()
        if not terms:
            raise ValueError("Search query cannot be empty")
        if source not in {"crossref", "europe-pmc", "both"}:
            raise ValueError("source must be crossref, europe-pmc, or both")
        found: dict[str, dict[str, Any]] = {}
        if source in {"crossref", "both"}:
            params = {"query.bibliographic": terms, "rows": min(max(rows, 1), 20)}
            response = await self.client.get(f"{CROSSREF}/works", params=params)
            response.raise_for_status()
            for item in response.json().get("message", {}).get("items", []):
                record = _crossref_record(item)
                if record:
                    found[record["doi"]] = record
        if source in {"europe-pmc", "both"}:
            today = self.today()
            query_text = f'TITLE_ABS:({terms}) AND FIRST_PDATE:[{today.year - 5}-01-01 TO {today.isoformat()}] AND NOT SRC:PPR'
            response = await self.client.get(
                f"{EUROPE_PMC}/search",
                params={"query": query_text, "format": "json", "resultType": "core", "pageSize": min(max(rows, 1), 20)},
            )
            response.raise_for_status()
            for item in response.json().get("resultList", {}).get("result", []):
                record = _europe_pmc_record(item)
                if not record:
                    continue
                previous = found.get(record["doi"])
                if previous:
                    for key in ("abstract", "pmcid", "pmid", "open_access", "publication_types"):
                        if record.get(key):
                            previous[key] = record[key]
                    previous["issns"] = list(dict.fromkeys(previous.get("issns", []) + record.get("issns", [])))
                    if not previous.get("publication_date") or len(record.get("publication_date", "")) > len(previous["publication_date"]):
                        previous["publication_date"] = record["publication_date"]
                        previous["date_precision"] = record["date_precision"]
                    previous["source"] = "crossref+europe-pmc"
                    found[record["doi"]] = previous
                else:
                    found[record["doi"]] = record
        results = sorted(found.values(), key=lambda item: (item.get("publication_date", ""), item["title"]), reverse=True)
        for result in results:
            self.store.save_paper(result["doi"], result)
        return results

    async def read(self, doi_value: str) -> dict[str, Any]:
        doi = normalize_doi(doi_value)
        paper = self.store.get_paper(doi)
        if paper is None:
            raise ValueError("Paper was not found in this run. Search for it first.")
        content = paper.get("content", "")
        if not content and paper.get("pmcid") and paper.get("open_access"):
            pmcid = quote(str(paper["pmcid"]), safe="")
            response = await self.client.get(f"{EUROPE_PMC}/{pmcid}/fullTextXML")
            if response.is_success and "xml" in response.headers.get("content-type", "").lower():
                try:
                    root = ET.fromstring(response.text)
                    sections = []
                    for node in root.iter():
                        if node.tag.rsplit("}", 1)[-1] in {"title", "p", "caption"} and node.text:
                            value = re.sub(r"\s+", " ", "".join(node.itertext())).strip()
                            if value:
                                sections.append(value)
                    content = "\n\n".join(sections)[:120_000]
                except ET.ParseError:
                    content = ""
        if not content:
            content = str(paper.get("abstract", ""))
        evidence_id = f"{doi}@{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}"
        paper["evidence_id"] = evidence_id
        paper["content"] = content
        paper["content_source"] = "Europe PMC full text" if paper.get("pmcid") and content != paper.get("abstract") else "abstract metadata"
        self.store.save_paper(doi, paper, content)
        return {key: paper.get(key) for key in ("doi", "title", "venue", "publication_date", "url", "abstract", "content", "content_source", "evidence_id")}

    async def verify(self, doi_value: str) -> dict[str, Any]:
        doi = normalize_doi(doi_value)
        paper = self.store.get_paper(doi)
        if paper is None:
            raise ValueError("Paper was not found in this run. Search for it first.")
        reasons = []
        interval = _date_interval(str(paper.get("publication_date", "")))
        if not interval:
            reasons.append("Publication date is missing or incomplete.")
        else:
            earliest, _ = interval
            today = self.today()
            try:
                cutoff = today.replace(year=today.year - 5)
            except ValueError:
                cutoff = today.replace(year=today.year - 5, day=28)
            if earliest < cutoff:
                reasons.append("The full publication-date range is not within the rolling five-year window.")
        if paper.get("type") != "journal-article":
            reasons.append("Source metadata does not identify this item as a journal article.")
        if paper.get("retracted"):
            reasons.append("Crossref reports a retraction relation.")
        if reasons:
            paper["review_status"] = "rejected"
            paper["review_reason"] = reasons
            self.store.save_paper(doi, paper)
            return {"status": "rejected", "reasons": reasons, "doi": doi}

        review_evidence = []
        try:
            article_url = f"{DOAJ}/search/articles/{quote('doi:' + doi, safe='')}"
            article_response = await self.client.get(article_url)
            article_response.raise_for_status()
            article_data = article_response.json()
            matches = []
            for result in article_data.get("results", []):
                bibjson = result.get("bibjson", {})
                identifiers = bibjson.get("identifier", [])
                article_dois = []
                for value in identifiers:
                    if isinstance(value, dict) and str(value.get("type", "")).lower() == "doi":
                        article_dois.append(normalize_doi(str(value.get("id", ""))))
                    elif isinstance(value, str) and "doi" in value.lower():
                        article_dois.append(normalize_doi(value.split(":", 1)[-1]))
                    elif isinstance(value, str) and normalize_doi(value) == doi:
                        article_dois.append(doi)
                if doi in article_dois:
                    matches.append(result)
            if not matches:
                reasons.append("DOAJ has no exact indexed article record for this DOI; peer review remains unconfirmed.")
            else:
                journal = matches[0].get("bibjson", {}).get("journal", {})
                journal_issns = journal.get("issns") or journal.get("issn") or []
                if isinstance(journal_issns, str):
                    journal_issns = journal_issns.split(",")
                issns = {str(value).replace("-", "").lower() for value in journal_issns}
                if not issns.intersection({str(value).replace("-", "").lower() for value in paper.get("issns", [])}):
                    reasons.append("DOAJ article ISSN does not match the Crossref venue metadata.")
                else:
                    review_evidence = [
                        {
                            "url": article_url,
                            "type": "DOAJ exact article record",
                            "title": matches[0].get("bibjson", {}).get("title", ""),
                        },
                        {"url": DOAJ_POLICY, "type": "DOAJ peer-review selection criteria"},
                    ]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            reasons.append(f"DOAJ verification could not be completed: {type(exc).__name__}.")

        status = "confirmed" if review_evidence and not reasons else "unknown"
        paper["review_status"] = status
        paper["review_evidence"] = review_evidence
        paper["review_reason"] = reasons or ["Exact DOI and venue ISSN matched DOAJ peer-review eligibility records."]
        self.store.save_paper(doi, paper)
        return {"status": status, "doi": doi, "venue": paper.get("venue"), "evidence": review_evidence, "reasons": paper["review_reason"]}
