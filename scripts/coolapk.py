#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酷安社区搜索 / 帖子正文 / 评论区抓取 CLI  (v1.0.0)

能力：
  login      交互式登录（粘贴 Cookie，自动归一化 + 自动验证 + 保存）
  check      查看当前登录状态
  logout     清除本机保存的登录凭证
  search     按关键词搜索帖子（游客可用）
  feed       抓取帖子正文 + 评论区（需登录）

设计原则：
  - 命令输出一律为 UTF-8 JSON（stdout），错误一律走 stderr + 受控错误码，绝不裸露 traceback。
  - Cookie 归一化：粘贴整段浏览器 Cookie 即可，脚本自动挑出 uid/username/token，忽略无关字段。
  - 凭证只写本机 ~/.coolapk/config.json，权限收紧为仅当前用户可读写。

依赖：bcrypt（唯一第三方依赖）。缺失时给出明确安装指引，不静默失败、不伪造结果。
"""

import argparse
import base64
import hashlib
import html
import json
import os
import random
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- stdout 统一 UTF-8（Windows GBK 控制台下中文不炸）----
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import bcrypt
except ImportError:
    bcrypt = None


# ============================ 受控错误码 ============================
ERR_DEP = "ERR_DEP"          # 第三方依赖缺失
ERR_INPUT = "ERR_INPUT"      # 参数缺失或非法
ERR_NOCOOKIE = "ERR_NOCOOKIE"  # 该操作需要登录
ERR_CAPTCHA = "ERR_CAPTCHA"  # 被风控/验证码拦截
ERR_NETWORK = "ERR_NETWORK"  # 网络不可达/超时
ERR_API = "ERR_API"          # 接口返回业务错误
ERR_UNKNOWN = "ERR_UNKNOWN"  # 未预期错误（不暴露堆栈）

COOKIE_HINT = (
    "该操作需要酷安登录凭证。最简做法：浏览器打开 https://www.coolapk.com 登录后，"
    "按 F12 在控制台执行 copy(document.cookie)，再运行 "
    "python scripts/coolapk.py login 粘贴即可。"
)


def _bootstrap_bcrypt():
    """当前解释器缺 bcrypt 时，找一个已装好的解释器重新执行本脚本。

    只在 bcrypt 缺失时触发；用环境变量防止递归。找不到就返回 None，
    由调用方给出 ERR_DEP 指引——不自动安装、不修改用户环境。
    """
    if os.environ.get("_COOLAPK_BOOTSTRAPPED"):
        return None
    home = os.path.expanduser("~")
    base = os.path.join(home, ".workbuddy", "binaries", "python", "envs", "default")
    cands = []
    if os.name == "nt":
        cands.append(os.path.join(base, "Scripts", "python.exe"))
    else:
        cands.append(os.path.join(base, "bin", "python3"))
        cands.append(os.path.join(base, "bin", "python"))
    env = dict(os.environ)
    env["_COOLAPK_BOOTSTRAPPED"] = "1"
    me = os.path.abspath(__file__)
    here = os.path.abspath(sys.executable)
    for py in cands:
        if not os.path.exists(py) or os.path.abspath(py) == here:
            continue
        try:
            probe = subprocess.run([py, "-c", "import bcrypt"],
                                   capture_output=True, timeout=20, env=env)
            if probe.returncode != 0:
                continue
            print(json.dumps({"notice": "AUTO_BOOTSTRAP",
                              "message": "当前解释器缺少 bcrypt，已自动切换到 "
                                         + py}, ensure_ascii=False), file=sys.stderr)
            r = subprocess.run([py, me] + sys.argv[1:], env=env)
            sys.exit(r.returncode)
        except Exception:
            continue
    return None


def fail(code, message, hint=None):
    """统一失败出口：JSON 到 stderr，退出码 1。"""
    payload = {"error": code, "message": message}
    if hint:
        payload["hint"] = hint
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def ok(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0)


# ============================ X-App-Token V2 ============================
_V2_SALT_KEY = "dcf01e569c1e3db93a3d0fcf191a622c"
_BCRYPT_B64 = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_LOOKUP = {c: i for i, c in enumerate(_BCRYPT_B64)}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64_decode(s: str, n: int) -> bytes:
    res = bytearray()
    i = 0
    rem = n
    while rem > 0 and i + 1 < len(s):
        c1 = _LOOKUP.get(s[i], 0)
        c2 = _LOOKUP.get(s[i + 1], 0)
        i += 2
        res.append((c1 << 2) | ((c2 & 0x30) >> 4))
        rem -= 1
        if rem == 0 or i >= len(s):
            break
        c3 = _LOOKUP.get(s[i], 0)
        i += 1
        res.append(((c2 & 0x0F) << 4) | ((c3 & 0x3C) >> 2))
        rem -= 1
        if rem == 0 or i >= len(s):
            break
        c4 = _LOOKUP.get(s[i], 0)
        i += 1
        res.append(((c3 & 0x03) << 6) | c4)
        rem -= 1
    return bytes(res[:n])


def _b64_encode(data: bytes) -> str:
    res = []
    i = 0
    while i < len(data):
        b1 = data[i]
        b2 = data[i + 1] if i + 1 < len(data) else 0
        b3 = data[i + 2] if i + 2 < len(data) else 0
        res.append(_BCRYPT_B64[(b1 >> 2) & 0x3F])
        res.append(_BCRYPT_B64[((b1 & 0x03) << 4) | ((b2 >> 4) & 0x0F)])
        res.append(_BCRYPT_B64[((b2 & 0x0F) << 2) | ((b3 >> 6) & 0x03)])
        res.append(_BCRYPT_B64[b3 & 0x3F])
        i += 3
    return "".join(res)[:22]


def gen_token(device_code: str) -> str:
    if bcrypt is None:
        fail(ERR_DEP, "缺少依赖 bcrypt，无法生成鉴权 Token",
             hint="请先安装：pip install --user bcrypt （或 python -m pip install bcrypt）")
    ts = str(int(time.time()))
    b64_ts = _b64(ts)
    md5_ts = _md5(ts)
    md5_dev = _md5(device_code)
    token_str = (
        f"token://com.coolapk.market/{_V2_SALT_KEY}"
        f"?{md5_ts}${md5_dev}&com.coolapk.market"
    )
    b64_token = _b64(token_str)
    md5_b64_token = _md5(b64_token)
    md5_token_str = _md5(token_str)
    salt_chars = f"{b64_ts}/{md5_token_str}"[:24] + "u"
    salt_bytes = _b64_decode(salt_chars, 16)
    salt_b64 = _b64_encode(salt_bytes)
    bh = bcrypt.hashpw(
        md5_b64_token.encode("utf-8"), f"$2b$10${salt_b64}".encode("utf-8")
    ).decode("utf-8")
    bh = bh.replace("$2b$", "$2y$", 1)
    return "v2" + _b64(bh)


# ============================ 设备码 ============================
def _rand_hex(n: int) -> str:
    return "".join(random.choice("0123456789ABCDEF") for _ in range(n))


def _rand_mac() -> str:
    return ":".join(
        "".join(random.choice("0123456789abcdef") for _ in range(2)) for _ in range(6)
    )


def gen_device() -> str:
    raw = f"{_rand_hex(16)}; ; ; {_rand_mac()}; Xiaomi; Xiaomi; 23113RKC6C; AQ3A.250226.002; null"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")[::-1]


CONFIG_DIR = os.path.expanduser("~/.coolapk")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def load_device() -> str:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    p = os.path.join(CONFIG_DIR, "device.txt")
    if os.path.exists(p):
        try:
            d = open(p, encoding="utf-8").read().strip()
            if d:
                return d
        except Exception:
            pass
    d = gen_device()
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(d)
    except Exception:
        pass
    return d


def _chmod600(p: str) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


# ============================ 凭证（登录） ============================
_KEY_RE = re.compile(r"(uid|username|token)\s*=\s*([^;,\s]+)", re.I)
_COOKIE_NEED = ("uid", "token")  # username 可选


def parse_cookie(raw: str) -> dict:
    """把任意形式的 Cookie 输入归一化为 {uid, username, token}。

    容忍：整段浏览器 Cookie、带空格分隔、JSON、仅 uid+token 三选二。
    """
    if raw is None:
        raise ValueError("empty")
    s = raw.strip().strip('"').strip("'")
    if not s:
        raise ValueError("empty")

    if s.startswith("{"):
        try:
            d = json.loads(s)
            parts = []
            for k in ("uid", "username", "token"):
                v = d.get(k)
                if v:
                    parts.append(f"{k}={v}")
            if parts:
                s = ";".join(parts)
        except Exception:
            pass

    found = {}
    for m in _KEY_RE.finditer(s):
        k = m.group(1).lower()
        v = m.group(2).strip().rstrip(",")
        if k not in found:
            found[k] = v

    for k in _COOKIE_NEED:
        if not found.get(k):
            raise ValueError(f"missing:{k}")
    if not re.fullmatch(r"\d+", found["uid"]):
        raise ValueError("bad:uid")

    found.setdefault("username", "")
    return {"uid": found["uid"], "username": found["username"], "token": found["token"]}


def cookie_str(c: dict) -> str:
    return f"uid={c['uid']};username={c['username']};token={c['token']}"


def load_cred() -> dict:
    """读取本机凭证；不存在返回空 dict。环境变量 COOLAPK_COOKIE 优先。"""
    env = os.environ.get("COOLAPK_COOKIE", "").strip()
    if env:
        try:
            return parse_cookie(env)
        except Exception:
            pass
    if os.path.exists(CONFIG_PATH):
        try:
            d = json.load(open(CONFIG_PATH, encoding="utf-8"))
            if d.get("uid") and d.get("token"):
                return {"uid": str(d["uid"]), "username": d.get("username", ""),
                        "token": d["token"]}
        except Exception:
            pass
    return {}


def save_cred(c: dict) -> str:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = dict(c)
    data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _chmod600(CONFIG_PATH)
    return CONFIG_PATH


def clear_cred() -> bool:
    if os.path.exists(CONFIG_PATH):
        try:
            os.remove(CONFIG_PATH)
            return True
        except Exception:
            return False
    return False


# ============================ 文本清洗 ============================
_A_TAG = re.compile(r"<a\s[^>]*>(.*?)</a>", re.I | re.S)
_ANY_TAG = re.compile(r"<[^>]+>")


def clean_text(s: str) -> str:
    """去掉酷安正文里的 HTML 标签，保留话题等纯文本。"""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _A_TAG.sub(r"\1", s)     # 先保留链接内文本（如 #话题#）
    s = _ANY_TAG.sub("", s)      # 再清掉其余标签
    s = html.unescape(s)
    s = s.replace("\u200b", "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def ts_to_str(v) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(v)))
    except Exception:
        return ""


# ============================ HTTP ============================
APP_VERSION = "16.2.0"
APP_CODE = "2604201"
TIMEOUT = 20


def build_headers(device: str, cookie: str = "") -> dict:
    h = {
        "X-Sdk-Int": "35",
        "X-Sdk-Locale": "zh-CN",
        "X-App-Mode": "universal",
        "X-App-Channel": "coolapk",
        "X-App-Id": "com.coolapk.market",
        "X-App-Device": device,
        "X-App-Version": APP_VERSION,
        "X-App-Code": APP_CODE,
        "X-Api-Version": "16",
        "X-App-Supported": APP_CODE,
        "X-Dark-Mode": "0",
        "X-Requested-With": "XMLHttpRequest",
        "X-App-Token": gen_token(device),
        "User-Agent": (
            "Dalvik/2.1.0 (Linux; U; Android 16; 23113RKC6C Build/AQ3A.250226.002) "
            "(#Build; Redmi; 23113RKC6C; AQ3A.250226.002; HyperOS_3.0; 3.0.1.0) "
            f"+CoolMarket/{APP_VERSION}-{APP_CODE}-universal"
        ),
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def get(path: str, params: dict = None, cookie: str = ""):
    device = load_device()
    url = "https://api.coolapk.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=build_headers(device, cookie))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def unwrap(j):
    if isinstance(j, dict) and "data" not in j and j.get("message"):
        raise RuntimeError(str(j.get("message")))
    return (j or {}).get("data") if isinstance(j, dict) else None


# ============================ 业务 ============================
def search(keyword: str, stype: str = "feed", pages: int = 1,
           cookie: str = "", limit: int = 0, delay: float = 1.2) -> list:
    out = []
    for pg in range(1, max(1, pages) + 1):
        if pg > 1 and delay > 0:
            time.sleep(delay)
        j = get("/v6/search",
                {"type": stype, "searchValue": keyword, "page": pg, "showAnonymous": "-1"},
                cookie=cookie)
        data = unwrap(j) or []
        if isinstance(data, list):
            for it in data:
                out.append({
                    "id": it.get("id"),
                    "username": it.get("username"),
                    "message": clean_text(it.get("message")),
                    "likenum": it.get("likenum"),
                    "replynum": it.get("replynum"),
                    "dateline": it.get("dateline"),
                    "datetime": ts_to_str(it.get("dateline")),
                    "feedType": it.get("feedType"),
                    "pic": it.get("picArr") or [],
                })
        if limit and len(out) >= limit:
            return out[:limit]
    return out


def feed(fid, replies: int = 1, cookie: str = "", limit: int = 0, delay: float = 1.2) -> dict:
    j = get("/v6/feed/detail", {"id": fid}, cookie=cookie)
    d = unwrap(j)
    if isinstance(d, list):
        d = d[0] if d else {}
    d = d or {}
    out = {
        "id": fid,
        "username": d.get("username"),
        "message": clean_text(d.get("message")),
        "likenum": d.get("likenum"),
        "replynum": d.get("replynum"),
        "dateline": d.get("dateline"),
        "datetime": ts_to_str(d.get("dateline")),
        "pic": d.get("picArr") or [],
        "replies": [],
    }
    for pg in range(1, max(1, replies) + 1):
        if pg > 1 and delay > 0:
            time.sleep(delay)
        rj = get("/v6/feed/replyList",
                 {"id": fid, "listType": "lastupdate_desc", "page": pg,
                  "discussMode": 1, "feedType": "feed"},
                 cookie=cookie)
        for rp in unwrap(rj) or []:
            out["replies"].append({
                "username": rp.get("username"),
                "message": clean_text(rp.get("message")),
                "likenum": rp.get("likenum"),
                "dateline": rp.get("dateline"),
                "datetime": ts_to_str(rp.get("dateline")),
            })
        if limit and len(out["replies"]) >= limit:
            out["replies"] = out["replies"][:limit]
            break
    return out


def _unknown_login(note="无法自动确认登录态"):
    return {"logged_in": None, "uid": None, "username": None, "note": note}


def probe_login(cookie: str):
    """兜底探测：拿一篇真实帖子试详情，能拿到即视为已登录。"""
    try:
        j = get("/v6/search",
                {"type": "feed", "searchValue": "酷安", "page": 1, "showAnonymous": "-1"},
                cookie=cookie)
        d = unwrap(j) or []
        fid = d[0].get("id") if isinstance(d, list) and d else None
        if not fid:
            return None
        d2 = unwrap(get("/v6/feed/detail", {"id": fid}, cookie=cookie))
        return True if d2 else None
    except RuntimeError as e:
        msg = str(e)
        if ("验证码" in msg) or ("设备异常" in msg) or ("登录" in msg):
            return False
        return None
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except Exception:
        return None


def whoami(cookie: str) -> dict:
    """探测登录态。返回 {logged_in: bool|None, uid, username, note}。"""
    if not cookie:
        return {"logged_in": False, "uid": None, "username": None,
                "note": "本机未保存登录凭证", "hint": COOKIE_HINT}
    try:
        j = get("/v6/account/checkLoginInfo", cookie=cookie)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"logged_in": False, "uid": None, "username": None,
                    "note": "凭证已被拒绝（可能过期）", "hint": COOKIE_HINT}
        j = None
    except Exception:
        j = None

    if isinstance(j, dict):
        msg = str(j.get("message") or "")
        data = j.get("data")
        if isinstance(data, list):
            data = data[0] if data else {}
        uid = str((data or {}).get("uid") or "")
        if uid and uid != "0":
            return {"logged_in": True, "uid": uid,
                    "username": (data or {}).get("username") or "", "note": "登录有效"}
        if ("登录信息有误" in msg) or ("未登录" in msg) or ("请登录" in msg) or j.get("status") == -1:
            return {"logged_in": False, "uid": None, "username": None,
                    "note": f"凭证无效或已过期（接口：{msg}）", "hint": COOKIE_HINT}

    # 探测接口不可用时，用真实帖子兜底判定
    r = probe_login(cookie)
    if r is True:
        return {"logged_in": True, "uid": None, "username": None,
                "note": "登录有效（经帖子详情探测确认）"}
    if r is False:
        return {"logged_in": False, "uid": None, "username": None,
                "note": "凭证无效或未生效（帖子详情仍被拦截）", "hint": COOKIE_HINT}
    return _unknown_login("探测接口不可用；请用 feed 命令实测一次，若仍报验证码则凭证无效")


# ============================ CLI ============================
def cmd_login(arg=None, cookie_arg=None):
    raw = cookie_arg
    if not raw:
        try:
            sys.stderr.write(
                "请粘贴酷安 Cookie（浏览器 F12 控制台执行 copy(document.cookie) 后粘贴），回车确认：\n")
            sys.stderr.flush()
            raw = input()
        except EOFError:
            fail(ERR_INPUT, "未读到输入", hint="可改用：python scripts/coolapk.py setcookie \"uid=..;token=..\"")
    try:
        cred = parse_cookie(raw)
    except ValueError as e:
        msg = str(e)
        if msg == "empty":
            fail(ERR_INPUT, "Cookie 为空")
        if msg.startswith("missing:"):
            fail(ERR_INPUT, f"Cookie 里缺少 {msg.split(':')[1]} 字段",
                 hint="整段浏览器 Cookie 直接粘贴即可；至少需包含 uid 与 token。")
        fail(ERR_INPUT, "Cookie 格式无法识别（uid 应为纯数字）", hint=COOKIE_HINT)

    path = save_cred(cred)
    st = whoami(cookie_str(cred))
    if st.get("logged_in") is False:
        # 明确无效：不留下脏凭证，避免后续一直误判
        clear_cred()
        ok({"saved": False, "uid": cred["uid"],
            "username": cred["username"] or None,
            "login_check": st,
            "next": "请重新复制一次 Cookie（确认已登录网页版酷安），再执行 login"})
    ok({"saved": True, "config": path, "uid": cred["uid"],
        "username": cred["username"] or None,
        "login_check": st,
        "next": "python scripts/coolapk.py check"})


def cmd_check():
    c = load_cred()
    if not c:
        ok({"logged_in": False, "source": None, "note": "本机未保存登录凭证", "hint": COOKIE_HINT})
    src = "env" if os.environ.get("COOLAPK_COOKIE") else CONFIG_PATH
    st = whoami(cookie_str(c))
    st["source"] = src
    st["uid"] = st.get("uid") or c.get("uid")
    st["username"] = st.get("username") or c.get("username") or None
    ok(st)


def cmd_logout():
    removed = clear_cred()
    ok({"removed": removed, "note": "已清除本机凭证" if removed else "本机原本没有保存凭证"})


def main():
    if bcrypt is None:
        _bootstrap_bcrypt()  # 找不到就落到 gen_token 的 ERR_DEP 指引
    ap = argparse.ArgumentParser(
        description="酷安搜索 / 帖子与评论抓取（支持登录）")
    ap.add_argument("cmd", choices=["login", "setcookie", "check", "logout", "search", "feed"])
    ap.add_argument("arg", nargs="?", help="关键词(search) 或 帖子ID(feed)")
    ap.add_argument("--type", default="feed", help="搜索类型: feed/user/feedTopic/app")
    ap.add_argument("--pages", type=int, default=1, help="搜索翻页数")
    ap.add_argument("--replies", type=int, default=1, help="抓取的评论页数")
    ap.add_argument("--limit", type=int, default=0, help="截断条数，0=不限")
    ap.add_argument("--delay", type=float, default=1.2, help="翻页间隔秒（控制频率）")
    ap.add_argument("--cookie", default=None, help="临时 Cookie，不落盘")
    args = ap.parse_args()

    # ---- 登录类命令 ----
    if args.cmd in ("login", "setcookie"):
        cmd_login(args.arg, args.cookie)
    if args.cmd == "check":
        cmd_check()
    if args.cmd == "logout":
        cmd_logout()

    # ---- 业务命令：解析凭证 ----
    if args.cookie:
        try:
            cred = parse_cookie(args.cookie)
        except ValueError:
            fail(ERR_INPUT, "--cookie 格式无法识别", hint=COOKIE_HINT)
        cookie = cookie_str(cred)
    else:
        c = load_cred()
        cookie = cookie_str(c) if c else ""

    if args.cmd == "search":
        if not args.arg:
            fail(ERR_INPUT, "缺少搜索关键词")
        try:
            res = search(args.arg, args.type, args.pages, cookie, args.limit, args.delay)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            fail(ERR_API, f"HTTP {e.code}", hint=body)
        except urllib.error.URLError as e:
            fail(ERR_NETWORK, "网络不可达或超时", hint=str(e.reason))
        except RuntimeError as e:
            fail(ERR_API, f"接口返回：{e}")
        except Exception:
            fail(ERR_UNKNOWN, "未预期错误（已拦截，详情未暴露）")
        ok(res)

    if args.cmd == "feed":
        if not args.arg:
            fail(ERR_INPUT, "缺少帖子 ID")
        if not re.fullmatch(r"\d+", str(args.arg)):
            fail(ERR_INPUT, "帖子 ID 应为纯数字", hint="可从 search 结果的 id 字段获取")
        try:
            res = feed(args.arg, args.replies, cookie, args.limit, args.delay)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                fail(ERR_NOCOOKIE, "需要登录才能查看该内容", hint=COOKIE_HINT)
            fail(ERR_API, f"HTTP {e.code}")
        except urllib.error.URLError as e:
            fail(ERR_NETWORK, "网络不可达或超时", hint=str(e.reason))
        except RuntimeError as e:
            msg = str(e)
            if ("验证码" in msg) or ("设备异常" in msg) or ("登录" in msg):
                fail(ERR_CAPTCHA, f"被酷安风控拦截：{msg}",
                     hint=(COOKIE_HINT if not cookie else
                           "已检测到本机凭证但仍被拦截，多半是凭证过期，请重新 login 一次。"))
            fail(ERR_API, f"接口返回：{msg}")
        except Exception:
            fail(ERR_UNKNOWN, "未预期错误（已拦截，详情未暴露）")
        ok(res)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print(json.dumps({"error": "ERR_INTERRUPTED", "message": "用户中断"},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(130)
