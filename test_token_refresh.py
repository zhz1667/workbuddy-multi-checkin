# -*- coding: utf-8 -*-
"""离线单测：v1.0.5 Token 自动续期 / 临期告警（不联网、不写真实配置）。"""
import io, sys, os, json, base64, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:/workbuddy space/wb-checkin-multi")
import wb_checkin_multi as w

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("✓" if cond else "✗", name, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def mk_token(uid, days):
    """造一个只有 exp/sub 的假 JWT（decode_jwt_exp 不验签）。"""
    exp = int(time.time() + days * 86400)
    return "%s.%s.%s" % (b64({"alg": "HS256", "typ": "JWT"}),
                         b64({"exp": exp, "iat": exp - 30 * 86400, "sub": uid}),
                         "sig")


def rec(uid, days):
    """造一条 scan_auth_files 风格的候选记录。"""
    ok, left, exp_str = w.token_status(mk_token(uid, days))
    return {"uid": uid, "token": mk_token(uid, days), "left": left, "valid": ok,
            "exp_str": exp_str, "domain": "copilot.tencent.com", "mtime": 0}


# 假 uid：故意不用 UUID 形状，避免与仓库的凭据防护钩子（会拦 UUID 字面量）冲突
UID_X = "fake-uid-alpha"
UID_Y = "fake-uid-beta"

# 把网络/磁盘副作用全掐掉
w.save_config = lambda cfg: None
SCAN = {"n": 0, "found": [], "raise": None}


def fake_scan(extra_dir=None):
    SCAN["n"] += 1
    if SCAN["raise"]:
        raise SCAN["raise"]
    return SCAN["found"], {"files": len(SCAN["found"]), "plain": len(SCAN["found"]),
                           "encrypted": 0, "expired": 0, "broken": 0}


w.scan_auth_files = fake_scan


def reset(found=None, raise_=None):
    SCAN["n"], SCAN["found"], SCAN["raise"] = 0, found or [], raise_


# ---------- 1) 已过期账号：能从本机登录态续期 ----------
reset([rec(UID_X, 25)])
acc = {"id": "x", "label": "张三", "uid": UID_X, "token": mk_token(UID_X, -2),
       "domain": "www.workbuddy.cn"}
r = w.auto_refresh_tokens({}, [acc])
check("已过期账号被续期", len(r["refreshed"]) == 1 and not r["missing"], str(r["refreshed"]))
check("  续期后 Token 有效且剩 ~25 天", (w.token_left_days(acc["token"]) or 0) > 24,
      "剩 %.1f 天" % (w.token_left_days(acc["token"]) or 0))
check("  续期后 domain 未被改动", acc["domain"] == "www.workbuddy.cn", acc["domain"])
check("  续期后 source=auto-refresh 且写了 updated_at",
      acc.get("source") == "auto-refresh" and bool(acc.get("updated_at")))
check("  旧到期文案不是负数", "已过期" in r["refreshed"][0][1], r["refreshed"][0][1])
check("  扫描被调用 1 次", SCAN["n"] == 1, "%d 次" % SCAN["n"])

# ---------- 2) 有效期还长：完全不碰登录态、不扫描 ----------
reset([rec(UID_X, 25)])
acc = {"id": "x", "label": "张三", "uid": UID_X, "token": mk_token(UID_X, 10),
       "domain": "www.workbuddy.cn"}
old = acc["token"]
r = w.auto_refresh_tokens({}, [acc])
check("剩 10 天（>3）→ 不续期", not r["refreshed"] and not r["missing"], str(r))
check("  剩 10 天 → 根本不扫描登录态（省时且不碰凭据）", SCAN["n"] == 0, "%d 次" % SCAN["n"])
check("  Token 未变", acc["token"] == old)

# ---------- 3) 临期 2 天：允许续期 ----------
reset([rec(UID_X, 25)])
acc = {"id": "x", "label": "张三", "uid": UID_X, "token": mk_token(UID_X, 2),
       "domain": "www.workbuddy.cn"}
r = w.auto_refresh_tokens({}, [acc])
check("剩 2 天（<=3）→ 触发续期", len(r["refreshed"]) == 1, str(r["refreshed"]))

# ---------- 4) 不降级：候选更短则保持原样 ----------
reset([rec(UID_X, 1)])
acc = {"id": "x", "label": "张三", "uid": UID_X, "token": mk_token(UID_X, 2),
       "domain": "www.workbuddy.cn"}
old = acc["token"]
r = w.auto_refresh_tokens({}, [acc])
check("候选更短 → 不降级，Token 不变", acc["token"] == old and not r["refreshed"], str(r))
check("  记入 missing 便于报告", r["missing"] == ["张三"], str(r["missing"]))

# ---------- 5) 登录态里没有该 uid：保持原样、不报错 ----------
reset([rec(UID_Y, 25)])
acc = {"id": "x", "label": "张三", "uid": UID_X, "token": mk_token(UID_X, -1),
       "domain": "www.workbuddy.cn"}
old = acc["token"]
r = w.auto_refresh_tokens({}, [acc])
check("捞不到 → 不抛异常、Token 不变", acc["token"] == old and r["missing"] == ["张三"], str(r))

# ---------- 6) 候选本身已过期：跳过 ----------
reset([{"uid": UID_X, "token": mk_token(UID_X, -5), "left": -5 * 86400,
        "valid": False, "exp_str": "x", "domain": None, "mtime": 0}])
acc = {"id": "x", "label": "张三", "uid": UID_X, "token": mk_token(UID_X, -1),
       "domain": "www.workbuddy.cn"}
old = acc["token"]
r = w.auto_refresh_tokens({}, [acc])
check("候选已过期 → 不采用", acc["token"] == old and not r["refreshed"], str(r))

# ---------- 7) 扫描抛异常：静默兜底，不影响主流程 ----------
reset([], raise_=RuntimeError("boom"))
acc = {"id": "x", "label": "张三", "uid": UID_X, "token": mk_token(UID_X, -1),
       "domain": "www.workbuddy.cn"}
try:
    r = w.auto_refresh_tokens({}, [acc])
    ok = not r["refreshed"]
except Exception:
    ok = False
check("扫描异常 → 静默返回、不炸主流程", ok)

# ---------- 8) 多账号混合：只动该动的 ----------
reset([rec(UID_X, 25), rec(UID_Y, 28)])
a1 = {"id": "a", "label": "甲", "uid": UID_X, "token": mk_token(UID_X, -1), "domain": "d1"}
a2 = {"id": "b", "label": "乙", "uid": UID_Y, "token": mk_token(UID_Y, 20), "domain": "d2"}
t2 = a2["token"]
r = w.auto_refresh_tokens({}, [a1, a2])
check("混合场景只续期临期的那一个",
      [x[0] for x in r["refreshed"]] == ["甲"] and a2["token"] == t2, str(r["refreshed"]))
check("  两个 domain 都未变", a1["domain"] == "d1" and a2["domain"] == "d2")

# ---------- 9) token_expiring 阈值（7 天） ----------
accs = [{"id": "1", "label": "6天", "token": mk_token(UID_X, 6)},
        {"id": "2", "label": "8天", "token": mk_token(UID_Y, 8)},
        {"id": "3", "label": "过期", "token": mk_token(UID_X, -1)},
        {"id": "4", "label": "无exp", "token": "not-a-jwt"}]
ex = w.token_expiring(accs)
got = dict(ex)
check("临期判定：6 天内 + 已过期在内，8 天与无 exp 在外",
      set(got) == {"6天", "过期"}, str(sorted(got)))
check("  已过期返回负数", got.get("过期", 1) <= 0, "%.2f" % got.get("过期", 0))
check("  6 天剩 5-6 天区间", 5 < got.get("6天", 0) <= 6, "%.2f" % got.get("6天", 0))

# ---------- 10) 默认配置带上开关 ----------
check("DEFAULT_CONFIG 含 token_auto_refresh=True",
      w.DEFAULT_CONFIG["settings"].get("token_auto_refresh") is True)

# ---------- 11) 通知标题分支（跑一次真实 execute_run，桩掉 IO） ----------
TOASTS, LOGS = [], []
w.desktop_toast = lambda t, b: TOASTS.append((t, b))
w.write_logs = lambda results: LOGS.append(results)


def stub_run_one(acc, settings, do_check, check_only, travel_mode, location_id,
                 activity_mode, gp):
    return {"id": acc["id"], "label": acc["label"], "status": "ok",
            "action": "already_signed", "points": 0, "balance": 100,
            "msg": "今日已签到", "travel": None, "activity": None, "greet": None}


w.run_one = stub_run_one


def run_once(accounts, notify=True):
    del TOASTS[:]
    cfg = {"settings": dict(w.DEFAULT_CONFIG["settings"]), "accounts": accounts}
    cfg["settings"]["token_auto_refresh"] = False   # 不让它偷偷换成健康 Token
    w.execute_run(cfg, accounts, do_check=False, check_only=False, travel_mode="off",
                  notify=notify, as_json=False, show_progress=False,
                  activity_mode="off", greet_mode="off")
    return TOASTS[0] if TOASTS else (None, None)


reset([])
t, b = run_once([{"id": "1", "label": "张三", "token": mk_token(UID_X, 5)}])
check("剩 5 天 → 通知标题是「凭证临期」", t and "凭证临期" in t, t)
check("  标题带剩余天数", t and "剩 5 天" in t, t)
check("  正文含 Token 临期提示", b and "Token 临期" in b, b)

t, b = run_once([{"id": "1", "label": "张三", "token": mk_token(UID_X, -1)}])
check("已过期 → 通知标题是「凭证失效」", t and "凭证失效" in t, t)
check("  正文含『已过期』且不再重复报『剩 0 天』",
      b and "Token 已过期" in b and "剩 0 天" not in b, b)

t, b = run_once([{"id": "1", "label": "张三", "token": mk_token(UID_X, 20)}])
check("Token 健康 → 标题不出现凭证字样", t and "凭证" not in t, t)
check("  正文不出现 Token 告警", b is not None and "Token" not in b, b)

t, b = run_once([{"id": "1", "label": "甲", "token": mk_token(UID_X, 5)},
                 {"id": "2", "label": "乙", "token": mk_token(UID_Y, -1)}])
check("同时有临期+过期 → 标题取更严重的「失效」", t and "凭证失效" in t, t)
check("  正文两类都列出", b and "Token 已过期" in b and "Token 临期" in b, b)

t, b = run_once([{"id": "1", "label": "张三", "token": mk_token(UID_X, 5)}], notify=False)
check("关闭通知 → 不弹窗", t is None)

print()
print("=" * 60)
print(("全部通过 ✅" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
