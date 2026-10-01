# -*- coding: utf-8 -*-
"""离线单测：问候功能的随机时刻 / 多语言随机 / 幂等 / 窗口判定（不联网）。"""
import io, sys, os, json, collections
from datetime import datetime, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:/workbuddy space/wb-checkin-multi")
import wb_checkin_multi as w

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("✓" if cond else "✗", name, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


# ---------- 1) 随机时刻必须落在 03:00-08:00 ----------
base = datetime(2026, 10, 1, 12, 0, 0)
sh, sm = w._hhmm(w.GREET_WINDOW[0])
eh, em = w._hhmm(w.GREET_WINDOW[1])
lo, hi = sh * 60 + sm, eh * 60 + em
tgts = [base.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=0)
        for _ in range(1)]
mins = []
for _ in range(3000):
    t = w._pick_target(base)
    h, m = w._hhmm(t)
    mins.append(h * 60 + m)
check("随机时刻全部落在窗口内", all(lo <= x <= hi for x in mins),
      "范围 %02d:%02d-%02d:%02d，实测 min=%02d:%02d max=%02d:%02d"
      % (sh, sm, eh, em, min(mins) // 60, min(mins) % 60, max(mins) // 60, max(mins) % 60))
check("随机时刻分布足够分散（覆盖 >50%% 的分钟）",
      len(set(mins)) > (hi - lo) * 0.5, "覆盖 %d/%d 分钟" % (len(set(mins)), hi - lo + 1))
check("随机时刻非常量", len(set(mins)) > 100)

# ---------- 2) 六种语言等概率、句子非重复 ----------
langs = list(w.GREET_PHRASES.keys())
check("语言共 6 种", len(langs) == 6, "，".join(langs))
check("六种语言齐备（汉语/英语/俄语/法语/意大利语/德语）",
      set(langs) == {"汉语", "英语", "俄语", "法语", "意大利语", "德语"})
for lg in langs:
    check("  %s 至少 3 条候选" % lg, len(w.GREET_PHRASES[lg]) >= 3,
          "%d 条" % len(w.GREET_PHRASES[lg]))

picks = [w.pick_greeting() for _ in range(6000)]
cnt = collections.Counter(l for l, _ in picks)
check("六种语言均被抽到", set(cnt) == set(langs), str(dict(cnt)))
mx, mn = max(cnt.values()), min(cnt.values())
check("语言分布大致均匀（极差 < 25%%）", (mx - mn) / (sum(cnt.values()) / 6.0) < 0.25,
      "max=%d min=%d" % (mx, mn))

# ---------- 3) 历史去重：用过的组合不再出现 ----------
pool = [(lg, t) for lg, ts in w.GREET_PHRASES.items() for t in ts]
hist = [{"lang": l, "text": t} for l, t in pool[:-1]]
last = pool[-1]
seen = {w.pick_greeting(hist) for _ in range(200)}
check("只剩 1 条未用时，优先选它", seen == {last}, "抽到 %s" % (seen,))
hist_all = [{"lang": l, "text": t} for l, t in pool]
back = {w.pick_greeting(hist_all) for _ in range(300)}
check("全部用过时回退到全量随机且不报错", len(back) > 5, "抽到 %d 种" % len(back))

# ---------- 4) 窗口/补发判定 ----------
STATE = w.GREET_STATE_PATH
backup = None
if os.path.isfile(STATE):
    backup = open(STATE, encoding="utf-8").read()
accs = [{"id": "a1", "label": "甲", "token": "x"}]

try:
    def fresh(target, date="2026-10-01"):
        w.greet_state_save({"date": date, "target": target, "accounts": {}, "history": []})

    d = datetime(2026, 10, 1, 2, 0)      # 窗口前
    fresh("05:00")
    p = w.greet_plan(accs, catchup=True, now=d)
    check("02:00 未到目标 → 不发", not p["due"] and p["reason"] == "wait", p["reason"])

    d = datetime(2026, 10, 1, 5, 30)     # 窗口内且过了目标
    p = w.greet_plan(accs, catchup=True, now=d)
    check("05:30 过目标 → 窗口内发送", p["due"] and p["reason"] == "due", p["reason"])

    d = datetime(2026, 10, 1, 4, 0)      # 窗口内但未到目标
    fresh("06:00")
    p = w.greet_plan(accs, catchup=True, now=d)
    check("04:00 未到目标(06:00) → 不发", not p["due"] and p["reason"] == "wait", p["reason"])

    d = datetime(2026, 10, 1, 9, 0)      # 过窗口，开补发
    fresh("05:00")
    p = w.greet_plan(accs, catchup=True, now=d)
    check("09:00 过窗口 + 开补发 → 补发", p["due"] and p["reason"] == "catchup", p["reason"])

    p = w.greet_plan(accs, catchup=False, now=d)
    check("09:00 过窗口 + 关补发 → 不发", not p["due"] and p["reason"] == "window_missed", p["reason"])

    # 幂等：当天已成功则不再发
    fresh("05:00")
    st = w.greet_state_load()
    st["accounts"] = {"a1": {"ok": True, "at": "2026-10-01 05:01", "lang": "俄语", "text": "Привет"}}
    w.greet_state_save(st)
    p = w.greet_plan(accs, catchup=True, now=datetime(2026, 10, 1, 9, 0))
    check("当天已发送 → 不重复发", not p["due"] and p["reason"] == "already_sent", p["reason"])
    p = w.greet_plan(accs, catchup=True, force=True, now=datetime(2026, 10, 1, 9, 0))
    check("--greet 强制 → 忽略已发", p["due"] and p["reason"] == "forced", p["reason"])

    # 重试上限
    fresh("05:00")
    st = w.greet_state_load()
    st["accounts"] = {"a1": {"ok": False, "attempts": w.GREET_MAX_ATTEMPTS, "err": "boom"}}
    w.greet_state_save(st)
    p = w.greet_plan(accs, catchup=True, now=datetime(2026, 10, 1, 9, 0))
    check("重试次数用尽 → 不再发", not p["due"] and p["reason"] == "already_sent", p["reason"])

    # 跨天重置
    fresh("05:00", date="2026-09-30")
    p = w.greet_plan(accs, catchup=True, now=datetime(2026, 10, 1, 9, 0))
    check("跨天自动重置并可补发", p["due"] and p["reason"] == "catchup", p["reason"])

    # 当天随机时刻持久化后再取应不变
    fresh("05:00")
    t1 = w.greet_state_today(datetime(2026, 10, 1, 1, 0))["target"]
    t2 = w.greet_state_today(datetime(2026, 10, 1, 7, 0))["target"]
    check("当天随机时刻持久化后不变", t1 == t2, "%s vs %s" % (t1, t2))
finally:
    if backup is not None:
        open(STATE, "w", encoding="utf-8").write(backup)
    elif os.path.isfile(STATE):
        os.remove(STATE)

print()
print("=" * 60)
print(("全部通过 ✅" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
