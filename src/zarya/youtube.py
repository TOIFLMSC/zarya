"""Bounded public YouTube text evidence; never downloads video or executes scripts."""

import asyncio
import hashlib
import html
import json
import math
import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit

import aiohttp

from zarya.research_fetch import PublicResolver, TextParser, check_target, fetch_error

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
ID = re.compile(r"[A-Za-z0-9_-]{11}")
DESCRIPTION_LIMIT = 12000
TRANSCRIPT_LIMIT = 18000


def is_youtube(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".") in HOSTS
    except ValueError:
        return False


def video_id(url: str) -> str | None:
    try:
        p = urlsplit(check_target(url))
        host = (p.hostname or "").lower().rstrip(".")
        parts = p.path.strip("/").split("/")
        value = ""
        if host == "youtu.be" and len(parts) == 1:
            value = parts[0]
        elif host in HOSTS - {"youtu.be"}:
            if p.path == "/watch":
                value = parse_qs(p.query).get("v", [""])[0]
            elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
                value = parts[1]
        return value if ID.fullmatch(value) else None
    except (ValueError, UnicodeError):
        return None


def embedded(document: str, name: str) -> dict[str, Any]:
    pattern = rf'(?:\b{re.escape(name)}|\["{re.escape(name)}"\])\s*=\s*'
    for match in list(re.finditer(pattern, document))[:3]:
        try:
            data, _ = json.JSONDecoder().raw_decode(document[match.end() :])
            if isinstance(data, dict):
                return data
        except (ValueError, RecursionError):
            pass
    return {}


def plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    return str(
        value.get("simpleText")
        or "".join(str(r.get("text", "")) for r in value.get("runs", []) if isinstance(r, dict))
    )


def nodes(value: Any) -> Any:
    stack = [value]
    for _ in range(30000):
        if not stack:
            return
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def full_description(initial: dict[str, Any]) -> str:
    for item in nodes(initial):
        renderer = item.get("videoDescriptionBodyRenderer", {})
        text = plain(renderer.get("description"))
        if text:
            return text
        attributed = item.get("attributedDescriptionBodyText", {})
        if isinstance(attributed, dict) and isinstance(attributed.get("content"), str):
            return str(attributed["content"])
    return ""


