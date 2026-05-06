from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image, Node, Nodes, Plain
try:
    from astrbot.core.message.components import File
except ImportError:
    from astrbot.api.message_components import File
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .exhentai.auth import (
    parse_cookie_string,
    cookies_to_string,
    login_with_credentials,
    validate_cookies,
    DEFAULT_HEADERS,
)
from .exhentai.api import (
    search_galleries,
    get_gallery_metadata,
    get_page_urls,
    _resolve_gid_token,
)
from .exhentai.downloader import download_images
from .exhentai.packager import pack_zip, pack_pdf


AUTH_HELP = (
    "未登录！请先配置 Cookie 或使用 /exhentai login <用户名> <密码> 登录。\n"
    "Cookie 获取方法：浏览器登录 exhentai.org 后，\n"
    "F12 → Application → Cookies → 复制 ipb_member_id, ipb_pass_hash, igneous 的值。"
)

WHITELIST_DENY = "该群不在白名单中，无法使用此功能。"
ADMIN_DENY = "仅 AstrBot 管理员可使用此功能。"
MAX_ACTIVE_DOWNLOADS = 1


class ExHentaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._download_locks: dict[str, asyncio.Lock] = {}
        self._download_queue_condition = asyncio.Condition()
        self._download_waiters = deque()
        self._active_downloads = 0
        self._active_sessions: list = []

    def _debug_log(self, msg: str):
        if self.config.get("debug_mode", False):
            logger.info(f"[ExHentai] {msg}")

    @staticmethod
    def _normalize_id_list(value) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            raw_items = value.replace("\n", ",").split(",")
        else:
            try:
                raw_items = list(value)
            except TypeError:
                raw_items = [value]
        return {str(item).strip() for item in raw_items if str(item).strip()}

    def _get_int_config(
        self,
        key: str,
        default: int,
        min_value: int = 1,
        max_value: int | None = None,
    ) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        value = max(min_value, value)
        if max_value is not None:
            value = min(max_value, value)
        return value

    def _get_bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开"}
        return bool(value)

    def _get_download_queue_limit(self) -> int:
        if not self._get_bool_config("download_queue_enabled", True):
            return 0
        return self._get_int_config("max_download_queue_size", 5, 0, 5)

    async def _reserve_download_slot(self) -> tuple[bool, object | None, str]:
        token = object()
        queue_limit = self._get_download_queue_limit()
        async with self._download_queue_condition:
            if (
                self._active_downloads < MAX_ACTIVE_DOWNLOADS
                and not self._download_waiters
            ):
                self._active_downloads += 1
                self._debug_log("Download slot acquired immediately")
                return True, None, ""

            if queue_limit <= 0:
                return False, None, "当前已有下载任务，请稍后再试。"

            if len(self._download_waiters) >= queue_limit:
                return (
                    False,
                    None,
                    f"下载队列已满（最多 {queue_limit} 个等待中），请稍后再试。",
                )

            self._download_waiters.append(token)
            position = len(self._download_waiters)
            self._debug_log(f"Download queued at position {position}")
            return (
                True,
                token,
                f"已加入下载队列，当前排第 {position} 位。"
                f"最多同时 {MAX_ACTIVE_DOWNLOADS} 个下载任务，最多等待 {queue_limit} 个。",
            )

    async def _wait_for_download_turn(self, token: object):
        async with self._download_queue_condition:
            while True:
                is_next = self._download_waiters and self._download_waiters[0] is token
                if is_next and self._active_downloads < MAX_ACTIVE_DOWNLOADS:
                    self._download_waiters.popleft()
                    self._active_downloads += 1
                    self._download_queue_condition.notify_all()
                    self._debug_log("Queued download slot acquired")
                    return
                await self._download_queue_condition.wait()

    async def _cancel_download_waiter(self, token: object):
        async with self._download_queue_condition:
            try:
                self._download_waiters.remove(token)
            except ValueError:
                return
            self._download_queue_condition.notify_all()
            self._debug_log("Queued download cancelled")

    async def _release_download_slot(self):
        async with self._download_queue_condition:
            if self._active_downloads > 0:
                self._active_downloads -= 1
            self._download_queue_condition.notify_all()
            self._debug_log("Download slot released")

    async def _get_download_queue_snapshot(self) -> tuple[int, int]:
        async with self._download_queue_condition:
            return self._active_downloads, len(self._download_waiters)

    def _get_cookies(self) -> dict[str, str] | None:
        cookie_str = self.config.get("exhentai_cookies", "")
        if cookie_str:
            return parse_cookie_string(cookie_str)
        return None

    async def _validate_cookies_for_current_site(self, cookies: dict[str, str]) -> bool:
        session = await self._build_client_session()
        try:
            return await validate_cookies(
                session,
                cookies,
                self.config.get("site_mode", "exhentai"),
            )
        finally:
            await session.close()
            if session in self._active_sessions:
                self._active_sessions.remove(session)

    async def _ensure_cookies(self) -> dict[str, str] | None:
        cookies = self._get_cookies()
        if cookies and cookies.get("ipb_member_id") and cookies.get("ipb_pass_hash"):
            if await self._validate_cookies_for_current_site(cookies):
                return cookies
            self._debug_log("Cached cookies failed site validation")
            return None

        if self.config.get("auth_method", "cookie") != "credential":
            return None

        username = self.config.get("username", "")
        password = self.config.get("password", "")
        if not username or not password:
            return None

        session = await self._build_client_session()
        try:
            cookies = await login_with_credentials(session, username, password)
            if not cookies:
                return None
            site_mode = self.config.get("site_mode", "exhentai")
            if not await validate_cookies(session, cookies, site_mode):
                self._debug_log("Credential login succeeded but site validation failed")
                return None
            self.config["exhentai_cookies"] = cookies_to_string(cookies)
            self.config.save_config()
            self._debug_log("Credential login succeeded and cookies were cached")
            return cookies
        except Exception as e:
            logger.error(f"Credential login error: {e}")
            return None
        finally:
            await session.close()
            if session in self._active_sessions:
                self._active_sessions.remove(session)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return str(getattr(event, "role", "")).lower() == "admin"

    @staticmethod
    def _is_group_message(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_group_id())
        except Exception:
            return bool(getattr(getattr(event, "message_obj", None), "group_id", ""))

    def _check_access(self, event: AstrMessageEvent) -> str | None:
        if self.config.get("admin_only", True) and not self._is_admin(event):
            return ADMIN_DENY
        if not self._check_group_access(event):
            return WHITELIST_DENY
        return None

    def _check_group_access(self, event: AstrMessageEvent) -> bool:
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = str(getattr(getattr(event, "message_obj", None), "group_id", "") or "").strip()
        whitelist = self._normalize_id_list(self.config.get("group_whitelist", []))
        if not whitelist:
            return True
        return not group_id or group_id in whitelist

    def _get_data_dir(self) -> str:
        p = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def _get_temp_dir(self, gid: int) -> str:
        p = os.path.join(self._get_data_dir(), "temp", str(gid))
        os.makedirs(p, exist_ok=True)
        return p

    def _cleanup_temp_dir(self, gid: int):
        temp_dir = os.path.join(self._get_data_dir(), "temp", str(gid))
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            self._debug_log(f"Cleaned up temp dir: {temp_dir}")

    async def _build_client_session(self) -> "aiohttp.ClientSession":
        import aiohttp
        proxy_type = self.config.get("proxy_type", "none")
        proxy_host = self.config.get("proxy_host", "")
        proxy_port = self.config.get("proxy_port", 0)
        proxy_user = self.config.get("proxy_username", "")
        proxy_pass = self.config.get("proxy_password", "")
        timeout = aiohttp.ClientTimeout(total=60)
        cookies = self._get_cookies()

        if proxy_type == "socks5" and proxy_host and proxy_port:
            from aiohttp_socks import ProxyConnector, ProxyType
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=proxy_host,
                port=proxy_port,
                username=proxy_user or None,
                password=proxy_pass or None,
                rdns=True,
            )
            session = aiohttp.ClientSession(
                connector=connector,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
            )
        else:
            session = aiohttp.ClientSession(
                headers=DEFAULT_HEADERS,
                timeout=timeout,
            )

        if proxy_type == "http" and proxy_host and proxy_port:
            proxy_url = f"http://{proxy_host}:{proxy_port}"
            proxy_auth = aiohttp.BasicAuth(proxy_user, proxy_pass) if proxy_user and proxy_pass else None
            _orig_request = session._request

            async def _proxy_request(method, url, **kwargs):
                if "proxy" not in kwargs:
                    kwargs["proxy"] = proxy_url
                if proxy_auth and "proxy_auth" not in kwargs:
                    kwargs["proxy_auth"] = proxy_auth
                return await _orig_request(method, url, **kwargs)

            session._request = _proxy_request
            self._debug_log(f"HTTP proxy set: {proxy_url}")

        if cookies:
            cookie_jar = session.cookie_jar
            for key, value in cookies.items():
                cookie_jar.update_cookies(
                    {key: value},
                    aiohttp.client.URL("https://e-hentai.org"),
                )

        self._active_sessions.append(session)
        return session

    @filter.command_group("exhentai")
    def exhentai(self):
        pass

    @exhentai.command("login")
    async def cmd_login(self, event: AstrMessageEvent, username: str = "", password: str = ""):
        '''登录 ExHentai'''
        denied = self._check_access(event)
        if denied:
            yield event.plain_result(denied)
            return

        if self._is_group_message(event):
            yield event.plain_result(
                "为避免泄露账号密码，请在私聊中使用 /exhentai login，"
                "或在 WebUI 中配置 Cookie / credential。"
            )
            return

        if not username or not password:
            yield event.plain_result(
                "用法：/exhentai login <用户名> <密码>\n"
                "也可以直接在插件配置中设置 Cookie 来登录。"
            )
            return

        yield event.plain_result("正在登录 ExHentai...")
        session = await self._build_client_session()
        try:
            cookies = await login_with_credentials(session, username, password)
            if cookies:
                site_mode = self.config.get("site_mode", "exhentai")
                valid = await validate_cookies(session, cookies, site_mode)
                if valid:
                    self.config["exhentai_cookies"] = cookies_to_string(cookies)
                    self.config["auth_method"] = "cookie"
                    self.config.save_config()
                    yield event.plain_result("登录成功！Cookie 已保存。")
                else:
                    yield event.plain_result(
                        "账号密码验证通过，但 ExHentai 访问失败。\n"
                        "请确认账号已通过 Hasuki Exchange 解锁 exhentai 访问权限。\n"
                        "Cookie 未保存，请改用可访问当前站点的账号或 Cookie。"
                    )
            else:
                yield event.plain_result(
                    "登录失败！请检查用户名和密码是否正确。\n"
                    "如果遇到 Cloudflare 验证，建议改用 Cookie 方式登录。"
                )
        except Exception as e:
            logger.error(f"Login error: {e}")
            yield event.plain_result(f"登录出错：{e}")
        finally:
            await session.close()
            if session in self._active_sessions:
                self._active_sessions.remove(session)

    @exhentai.command("logout")
    async def cmd_logout(self, event: AstrMessageEvent):
        '''登出 ExHentai'''
        denied = self._check_access(event)
        if denied:
            yield event.plain_result(denied)
            return

        self.config["exhentai_cookies"] = ""
        self.config["auth_method"] = "cookie"
        self.config.save_config()
        yield event.plain_result("已清除登录信息。")

    @exhentai.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        '''查看当前状态'''
        denied = self._check_access(event)
        if denied:
            yield event.plain_result(denied)
            return

        logged_in = bool(await self._ensure_cookies())
        proxy_type = self.config.get("proxy_type", "none")
        pack_fmt = self.config.get("pack_format", "zip")
        pass_set = bool(self.config.get("pack_password", ""))
        site = self.config.get("site_mode", "exhentai")
        active_downloads, queued_downloads = await self._get_download_queue_snapshot()
        queue_limit = self._get_download_queue_limit()

        lines = [
            f"站点: {site}",
            f"登录状态: {'已登录' if logged_in else '未登录'}",
            f"代理: {proxy_type or '无'}",
            f"打包格式: {pack_fmt}",
            f"加密密码: {'已设置' if pass_set else '未设置'}",
            f"并发数: {self._get_int_config('download_concurrency', 3, 1, 10)}",
            f"下载队列: {'开' if queue_limit > 0 else '关'} "
            f"(运行中 {active_downloads}/{MAX_ACTIVE_DOWNLOADS}, "
            f"等待 {queued_downloads}/{queue_limit})",
            f"自动清理: {'开' if self.config.get('auto_cleanup', True) else '关'}",
            f"自动撤回: {'开' if self.config.get('auto_revoke', False) else '关'}",
            f"封面预览: {'开' if self.config.get('cover_preview', True) else '关'}",
            f"搜索封面: {'开' if self._get_bool_config('search_result_covers', False) else '关'}",
            f"调试模式: {'开' if self.config.get('debug_mode', False) else '关'}",
        ]
        yield event.plain_result("\n".join(lines))

    @exhentai.command("search")
    async def cmd_search(self, event: AstrMessageEvent):
        '''搜索漫画'''
        denied = self._check_access(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not await self._ensure_cookies():
            yield event.plain_result(AUTH_HELP)
            return

        keyword, page = _parse_search_args(_get_command_tail(event, "exhentai", "search"))
        if not keyword:
            yield event.plain_result(
                "用法：/exhentai search <关键词> [页码]\n"
                "示例：/exhentai search mind control 1"
            )
            return

        yield event.plain_result(f"正在搜索: {keyword} ...")
        session = await self._build_client_session()
        try:
            galleries = await search_galleries(
                session, self._get_cookies(), keyword, page,
                self.config.get("site_mode", "exhentai"),
            )
            if not galleries:
                yield event.plain_result(f"未找到与 '{keyword}' 相关的结果。")
                return

            include_search_covers = self._get_bool_config("search_result_covers", False)
            if _is_qq_platform(event):
                yield event.chain_result([
                    _build_search_forward_nodes(
                        event,
                        keyword,
                        galleries,
                        include_covers=include_search_covers,
                    )
                ])
                return

            lines = [f"搜索 '{keyword}' 结果 ({len(galleries)} 个):", ""]
            for i, g in enumerate(galleries[:10], 1):
                line = (
                    f"{i}. [{g.gid}/{g.token}] {g.title} "
                    f"({g.filecount}P, {g.filesize_mb}MB, {g.rating:.1f})"
                )
                if include_search_covers and g.thumb_url:
                    line += f"\n   封面: {g.thumb_url}"
                lines.append(line)
            if len(galleries) > 10:
                lines.append(f"\n... 还有 {len(galleries) - 10} 个结果")
            lines.append(f"\n使用 /exhentai info {galleries[0].gid}/{galleries[0].token} 查看详情")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"Search error: {e}")
            yield event.plain_result(f"搜索出错：{e}")
        finally:
            await session.close()
            if session in self._active_sessions:
                self._active_sessions.remove(session)

    @exhentai.command("info")
    async def cmd_info(self, event: AstrMessageEvent, gid_or_url: str):
        '''查看漫画详情'''
        denied = self._check_access(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not await self._ensure_cookies():
            yield event.plain_result(AUTH_HELP)
            return

        gid, token = _resolve_gid_token(gid_or_url)
        if not gid or not token:
            yield event.plain_result(
                "请提供有效的 Gallery ID 或 URL。\n用法：/exhentai info <gid/token 或完整URL>"
            )
            return

        yield event.plain_result(f"正在获取 #{gid} 的详情...")
        session = await self._build_client_session()
        try:
            gallery = await get_gallery_metadata(session, self._get_cookies(), gid, token)
            if not gallery:
                yield event.plain_result(f"未找到 #{gid} 的信息，请检查 ID 和登录状态。")
                return

            if _is_qq_platform(event):
                yield event.chain_result([
                    _build_gallery_info_forward_nodes(
                        event,
                        gallery,
                        f"使用 /exhentai download {gallery.gid}/{gallery.token} 下载",
                        include_cover=self.config.get("cover_preview", True),
                    )
                ])
                return

            if self.config.get("cover_preview", True) and gallery.thumb_url:
                try:
                    yield event.image_result(gallery.thumb_url)
                except Exception as e:
                    self._debug_log(f"Cover preview failed: {e}")

            yield event.plain_result(
                gallery.format_info()
                + f"\n\n使用 /exhentai download {gallery.gid}/{gallery.token} 下载"
            )
        except Exception as e:
            logger.error(f"Info error: {e}")
            yield event.plain_result(f"获取详情出错：{e}")
        finally:
            await session.close()
            if session in self._active_sessions:
                self._active_sessions.remove(session)

    @exhentai.command("download")
    async def cmd_download(self, event: AstrMessageEvent, gid_or_url: str):
        '''下载漫画'''
        denied = self._check_access(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not await self._ensure_cookies():
            yield event.plain_result(AUTH_HELP)
            return

        gid, token = _resolve_gid_token(gid_or_url)
        if not gid or not token:
            yield event.plain_result(
                "请提供有效的 Gallery ID 或 URL。\n用法：/exhentai download <gid/token 或完整URL>"
            )
            return

        download_key = f"{gid}_{token}"
        if download_key not in self._download_locks:
            self._download_locks[download_key] = asyncio.Lock()

        if self._download_locks[download_key].locked():
            yield event.plain_result(f"#{gid} 正在下载中，请等待完成。")
            return

        queue_token = None
        slot_acquired = False
        async with self._download_locks[download_key]:
            try:
                accepted, queue_token, queue_msg = await self._reserve_download_slot()
                if not accepted:
                    yield event.plain_result(queue_msg)
                    return
                if queue_msg:
                    yield event.plain_result(queue_msg)
                    await self._wait_for_download_turn(queue_token)
                    queue_token = None
                    slot_acquired = True
                    yield event.plain_result(f"排队完成，开始下载 #{gid}。")
                else:
                    slot_acquired = True

                async for result in self._do_download(event, gid, token):
                    yield result
                return
            finally:
                if queue_token is not None:
                    await self._cancel_download_waiter(queue_token)
                if slot_acquired:
                    await self._release_download_slot()

    async def _do_download(self, event: AstrMessageEvent, gid: int, token: str):
        cookies = self._get_cookies()
        session = await self._build_client_session()
        try:
            yield event.plain_result(f"正在获取 #{gid} 的信息...")
            site_mode = self.config.get("site_mode", "exhentai")
            gallery = await get_gallery_metadata(session, cookies, gid, token)
            if not gallery:
                yield event.plain_result(f"无法获取 #{gid} 的信息。")
                return

            if _is_qq_platform(event):
                yield event.chain_result([
                    _build_gallery_info_forward_nodes(
                        event,
                        gallery,
                        "正在获取图片列表...",
                        include_cover=self.config.get("cover_preview", True),
                    )
                ])
            else:
                if self.config.get("cover_preview", True) and gallery.thumb_url:
                    try:
                        yield event.image_result(gallery.thumb_url)
                    except Exception as e:
                        self._debug_log(f"Cover preview failed: {e}")

                yield event.plain_result(
                    f"{gallery.format_info()}\n\n正在获取图片列表..."
                )

            page_urls = await get_page_urls(session, cookies, gid, token, site_mode)
            if not page_urls:
                yield event.plain_result("无法获取图片列表，请检查登录状态。")
                return

            total = len(page_urls)
            concurrency = self._get_int_config("download_concurrency", 3, 1, 10)
            retry_count = self._get_int_config("retry_count", 3, 1, 10)
            timeout = self._get_int_config("timeout", 30, 5, 300)
            yield event.plain_result(f"共 {total} 页，开始下载 (并发: {concurrency})...")

            temp_dir = self._get_temp_dir(gid)
            last_progress = {"t": 0}

            async def progress_callback(completed_num: int, total_num: int, failed_num: int):
                now = time.time()
                if now - last_progress["t"] >= 3:
                    last_progress["t"] = now
                    msg = f"下载进度: {completed_num}/{total_num}"
                    if failed_num > 0:
                        msg += f" (失败: {failed_num})"
                    try:
                        await event.send(msg)
                    except Exception:
                        pass

            downloaded = await download_images(
                session, page_urls, temp_dir, cookies,
                concurrency=concurrency,
                retry_count=retry_count,
                timeout=timeout,
                progress_callback=progress_callback,
            )

            if not downloaded:
                yield event.plain_result("下载失败，没有成功下载任何图片。")
                if self.config.get("auto_cleanup", True):
                    self._cleanup_temp_dir(gid)
                return

            if not _is_qq_platform(event):
                yield event.plain_result(
                    f"下载完成: {len(downloaded)}/{total} 页。正在打包..."
                )

            pack_format = self.config.get("pack_format", "zip")
            password = self.config.get("pack_password", "") or None
            if pack_format == "pdf" and _contains_webm(downloaded):
                pack_format = "zip"
                fallback_msg = "检测到动图/视频页面，PDF 不支持，已自动改用 ZIP 打包以保留完整内容。"
                if not _is_qq_platform(event):
                    yield event.plain_result(fallback_msg)
            archive_name = f"{gallery.gid}_{_safe_filename(gallery.title)}.{pack_format}"
            archive_path = os.path.join(self._get_data_dir(), archive_name)

            if pack_format == "zip":
                pack_zip(downloaded, archive_path, password)
            else:
                pack_pdf(downloaded, archive_path, password)

            file_size_mb = round(os.path.getsize(archive_path) / (1024 * 1024), 2)
            extra = ""
            if password and pack_format in {"zip", "pdf"}:
                extra = " (已加密)"

            if not _is_qq_platform(event):
                yield event.plain_result(f"打包完成 ({file_size_mb} MB){extra}")

            chain = [
                File(file=archive_path, name=os.path.basename(archive_path)),
            ]

            if self.config.get("auto_revoke", False):
                umo = event.unified_msg_origin
                result = await self.context.send_message(umo, chain)
                await asyncio.sleep(2)
                await self._try_revoke_message(event, umo, result)
            else:
                yield event.chain_result(chain)

            if self.config.get("auto_cleanup", True):
                self._cleanup_temp_dir(gid)
                if os.path.exists(archive_path):
                    os.remove(archive_path)

        except Exception as e:
            logger.error(f"Download error: {e}")
            yield event.plain_result(f"下载过程出错：{e}")
            if self.config.get("auto_cleanup", True):
                self._cleanup_temp_dir(gid)
        finally:
            await session.close()
            if session in self._active_sessions:
                self._active_sessions.remove(session)

    async def _try_revoke_message(
        self, event: AstrMessageEvent, umo: str, send_result=None
    ):
        try:
            platform_name = event.get_platform_name()
            if platform_name != "aiocqhttp":
                return
            message_id = None
            if send_result and hasattr(send_result, "message_id"):
                message_id = send_result.message_id
            elif hasattr(send_result, "get"):
                message_id = send_result.get("message_id")
            elif isinstance(send_result, (list, tuple)):
                for item in send_result:
                    if hasattr(item, "message_id"):
                        message_id = item.message_id
                        break
                    if hasattr(item, "get"):
                        message_id = item.get("message_id")
                        if message_id:
                            break
            if not message_id:
                self._debug_log("Auto-revoke skipped: sent message_id not found")
                return

            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if isinstance(event, AiocqhttpMessageEvent):
                client = event.bot
                if client and message_id:
                    await client.api.call_action("delete_msg", message_id=message_id)
                    self._debug_log(f"Revoked message {message_id}")
        except ImportError:
            self._debug_log("aiocqhttp platform adapter not available")
        except Exception as e:
            self._debug_log(f"Auto-revoke failed: {e}")

    async def terminate(self):
        data_dir = self._get_data_dir()
        temp_dir = os.path.join(data_dir, "temp")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        for s in self._active_sessions:
            try:
                await s.close()
            except Exception:
                pass
        self._active_sessions.clear()


def _safe_filename(title: str, max_len: int = 50) -> str:
    safe = "".join(c for c in title if c.isalnum() or c in "._- ")
    safe = safe.strip().replace(" ", "_")
    if len(safe) > max_len:
        safe = safe[:max_len]
    return safe or "gallery"


def _contains_webm(paths: list[str]) -> bool:
    return any(os.path.splitext(path)[1].lower() == ".webm" for path in paths)


def _is_qq_platform(event: AstrMessageEvent) -> bool:
    try:
        return event.get_platform_name() == "aiocqhttp"
    except Exception:
        return False


def _get_forward_bot_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_self_id())
    except Exception:
        return "0"


def _build_gallery_info_forward_nodes(
    event: AstrMessageEvent,
    gallery,
    footer: str = "",
    include_cover: bool = True,
) -> Nodes:
    bot_id = _get_forward_bot_id(event)
    nodes = []
    if include_cover and gallery.thumb_url:
        try:
            nodes.append(
                Node(
                    uin=bot_id,
                    name="封面",
                    content=[Image.fromURL(gallery.thumb_url)],
                )
            )
        except Exception:
            pass

    info_text = gallery.format_info()
    if footer:
        info_text += f"\n\n{footer}"
    nodes.append(
        Node(
            uin=bot_id,
            name="本子信息",
            content=[Plain(info_text)],
        )
    )
    return Nodes(nodes)


def _build_search_forward_nodes(
    event: AstrMessageEvent,
    keyword: str,
    galleries,
    include_covers: bool = False,
) -> Nodes:
    bot_id = _get_forward_bot_id(event)

    nodes = [
        Node(
            uin=bot_id,
            name="ExHentai 搜索",
            content=[Plain(f"搜索 '{keyword}' 结果，共 {len(galleries)} 个。")],
        )
    ]
    for index, gallery in enumerate(galleries[:10], 1):
        content = []
        if include_covers and gallery.thumb_url:
            try:
                content.append(Image.fromURL(gallery.thumb_url))
            except Exception:
                pass
        content.append(
            Plain(
                f"{index}. {gallery.title}\n"
                f"ID: {gallery.gid}/{gallery.token}\n"
                f"分类: {gallery.category or '-'}\n"
                f"页数: {gallery.filecount}P\n"
                f"大小: {gallery.filesize_mb} MB\n"
                f"评分: {gallery.rating:.1f}\n"
                f"查看详情: /exhentai info {gallery.gid}/{gallery.token}\n"
                f"下载: /exhentai download {gallery.gid}/{gallery.token}"
            )
        )
        nodes.append(
            Node(
                uin=bot_id,
                name=f"结果 {index}",
                content=content,
            )
        )

    if len(galleries) > 10:
        nodes.append(
            Node(
                uin=bot_id,
                name="更多结果",
                content=[Plain(f"还有 {len(galleries) - 10} 个结果未展示，请使用下一页继续搜索。")],
            )
        )
    return Nodes(nodes)


def _get_command_tail(event: AstrMessageEvent, *tokens: str) -> str:
    message = str(getattr(event, "message_str", "") or "")
    text = message.strip()
    for token in tokens:
        pattern = rf"^/?{re.escape(token)}(?:\s+|$)"
        text = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()
    return text


def _parse_search_args(raw_text: str) -> tuple[str, int]:
    text = (raw_text or "").strip()
    if not text:
        return "", 0

    named_page = re.search(r"(?:^|[\s,;])page\s*=\s*(-?\d+)(?=$|[\s,;])", text)
    if named_page:
        page = max(0, int(named_page.group(1)))
        keyword_part = re.sub(
            r"(?:^|[\s,;])page\s*=\s*-?\d+(?=$|[\s,;])",
            " ",
            text,
            count=1,
        ).strip(" ,;")
    else:
        page = 0
        keyword_part = text

    if keyword_part.lower().startswith("keyword="):
        keyword_part = keyword_part.split("=", 1)[1].strip(" ,;")

    if not named_page:
        parts = keyword_part.rsplit(maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"-?\d+", parts[1]):
            keyword_part = parts[0]
            page = max(0, int(parts[1]))

    return keyword_part.strip(), page
