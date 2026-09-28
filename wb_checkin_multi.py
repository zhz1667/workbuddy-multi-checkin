#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号自动签到工具（独立版 v1.0.0）

设计目标（与官方 Skill 版的核心差异）：
  1. **多账号**：一个配置文件管 N 个账号，一次运行全部签到；并发执行（默认 4 线程）。
  2. **派出小猫**：每个账号独立跑「派 Buddy 旅行」闭环（先领后派），互不干扰。
  3. **不依赖 WorkBuddy 登录态**：Token 存在本工具自己的 accounts.json 里，
     运行期完全不读取、不依赖 WorkBuddy 客户端；客户端退出 / 未登录 / 登录态
     被本地加密，都不影响本工具。可由 Windows 计划任务独立触发。

Token 从哪来（一次性准备，之后长期独立运行）：
  - `--capture`  从本机 WorkBuddy 历史登录态文件中**只读**提取明文 Token，
                 按 account.uid 归组，自动写入 accounts.json（同一账号取最新有效的一份）。
  - `--add`      手工添加（Token 来自任何渠道，带 --label/--domain）。
  - `--set-token` 手工更新既有账号的 Token。
  Token 有效期约 30 天，脚本用 JWT 的 exp 声明做**离线**到期检查并提前告警。

用法：
  python wb_checkin_multi.py --menu              # 交互式菜单（推荐，也可双击 启动菜单.bat）
  python wb_checkin_multi.py                 # 所有启用账号：签到 + 派猫猫旅行（并发）
  python wb_checkin_multi.py --check-only    # 只查状态，不领取（只读）
  python wb_checkin_multi.py --no-travel     # 只签到，不派小猫
  python wb_checkin_multi.py --travel-only   # 只跑派猫猫旅行
  python wb_checkin_multi.py --location 3    # 指定派遣地点（1-4，缺省随机）
  python wb_checkin_multi.py --accounts a1,a2  # 只跑指定账号
  python wb_checkin_multi.py --capture       # 从本机历史登录态提取 Token 到配置
  python wb_checkin_multi.py --add --label 账号B --domain www.workbuddy.cn
  python wb_checkin_multi.py --list          # 列出账号（Token 脱敏 + 剩余有效期）
  python wb_checkin_multi.py --diagnose      # 环境自检（不发起签到请求）
  python wb_checkin_multi.py --no-notify     # 关闭桌面通知
  python wb_checkin_multi.py --json          # 输出纯 JSON（便于外部消费）

退出码：全部账号成功/已签 0；存在失败 1；配置错误 2。

安全约定：
  - 任何输出（终端 / 日志 / 通知）都不包含完整 Token，一律脱敏。
  - accounts.json 是本工具唯一的凭据文件，含明文 Token，请勿外传 / 勿提交仓库。
  - 写操作仅限 3 个已验证端点：daily-checkin、travel/claim、travel/depart。
"""

import argparse
import base64
import concurrent.futures
import csv
import glob
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

if sys.version_info < (3, 6):
    sys.stderr.write("需要 Python 3.6+，当前 %s\n" % sys.version.split()[0])
    sys.exit(2)

VERSION = "1.0.0"

# ---------------- 路径与常量 ----------------
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(TOOL_DIR, "accounts.json")
LOG_DIR = os.path.join(TOOL_DIR, "logs")

# 签到接口：POST https://<domain>/v2 + PATH
STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"

# 派小猫旅行接口：域名固定 www.workbuddy.cn，且**不带 /v2** 前缀（写错必 404）
TRAVEL_BASE = "https://www.workbuddy.cn"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"

# 域名候选：配置里的 domain 优先，失败时按此顺序回退（实测三个都曾作为 auth.domain 出现）
DEFAULT_DOMAINS = ["www.workbuddy.cn", "www.codebuddy.cn", "copilot.tencent.com"]

HTTP_TIMEOUT = 12
MAX_RETRY = 2
EXPIRY_WARN_DAYS = 7

TRAVEL_STATE_TEXT = {"idle": "空闲(可派遣)", "traveling": "旅行中", "arrived": "已到达待领取"}

_print_lock = threading.Lock()


# ---------------- 基础工具 ----------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mask_token(token):
    """Token 脱敏，绝不输出完整值。"""
    if not token:
        return "<空>"
    if not isinstance(token, str):
        return "<非字符串>"
    if len(token) <= 12:
        return token[:2] + "***"
    return token[:8] + "..." + token[-4:]


def mask_id(value):
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= 4 else s[:3] + "***" + s[-2:]


def _disp_width(text):
    """按东亚字符宽度计算显示宽度，用于中文列对齐。"""
    import unicodedata
    w = 0
    for ch in str(text):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(text, width):
    """左侧对齐并补齐到指定显示宽度（中文按 2 个字符宽计算）。"""
    text = "" if text is None else str(text)
    return text + " " * max(0, width - _disp_width(text))


def clean_token(token):
    """去掉 'Bearer ' 前缀与空白/换行。"""
    if not token:
        return ""
    return token.strip().strip('"').strip("'").replace("Bearer ", "").replace("bearer ", "").strip()


def decode_jwt_exp(token):
    """离线解析 JWT 的 exp（不验签、不联网）。失败返回 None。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
        exp = data.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def token_status(token):
    """返回 (是否有效, 剩余秒数, 到期日期字符串)。无 exp 时视为未知有效。"""
    exp = decode_jwt_exp(token)
    if not exp:
        return True, None, None
    left = exp - int(time.time())
    return left > 0, left, datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")


def fmt_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return "%d 天 %d 小时" % (d, h)
    if h > 0:
        return "%d 小时 %d 分" % (h, m)
    return "%d 分钟" % m


