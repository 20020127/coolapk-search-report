# coolapk-search-report

WorkBuddy 技能包：搜索酷安帖子、抓取正文与评论区，输出结构化的话题舆情 / 选题灵感 / 痛点归纳报告。

- **搜索**：免登录即可用，一次约 20 条。
- **帖子详情 + 评论区**：需要登录（酷安对未登录请求返回验证码拦截）。
- **登录**：浏览器一键复制 Cookie 粘贴即可，不需要抓包。

## 安装

把整个目录放到 WorkBuddy 的技能目录下即可，例如：

```
~/.workbuddy/skills/coolapk-search-report/
```

目录结构：

```
SKILL.md                 # 技能定义（AI 读这份）
scripts/coolapk.py       # 抓取 CLI
templates/report.md      # 报告骨架
references/api-notes.md  # 接口字段、错误码、登录排查
```

## 依赖

唯一第三方依赖是 `bcrypt`。

通常什么都不用装：脚本若发现当前 Python 缺少 `bcrypt`，会自动切换到本机已装好的解释器继续执行（stderr 打印一行 `AUTO_BOOTSTRAP`），不会改动你的环境，也不会自动安装包。

想消除这条提示就装一次：

```bash
python -m pip install --user bcrypt
```

## 登录（约 30 秒）

1. 浏览器打开 <https://www.coolapk.com/> 并登录。
2. 按 `F12` 打开控制台，执行：

   ```js
   copy(document.cookie)
   ```

3. 执行并把剪贴板内容粘贴进去回车：

   ```bash
   python scripts/coolapk.py login
   ```

脚本会自动挑出 `uid` / `username` / `token`（其余字段丢弃），存到 `~/.coolapk/config.json`，然后立即联网校验并回显登录用户。凭证无效时会明确告知，且不留下脏凭证。

其它方式：

| 方式 | 命令 |
| --- | --- |
| 非交互登录 | `python scripts/coolapk.py setcookie "uid=..;username=..;token=.."` |
| 查看登录状态 | `python scripts/coolapk.py check` |
| 退出登录 | `python scripts/coolapk.py logout` |
| 环境变量（不落盘） | `export COOLAPK_COOKIE="uid=..;token=.."` |

## 用法

```bash
python scripts/coolapk.py search "关键词"              # 搜索（免登录）
python scripts/coolapk.py search "关键词" --pages 3    # 翻 3 页
python scripts/coolapk.py feed <帖子ID> --replies 2    # 正文 + 评论（需登录）
```

命令输出一律为 UTF-8 JSON；失败时 JSON 走 stderr，退出码 1，错误码见 `references/api-notes.md`。

## 说明与免责

- 本项目仅抓取酷安**公开可见**内容，用于个人学习与舆情整理。请遵守酷安的 robots 与服务条款，**不要用于刷量、群发、骚扰或未经授权的数据获取**。
- 项目内的鉴权与设备码实现来自社区逆向研究，酷安接口变更或风控策略调整都可能导致失效，届时请自行评估后再使用。
- 技能本身只读，不具备发帖、回复、点赞、关注能力。
- 登录凭证只保存在本机，请勿提交到任何仓库或外传。

## 许可

代码仅供学习交流使用，使用者需自行承担合规责任。
