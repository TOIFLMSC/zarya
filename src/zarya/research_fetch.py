"""Bounded HTTP text fetch with pinned public DNS and checked redirects."""

import asyncio
import hashlib
import posixpath
import socket
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from zarya.research_logic import public_address, public_url

MAX_BYTES = 1_000_000


def fetch_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
        return "tls_certificate"
    if isinstance(exc, aiohttp.ClientSSLError):
        return "tls_error"
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return "dns_error"
    if isinstance(exc, aiohttp.ClientConnectionError):
        return "connection_error"
    if isinstance(exc, (ValueError, LookupError)):
        code = str(exc)
        return (
            code
            if code.startswith(
                (
                    "unsafe",
                    "private",
                    "http_",
                    "redirect",
                    "unsupported",
                    "size",
                    "no_",
                    "youtube_",
                    "captions_",
                )
            )
            else "invalid_source"
        )
    return "fetch_failed"


def check_target(url: str) -> str:
    current = public_url(url)
    parsed = urlsplit(current)
    path = parsed.path
    for _ in range(3):
        path = unquote(path)
    path = posixpath.normpath("/" + path.lstrip("/").replace("\\", "/"))
    if (parsed.hostname or "").lower().rstrip(".") in {
        "t.me",
        "telegram.me",
        "www.t.me",
    } and path.startswith(("/c/", "/+", "/joinchat/")):
        raise ValueError("private_telegram_post")
    return current


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title = ""
        self.description = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "footer", "noscript", "svg"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            a = dict(attrs)
            if a.get("name") == "description" or a.get("property") == "og:description":
                self.description = (a.get("content") or "")[:1500]
        if tag in {"p", "div", "br", "h1", "h2", "li", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data[:300]
        elif not self.skip:
            self.parts.append(data)


class PublicResolver(AbstractResolver):
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[Any]:
        answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = {str(r[4][0]) for r in answers}
        if not addresses or not all(public_address(a) for a in addresses):
            raise ValueError("private_dns")
        return [
            {
                "hostname": host,
                "host": a,
                "port": port,
                "family": socket.AF_INET6 if ":" in a else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for a in sorted(addresses)
        ]

    async def close(self) -> None:
        pass


class Fetcher(Protocol):
    async def fetch(self, url: str) -> dict[str, Any]: ...


class SafeFetcher:
    async def fetch(self, url: str) -> dict[str, Any]:
        from zarya.youtube import YouTubeFetcher, is_youtube

        if is_youtube(url):
            return await YouTubeFetcher().fetch(url)
        return await self.fetch_page(url)

    async def fetch_page(self, url: str) -> dict[str, Any]:
        source: dict[str, Any] = {
            "requested_url": url,
            "url": None,
            "title": "",
            "text": "",
            "coverage": "unavailable",
            "error": None,
        }
        try:
            current = check_target(url)
            connector = aiohttp.TCPConnector(
                resolver=PublicResolver(), use_dns_cache=False, limit=1
            )
            async with (
                asyncio.timeout(15),
                aiohttp.ClientSession(
                    connector=connector,
                    trust_env=False,
                    auto_decompress=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    timeout=aiohttp.ClientTimeout(total=12),
                    headers={
                        "User-Agent": "Zarya/0.1 (text research)",
                        "Accept": "text/html,text/plain",
                        "Accept-Encoding": "identity",
                    },
                ) as client,
            ):
                for attempt in range(4):
                    check_target(current)
                    async with client.get(current, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            if attempt == 3 or not response.headers.get("Location"):
                                raise ValueError("redirect_limit")
                            current = check_target(urljoin(current, response.headers["Location"]))
                            continue
                        if response.status != 200:
                            raise ValueError("http_" + str(response.status))
                        if (
                            response.headers.get("Content-Encoding", "identity").lower()
                            != "identity"
                        ):
                            raise ValueError("unsupported_compression")
                        kind = response.headers.get("Content-Type", "").split(";")[0].lower()
                        if kind not in {"text/html", "text/plain", "application/xhtml+xml"}:
                            raise ValueError("unsupported_content")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(16384):
                            data.extend(chunk)
                            if len(data) > MAX_BYTES:
                                raise ValueError("size_limit")
                        text = bytes(data).decode(response.charset or "utf-8", "replace")
                        parser = TextParser()
                        if kind != "text/plain":
                            parser.feed(text)
                            text = "\n".join(
                                " ".join(p.split())
                                for p in "".join(parser.parts).splitlines()
                                if p.strip()
                            )
                        host = (urlsplit(current).hostname or "").lower()
                        video = host in {
                            "youtube.com",
                            "www.youtube.com",
                            "m.youtube.com",
                            "youtu.be",
                            "x.com",
                            "www.x.com",
                            "twitter.com",
                            "www.twitter.com",
                        }
                        if video:
                            text = (parser.title + "\n" + parser.description).strip()
                        source.update(
                            url=current,
                            title=parser.title.strip()[:300] or host,
                            text=text[:12000],
                            coverage="metadata_only" if video else "partial_text",
                            hash=hashlib.sha256(text.encode()).hexdigest(),
                        )
                        if not text:
                            source.update(coverage="unavailable", error="no_readable_text")
                        return source
        except (ValueError, LookupError) as exc:
            source["error"] = (
                str(exc)
                if str(exc).startswith(
                    ("unsafe", "private", "http_", "redirect", "unsupported", "size", "no_")
                )
                else "invalid_url"
            )
        except TimeoutError:
            source["error"] = "timeout"
        except Exception as exc:
            source["error"] = fetch_error(exc)
        return source
