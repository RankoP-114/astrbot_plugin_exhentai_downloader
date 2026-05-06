from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .auth import DEFAULT_HEADERS, cookies_to_string
from .log import logger
from .models import Gallery, ImagePage

API_BASE = "https://api.e-hentai.org/api.php"
E_HENTAI_BASE = "https://e-hentai.org"
EXHENTAI_BASE = "https://exhentai.org"
MAX_GALLERY_LIST_PAGES = 200


def _resolve_gid_token(url_or_gid: str) -> tuple[int, str]:
    value = (url_or_gid or "").strip()
    if not value:
        return 0, ""
    pattern = r"/g/(\d+)/([a-f0-9]+)/?"
    match = re.search(pattern, value, re.IGNORECASE)
    if match:
        return int(match.group(1)), match.group(2)

    parts = [part for part in value.strip("/").split("/") if part]
    if len(parts) >= 2 and parts[0].isdigit() and parts[1]:
        return int(parts[0]), parts[1]
    if len(parts) == 1 and parts[0].isdigit():
        return int(parts[0]), ""
    return 0, ""


def _build_session_headers(cookies: dict[str, str]) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    if cookies:
        headers["Cookie"] = cookies_to_string(cookies)
    return headers


async def search_galleries(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    keyword: str,
    page: int = 0,
    site_mode: str = "exhentai",
) -> list[Gallery]:
    headers = _build_session_headers(cookies)
    payload = {
        "method": "gdata",
        "gidlist": [],
    }
    galleries = []
    try:
        search_url = _build_search_url(keyword, page, site_mode)
        async with session.get(search_url, headers=headers, timeout=30) as resp:
            if resp.status != 200:
                logger.error(f"Search returned status {resp.status}")
                return []
            html = await resp.text()
            results = _parse_search_results(html, site_mode)
            if results:
                gids = [[r["gid"], r["token"]] for r in results]
                payload["gidlist"] = gids
                payload["method"] = "gdata"
                async with session.post(
                    API_BASE, json=payload, headers=headers, timeout=30
                ) as api_resp:
                    if api_resp.status == 200:
                        data = await api_resp.json(content_type=None)
                        gmetadatas = data.get("gmetadata", [])
                        for i, meta in enumerate(gmetadatas):
                            gallery = _parse_gallery_meta(meta)
                            if i < len(results):
                                result_thumb = results[i].get("thumb_url", "")
                                if _is_remote_url(result_thumb):
                                    gallery.thumb_url = result_thumb
                            galleries.append(gallery)
    except Exception as e:
        logger.error(f"Search failed: {e}")
    return galleries


def _build_search_url(keyword: str, page: int = 0, site_mode: str = "exhentai") -> str:
    base = "https://exhentai.org/" if site_mode == "exhentai" else "https://e-hentai.org/"
    params = {
        "f_search": keyword,
        "page": page,
    }
    return f"{base}?{_urlencode(params)}"


def _urlencode(params: dict) -> str:
    from urllib.parse import quote

    parts = []
    for k, v in params.items():
        if k == "f_search":
            parts.append(f"f_search={quote(str(v))}")
        else:
            parts.append(f"{k}={v}")
    return "&".join(parts)


def _parse_search_results(html: str, site_mode: str = "exhentai") -> list[dict]:
    results = []
    seen = set()
    soup = BeautifulSoup(html, "lxml")
    for item in soup.select(".itg.gltc tr, .itg.gltc .gtr0, .itg.gltc .gtr1"):
        link = item.select_one("a[href]")
        if not link:
            continue
        href = link.get("href", "")
        match = re.search(r"/g/(\d+)/([a-f0-9]+)/?", href)
        if not match:
            continue
        gid = int(match.group(1))
        token = match.group(2)
        key = (gid, token)
        if key in seen:
            continue
        seen.add(key)
        thumb = _extract_thumbnail_url(item, site_mode)
        results.append({"gid": gid, "token": token, "thumb_url": thumb})

    if results:
        return results

    for link in soup.select('a[href*="/g/"]'):
        href = link.get("href", "")
        match = re.search(r"/g/(\d+)/([a-f0-9]+)/?", href)
        if not match:
            continue
        gid = int(match.group(1))
        token = match.group(2)
        key = (gid, token)
        if key in seen:
            continue
        seen.add(key)
        container = link.find_parent(["tr", "div", "td"]) or link
        thumb = _extract_thumbnail_url(container, site_mode)
        results.append({"gid": gid, "token": token, "thumb_url": thumb})
    return results