def chapters(description: str, initial: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[int, str] = {}
    for item in nodes(initial):
        if isinstance(item, dict):
            chapter = item.get("chapterRenderer")
            if isinstance(chapter, dict):
                try:
                    seconds = int(chapter["timeRangeStartMillis"]) // 1000
                    title = plain(chapter.get("title"))[:200]
                    if seconds >= 0 and title:
                        found[seconds] = title
                except (ValueError, TypeError, KeyError):
                    pass
            marker = item.get("macroMarkersListItemRenderer", {})
            timestamp = plain(marker.get("timeDescription"))
            title = plain(marker.get("title"))[:200]
            if re.fullmatch(r"(?:\d{1,3}:)?\d{1,2}:\d{2}", timestamp) and title:
                seconds = 0
                for part in timestamp.split(":"):
                    seconds = seconds * 60 + int(part)
                found.setdefault(seconds, title)
    for match in re.finditer(
        r"(?m)^\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{2})\s*[-–—|:]?\s*(\S[^\n]*)", description
    ):
        hours, minutes, seconds = int(match[1] or 0), int(match[2]), int(match[3])
        if minutes < 60 and seconds < 60:
            found.setdefault(hours * 3600 + minutes * 60 + seconds, match[4][:200])
    return [{"seconds": t, "title": found[t]} for t in sorted(found)[:100]]


def caption_url(value: str, identifier: str) -> str:
    p = urlsplit(check_target(value))
    if (
        p.scheme != "https"
        or p.hostname not in {"www.youtube.com", "youtube.com"}
        or p.path != "/api/timedtext"
        or parse_qs(p.query).get("v") != [identifier]
    ):
        raise ValueError("unsafe_caption_url")
    query = [(k, v) for k, v in parse_qsl(p.query) if k != "fmt"] + [("fmt", "json3")]
    return p._replace(query=urlencode(query), fragment="").geturl()


def transcript(document: str) -> tuple[str, bool]:
    data = json.loads(document)
    lines: list[str] = []
    size = 0
    truncated = False
    previous = ""
    for event in data.get("events", []):
        text = html.unescape("".join(str(s.get("utf8", "")) for s in event.get("segs", [])))
        text = " ".join(text.split())
        if not text or text == previous:
            continue
        seconds = float(event.get("tStartMs", 0)) / 1000
        if not math.isfinite(seconds) or seconds < 0:
            continue
        line = f"[{int(seconds // 60):02d}:{int(seconds % 60):02d}] {text}"
        if size + len(line) + 1 > TRANSCRIPT_LIMIT:
            truncated = True
            break
        lines.append(line)
        previous = text
        size += len(line) + 1
    return "\n".join(lines), truncated


class YouTubeFetcher:
    async def get(self, url: str, *, limit: int = 4_000_000) -> str:
        current = check_target(url)
        caption_id = (
            parse_qs(urlsplit(current).query).get("v", [""])[0]
            if urlsplit(current).path == "/api/timedtext"
            else None
        )
        oembed_id = (
            video_id(parse_qs(urlsplit(current).query).get("url", [""])[0])
            if urlsplit(current).path == "/oembed"
            else None
        )
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False, limit=1)
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=10),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Zarya/0.1; text research)",
                "Accept": "text/html,application/json,text/plain",
                "Accept-Encoding": "identity",
            },
        ) as client:
            for attempt in range(4):
                p = urlsplit(check_target(current))
                if p.hostname not in HOSTS:
                    raise ValueError("youtube_redirect_restricted")
                if caption_id is not None:
                    current = caption_url(current, caption_id)
                if oembed_id is not None and (
                    p.scheme != "https"
                    or p.path != "/oembed"
                    or video_id(parse_qs(p.query).get("url", [""])[0]) != oembed_id
                ):
                    raise ValueError("youtube_identity_mismatch")
                async with client.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if attempt == 3 or not response.headers.get("Location"):
                            raise ValueError("redirect_limit")
                        current = check_target(urljoin(current, response.headers["Location"]))
                        continue
                    if response.status != 200:
                        raise ValueError(f"http_{response.status}")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ValueError("unsupported_compression")
                    kind = response.headers.get("Content-Type", "").split(";")[0].lower()
                    if kind not in {
                        "text/html",
                        "application/json",
                        "text/plain",
                        "application/xml",
                        "text/xml",
                    }:
                        raise ValueError("unsupported_content")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ValueError("size_limit")
                    return bytes(data).decode(response.charset or "utf-8", "replace")
        raise ValueError("redirect_limit")

    async def fetch(self, url: str) -> dict[str, Any]:
        identifier = video_id(url)
        source: dict[str, Any] = {
            "requested_url": url,
            "url": None,
            "title": "",
            "text": "",
            "coverage": "unavailable",
            "error": None,
        }
        if not identifier:
            source["error"] = "youtube_invalid_url"
            return source
        canonical = f"https://www.youtube.com/watch?v={identifier}"
        source["url"] = canonical
        info: dict[str, Any] = {
            "video_id": identifier,
            "channel": "",
            "duration_seconds": None,
            "description": "",
            "description_truncated": False,
            "chapters": [],
            "transcript_status": "unavailable",
            "transcript_language": None,
            "transcript_automatic": False,
            "transcript_truncated": False,
            "video_watched": False,
            "diagnostics": [],
            "fallback_reason": None,
        }
        source["youtube"] = info
        try:
            async with asyncio.timeout(35):
                await self.collect(source, info, canonical, identifier)
        except Exception as exc:
            info["diagnostics"].append({"step": "overall", "code": fetch_error(exc)})
        blocks = []
        if source["title"]:
            blocks.append("Название: " + source["title"])
        if info["channel"]:
            blocks.append("Канал: " + info["channel"])
        if info["description"]:
            blocks.append("Описание автора:\n" + info["description"])
        if info["chapters"]:
            blocks.append(
                "Главы (названия не подтверждают содержание видеоряда):\n"
                + "\n".join(f"{c['seconds']} сек: {c['title']}" for c in info["chapters"])
            )
        if info.get("transcript_text"):
            blocks.append("Субтитры (речь, не видеоряд):\n" + info.pop("transcript_text"))
            source["coverage"] = "youtube_transcript"
        elif blocks:
            source["coverage"] = "youtube_metadata"
        source["text"] = "\n\n".join(blocks)
        source["hash"] = hashlib.sha256(source["text"].encode()).hexdigest()
        if not blocks:
            source["error"] = (
                info["diagnostics"][0]["code"] if info["diagnostics"] else "no_readable_text"
            )
        if (
            info["transcript_status"] != "available"
            and not info["chapters"]
            and len(info["description"]) < 160
        ):
            info["fallback_reason"] = "insufficient_youtube_text"
        return source

    async def collect(
        self, source: dict[str, Any], info: dict[str, Any], canonical: str, identifier: str
    ) -> None:
        tracks: list[Any] = []
        try:
            document = await self.get(canonical)
            parser = TextParser()
            parser.feed(document)
            player = embedded(document, "ytInitialPlayerResponse")
            initial = embedded(document, "ytInitialData")
            details = player.get("videoDetails", {})
            if details.get("videoId") and details["videoId"] != identifier:
                raise ValueError("youtube_identity_mismatch")
            if not details.get("videoId"):
                raise ValueError("youtube_missing_video_metadata")
            source["title"] = str(details.get("title") or parser.title).strip()[:300]
            info["channel"] = str(details.get("author", ""))[:300]
            description = str(
                details.get("shortDescription") or full_description(initial) or parser.description
            )
            info["description"] = description[:DESCRIPTION_LIMIT]
            info["description_truncated"] = len(description) > DESCRIPTION_LIMIT
            try:
                info["duration_seconds"] = max(0, int(details["lengthSeconds"]))
            except (ValueError, TypeError, KeyError):
                pass
            info["chapters"] = chapters(description, initial)
            playability = player.get("playabilityStatus", {}).get("status")
            if playability and playability != "OK":
                info["diagnostics"].append(
                    {"step": "player", "code": "youtube_unavailable_or_restricted"}
                )
            else:
                tracks = (
                    player.get("captions", {})
                    .get("playerCaptionsTracklistRenderer", {})
                    .get("captionTracks", [])
                )
            if not tracks:
                info["diagnostics"].append({"step": "captions", "code": "captions_not_exposed"})
        except Exception as exc:
            info["diagnostics"].append({"step": "watch_page", "code": fetch_error(exc)})
        if not source["title"]:
            try:
                document = await self.get(
                    "https://www.youtube.com/oembed?"
                    + urlencode({"url": canonical, "format": "json"}),
                    limit=100000,
                )
                metadata = json.loads(document)
                source["title"] = str(metadata.get("title", ""))[:300]
                info["channel"] = str(metadata.get("author_name", ""))[:300]
            except Exception as exc:
                info["diagnostics"].append({"step": "oembed", "code": fetch_error(exc)})
        candidates = sorted(
            (t for t in tracks if isinstance(t, dict)),
            key=lambda t: (
                0
                if str(t.get("languageCode", "")).startswith("ru")
                else 1
                if str(t.get("languageCode", "")).startswith("en")
                else 2,
                t.get("kind") == "asr",
            ),
        )
        for track in candidates[:2]:
            try:
                target = caption_url(str(track.get("baseUrl", "")), identifier)
                text, truncated = transcript(await self.get(target, limit=2_000_000))
                if not text:
                    raise ValueError("captions_empty")
                info.update(
                    transcript_status="available",
                    transcript_text=text,
                    transcript_language=str(track.get("languageCode", ""))[:20],
                    transcript_automatic=track.get("kind") == "asr",
                    transcript_truncated=truncated,
                )
                break
            except Exception as exc:
                info["diagnostics"].append({"step": "captions", "code": fetch_error(exc)})