# ---------------- 配置读写 ----------------
DEFAULT_CONFIG = {
    "version": 1,
    "settings": {
        "domains": DEFAULT_DOMAINS,
        "concurrency": 4,
        "travel_default": True,
        "desktop_notify": True,
    },
    "accounts": [],
}


def load_config(create_if_missing=True):
    if not os.path.isfile(CONFIG_PATH):
        if not create_if_missing:
            return None
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise RuntimeError("accounts.json 格式错误（顶层应为对象）")
    cfg.setdefault("settings", {})
    cfg.setdefault("accounts", [])
    for k, v in DEFAULT_CONFIG["settings"].items():
        cfg["settings"].setdefault(k, v)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:  # 尽量收紧权限（Windows 下效果有限，仅作尽力而为）
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def find_account(cfg, key):
    """按 id 或 label 查找账号（不区分大小写）。"""
    key = (key or "").strip().lower()
    for acc in cfg["accounts"]:
        if str(acc.get("id", "")).lower() == key or str(acc.get("label", "")).lower() == key:
            return acc
    return None


def upsert_account(cfg, uid, label, token, domain, source="capture"):
    """按 uid 归组写入；已存在则更新 Token（取更新的一份）。返回 (账号, 是否新增)。"""
    for acc in cfg["accounts"]:
        if acc.get("uid") and str(acc["uid"]) == str(uid):
            if token:
                acc["token"] = token
                acc["domain"] = domain or acc.get("domain")
                acc["updated_at"] = now_str()
            return acc, False
    acc = {
        "id": "acc%d" % (len(cfg["accounts"]) + 1),
        "label": label,
        "uid": str(uid) if uid else None,
        "token": token,
        "domain": domain or DEFAULT_DOMAINS[0],
        "enabled": True,
        "travel": True,
        "location_id": None,
        "source": source,
        "updated_at": now_str(),
    }
    cfg["accounts"].append(acc)
    return acc, True


# ---------------- HTTP ----------------
def api_call(base, path, token, payload=None, method="POST", timeout=HTTP_TIMEOUT):
    """统一请求封装：非 2xx 也解析响应体（已签到返回 HTTP 400 + code=10001 属正常）。"""
    url = base + path
    if method == "GET" and payload is None:
        data = None
    else:
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "WorkBuddy-MultiCheckin/%s" % VERSION)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"raw": body}
        except Exception:
            return e.code, {"raw": ""}


def resolve_base(acc, domains):
    """确定该账号可用的签到 base（https://<domain>/v2）。

    依次尝试候选域名调只读状态接口：HTTP 200/400 且返回 JSON（含 code 字段）即视为命中；
    404 / 网络错误则换下一个。全部失败返回 (None, 最后一次错误)。
    """
    candidate = []
    for d in [acc.get("domain")] + list(domains or []) + DEFAULT_DOMAINS:
        if d and d not in candidate:
            candidate.append(d)
    last_err = "无可用域名"
    for dom in candidate:
        base = "https://%s/v2" % dom
        try:
            st, body = api_call(base, STATUS_PATH, acc.get("token"), timeout=8)
            if st in (200, 400) and isinstance(body, dict) and ("code" in body or "data" in body):
                return base, dom, None
            last_err = "HTTP %s" % st
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    return None, None, last_err


def extract_balance(*bodies):
    """尽力提取积分余额（不同版本字段名不统一；实测常见 total_credits 复数）。"""
    keys = ("total_credits", "total_credit", "total_credit_balance", "credits",
            "total_points", "points_balance", "credit_balance", "balance",
            "remain_credit", "remain_credits", "remain", "score",
            "integral", "totalCredits", "totalCredit", "pointsBalance")
    for body in bodies:
        if not isinstance(body, dict):
            continue
        nodes = [body]
        for sec in ("data", "result", "data.result"):
            node = body
            for part in sec.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, dict):
                nodes.append(node)
        for node in nodes:
            for k in keys:
                v = node.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return v
    return None