def _extract_thumbnail_url(container, site_mode: str = "exhentai") -> str:
    if not hasattr(container, "select"):
        return ""

    for img_tag in container.select("img"):
        for attr in ("data-src", "data-original", "data-lazy-src", "data-url", "src"):
            thumb = _normalize_thumbnail_url(img_tag.get(attr, ""), site_mode)
            if thumb:
                return thumb
        for attr in ("data-srcset", "srcset"):
            for srcset_url in _split_srcset(img_tag.get(attr, "")):
                thumb = _normalize_thumbnail_url(srcset_url, site_mode)
                if thumb:
                    return thumb
        for style_url in _extract_style_urls(img_tag.get("style", "")):
            thumb = _normalize_thumbnail_url(style_url, site_mode)
            if thumb:
                return thumb

    for styled in container.select('[style*="url("]'):
        for style_url in _extract_style_urls(styled.get("style", "")):
            thumb = _normalize_thumbnail_url(style_url, site_mode)
            if thumb:
                return thumb
    return ""


def _split_srcset(srcset: str) -> list[str]:
    urls = []
    for candidate in (srcset or "").split(","):
        url = candidate.strip().split(" ", 1)[0].strip()
        if url:
            urls.append(url)
    return urls


def _extract_style_urls(style: str) -> list[str]:
    return re.findall(r"url\(['\"]?([^'\")]+)['\"]?\)", style or "", re.IGNORECASE)


def _normalize_thumbnail_url(url: str, site_mode: str = "exhentai") -> str:
    value = (url or "").strip().strip("'\"")
    if not value:
        return ""
    lower = value.lower()
    if lower.startswith(("data:", "about:", "javascript:")):
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    elif not _is_remote_url(value):
        base = EXHENTAI_BASE if site_mode == "exhentai" else E_HENTAI_BASE
        value = urljoin(base, value)
    return value if _is_remote_url(value) else ""


def _is_remote_url(url: str) -> bool:
    return bool(re.match(r"^https?://", (url or "").strip(), re.IGNORECASE))


async def get_gallery_metadata(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    gid: int,
    token: str,
) -> Gallery | None:
    headers = _build_session_headers(cookies)
    payload = {
        "method": "gdata",
        "gidlist": [[gid, token]],
        "namespace": 1,
    }
    try:
        async with session.post(API_BASE, json=payload, headers=headers, timeout=30) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            gmetadatas = data.get("gmetadata", [])
            if not gmetadatas or "error" in gmetadatas[0]:
                return None
            meta = gmetadatas[0]
            gallery = _parse_gallery_meta(meta)
            gallery.gid = gid
            gallery.token = token
            gallery.thumb_url = meta.get("thumb", "")
            return gallery
    except Exception as e:
        logger.error(f"Failed to get gallery metadata: {e}")
        return None


def _parse_gallery_meta(meta: dict) -> Gallery:
    tags = meta.get("tags", [])
    tag_strings = []
    for tag in tags:
        if isinstance(tag, str):
            tag_strings.append(tag)
        elif isinstance(tag, list) and len(tag) >= 1:
            tag_strings.append(str(tag[0]))

    return Gallery(
        gid=meta.get("gid", 0),
        token=meta.get("token", ""),
        title=meta.get("title", ""),
        title_jpn=meta.get("title_jpn", ""),
        category=meta.get("category", ""),
        thumb_url=meta.get("thumb", ""),
        tags=tag_strings,
        uploader=meta.get("uploader", ""),
        posted=meta.get("posted", ""),
        filecount=int(meta.get("filecount", 0)),
        filesize=int(meta.get("filesize", 0)),
        rating=float(meta.get("rating", 0.0)),
        language=meta.get("language", ""),
        parent_gid=meta.get("parent_gid"),
        parent_key=meta.get("parent_key"),
    )


