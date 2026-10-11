# 接口与错误码说明

## 端点

| 用途 | 端点 | 登录要求 |
| --- | --- | --- |
| 搜索 | `/v6/search?type=&searchValue=&page=&showAnonymous=-1` | 游客可用 |
| 帖子详情 | `/v6/feed/detail?id=` | 需登录 |
| 评论列表 | `/v6/feed/replyList?id=&listType=lastupdate_desc&page=&discussMode=1&feedType=feed` | 需登录 |
| 登录态探测 | `/v6/account/checkLoginInfo` | 需带 Cookie |

- `type` 取值：`feed`（帖子，默认）/ `user` / `feedTopic` / `app`
- 搜索每页约 20 条；`--pages` 之间默认间隔 1.2s

## 错误码（stdout 为空，JSON 走 stderr，退出码 1）

| 错误码 | 含义 | 处置 |
| --- | --- | --- |
| `ERR_DEP` | 缺 bcrypt 依赖 | `pip install --user bcrypt` |
| `ERR_INPUT` | 参数缺失/非法、Cookie 解析失败 | 检查关键词、帖子 ID（纯数字）、Cookie 是否含 uid 与 token |
| `ERR_NOCOOKIE` | 接口返回 401/403，需要登录 | 引导用户执行 `login` |
| `ERR_CAPTCHA` | 被风控拦截（"需要验证码"/"设备异常"） | 凭证缺失或过期 → 重新 `login`；走降级报告 |
| `ERR_NETWORK` | 网络不可达/超时 | 重试一次，仍失败则终止 |
| `ERR_API` | 其它接口业务错误 | 原样转述 message，不猜测 |
| `ERR_UNKNOWN` | 未预期错误（已拦截，不暴露堆栈） | 报告给用户的只有错误码与一句话 |

## Cookie 归一化规则

`login` / `setcookie` 接受以下任一形式，自动提取 `uid`、`username`、`token`，其余字段丢弃：

- 整段浏览器 Cookie（`copy(document.cookie)` 的结果）
- `uid=123;username=abc;token=xxxx`
- 仅 `uid=123;token=xxxx`（username 留空）
- JSON：`{"uid":"123","username":"abc","token":"xxxx"}`

校验：`uid` 必须为纯数字，且 `token` 必须存在，否则报 `ERR_INPUT`。

## 登录排查表

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `ERR_INPUT` 缺 token | 网页版 Cookie 里没有 `token` 字段（未登录、或复制的是别的站点） | 确认浏览器地址栏是 `coolapk.com` 且已登录；仍不行改用 App 抓包 |
| `ERR_INPUT` 缺 uid | 同上 | 同上 |
| 提示「登录信息有误」 | Cookie 过期、复制时未登录、或复制了其他域的 Cookie | 重新登录网页版后再复制一次 |
| `check` 返回 `logged_in: null` | 探测接口临时不可用 | 用 `feed <真实帖子ID>` 实测：仍报 `ERR_CAPTCHA` 即视为失效 |
| 昨天能用今天不能 | Cookie 有时效 | 重走一次登录即可 |
| 控制台禁止粘贴 | Chrome/Edge 的防粘贴提示 | 手打 `copy(document.cookie)`（21 个字符），或先输入 `allow pasting` 回车 |

### 取 Cookie 的推荐命令

在酷安网页版登录后，F12 → Console 执行：

```js
copy(document.cookie)
```

如需先确认有没有关键字段，用这一行（会同时打印结果并复制）：

```js
(function(){var c=document.cookie;var g=function(k){var m=c.match(new RegExp('(^|;\\s*)'+k+'=([^;]*)'));return m?m[2]:''};var s='uid='+g('uid')+';username='+g('username')+';token='+g('token');copy(s);console.log(s)})()
```

## 凭证存储

- 路径：`~/.coolapk/config.json`，权限收紧为仅当前用户读写
- 环境变量 `COOLAPK_COOKIE` 优先级更高，且不落盘
- `logout` 删除该文件
- 设备码缓存于 `~/.coolapk/device.txt`，删除会换设备指纹，可能触发风控，**不要频繁删除**