# ---------------- 签到 ----------------
def do_checkin(acc, base, check_only=False):
    """单账号签到。返回结果片段。"""
    res = {"status": "error", "action": None, "points": None, "balance": None,
           "streak_days": None, "msg": "", "detail": {}}
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            st_code, st_body = api_call(base, STATUS_PATH, acc["token"])
            res["detail"]["status_http"] = st_code
            res["balance"] = extract_balance(st_body)

            if isinstance(st_body, dict) and "code" in st_body and str(st_body.get("code")) not in ("0", "10001"):
                if st_code in (401, 403):
                    res.update(status="error", action="token_invalid",
                               msg="Token 已失效或被拒绝（HTTP %s），请用 --set-token 更新" % st_code)
                    return res
            today_signed = False
            if isinstance(st_body, dict):
                if st_body.get("today_checked_in") is True:
                    today_signed = True
                elif isinstance(st_body.get("data"), dict) and st_body["data"].get("today_checked_in") is True:
                    today_signed = True
                elif str(st_body.get("code")) == "10001":
                    today_signed = True
            if isinstance(st_body, dict) and isinstance(st_body.get("data"), dict):
                res["streak_days"] = st_body["data"].get("streak_days")
                if res["balance"] is None:
                    res["balance"] = extract_balance(st_body["data"])

            if check_only:
                res.update(status="ok", action="check_only",
                           msg="状态查询成功%s" % ("（今日已签）" if today_signed else "（今日未签）"))
                res["detail"]["today_signed"] = today_signed
                return res

            if today_signed:
                res.update(status="ok", action="already_signed", msg="今日已签到，跳过领取")
                return res

            ck_code, ck_body = api_call(base, CHECKIN_PATH, acc["token"])
            res["detail"]["checkin_http"] = ck_code
            if not isinstance(ck_body, dict):
                res.update(status="error", action="failed", msg="领取接口返回非 JSON")
                return res
            code = str(ck_body.get("code", ""))
            msg = ck_body.get("msg") or ck_body.get("message") or ""
            data = ck_body.get("data") if isinstance(ck_body.get("data"), dict) else {}
            if code == "10001" or "已签到" in msg:
                res.update(status="ok", action="already_signed", msg="今日已签到（服务端幂等返回）")
                return res
            if 200 <= ck_code < 300 and code in ("", "0", "200"):
                credit = (ck_body.get("credit") or data.get("credit")
                          or data.get("daily_credit") or data.get("today_credit"))
                streak = ck_body.get("streak_days") or data.get("streak_days")
                bbal = extract_balance(ck_body)
                if bbal is not None:
                    res["balance"] = bbal
                res.update(status="ok", action="signed", points=credit, streak_days=streak,
                           msg="签到成功%s%s" % (("，+%s 积分" % credit) if credit else "",
                                              ("，连续第 %s 天" % streak) if streak else ""))
                return res
            if ck_code in (401, 403):
                res.update(status="error", action="token_invalid",
                           msg="Token 已失效或被拒绝（HTTP %s），请用 --set-token 更新" % ck_code)
                return res
            res.update(status="error", action="failed",
                       msg=msg or ("HTTP %s（业务码 %s）" % (ck_code, code)))
            return res
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s: %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            last_err = "网络错误: %s" % e.reason
        except Exception as e:  # noqa: BLE001
            last_err = "异常: %s" % e
        if attempt < MAX_RETRY:
            time.sleep(1.5)
    res.update(status="error", action="failed", msg="重试 %d 次后仍失败：%s" % (MAX_RETRY, last_err))
    return res


# ---------------- 派小猫旅行 ----------------
def travel_get(token, path):
    try:
        st, body = api_call(TRAVEL_BASE, path, token, method="GET")
        if st == 200 and isinstance(body, dict) and body.get("code") == 0:
            return body.get("data")
    except Exception:  # noqa: BLE001
        return None
    return None


def travel_claim(token):
    try:
        st, body = api_call(TRAVEL_BASE, TRAVEL_CLAIM_PATH, token, payload={})
    except Exception as e:  # noqa: BLE001
        return {"action": "failed", "message": "请求异常：%s" % e}
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        credit = (body.get("data") or {}).get("reward_credit")
        return {"action": "claimed", "reward_credit": credit,
                "message": "已领取旅行奖励 +%s 积分" % (credit if credit is not None else "?")}
    msg = (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st)
    return {"action": "failed", "message": msg}


def travel_depart(token, location_id=None, locations=None):
    chosen = location_id
    if chosen is None:
        ids = [loc.get("id") for loc in (locations or []) if loc.get("id") is not None]
        if not ids:
            return {"action": "failed", "message": "未取到可选地点，已跳过派遣"}
        chosen = random.choice(ids)
    try:
        st, body = api_call(TRAVEL_BASE, TRAVEL_DEPART_PATH, token, payload={"location_id": chosen})
    except Exception as e:  # noqa: BLE001
        return {"action": "failed", "message": "请求异常：%s" % e}
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        loc = data.get("location") or {}
        return {"action": "departed", "location_id": chosen, "location_name": loc.get("name"),
                "message": "已派出 Buddy 前往【%s】" % (loc.get("name") or ("地点 %s" % chosen))}
    msg = (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st)
    return {"action": "failed", "location_id": chosen, "message": msg}


def travel_auto(token, location_id=None, read_only=False):
    """派小猫闭环：先领取、后派遣。派遣前必查 daily_limit_reached。"""
    out = {"available": False, "state": None, "state_text": None, "location_name": None,
           "remaining_text": None, "reward_credit": None, "auto_log": []}
    status = travel_get(token, TRAVEL_STATUS_PATH)
    if status is None:
        out["auto_log"].append("查询旅行状态失败（接口不可用或 Token 失效），已跳过")
        return out
    out["available"] = True

    if not read_only and status.get("state") == "arrived":
        r = travel_claim(token)
        out["auto_log"].append(r.get("message"))
        if r.get("action") == "claimed":
            fresh = travel_get(token, TRAVEL_STATUS_PATH)
            if fresh is not None:
                status = fresh

    if not read_only:
        if status.get("daily_limit_reached"):
            out["auto_log"].append("今日派遣次数已用完，跳过派遣")
        elif status.get("state") in (None, "", "idle"):
            cfg_locs = (travel_get(token, TRAVEL_CONFIG_PATH) or {}).get("locations") or []
            r = travel_depart(token, location_id, cfg_locs)
            out["auto_log"].append(r.get("message"))
            if r.get("action") == "departed":
                fresh = travel_get(token, TRAVEL_STATUS_PATH)
                if fresh is not None:
                    status = fresh
        else:
            out["auto_log"].append("Buddy 正在旅行中，无需派遣")

    state = status.get("state")
    out["state"] = state
    out["state_text"] = TRAVEL_STATE_TEXT.get(state, state)
    loc = status.get("location")
    out["location_name"] = loc.get("name") if isinstance(loc, dict) else None
    out["reward_credit"] = status.get("reward_credit")
    if state == "traveling":
        arrive, sn = status.get("arrive_at"), status.get("server_now")
        if isinstance(arrive, (int, float)) and isinstance(sn, (int, float)):
            out["remaining_text"] = fmt_duration(max(0, arrive - sn))
    return out


