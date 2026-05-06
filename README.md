# astrbot_plugin_exhentai_downloader

AstrBot 插件：在聊天中搜索 ExHentai / E-Hentai 画廊，查看详情，下载图片并打包发送。

许可证：AGPL-3.0-only

## 功能

- 搜索画廊：支持 ExHentai / E-Hentai 关键词搜索。
- 查看详情：展示标题、分类、页数、大小、评分、标签、封面等信息。
- 下载画廊：按图片页 Referer 下载，支持并发、重试、超时和进度提示。
- 安全打包：支持 ZIP / PDF；设置密码后两种格式都会加密。
- 内容校验：下载阶段会拒绝 HTML / JSON / 文本错误页，避免把限流页打进压缩包。
- 多页抓取保护：分页抓取会去重并设置页数上限，避免异常分页导致卡死。
- 动图兼容：`.webm` 会保留正确扩展名；PDF 遇到 `.webm` 时会自动改用 ZIP。
- 认证方式：支持手动 Cookie 和账号密码自动登录，并会验证当前站点可用性。
- 权限控制：默认仅 AstrBot 管理员可用，支持群白名单。
- 平台支持：`aiocqhttp`、`telegram`、`discord`；文件消息能力取决于平台适配器。

## 安装

### 普通安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/RankoP-114/astrbot_plugin_exhentai_downloader
cd astrbot_plugin_exhentai_downloader
pip install -r requirements.txt
```

然后在 AstrBot WebUI 中重载插件。

### Docker 中的 AstrBot

如果 AstrBot 跑在 Docker 里，需要在容器内安装依赖，示例：

```bash
docker exec -it <astrbot_container> bash
cd /AstrBot/data/plugins/astrbot_plugin_exhentai_downloader
pip install -r requirements.txt
```

安装完成后，在 WebUI 中重载插件或按你的部署方式重启 AstrBot。

## 依赖

```text
aiohttp>=3.9
aiohttp-socks>=0.8
Pillow>=10.0
img2pdf>=0.5.1
beautifulsoup4>=4.12
lxml>=5.0
pyzipper>=0.3.6
pypdf[crypto]>=4.0.0
requests>=2.31
```

建议使用 Python 3.9 或更高版本。

## 配置

在 AstrBot WebUI 的插件配置页中设置：

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `auth_method` | 认证方式：`cookie` 或 `credential` | `cookie` |
| `exhentai_cookies` | 手动粘贴 Cookie，至少包含 `ipb_member_id`、`ipb_pass_hash`，ExHentai 通常还需要 `igneous` | 空 |
| `username` / `password` | `credential` 模式自动登录使用 | 空 |
| `site_mode` | 站点：`exhentai` 或 `ehentai` | `exhentai` |
| `proxy_type` | 代理类型：`none`、`http`、`socks5` | `none` |
| `proxy_host` / `proxy_port` | 代理地址和端口 | 空 / `0` |
| `proxy_username` / `proxy_password` | 代理认证，可选 | 空 |
| `download_concurrency` | 单个任务内的图片下载并发数，运行时限制为 `1-10` | `3` |
| `retry_count` | 单页下载重试次数，运行时限制为 `1-10` | `3` |
| `timeout` | 单页下载超时秒数，运行时限制为 `5-300` | `30` |
| `download_queue_enabled` | 启用下载排队；全局最多同时运行 1 个下载任务 | `true` |
| `max_download_queue_size` | 最大等待队列数，运行时限制为 `0-5` | `5` |
| `pack_format` | 打包格式：`zip` 或 `pdf` | `zip` |
| `pack_password` | ZIP / PDF 加密密码；留空则不加密 | 空 |
| `admin_only` | 仅 AstrBot 管理员可使用指令 | `true` |
| `group_whitelist` | 允许使用的群号列表；为空则不限制，私聊不受限制 | `[]` |
| `auto_cleanup` | 发送后删除本地临时文件和打包文件 | `true` |
| `auto_revoke` | 发送后自动撤回文件消息，仅 OneBot v11 / aiocqhttp | `false` |
| `cover_preview` | 下载或查看详情前发送封面预览 | `true` |
| `debug_mode` | 输出调试日志；日志会写入 AstrBot 日志系统 | `false` |

## 认证

推荐优先在 WebUI 中配置 Cookie。Cookie 格式示例：

```text
ipb_member_id=123456; ipb_pass_hash=abcdef123456; igneous=mysterystring
```

获取方式：

1. 在浏览器登录 `exhentai.org` 或 `e-hentai.org`。
2. 打开开发者工具，进入 `Application` / `Storage` / `Cookies`。
3. 复制 `ipb_member_id`、`ipb_pass_hash`，访问 ExHentai 时通常还要复制 `igneous`。
4. 填入插件配置中的 `exhentai_cookies`。

`credential` 模式会用用户名和密码登录论坛，再验证当前 `site_mode` 是否可访问；验证失败不会缓存 Cookie。

聊天命令 `/exhentai login <用户名> <密码>` 只允许私聊使用，避免账号密码出现在群消息里。手动 login 成功后会切换为 Cookie 模式并保存可用 Cookie。

## 命令

所有命令以 `/exhentai` 开头：

```text
/exhentai login <用户名> <密码>       私聊登录并保存 Cookie
/exhentai logout                    清除已保存 Cookie
/exhentai status                    查看站点、登录、打包、权限等状态
/exhentai search <关键词> [页码]     搜索画廊
/exhentai info <gid/token或URL>      查看画廊详情
/exhentai download <gid/token或URL>  下载并打包画廊
```

示例：

```text
/exhentai search artist:name 0
/exhentai info 123456/abcdef1234
/exhentai download https://exhentai.org/g/123456/abcdef1234/
```

## 下载和打包行为

- 下载前会校验登录状态；缓存 Cookie 也会按当前站点复验。
- 下载任务全局排队执行，最多同时运行 1 个下载任务；默认最多允许 5 个任务等待。
- 同一个画廊同时只会启动一个下载任务，重复请求会提示正在下载。
- 图片页 URL 会作为下载 Referer，降低直链下载被拒绝的概率。
- 下载到本地的旧文件会先校验文件头；无效文件会删除后重新下载。
- 下载响应如果是 HTML、文本或 JSON，会被视为失败，不会写入图片文件。
- QQ / OneBot v11 平台的搜索结果会用合并转发消息发送，每个条目一个转发节点。
- ZIP 设置密码时会使用 AES 加密；如果加密依赖缺失或加密失败，不会回退发送未加密 ZIP。
- PDF 设置密码时会使用 `pypdf` 加密；如果加密依赖缺失或加密失败，不会回退发送未加密 PDF。
- 打包密码不会在聊天完成提示里回显，请在 WebUI 配置中查看或提前告知接收者。
- PDF 只适合普通图片页面；如果画廊包含 `.webm`，插件会自动改用 ZIP。

## 权限和平台说明

- `admin_only` 默认为开启，非 AstrBot 管理员无法使用任何插件命令。
- `group_whitelist` 只限制群聊；私聊不受白名单限制。
- `auto_revoke` 目前只对 `aiocqhttp` / OneBot v11 平台执行。
- `File` 消息段在不同平台支持程度不同；如果平台不支持文件消息，下载可能成功但发送失败。

## 常见问题

### 显示未登录或搜索失败

确认 `site_mode` 和账号权限一致。`exhentai` 需要账号已获得 ExHentai 访问权限；只登录论坛或只能访问 E-Hentai 时，ExHentai 验证会失败。

### 下载页数不完整

插件会跟随画廊分页，但最多抓取 200 个画廊列表页以避免异常循环。超大画廊如果超过这个范围，需要调整代码里的 `MAX_GALLERY_LIST_PAGES`。

### PDF 变成 ZIP

画廊中包含 `.webm` 动图/视频页面时，PDF 无法完整表达内容，插件会自动改用 ZIP。

### 设置了密码但没看到密码

这是当前安全策略。ZIP / PDF 都只提示“已加密”，不会把密码发到同一个聊天窗口。

## 许可

本项目使用 GNU Affero General Public License v3.0，详见 [LICENSE](LICENSE)。
