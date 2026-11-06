from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import bleach
import markdown


ALLOWED_TAGS = {
    "p", "br", "strong", "em", "b", "i", "code", "pre", "blockquote", "ul", "ol", "li",
    "h2", "h3", "h4", "a", "table", "thead", "tbody", "tr", "th", "td", "hr",
}
ALLOWED_ATTRIBUTES = {"a": ["href", "title", "rel"], "th": ["align"], "td": ["align"]}
ALLOWED_PROTOCOLS = {"http", "https", "mailto"}


class _LinkTargets(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def render_final_body(body_markdown: str, *, output_format: str) -> str:
    """Render and sanitize the single body used by preview, review, and delivery."""
    if output_format == "markdown":
        if re.search(r"!\[[^\]]*\]\(", body_markdown):
            raise ValueError("Inline images are disabled in article bodies")
        escaped_html = re.sub(r"</?[A-Za-z][^>]*>", lambda match: html.escape(match.group(0)), body_markdown)
        rendered = markdown.markdown(escaped_html, extensions=["tables", "fenced_code"])
        parser = _LinkTargets()
        parser.feed(rendered)
        for target in parser.hrefs:
            parsed = urlsplit(html.unescape(target).strip())
            if parsed.scheme and parsed.scheme.lower() not in ALLOWED_PROTOCOLS:
                raise ValueError("Article contains a link protocol that is not allowed")
            if target.startswith("//"):
                raise ValueError("Protocol-relative article links are disabled")
        return escaped_html
    if output_format not in {"html", "plain_text"}:
        raise ValueError("output_format must be markdown, html, or plain_text")
    rendered = markdown.markdown(body_markdown, extensions=["tables", "fenced_code"])
    sanitized = bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    if output_format == "plain_text":
        return bleach.clean(sanitized, tags=set(), strip=True)
    return bleach.linkify(sanitized, callbacks=[], skip_tags={"pre", "code"})


def body_fingerprint(final_body: str) -> str:
    return hashlib.sha256(final_body.encode("utf-8")).hexdigest()