# ---------------- 单账号全流程 ----------------
def run_one(acc, settings, do_check=True, check_only=False, travel_mode="auto", location_id=None):
    """单账号全流程。

    do_check：是否执行签到；check_only：签到只读；travel_mode: auto/readonly/off
    """
    started = time.time()
    res = {
        "id": acc.get("id"), "label": acc.get("label"), "uid_masked": mask_id(acc.get("uid")),
        "status": "error", "action": None, "points": None, "balance": None, "streak_days": None,
        "domain": None, "token_masked": mask_token(acc.get("token")), "msg": "", "travel": None,
    }
    token = clean_token(acc.get("token"))
    if not token:
        res["msg"] = "未配置 Token，请用 --capture 或 --set-token 补充"
        return res

    ok, left, exp_str = token_status(token)
    if not ok:
        res.update(status="error", action="token_expired",
                   msg="Token 已于 %s 过期，请更新（--set-token）" % exp_str)
        return res

    if do_check:
        base, domain, err = resolve_base(acc, settings.get("domains"))
        if not base:
            res["msg"] = "无法连接签到接口（所有候选域名均失败：%s）" % err
            return res
        res["domain"] = domain

    if do_check:
        ck = do_checkin(acc, base, check_only=check_only)
        res.update(status=ck["status"], action=ck["action"], points=ck["points"],
                   balance=ck["balance"], streak_days=ck["streak_days"], msg=ck["msg"])

    if travel_mode != "off":
        tv = travel_auto(token, location_id, read_only=(travel_mode == "readonly"))
        res["travel"] = tv
        bits = []
        if tv.get("state_text"):
            bits.append(tv["state_text"])
        if tv.get("location_name"):
            bits.append(tv["location_name"])
        if tv.get("remaining_text"):
            bits.append("还需 %s" % tv["remaining_text"])
        for line in tv.get("auto_log") or []:
            if line:
                bits.append(str(line))
        if bits:
            res["msg"] = (res["msg"] + " ｜ 小猫：" + "，".join(bits)) if res["msg"] else "小猫：" + "，".join(bits)
        if not do_check:
            # 纯旅行模式：整体状态完全由旅行接口可用性决定
            res["status"] = "ok" if tv.get("available") else "error"
            res["action"] = "travel"
            if not tv.get("available"):
                res["msg"] = "查询旅行状态失败（Token 失效或网络异常）"

    res["elapsed_ms"] = int((time.time() - started) * 1000)
    return res


# ---------------- 从本机历史登录态提取 Token（一次性，只读） ----------------
def auth_dirs():
    home = os.path.expanduser("~")
    cands = []
    if sys.platform.startswith("win"):
        for env in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "CodeBuddyExtension", "Data", "Public", "auth"))
    elif sys.platform == "darwin":
        cands.append(os.path.join(home, "Library", "Application Support",
                                  "CodeBuddyExtension", "Data", "Public", "auth"))
    else:
        cands.append(os.path.join(home, ".config", "CodeBuddyExtension",
                                  "Data", "Public", "auth"))
    cands.append(os.path.join(home, ".workbuddy", "auth"))
    return [d for d in cands if os.path.isdir(d)]


def scan_auth_files(extra_dir=None):
    """扫描登录态文件，返回 (候选列表, 扫描统计)。只读，不落任何凭据到磁盘以外。"""
    dirs = ([extra_dir] if extra_dir else []) + auth_dirs()
    files = []
    for d in dirs:
        files.extend(glob.glob(os.path.join(d, "workbuddy-desktop*.info")))
    stats = {"dirs": dirs, "files": len(files), "plain": 0, "encrypted": 0, "expired": 0, "broken": 0}
    per_uid = {}
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                root = json.load(f)
        except Exception:
            stats["broken"] += 1
            continue
        auth = root.get("auth") or {}
        accnt = root.get("account") or {}
        token = auth.get("accessToken")
        if not isinstance(token, str) or not token:
            stats["encrypted"] += 1
            continue
        stats["plain"] += 1
        ok, left, exp_str = token_status(token)
        mtime = os.path.getmtime(path)
        # 昵称在较早的快照里是明文，可用于给账号起个可读的名字；加密时回落到 uin 后四位
        nickname = None
        for node in (accnt, (root.get("accounts") or [{}])[0] if isinstance(root.get("accounts"), list) and root.get("accounts") else {}):
            if isinstance(node, dict) and isinstance(node.get("nickname"), str) and node["nickname"]:
                nickname = node["nickname"]
                break
        rec = {
            "path": path, "uid": accnt.get("uid"), "uin": accnt.get("uin"),
            "token": token, "domain": auth.get("domain"), "exp_str": exp_str,
            "left": left, "valid": ok, "mtime": mtime, "nickname": nickname,
            "label": nickname or ("账号-%s" % (str(accnt.get("uin") or accnt.get("uid") or "?")[-4:])),
        }
        if not ok:
            stats["expired"] += 1
        key = str(accnt.get("uid") or path)
        if key not in per_uid or (rec["valid"] and not per_uid[key]["valid"]) or \
           (rec["valid"] == per_uid[key]["valid"] and mtime > per_uid[key]["mtime"]):
            per_uid[key] = rec
    return list(per_uid.values()), stats


