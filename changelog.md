# 更新日志

本文件记录 `astrbot_plugin_exhentai_downloader` 的重要版本变更。

## [1.3.1] - 2026-05-06

### 修复

- 修复 QQ / OneBot 合并转发中的搜索结果封面可能不显示或提示“图片已过期”的问题。
- 搜索结果、详情页和下载前展示的封面会先缓存为本地文件，再通过 `Image.fromFileSystem` 发送，避免依赖 E-Hentai / ExHentai 缩略图外链时效。

### 兼容性

- 不新增依赖。
- 现有 `search_result_covers` 和 `cover_preview` 配置行为保持不变，只改变 QQ / OneBot 平台封面的发送方式。

## [1.3.0] - 2026-05-06

### 新增

- 新增 `private_admin_only` 配置项，可单独限制私聊入口只允许 AstrBot 管理员使用。

## [1.2.0] - 2026-05-06

### 新增

- 新增 `user_blacklist` 配置项，黑名单用户无法触发任何插件命令。

## [1.1.0] - 2026-05-06

### 新增

- 新增 `search_result_covers` 配置项，可让搜索结果列表附带封面。

## [1.0.0] - 2026-05-06

### 新增

- 首次发布 ExHentai / E-Hentai 搜索、详情、下载、ZIP / PDF 打包与加密功能。
- 支持 Cookie / credential 登录、代理、管理员限制、群白名单、下载队列和自动清理。
