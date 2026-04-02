# astrbot_plugin_Xagent_searcher

<p align="center">
  <b>🐦 AstrBot 推特 (X) 数据集成插件</b><br>
  基于 X API v2 的高性能异步推特解析工具集，支持搜索、趋势、时间线、链接自动解析与媒体处理。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v0.0.4-blue?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/license-AGPL--v3-orange?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/Python-3.10%2B-yellow?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D4.0.0-green?style=flat-square" alt="astrbot">
</p>

---

## 功能概览

| 功能 | 指令 | 说明 |
|------|------|------|
| 关键词搜索 | `/xsearch <关键词>` | 搜索推特最新推文，返回带缩略图的列表 |
| 地区热点趋势 | `/xtrend [地区]` | 获取指定地区的推特热门趋势 |
| 用户时间线 | `/xtl <用户名>` | 获取指定用户的最新推文列表 |
| 推文解析 | `/xparse <序号>` | 解析列表中指定序号的推文完整详情（含全部媒体） |
| 翻页 | `/xnext` | 加载下一页推文/趋势 |
| 缩略图重试 | `/xretry <序号>` | 重新获取因超时而未能显示的缩略图 |
| 链接自动解析 | 直接发送推文链接 | 自动检测聊天中的推文 URL 并解析完整内容 |

---

## 快速开始

### 1. 安装

将本插件文件夹放置于 AstrBot 的 `data/plugins/` 目录下，或通过 AstrBot 插件管理安装。

### 2. 依赖

插件会自动安装以下核心依赖：

```
httpx[http2]>=0.26.0    # 异步 HTTP 客户端（支持 HTTP/2 与代理隧道）
Pillow>=10.1.0          # 图片压缩与缩略图生成
pydantic>=2.4.0         # API 响应数据验证
```

### 3. 配置

在 AstrBot WebUI 的插件配置页面中填写以下必要项：

| 配置项 | 必填 | 说明 |
|--------|------|------|
| **API Bearer Token** | ✅ | X API v2 的 Bearer Token（App-Only 认证） |
| Consumer Key / Secret | 可选 | OAuth 1.0a 用户上下文认证，拥有更高权限 |
| Access Token / Secret | 可选 | 与 Consumer Key 配合使用 |
| **Cookie 降级 - auth_token** | 可选 | API 额度耗尽时的降级认证，见下方获取说明 |
| **Cookie 降级 - ct0** | 可选 | 与 auth_token 配合使用的 CSRF 令牌，二者缺一不可 |
| **GraphQL queryId** | 可选 | Cookie 降级单条推文查询专用，留空使用内置默认值；queryId 失效时在此覆写 |
| 代理地址 | 推荐 | 默认 `http://127.0.0.1:7890`，中国大陆用户必须配置 |

> **认证优先级**：OAuth 1.0a（全部4个凭据） > Bearer Token > Cookie 降级（GraphQL / v1.1 内部 API）

  **获取 API Token 参见**：X API 官方文档：https://docs.x.com/overview

### Cookie 降级认证配置说明

当 API 额度耗尽（402）或权限不足（403）时，插件会自动切换到 Twitter v1.1 内部 API 通道，
此时需要提供浏览器登录后的两个 Cookie 值：