def cmd_capture(args):
    cfg = load_config()
    found, stats = scan_auth_files(args.capture_dir)
    if not found:
        print("未发现任何可用的明文 Token（本机登录态可能已全部被本地加密，或客户端从未登录过）。")
        print("可用 --add 手工添加，或 --capture-dir 指定目录。")
        print("扫描目录：%s" % "，".join(stats["dirs"]))
        return 2
    print("扫描 %d 个登录态文件：明文 %d / 加密 %d / 已过期 %d / 损坏 %d"
          % (stats["files"], stats["plain"], stats["encrypted"], stats["expired"], stats["broken"]))
    added, updated = 0, 0
    for rec in sorted(found, key=lambda r: r["mtime"], reverse=True):
        if not rec["valid"]:
            print("  ✗ 跳过已过期账号 %s（%s 到期）" % (mask_id(rec["uid"]), rec["exp_str"]))
            continue
        label = rec["label"]
        if args.label_prefix:
            label = "%s-%s" % (args.label_prefix, rec["label"])
        acc, is_new = upsert_account(cfg, rec["uid"], label, rec["token"], rec["domain"])
        if is_new:
            added += 1
        else:
            updated += 1
            if not args.keep_label:   # 默认用最新昵称刷新显示名
                acc["label"] = label
        print("  ✓ %s %s（uid %s，域名 %s，%s 到期，剩余 %s）"
              % ("新增" if is_new else "更新", label, mask_id(rec["uid"]),
                 rec["domain"], rec["exp_str"], fmt_duration(rec["left"])))
    save_config(cfg)
    print("\n配置已写入：%s（新增 %d，更新 %d，账号合计 %d）"
          % (CONFIG_PATH, added, updated, len(cfg["accounts"])))
    print("提示：该文件含明文 Token，请勿外传或提交到代码仓库。")
    return 0


def cmd_add(args):
    cfg = load_config()
    token = clean_token(args.token) if args.token else ""
    if not token:
        token = clean_token(input("粘贴该账号的 accessToken（不回显到日志）: "))
    if not token:
        print("未提供 Token，已取消。")
        return 2
    ok, left, exp_str = token_status(token)
    if not ok:
        print("该 Token 已于 %s 过期，仍会写入但无法使用。" % exp_str)
    label = args.label or "账号-%s" % mask_id(len(cfg["accounts"]) + 1)
    acc, is_new = upsert_account(cfg, args.uid, label, token, args.domain or DEFAULT_DOMAINS[0], source="manual")
    save_config(cfg)
    print("%s账号：%s（id=%s，域名 %s%s）"
          % ("新增" if is_new else "更新", label, acc["id"], acc["domain"],
             "，%s 到期" % exp_str if exp_str else ""))
    return 0


def cmd_set_token(args):
    cfg = load_config()
    acc = find_account(cfg, args.set_token)
    if not acc:
        print("未找到账号：%s（用 --list 查看 id/label）" % args.set_token)
        return 2
    token = clean_token(args.token) if args.token else ""
    if not token:
        token = clean_token(input("粘贴 %s 的新 accessToken: " % acc.get("label")))
    if not token:
        print("未提供 Token，已取消。")
        return 2
    ok, left, exp_str = token_status(token)
    acc["token"] = token
    acc["updated_at"] = now_str()
    if args.domain:
        acc["domain"] = args.domain
    save_config(cfg)
    print("已更新 %s 的 Token%s%s"
          % (acc.get("label"), "，到期 %s" % exp_str if exp_str else "",
             "" if ok else "（⚠ 已过期）"))
    return 0


def cmd_list(args):
    cfg = load_config()
    accounts = cfg["accounts"]
    if not accounts:
        print("配置里还没有账号。先跑 --capture（从本机历史登录态提取）或 --add 手工添加。")
        return 0
    print("%s %s %s %s %s %s"
          % (pad("id", 6), pad("账号名", 18), pad("uid", 11), pad("域名", 23),
             pad("启用", 6), "Token 状态"))
    print("-" * 92)
    for acc in accounts:
        ok, left, exp_str = token_status(clean_token(acc.get("token")))
        if not acc.get("token"):
            tstate = "未配置"
        elif not ok:
            tstate = "已过期 %s" % exp_str
        elif left is not None and left < EXPIRY_WARN_DAYS * 86400:
            tstate = "⚠ %s 到期（剩 %s）" % (exp_str, fmt_duration(left))
        elif exp_str:
            tstate = "有效至 %s" % exp_str
        else:
            tstate = "有效（无 exp）"
        print("%s %s %s %s %s %s"
              % (pad(acc.get("id"), 6), pad(acc.get("label"), 18),
                 pad(mask_id(acc.get("uid")), 11), pad(acc.get("domain") or "-", 23),
                 pad("是" if acc.get("enabled", True) else "否", 6), tstate))
    print("\n配置文件：%s" % CONFIG_PATH)
    return 0


# ---------------- 账号与设置的可编程操作（菜单与命令行共用） ----------------

def select_accounts(cfg, spec=None):
    """按 spec（逗号分隔的 id/label）筛选账号；spec 为空时返回全部启用账号。"""
    allacc = cfg["accounts"]
    if not spec:
        return [a for a in allacc if a.get("enabled", True)]
    wanted = [x.strip().lower() for x in (spec.split(",") if isinstance(spec, str) else spec) if str(x).strip()]
    return [a for a in allacc
            if str(a.get("id", "")).lower() in wanted or str(a.get("label", "")).lower() in wanted]


def toggle_account(cfg, key, enabled):
    """启用/停用账号。返回 (账号, 是否找到)。"""
    acc = find_account(cfg, key)
    if not acc:
        return None, False
    acc["enabled"] = bool(enabled)
    save_config(cfg)
    return acc, True


def remove_account(cfg, key):
    """从配置中删除账号（不可恢复）。返回 (账号, 是否找到)。"""
    acc = find_account(cfg, key)
    if not acc:
        return None, False
    cfg["accounts"] = [a for a in cfg["accounts"] if a is not acc]
    save_config(cfg)
    return acc, True


