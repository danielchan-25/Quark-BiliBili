from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse


_ARTICLE_ID = re.compile(r"/(?:article|group)/(\d+)")


@dataclass(frozen=True)
class ToutiaoArticle:
    article_id: str
    url: str


def parse_article(url: str) -> ToutiaoArticle:
    """Return a canonical 今日头条 article target or fail before recording it."""
    parsed = urlparse(url)
    if parsed.netloc not in {"toutiao.com", "www.toutiao.com"}:
        raise ValueError("article URL must be a toutiao.com article URL")
    match = _ARTICLE_ID.search(parsed.path)
    if not match:
        raise ValueError("could not find a Toutiao article ID in the URL")
    return ToutiaoArticle(match.group(1), f"https://www.toutiao.com/article/{match.group(1)}/")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
