from __future__ import annotations

import aiohttp

from .log import logger


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
}


LOGIN_URL = "https://forums.e-hentai.org/index.php?act=Login&CODE=01"
VERIFY_URL = "https://e-hentai.org/home.php"
EXHENTAI_VERIFY_URL = "https://exhentai.org/"


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    cookies = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            key, _, value = item.partition("=")
            cookies[key.strip()] = value.strip()
    return cookies


def cookies_to_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def login_with_credentials(
    session: aiohttp.ClientSession,
    username: str,
    password: str,
) -> dict[str, str] | None:
    data = {
        "UserName": username,
        "PassWord": password,
        "CookieDate": "1",
        "b": "d",
        "bt": "1-1",
    }
    headers = {
        **DEFAULT_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://forums.e-hentai.org",
        "Referer": "https://forums.e-hentai.org/index.php",
    }
    try:
        async with session.post(
            LOGIN_URL, data=data, headers=headers, allow_redirects=False
        ) as resp:
            logger.debug(f"Login response status: {resp.status}")
            cookies = {}
            for key, cookie in session.cookie_jar.filter_cookies(
                "https://e-hentai.org"
            ).items():
                cookies[key] = cookie.value
            for key, cookie in session.cookie_jar.filter_cookies(
                "https://forums.e-hentai.org"
            ).items():
                cookies[key] = cookie.value
            if cookies.get("ipb_member_id") and cookies.get("ipb_pass_hash"):
                return cookies
    except Exception as e:
        logger.error(f"Login failed: {e}")
    return None


async def validate_cookies(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    site_mode: str = "exhentai",
) -> bool:
    url = EXHENTAI_VERIFY_URL if site_mode == "exhentai" else VERIFY_URL
    cookie_str = cookies_to_string(cookies)
    headers = {**DEFAULT_HEADERS, "Cookie": cookie_str}
    try:
        async with session.get(
            url, headers=headers, allow_redirects=False, timeout=15
        ) as resp:
            if resp.status == 200:
                text = await resp.text()
                if site_mode == "exhentai":
                    return "ExHentai" in text or "Front Page" in text
                else:
                    return "E-Hentai" in text or "Front Page" in text
    except Exception as e:
        logger.error(f"Cookie validation failed: {e}")
    return False
