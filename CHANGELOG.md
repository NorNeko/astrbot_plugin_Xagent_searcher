# Changelog

本文件记录 `astrbot_plugin_Xagent_searcher` 插件的所有版本变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

----

## [0.0.4] - 2026-04-03

### Fixed

- **Cookie 单条推文查询失效**：`statuses/show.json`（Twitter v1.1 REST 端点）在 Cookie 认证模式下已停止响应，导致直接发送推文链接时始终返回"推文不存在"。根因为 Twitter Web 客户端早于 2022 年全面迁移至 GraphQL API，v1.1 show 端点对 Cookie 鉴权不再生效，而搜索与时间线 v1.1 端点仍正常工作。

### Changed

- **单条推文查询路由升级至 GraphQL `TweetResultByRestId`**：Cookie 降级模式下，`/xparse` 与推文链接自动解析的单条查询请求现改用 `twitter.com/i/api/graphql/{queryId}/TweetResultByRestId`，与 Twitter Web 客户端行为完全一致。搜索、时间线、用户查询端点保持 v1.1 不变。
- **新增 `graphql_tweet_query_id` WebUI 配置项**：内置默认 queryId，当 Twitter 前端更新导致 queryId 变更时，用户可在 WebUI 中自行覆写，无需等待插件版本更新。获取方法：浏览器 F12 → Network → 过滤 `TweetResultByRestId` 请求查看路径中的 ID 段。
- **增强 v1.1 错误诊断日志**：v1.1 端点返回 4xx 时，现将完整响应 Body 记录至 `WARNING` 日志，便于区分"推文真实不存在"与"端点失效"等场景。
- **`TweetUnavailable` 专项处理**：GraphQL 响应中 `__typename == "TweetUnavailable"` 时直接抛出 `FileNotFoundError`，用户看到明确的"推文不可访问或已删除"提示而非通用错误。

### Compatibility

- 本次更新对上层指令与调用接口无破坏性变更，Cookie 模式下搜索、时间线、用户查询、趋势等功能不受影响。

---

## [0.0.3] - 2026-03-12

### Fixed

- 修复翻页功能（`/xnext`）在启用 `min_faves` 点赞数过滤时，搜索类列表可能提前报「已到最后一页」的问题。根因为 `_fetch_more_items` 的 search 分支仅执行单轮 API 调用，未复刻 v0.0.2 引入的多轮补齐循环。

### Changed

- 提取共用私有方法 `_search_with_filter`，封装多轮 API 循环 + 客户端 `min_faves` 过滤逻辑。`/xsearch`、`/xparse`（趋势分支）、`/xnext`（search 翻页）三处统一调用，消除重复实现，确保过滤行为完全一致。

### Compatibility

- 本次更新兼容原有所有功能，配置项变更仅影响 WebUI 展示与过滤逻辑。

---

## [0.0.2] - 2026-03-09

### Changed

- 优化关键词搜索与趋势搜索：新增客户端最小点赞数（min_faves）过滤，支持多轮 API 请求自动补齐，最大轮次与过滤阈值均可在 WebUI 配置。
- 修复 WebUI 配置项 `max_return_count` 校验漏洞，改为 slider 控件，严格限制单次请求最大推文数（10-100）。
- 新增 `search_min_faves`（点赞数过滤）与 `search_filter_max_rounds`（补齐轮次）配置项，均为 slider 控件，附性能与费用提示。
- 翻页功能支持 min_faves 过滤，确保所有结果均符合点赞阈值。

### Fixed

- 解决 API 端 min_faves 操作符导致 400 错误的问题，改为客户端本地过滤，兼容所有 API 计费等级。

### Compatibility

- 本次更新兼容原有所有功能，配置项变更仅影响 WebUI 展示与过滤逻辑。

---

## [0.0.1] - 2026-03-07

### Added

- **推文关键词搜索** (`/xsearch`)：基于 X API v2 `search/recent` 端点，支持关键词搜索最新推文
- **地区热点趋势** (`/xtrend`)：支持全球、日本、美国、韩国四个预设地区的热门趋势查询
- **用户时间线** (`/xtl`)：获取指定推特用户的最新推文列表，支持用户名和数字 ID
- **推文详情解析** (`/xparse`)：解析缓存列表中指定序号的推文完整详情（含全部媒体），趋势条目自动转搜索
- **翻页功能** (`/xnext`)：分页加载推文/趋势列表，连续编号不重复
- **缩略图重试** (`/xretry`)：重新获取因超时降级为纯文本的推文缩略图
- **推文链接自动解析**：自动检测聊天中的 `x.com` / `twitter.com` 推文 URL 并解析完整内容与媒体
- **OAuth 1.0a HMAC-SHA1 签名**：完整实现 RFC 5849 规范的 OAuth 1.0a 用户上下文认证
- **三级认证降级策略**：OAuth 1.0a → Bearer Token → Cookie 模拟
- **媒体处理管线**：变体筛选（最优 MP4）、大文件熔断拦截、PIL 二分搜索压缩、缩略图生成
- **消息发送降级策略（方案B）**：图文优先发送，超时自动降级为纯文本重试
- **分页缓存系统**：支持即时清理语义、缓冲区策略、API cursor 自动续取
- **群组缓存隔离**：可选开启群组内成员独立缓存与翻页状态
- **黑白名单访问控制** (`SecurityACL`)：基于 UMO 的三模式（Off / Whitelist / Blacklist）会话级权限管控
- **速率限制断路器**：自动从 API 响应头提取限流信息，触发阈值后断路保护，窗口重置后自动恢复
- **媒体缓存自动清理**：支持按天数自动清理过期的图片/缩略图/视频缓存
- **WebUI 完整配置**：通过 `_conf_schema.json` 提供 API 凭据、代理、ACL、媒体处理等全部参数的可视化配置
- **Pydantic v2 数据模型**：全部 API 响应经 Pydantic 强制验证，`extra="ignore"` 防御 API 字段变动