def set_account_location(cfg, key, location_id):
    """设置账号的固定派遣地点；None 表示每次随机。"""
    acc = find_account(cfg, key)
    if not acc:
        return None, False
    acc["location_id"] = location_id
    save_config(cfg)
    return acc, True


def set_setting(cfg, key, value):
    """写入 settings 中的一项（并发数 / 桌面通知 / 域名等）。"""
    cfg["settings"][key] = value
    save_config(cfg)
    return value


def cmd_toggle(args):
    cfg = load_config()
    key = args.enable or args.disable
    acc, found = toggle_account(cfg, key, bool(args.enable))
    if not found:
        print("未找到账号：%s（用 --list 查看 id/label）" % key)
        return 2
    print("已%s账号：%s（id=%s）" % ("启用" if args.enable else "停用", acc.get("label"), acc.get("id")))
    return 0


def cmd_remove(args):
    cfg = load_config()
    acc, found = remove_account(cfg, args.remove)
    if not found:
        print("未找到账号：%s" % args.remove)
        return 2
    print("已删除账号：%s（剩余 %d 个）" % (acc.get("label"), len(cfg["accounts"])))
    return 0


def cmd_set_location(args):
    cfg = load_config()
    raw = str(args.set_location_value).strip().lower()
    loc = None if raw in ("", "random", "r", "0") else int(raw)
    if loc is not None and loc not in (1, 2, 3, 4):
        print("地点只能是 1-4，或 random 表示随机。")
        return 2
    acc, found = set_account_location(cfg, args.set_location, loc)
    if not found:
        print("未找到账号：%s" % args.set_location)
        return 2
    print("已将 %s 的派遣地点设为：%s" % (acc.get("label"), "随机" if loc is None else loc))
    return 0


def cmd_notify(args):
    cfg = load_config()
    on = str(args.notify).strip().lower() in ("on", "true", "1", "yes", "开")
    set_setting(cfg, "desktop_notify", on)
    print("桌面通知已%s。" % ("开启" if on else "关闭"))
    return 0


# ---------------- 统一执行入口（命令行与菜单共用） ----------------

def execute_run(cfg, accounts, do_check=True, check_only=False, travel_mode="auto",
                location_id=None, workers=None, notify=None, as_json=False,
                show_progress=True):
    """跑完一批账号，负责并发、日志、报表与通知。

    返回 (results, elapsed, exit_code)。
    """
    settings = cfg["settings"]
    if not accounts:
        if not as_json:
            print("没有可执行的账号。请先提取或添加账号。")
        return [], 0.0, 2
    if workers is None:
        workers = int(settings.get("concurrency", 4))
    workers = max(1, min(int(workers), len(accounts)))
    if notify is None:
        notify = bool(settings.get("desktop_notify", True))

    if show_progress and not as_json:
        print("开始处理 %d 个账号（并发 %d，模式：%s%s）…"
              % (len(accounts), workers,
                 "只查询" if check_only else ("只派小猫" if not do_check else "签到+派小猫"),
                 "" if travel_mode == "auto" else "（旅行只读）"))

    started = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(run_one, acc, settings, do_check, check_only, travel_mode,
                            location_id if location_id else acc.get("location_id")): acc
                for acc in accounts}
        for fut in concurrent.futures.as_completed(futs):
            acc = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001
                r = {"id": acc.get("id"), "label": acc.get("label"), "status": "error",
                     "action": "exception", "points": None, "balance": None,
                     "msg": "线程异常：%s" % e, "travel": None}
            results.append(r)
            if show_progress and not as_json:
                with _print_lock:
                    print("  %s %s — %s" % ("✓" if r["status"] == "ok" else "✗",
                                            r.get("label"), (r.get("msg") or "")[:70]))
    results.sort(key=lambda r: str(r.get("id")))
    elapsed = time.time() - started

    write_logs(results)
    if as_json:
        print(json.dumps({"time": now_str(), "elapsed_s": round(elapsed, 2),
                          "results": results}, ensure_ascii=False, indent=2))
    else:
        print_report(results, elapsed, do_check, check_only, travel_mode)
        print("日志：%s" % LOG_DIR)

    if notify:
        ok = [r for r in results if r["status"] == "ok"]
        bad = [r for r in results if r["status"] != "ok"]
        signed = [r for r in results if r["action"] == "signed"]
        pts = sum(r["points"] or 0 for r in signed)
        title = "WorkBuddy 签到：%d/%d 成功" % (len(ok), len(results))
        if do_check:
            body = ("新签 %d 个账号 +%s 积分" % (len(signed), pts)) if signed else "今日均已签到"
        else:
            body = "小猫旅行已处理 %d 个账号" % len(ok)
        if bad:
            body += " ｜ 失败 %d：%s" % (len(bad), "，".join(str(b.get("label")) for b in bad))
        desktop_toast(title, body)

    return results, elapsed, (0 if all(r["status"] == "ok" for r in results) else 1)


