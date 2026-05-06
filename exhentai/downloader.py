from __future__ import annotations

import asyncio
import os

import aiohttp

from .log import logger
from .models import ImagePage


def _bounded_int(
    value,
    default: int,
    min_value: int,
    max_value: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _has_expected_magic(data: bytes, ext: str) -> bool:
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if ext == ".bmp":
        return data.startswith(b"BM")
    if ext == ".webm":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _has_any_supported_magic(data: bytes) -> bool:
    return any(
        _has_expected_magic(data, ext)
        for ext in (".jpg", ".png", ".gif", ".webp", ".bmp", ".webm")
    )


def _is_expected_download(data: bytes, content_type: str, filename: str) -> bool:
    if not data:
        return False

    ext = os.path.splitext(filename)[1].lower()
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type in {"text/html", "text/plain", "application/json"}:
        return False
    if _has_expected_magic(data, ext):
        return True
    if media_type.startswith("image/") and _has_any_supported_magic(data):
        return True
    if media_type == "video/webm" and _has_expected_magic(data, ".webm"):
        return True
    if media_type in {"", "application/octet-stream"} and _has_any_supported_magic(data):
        return True
    return False


def _is_existing_download_valid(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            sample = f.read(16)
    except OSError:
        return False
    return _is_expected_download(sample, "", os.path.basename(path))


def _find_existing_download(temp_dir: str, page: ImagePage) -> str | None:
    root = f"{page.index:04d}"
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".webm"):
        path = os.path.join(temp_dir, f"{root}{ext}")
        if not os.path.exists(path):
            continue
        if _is_existing_download_valid(path):
            page.filename = os.path.basename(path)
            page.local_path = path
            return path
        try:
            os.remove(path)
        except OSError:
            pass
    return None


def _path_for_download_content(local_path: str, data: bytes, content_type: str) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "video/webm" or _has_expected_magic(data, ".webm"):
        root, _ = os.path.splitext(local_path)
        return f"{root}.webm"
    return local_path


def _download_content_with_requests(
    url: str,
    headers: dict[str, str],
    timeout: int,
) -> tuple[int, str, bytes]:
    import requests

    resp = requests.get(url, headers=headers, timeout=(10, timeout))
    body = resp.content
    content_length = resp.headers.get("Content-Length")
    if resp.status_code == 200 and content_length:
        try:
            expected_length = int(content_length)
        except ValueError:
            expected_length = 0
        if expected_length > 0 and len(body) < expected_length:
            raise IOError(
                f"incomplete response: got {len(body)} of {expected_length} bytes"
            )
    return resp.status_code, resp.headers.get("Content-Type", ""), body


async def _refresh_image_url(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    page: ImagePage,
    local_path: str,
) -> str:
    if not page.page_url:
        return local_path

    try:
        from .api import _guess_extension, _resolve_image_url

        image_url = await _resolve_image_url(session, cookies, page.page_url)
    except Exception as e:
        logger.debug(f"Failed to refresh image URL for {page.filename}: {e}")
        return local_path

    if not image_url:
        return local_path

    page.image_url = image_url
    ext = _guess_extension(image_url)
    if ext and os.path.splitext(page.filename)[1].lower() != ext:
        page.filename = f"{page.index:04d}{ext}"
        local_path = os.path.join(os.path.dirname(local_path), page.filename)
    return local_path


async def download_images(
    session: aiohttp.ClientSession,
    image_pages: list[ImagePage],
    temp_dir: str,
    cookies: dict[str, str],
    concurrency: int = 3,
    retry_count: int = 3,
    timeout: int = 30,
    progress_callback=None,
) -> list[str]:
    os.makedirs(temp_dir, exist_ok=True)
    concurrency = _bounded_int(concurrency, 3, 1, 10)
    retry_count = _bounded_int(retry_count, 3, 1, 10)
    timeout = _bounded_int(timeout, 30, 5, 300)
    semaphore = asyncio.Semaphore(concurrency)
    total = len(image_pages)
    completed = [0]
    failed = [0]
    lock = asyncio.Lock()

    async def download_one(page: ImagePage) -> str | None:
        async with semaphore:
            headers = {
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Encoding": "identity",
            }
            if cookies:
                from .auth import cookies_to_string
                headers["Cookie"] = cookies_to_string(cookies)
            if page.page_url:
                headers["Referer"] = page.page_url

            local_path = os.path.join(temp_dir, page.filename)
            existing_path = _find_existing_download(temp_dir, page)
            if existing_path:
                async with lock:
                    completed[0] += 1
                    if progress_callback:
                        await progress_callback(completed[0], total, failed[0])
                return existing_path

            for attempt in range(retry_count):
                try:
                    if attempt > 0 or not page.image_url:
                        local_path = await _refresh_image_url(
                            session, cookies, page, local_path
                        )
                    if not page.image_url:
                        continue

                    status, content_type, body = await asyncio.to_thread(
                        _download_content_with_requests,
                        page.image_url,
                        headers,
                        timeout,
                    )
                    if status == 200:
                        if not _is_expected_download(body, content_type, page.filename):
                            logger.warning(
                                "Unexpected download content for "
                                f"{page.filename}: content_type={content_type or 'unknown'}, "
                                f"size={len(body)}"
                            )
                        else:
                            local_path = _path_for_download_content(
                                local_path, body, content_type
                            )
                            page.filename = os.path.basename(local_path)
                            with open(local_path, "wb") as f:
                                f.write(body)
                            page.local_path = local_path
                            async with lock:
                                completed[0] += 1
                                if progress_callback:
                                    await progress_callback(completed[0], total, failed[0])
                            return local_path
                    else:
                        logger.debug(
                            f"Unexpected status {status} downloading "
                            f"{page.filename} (attempt {attempt + 1})"
                        )
                except asyncio.TimeoutError:
                    logger.debug(f"Timeout downloading {page.filename} (attempt {attempt + 1})")
                except Exception as e:
                    logger.debug(f"Error downloading {page.filename}: {e} (attempt {attempt + 1})")
                await asyncio.sleep(1 * (attempt + 1))

            logger.warning(f"Failed to download {page.filename} after {retry_count} attempts")
            async with lock:
                failed[0] += 1
                if progress_callback:
                    await progress_callback(completed[0], total, failed[0])
            return None

    tasks = [download_one(page) for page in image_pages]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    paths = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(f"Download task crashed: {result}")
        elif isinstance(result, str):
            paths.append(result)
    return paths