**获取步骤：**
1. 用浏览器登录 [twitter.com](https://twitter.com)（确保已登录状态）
2. 按 `F12` 打开开发者工具
3. 切换到 **Application**（应用程序）选项卡
4. 左侧展开 **Cookies** → 点击 `https://twitter.com`
5. 在 Cookie 列表中找到以下两个条目：
   - `auth_token`：登录凭证，复制「值」列的内容
   - `ct0`：CSRF 防护令牌，复制「值」列的内容
6. 将两个值分别填入 WebUI 对应配置项

> ⚠️ **注意事项：**
> - `auth_token` 与账号密码等效，**请勿泄露**
> - `ct0` 会定期轮换，若降级认证出现 401/403 时请重新获取
> - 两个值必须同时填写，缺一不可
> - Cookie 降级下**单条推文查询**（推文链接自动解析、`/xparse` 解析推文条目）使用 **Twitter GraphQL API**（`TweetResultByRestId`），与 Twitter Web 客户端行为一致
> - 搜索、时间线、用户查询继续使用 Twitter **v1.1 内部 API**
> - 若推文链接解析突然失败并日志出现 `GraphQL 推文查询错误响应 [404]`，说明 Twitter 前端已更新 queryId，请在 WebUI「GraphQL queryId」字段填入新值（获取方法：浏览器登录 twitter.com → F12 → Network → 过滤 `TweetResultByRestId` 请求 → 查看请求路径中的 ID 段）
---

## 指令详细说明

### `/xsearch <关键词>` — 推文搜索

搜索推特上与关键词匹配的最新推文，返回带有缩略图的推文摘要列表。

```
/xsearch 猫咪
/推特搜索 AI
```

返回格式示例：
```
[缩略图] 1. @username (显示名)
   📝 推文内容摘要...
   ⏰ 2026-03-07 12:00:00 | ❤️42 🔄10
   📎 完整推文包含：2张图片，使用 /xparse 1 获取完整推文。

💡 第 1-3 条。使用 /xparse <序号> 解析指定推文。 使用 /xnext 查看更多。
```

### `/xtrend [地区]` — 热点趋势

获取指定地区的推特热门趋势话题。

```
/xtrend jp        # 日本趋势
/xtrend us        # 美国趋势
/xtrend 韩国      # 韩国趋势
/xtrend           # 全球趋势（默认）
```

**（暂时）支持的地区与别名：**

| 地区 | 可用别名 |
|------|----------|
| 全球 | `全球` `global` `world` `worldwide` |
| 日本 | `日本` `jp` `japan` |
| 美国 | `美国` `us` `usa` `america` |
| 韩国 | `韩国` `kr` `korea` |

> 不支持的地区会返回提示信息，不会发起 API 请求。

获取趋势列表后，可使用 `/xparse <序号>` 搜索指定趋势条目的相关推文。

### `/xtl <用户名>` — 用户时间线

拉取指定推特用户的最新推文，支持用户名和数字用户 ID。

```
/xtl elonmusk     # 通过用户名查询
/xtl @NASA        # 带 @ 前缀也可以
/xtl 44196397     # 通过数字用户ID查询
/推特时间线 username
```

### `/xparse <序号>` — 推文详情解析

解析缓存列表中指定序号的条目。行为因条目类型而异：

- **推文条目**：获取推文完整详情，包含全部图片、视频、GIF 及互动指标。
- **趋势条目**：自动以趋势话题名称执行搜索，返回相关推文列表（附缩略图）。

```
/xparse 2         # 解析第 2 条
/推特解析 1
```

### `/xnext` — 翻页

加载下一批推文/趋势。翻页编号连续计数，不与前页重复。

```
/xnext
/下一页
```

翻页机制会优先消费本地缓冲区中的条目，缓冲区耗尽后自动通过 API cursor 拉取下一批数据。

### `/xretry <序号>` — 缩略图重试

当推文缩略图因网络超时而发送失败（自动降级为纯文本）时，可使用此指令重新获取缩略图。

```
/xretry 3         # 重试第 3 条的缩略图
/重试缩略图 1
```

### 自动链接解析

在聊天中直接发送推文链接，插件会自动检测并解析，无需指令前缀。

支持的链接格式：
```
https://x.com/username/status/1234567890
https://twitter.com/username/status/1234567890
```

---

## WebUI 配置项一览

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| API Bearer Token | string | `""` | X API v2 Bearer Token |
| Consumer Key | string | `""` | OAuth 1.0a Consumer Key |
| Consumer Secret | string | `""` | OAuth 1.0a Consumer Secret |
| Access Token | string | `""` | OAuth 1.0a Access Token |
| Access Token Secret | string | `""` | OAuth 1.0a Access Token Secret |
| 降级备用 Cookie - auth_token | text | `""` | 登录凭证 Cookie，与 ct0 配对使用 |
| 降级备用 Cookie - ct0 | text | `""` | CSRF 防护令牌，与 auth_token 配对使用 |
| GraphQL 推文查询 ID | string | `""` | Cookie 降级单条推文查询 queryId，留空使用内置默认值 |
| 启用网络代理 | bool | `true` | 所有请求是否走代理 |
| 代理地址 | string | `http://127.0.0.1:7890` | HTTP 代理地址 |
| 最大返回条数 | int | `10` | 单次 API 拉取最大数量（10-20） |
| 每页显示条数 | int | `3` | 列表每页显示数量（1-10） |
| 显示互动指标 | bool | `true` | 是否展示点赞/转发/回复数 |
| 大文件拦截阈值 | int | `25` MB | 超限文件仅输出直链不下载 |
| 图片压缩目标 | int | `2048` KB | PIL 压缩目标体积 |
| 展示媒体详情 | bool | `true` | 向智能体展示媒体类型/尺寸 |
| 访问控制模式 | enum | `Off` | Off / Whitelist / Blacklist |
| 白名单 | list | `[]` | 允许访问的会话 UMO |
| 黑名单 | list | `[]` | 禁止访问的会话 UMO |
| 缓存清理天数 | int | `3` | 媒体缓存自动清理周期 |
| 群组缓存隔离 | bool | `false` | 群组内成员独立缓存 |
| 最大翻页累积 | int | `50` | 翻页允许累积的最大条目数 |

---

## 架构设计

### 项目结构

```
astrbot_plugin_Xagent_searcher/
├── main.py                      # 插件入口与指令路由
├── _conf_schema.json            # WebUI 配置 schema
├── metadata.yaml                # 插件元数据
├── requirements.txt             # 依赖清单
├── core/
│   ├── x_api_client.py          # X API v2 异步 HTTP 客户端
│   ├── media_processor.py       # 媒体处理管线
│   └── security_acl.py          # 黑白名单访问控制中间件
└── models/
    └── x_response_models.py     # Pydantic v2 数据模型
```

### 核心模块职责

#### `main.py` — 指令路由与业务编排

- 注册所有正则表达式指令（`/xsearch`、`/xtrend`、`/xtl`、`/xparse`、`/xnext`、`/xretry`）
- 管理分页缓存（`PagedCache`），支持多会话隔离与群组成员独立缓存
- 实现逐条消息发送策略（**方案B**）：优先发送图文，超时自动降级为纯文本重试
- 推文链接自动检测（正则匹配 `x.com`/`twitter.com` 域名的推文 URL）

#### `x_api_client.py` — 异步 API 客户端

- 基于 `httpx.AsyncClient` 实现全异步 HTTP 通信（HTTP/2 + 连接池 + 代理隧道）
- **OAuth 1.0a HMAC-SHA1 签名**：完整实现 RFC 5849 规范的签名流程（参数收集 → 排序 → 签名基础字符串 → HMAC-SHA1 → Base64 编码）
- **三级认证降级策略**：OAuth 1.0a → Bearer Token → Cookie 降级（Twitter v1.1 内部 API）
- 封装 5 个核心 API 端点：推文获取、搜索、用户查询、时间线、热点趋势
- 延迟初始化模式：HTTP 客户端在首次请求时创建，避免在 `__init__` 中执行网络操作

#### `media_processor.py` — 媒体处理管线

- **变体筛选**：从视频/GIF 多变体中择优出最高码率的 MP4（排除 HLS 流媒体）
- **大文件熔断**：HEAD 预检请求探测文件体积，超限则拦截并输出直链降级文本
- **流式下载**：httpx 异步流式下载，附带双层体积守卫（预检 + 实时）
- **PIL 压缩**：二分搜索算法逼近目标体积，CPU 密集型操作通过 `asyncio.to_thread()` 委派线程池
- **缩略图生成**：为推文列表生成低分辨率 JPEG 预览图

#### `security_acl.py` — 访问控制中间件

- 三模式支持：Off（关闭）、Whitelist（白名单）、Blacklist（黑名单）
- 基于 AstrBot 的 `unified_msg_origin`（UMO）进行会话级匹配
- 白名单为空时不做限制（与 AstrBot 生态插件行为一致）

#### `x_response_models.py` — Pydantic 数据模型

- 全部基于 Pydantic v2 的 `BaseModel`，启用 `extra="ignore"` 防御 API 字段变动
- 覆盖推文、用户、媒体（含多变体）、趋势、速率限制等完整数据结构
- 支持关系型数据水合（通过 `media_key` 在 `includes` 中关联媒体实体）

### 关键实现逻辑

#### 分页缓存机制

每次搜索/趋势/时间线拉取会创建一个 `PagedCache` 实例，缓存会话状态：

1. **即时清理语义**：新搜索/趋势/时间线请求会覆盖旧缓存，翻页和解析始终跟随最新列表
2. **缓冲区策略**：API 拉取 `max_return_count` 条 → 显示 `fetch_count` 条 → 剩余存入缓冲区 → `/xnext` 优先消费缓冲区 → 缓冲区耗尽后通过 API cursor 拉取下一批
3. **群组隔离**：开启后缓存 key 附加 `sender_id`，群组内成员拥有独立的列表和翻页状态

#### 消息发送降级策略（方案B）

为应对 NapCat/NTQQ 消息平台的多图消息超时问题（retcode=1200）：

1. 每条推文独立发送（1 张缩略图 + 文本摘要）
2. 发送失败时自动降级为纯文本重试
3. 降级后在文本末尾追加 `/xretry <序号>` 提示，用户可手动重试缩略图

#### 推文链接正则匹配

```python
TWEET_URL_PATTERN = re.compile(
    r'https?://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d+)'
)
```

监听所有消息事件，匹配到推文链接时自动触发解析，无需指令前缀。以 `/` 开头的消息会被跳过，避免与指令处理器冲突。

#### 速率限制断路器

从 X API 响应头提取 `x-rate-limit-remaining` 和 `x-rate-limit-reset`：

- 剩余请求 < 5 时激活断路器，拒绝后续请求
- 当前时间超过 reset 时间戳后自动解除断路器

---

## 访问控制（ACL）

插件支持基于会话级别的黑白名单访问控制。

### 获取会话 ID

在聊天中发送 `sid` 命令即可获取当前会话的 UMO（Unified Message Origin）。

### 配置方式

在 WebUI 中设置：

1. **访问控制模式**：选择 `Off`（关闭）、`Whitelist`（白名单）或 `Blacklist`（黑名单）
2. **白名单 / 黑名单**：填入会话 UMO 字符串

---

## 常见问题

### Q: 代理配置不生效？

确保代理地址格式正确（如 `http://127.0.0.1:7890`），且代理服务正在运行。插件所有的 API 请求和媒体下载都通过该代理。

### Q: 搜索/时间线无结果？

- 检查 Bearer Token 是否正确配置
- 确认 X Developer Portal 中的 App 权限（至少需要 Read 权限）
- 查看 AstrBot 日志中是否有 API 错误信息

### Q: 图片/视频发送失败？

- NapCat/NTQQ 平台存在多图消息超时问题，插件已内置降级策略
- 使用 `/xretry <序号>` 重试失败的缩略图
- 检查大文件拦截阈值设置，过大的文件会被拦截并输出直链

### Q: 趋势查询显示不支持的地区？

目前仅支持全球、日本、美国、韩国四个预设地区。其他地区的 WOEID 暂不提供。

### Q: /xsearch搜索效果不理想？

搜索结果与推特网页版「热门」标签存在差异是 **X API 的平台级限制**，并非插件 bug，主要原因有以下三点：

1. **`relevancy` 算法与网页端「热门」本质不同**
   网页端「热门」使用的是专属机器学习排序模型，综合考量互动率、账号权重、扩散链路等复杂信号。而 X API 的 `sort_order=relevancy` 是基于简单相关度评分的轻量算法，在 pay-per-use 计费层级下，社区反馈其实际表现常与 `recency`（时间倒序）高度相近。

2. **搜索候选池仅限最近 7 天**
   `/xsearch` 使用的 `search/recent` 端点有 **7 天时间窗口的硬限制**，超过一周的热门帖子永远不会出现在结果中。网页端「热门」无此限制，许多破圈大帖传播周期较长，因此两者结果差异明显。

3. **`relevancy` 模式禁用多轮分页**
   由于 X API 的 `next_token` 分页在 `relevancy` 模式下是按时间倒序推进的（越翻越旧），继续分页只会带来更旧的推文，与"热门"语义相悖。因此该模式下插件仅执行单次请求，不进行多轮补取。

**建议**：若希望获取更符合预期的热门内容，可在 WebUI 中适当调大「单次 API 请求最大推文数」（如设为 50-100），扩大候选池后 `relevancy` 算法有更多内容可筛选。

---

## 致谢

- [AstrBot](https://github.com/Soulter/AstrBot) — 多平台 LLM 聊天机器人框架
- [X API v2](https://docs.x.com/x-api/) — Twitter/X 官方 API
- [httpx](https://www.python-httpx.org/) — 现代异步 HTTP 客户端
- [Pydantic v2](https://docs.pydantic.dev/) — 数据验证框架
- [Pillow](https://pillow.readthedocs.io/) — Python 图像处理库
- [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser) - 为本项目提供的解析方案思路
- [astrbot_plugin_pixiv_reborn](https://github.com/vmoranv-reborn/astrbot_plugin_pixiv_reborn) - 为本项目提供缓存与转发思路

---
## 未来

本项目为测试项目，旨在能够在QQNT消息平台模拟推特的浏览模式。原本是想做成平台可以接入更多网页，碍于水平低下无力完成。或许在以后的更新或是新项目中能实现目标。本项目由gemini 3.1 pro大模型提供基础开发框架与撰写开发规范，代码部分95%由Claude Opus 4.6完成编写，剩下5%的是部分文案与promot由人工编写。总而言之，我和Claude真厉害就对了。
至于项目名中的agent，大概后面会通过函数工具实现到agent里面，可以通过自然语言让智能体稳定调用。
如有需要的功能更新或bug修复，请提交issue，我一定会看，不一定能做成，但是一定会尽力完成。


---
## 许可证

本项目采用 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 许可证分发。由于本插件涉及 X (Twitter) 等商业平台的 API 接口集成，选择 AGPL v3 以确保所有衍生作品（包括网络服务部署）均保持开源。