# ---------------- 输出与通知 ----------------
def print_report(results, elapsed, do_check=True, check_only=False, travel_mode="auto"):
    ok = [r for r in results if r["status"] == "ok"]
    bad = [r for r in results if r["status"] != "ok"]
    signed = [r for r in results if r["action"] == "signed"]
    skipped = [r for r in results if r["action"] == "already_signed"]
    total_points = sum(r["points"] or 0 for r in signed)

    print("=" * 92)
    print("WorkBuddy 多账号签到报告 · %s · 账号 %d 个 · 耗时 %.1fs"
          % (now_str(), len(results), elapsed))
    print("=" * 92)

    for r in results:
        mark = "✓" if r["status"] == "ok" else "✗"
        print("%s [%s] %s" % (mark, r.get("id"), r.get("label")))
        if r["status"] != "ok":
            print("    结果：%s" % r["msg"])
            continue
        if do_check:
            line = "    签到：%s" % (r["msg"] or "").split(" ｜ ")[0]
            if r.get("points"):
                line += " | +%s" % r["points"]
            if r.get("balance") is not None:
                line += " | 余额 %s" % r["balance"]
            if r.get("streak_days"):
                line += " | 连续 %s 天" % r["streak_days"]
            print(line)
        tv = r.get("travel") or {}
        if tv:
            tl = "    小猫：%s" % (tv.get("state_text") or "不可用")
            if tv.get("location_name"):
                tl += " · %s" % tv["location_name"]
            if tv.get("remaining_text"):
                tl += " · 还需 %s" % tv["remaining_text"]
            print(tl)
            for x in tv.get("auto_log") or []:
                if x:
                    print("          - %s" % x)
        print("    Token：%s | 域名：%s | 耗时 %sms"
              % (r.get("token_masked"), r.get("domain") or "-", r.get("elapsed_ms")))

    print("-" * 92)
    print("汇总：成功 %d / 失败 %d ｜ 本次新签 %d 个账号(+%s 积分) ｜ 今日已签 %d"
          % (len(ok), len(bad), len(signed), total_points, len(skipped)))
    if bad:
        print("失败账号：%s" % "，".join("%s(%s)" % (b.get("label"), str(b.get("msg"))[:40]) for b in bad))
    print("=" * 92)


def desktop_toast(title, body):
    """跨平台桌面通知（best-effort，失败静默）。"""
    try:
        if sys.platform == "darwin":
            msg = body.replace('"', "'")
            subprocess.run(["osascript", "-e",
                            'display notification "%s" with title "%s"' % (msg, title)],
                           timeout=6, check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, body], timeout=6, check=False)
        elif sys.platform == "win32":
            st = title.replace("'", "''")
            sb = body.replace("'", "''")
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "Add-Type -AssemblyName System.Drawing;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;"
                  "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                  "$n.Visible=$true;"
                  "$n.ShowBalloonTip(6000,'%s','%s','Info');"
                  "Start-Sleep -Milliseconds 200;$n.Dispose()") % (st, sb)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           timeout=15, check=False)
    except Exception:  # noqa: BLE001
        pass