async def get_page_urls(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    gid: int,
    token: str,
    site_mode: str = "exhentai",
) -> list[ImagePage]:
    base = EXHENTAI_BASE if site_mode == "exhentai" else E_HENTAI_BASE
    gallery_url = f"{base}/g/{gid}/{token}/"
    headers = _build_session_headers(cookies)
    pages = []
    current_url = gallery_url
    page_index = 0
    visited_urls = set()

    try:
        while current_url:
            visit_key = current_url.split("#", 1)[0].rstrip("/")
            if visit_key in visited_urls:
                logger.warning(f"Stopped gallery pagination on repeated URL: {current_url}")
                break
            visited_urls.add(visit_key)
            if len(visited_urls) > MAX_GALLERY_LIST_PAGES:
                logger.warning(
                    f"Stopped gallery pagination after {MAX_GALLERY_LIST_PAGES} pages"
                )
                break

            async with session.get(current_url, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    break
                html = await resp.text()
                soup = BeautifulSoup(html, "lxml")

                for page_url in _iter_gallery_page_links(soup, base):
                    page_index += 1
                    pages.append(ImagePage(
                        index=page_index,
                        image_url="",
                        page_url=page_url,
                        filename=f"{page_index:04d}.jpg",
                    ))

                next_url = _find_next_page_url(soup, base)
                if next_url and next_url.split("#", 1)[0].rstrip("/") in visited_urls:
                    logger.warning(f"Stopped gallery pagination on repeated next URL: {next_url}")
                    break
                current_url = next_url
    except Exception as e:
        logger.error(f"Failed to get page URLs: {e}")

    return pages


def _iter_gallery_page_links(soup: BeautifulSoup, base: str) -> list[str]:
    links = []
    seen = set()
    for gdt_entry in soup.select(".gdtm a[href], .gdtl a[href], #gdt a[href], a[href*='/s/'][href]"):
        href = gdt_entry.get("href", "")
        if not href or not re.search(r"/s/[a-f0-9]+/\d+-\d+", href, re.IGNORECASE):
            continue
        page_url = href if href.startswith("http") else urljoin(base, href)
        if page_url in seen:
            continue
        seen.add(page_url)
        links.append(page_url)
    return links


def _find_next_page_url(soup: BeautifulSoup, base: str) -> str | None:
    for link in soup.select(".ptt a[href]"):
        text = link.get_text(strip=True).lower()
        if text not in {">", "›", "»", "next"}:
            continue
        href = link.get("href", "")
        if not href:
            return None
        return href if href.startswith("http") else urljoin(base, href)
    return None


async def _resolve_image_url(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    page_url: str,
) -> str | None:
    headers = _build_session_headers(cookies)
    try:
        async with session.get(page_url, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            soup = BeautifulSoup(html, "lxml")
            img_tag = soup.select_one("#img")
            if not img_tag:
                img_tag = soup.select_one("img#img") or soup.select_one("#i3 img")
            if img_tag:
                src = img_tag.get("src", "")
                if not src and img_tag.get("data-src"):
                    src = img_tag.get("data-src", "")
                if src:
                    return src
            gif_src = soup.find("source", type="video/webm")
            if gif_src:
                return gif_src.get("src", "")
    except Exception as e:
        logger.debug(f"Failed to resolve image URL from {page_url}: {e}")
    return None


def _guess_extension(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if "." in path.rsplit("/", 1)[-1]:
        ext = "." + path.rsplit(".", 1)[-1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".webm"):
            return ext
    return ".jpg"
