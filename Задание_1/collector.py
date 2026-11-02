from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, quote_plus
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

SOURCES = {
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "The Record": "https://therecord.media/feed",
    "Securelist": "https://securelist.com/feed/",
}
MAX_FEED_BYTES = 4_000_000
DEFAULT_DB = Path(__file__).parent / "data" / "articles.sqlite3"

SEARCH_COMPANIES = ["Microsoft", "Cisco", "Cloudflare", "GitLab", "Okta", "VK", "ConnectWise"]

def search_sources():
    result = {}
    for company in SEARCH_COMPANIES:
        query = quote_plus(f'"{company}" cyberattack when:90d')
        result[f'GoogleNews: {company}'] = (
            'https://news.google.com/rss/search?q=' + query + '&hl=en-US&gl=US&ceid=US:en'
        )
    return result

class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

def clean_text(raw: str) -> str:
    parser = _HTMLText()
    parser.feed(raw)
    text = unescape(" ".join(parser.parts))
    return re.sub(r"\s+", " ", text).strip()

def tag_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()

def first_child(element: ET.Element, *names: str) -> ET.Element | None:
    wanted = set(names)
    for child in element:
        if tag_name(child) in wanted:
            return child
    return None

def child_text(element: ET.Element, *names: str) -> str:
    child = first_child(element, *names)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()

def publication_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        value = parsedate_to_datetime(raw)
    except (ValueError, TypeError, IndexError):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

def is_public_url(raw: str) -> bool:
    parts = urlsplit(raw)
    return parts.scheme in ("https", "http") and bool(parts.netloc)

def parse_feed(xml_bytes: bytes, source: str) -> list[dict[str, str]]:

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Источник {source}: ответ не похож на корректный XML") from exc

    kind = tag_name(root)
    if kind in ("rss", "rdf"):
        channel = first_child(root, "channel")
        if channel is None:
            channel = root
        entries = [elem for elem in channel if tag_name(elem) == "item"]
        is_atom = False
    elif kind == "feed":
        entries = [elem for elem in root if tag_name(elem) == "entry"]
        is_atom = True
    else:
        raise ValueError(f"Источник {source}: неподдерживаемый формат ленты: {kind}")

    articles: list[dict[str, str]] = []
    for entry in entries:
        title = clean_text(child_text(entry, "title"))
        if is_atom:
            link = ""
            for candidate in entry:
                if tag_name(candidate) == "link" and candidate.attrib.get("rel", "alternate") == "alternate":
                    link = candidate.attrib.get("href", "")
                    break
            if not link:
                link = child_text(entry, "id")
            summary = child_text(entry, "summary", "content")
            published = child_text(entry, "published", "updated")
        else:
            link = child_text(entry, "link")
            summary = child_text(entry, "description", "summary")
            published = child_text(entry, "pubdate", "date")

        if not title or not is_public_url(link):
            continue
        articles.append({
            "source": source,
            "title": title,
            "url": link.strip(),
            "summary": clean_text(summary)[:3000],
            "published_at": publication_date(published),
        })
    return articles

def download_feed(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "CyberScope-Student-Research/0.1 RSS reader",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urlopen(request, timeout=15) as response:
        content = response.read(MAX_FEED_BYTES + 1)
    if len(content) > MAX_FEED_BYTES:
        raise ValueError("RSS-лента слишком большая для безопасной обработки")
    return content

def save_articles(connection: sqlite3.Connection, articles: list[dict[str, str]]) -> int:
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    before = connection.total_changes
    with connection:
        connection.executemany(
            """INSERT OR IGNORE INTO articles
               (source, title, url, summary, published_at, collected_at)
               VALUES (:source, :title, :url, :summary, :published_at, :collected_at)""",
            [dict(article, collected_at=collected_at) for article in articles],
        )
    return connection.total_changes - before

def connect_db(db_file: Path) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            summary TEXT NOT NULL,
            published_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            UNIQUE(source, url)
        )
    """)
    connection.commit()
    return connection

def run(db_file: Path, search: bool = False) -> int:
    conn = connect_db(db_file)
    ok = 0
    feeds = list(SOURCES.items())
    if search:
        feeds.extend(search_sources().items())

    try:
        for source, url in feeds:
            try:
                data = download_feed(url)
                articles = parse_feed(data, source)
                inserted = save_articles(conn, articles)
                ok += 1
                print(f"[OK] {source}: получено {len(articles)}, новых {inserted}")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                print(f"[ОШИБКА] {source}: {error}")

        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"\nВсего публикаций в базе: {count}")
        print(f"База данных: {db_file.resolve()}")
        print("Последние добавленные записи:")
        for source, title in conn.execute(
            "SELECT source, title FROM articles ORDER BY id DESC LIMIT 5"
        ):
            print(f"  [{source}] {title[:100]}")
        return 0 if ok else 1
    finally:
        conn.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="Пассивный сбор открытых RSS-публикаций")
    parser.add_argument("--search", action="store_true", help="Дополнительно искать новости о компаниях через Google News RSS")
    parser.add_argument("--db", type=Path, default=None, help="Путь к базе SQLite")
    args = parser.parse_args()
    db_file = args.db or DEFAULT_DB
    raise SystemExit(run(db_file, args.search))

if __name__ == "__main__":
    main()