def write_logs(results):
    """追加 CSV 明细 + 覆写 last_run.json（均不含完整 Token）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        csv_path = os.path.join(LOG_DIR, "checkin-%s.csv" % datetime.now().strftime("%Y-%m"))
        new_file = not os.path.isfile(csv_path)
        with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["时间", "账号ID", "账号名", "UID", "状态", "动作", "积分", "余额",
                            "连续天数", "小猫状态", "小猫地点", "域名", "说明"])
            for r in results:
                tv = r.get("travel") or {}
                w.writerow([now_str(), r.get("id"), r.get("label"), r.get("uid_masked"),
                            r.get("status"), r.get("action"), r.get("points"), r.get("balance"),
                            r.get("streak_days"), tv.get("state_text"), tv.get("location_name"),
                            r.get("domain"), r.get("msg")])
        with open(os.path.join(LOG_DIR, "last_run.json"), "w", encoding="utf-8") as f:
            json.dump({"time": now_str(), "results": results}, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


# ---------------- 自检 ----------------
def cmd_diagnose(args):
    cfg = load_config()
    rep = {"version": VERSION, "python": sys.version.split()[0], "config": CONFIG_PATH,
           "accounts": [], "network": {}, "desktop_session": None}
    print("WorkBuddy 多账号签到工具 · 环境自检 v%s" % VERSION)
    print("-" * 92)
    print("Python           : %s" % sys.version.split()[0])
    print("配置文件         : %s（%s）" % (CONFIG_PATH, "存在" if os.path.isfile(CONFIG_PATH) else "不存在，将自动创建"))
    print("账号数量         : %d（启用 %d）"
          % (len(cfg["accounts"]), len([a for a in cfg["accounts"] if a.get("enabled", True)])))
    ok_cnt = warn_cnt = bad_cnt = 0
    for acc in cfg["accounts"]:
        ok, left, exp_str = token_status(clean_token(acc.get("token")))
        if not acc.get("token"):
            state, bad_cnt = "未配置 Token", bad_cnt + 1
        elif not ok:
            state, bad_cnt = "已过期（%s）" % exp_str, bad_cnt + 1
        elif left is not None and left < EXPIRY_WARN_DAYS * 86400:
            state, warn_cnt = "⚠ 即将过期（剩 %s）" % fmt_duration(left), warn_cnt + 1
        else:
            state, ok_cnt = "可用（到期 %s）" % (exp_str or "未知"), ok_cnt + 1
        rep["accounts"].append({"id": acc.get("id"), "label": acc.get("label"),
                                "state": state, "domain": acc.get("domain")})
        print("  - %s %s %s %s"
              % (pad(acc.get("id"), 6), pad(acc.get("label"), 18),
                 pad(acc.get("domain") or "-", 23), state))
    print("Token 体检       : 可用 %d / 将过期 %d / 不可用 %d" % (ok_cnt, warn_cnt, bad_cnt))
    dom = (cfg["accounts"][0].get("domain") if cfg["accounts"] else None) or DEFAULT_DOMAINS[0]
    for host in {dom, "www.workbuddy.cn"}:
        try:
            ip = socket.gethostbyname(host)
            rep["network"][host] = ip
            print("DNS              : %-22s → %s" % (host, ip))
        except Exception as e:  # noqa: BLE001
            rep["network"][host] = str(e)
            print("DNS              : %-22s → 解析失败 %s" % (host, e))
    found, stats = scan_auth_files(args.capture_dir)
    print("本机登录态扫描   : 文件 %d（明文 %d / 加密 %d / 过期 %d）；可提取账号 %d"
          % (stats["files"], stats["plain"], stats["encrypted"], stats["expired"], len(found)))
    print("                   %s" % "，".join(stats["dirs"]))
    rep["local_auth"] = {"files": stats["files"], "plain": stats["plain"],
                         "encrypted": stats["encrypted"], "usable_accounts": len(found)}
    print("-" * 92)
    print("说明：本工具运行期只读 accounts.json，不依赖 WorkBuddy 客户端登录态。")
    print("      以上登录态扫描仅用于 --capture 提取 Token，属一次性动作。")
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


# ---------------- main ----------------
def build_parser():
    p = argparse.ArgumentParser(
        description="WorkBuddy 多账号自动签到 + 派小猫旅行（独立运行，不依赖 WorkBuddy 登录态）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python wb_checkin_multi.py --menu              # 交互式菜单（推荐）\n"
               "  python wb_checkin_multi.py --capture          # 首次：提取 Token\n"
               "  python wb_checkin_multi.py --list             # 查看账号与有效期\n"
               "  python wb_checkin_multi.py                     # 每日：全部账号签到+派猫\n")
    p.add_argument("--capture", action="store_true", help="从本机历史登录态提取 Token 写入配置")
    p.add_argument("--capture-dir", help="自定义登录态目录（配合 --capture）")
    p.add_argument("--label-prefix", help="capture 时给账号名加统一前缀，如 公司")
    p.add_argument("--keep-label", action="store_true",
                   help="capture 时保留既有账号名（默认会用快照里的昵称刷新显示名）")
    p.add_argument("--add", action="store_true", help="手工添加账号（配合 --label/--token/--domain）")
    p.add_argument("--set-token", metavar="账号", help="更新指定账号的 Token")
    p.add_argument("--token", help="Token（配合 --add/--set-token；省略则交互式粘贴）")
    p.add_argument("--label", help="账号显示名（配合 --add）")
    p.add_argument("--uid", help="账号 uid（配合 --add，用于去重）")
    p.add_argument("--domain", help="接口域名，默认 www.workbuddy.cn")
    p.add_argument("--list", action="store_true", help="列出账号与 Token 有效期")
    p.add_argument("--diagnose", action="store_true", help="环境自检（不发起签到请求）")
    p.add_argument("--enable", metavar="账号", help="启用指定账号（id 或名称）")
    p.add_argument("--disable", metavar="账号", help="停用指定账号（保留配置，不参与运行）")
    p.add_argument("--remove", metavar="账号", help="从配置中删除指定账号")
    p.add_argument("--set-location", metavar="账号", dest="set_location",
                   help="设置该账号的固定派遣地点（配合 --set-location-value）")
    p.add_argument("--set-location-value", metavar="N", dest="set_location_value",
                   help="1-4 指定地点，random 表示随机")
    p.add_argument("--notify", metavar="on|off", help="开启/关闭桌面通知（持久化到配置）")
    p.add_argument("--check-only", action="store_true", help="只查状态，不领取（只读）")
    p.add_argument("--no-travel", action="store_true", help="只签到，不派小猫")
    p.add_argument("--travel-only", action="store_true", help="只跑派小猫旅行")
    p.add_argument("--location", type=int, choices=[1, 2, 3, 4], help="派遣地点（1-4，缺省随机）")
    p.add_argument("--accounts", help="只跑指定账号，逗号分隔 id 或 label")
    p.add_argument("--concurrency", type=int, help="并发线程数（默认取配置，建议 3-6）")
    p.add_argument("--no-notify", action="store_true", help="不弹桌面通知")
    p.add_argument("--json", action="store_true", help="结果以纯 JSON 输出")
    p.add_argument("--menu", action="store_true",
                   help="启动交互式菜单（不会用命令行参数时的首选；也可双击 启动菜单.bat）")
    p.add_argument("--version", action="version", version="wb_checkin_multi %s" % VERSION)
    return p


def main():
    args = build_parser().parse_args()

    if args.menu:
        import wb_menu  # 延迟导入，避免与菜单模块形成循环依赖
        return wb_menu.main()

    if args.capture:
        return cmd_capture(args)
    if args.add:
        return cmd_add(args)
    if args.set_token:
        return cmd_set_token(args)
    if args.list:
        return cmd_list(args)
    if args.diagnose:
        return cmd_diagnose(args)
    if args.enable or args.disable:
        return cmd_toggle(args)
    if args.remove:
        return cmd_remove(args)
    if args.set_location:
        return cmd_set_location(args)
    if args.notify:
        return cmd_notify(args)

    cfg = load_config()
    accounts = select_accounts(cfg, args.accounts)
    if not accounts:
        print("没有可执行的账号。请先运行：python %s --capture  或  --add"
              % os.path.basename(__file__))
        return 2

    if args.travel_only:
        do_check = False
        check_only = bool(args.check_only)
        travel_mode = "readonly" if args.check_only else "auto"
    else:
        do_check = True
        check_only = bool(args.check_only)
        travel_mode = "off" if args.no_travel else ("readonly" if args.check_only else "auto")

    _, _, code = execute_run(
        cfg, accounts, do_check=do_check, check_only=check_only,
        travel_mode=travel_mode, location_id=args.location,
        workers=args.concurrency, notify=(not args.no_notify), as_json=args.json)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("未捕获异常：%s\n" % e)
        sys.exit(1)
