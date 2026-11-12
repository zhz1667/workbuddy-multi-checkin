# -*- coding: utf-8 -*-
"""离线单测：只读接口的重试与错误原因（v1.0.11，不联网）。"""
import io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:/workbuddy space/wb-checkin-multi")
import wb_checkin_multi as w

fails = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("✓" if cond else "✗", name, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


ORIG = w.api_call
WAITS = (0, 0.01, 0.01)          # 测试用极短等待


def stub(seq):
    """按顺序返回预设结果，并记录调用次数。seq 元素：('ok',data) / ('code',st,body) / ('exc',e)"""
    state = {"n": 0}

    def _call(base, path, token, payload=None, method="POST", timeout=None):
        i = state["n"]
        state["n"] += 1
        item = seq[min(i, len(seq) - 1)]
        if item[0] == "ok":
            return 200, {"code": 0, "data": item[1]}
        if item[0] == "code":
            return item[1], item[2]
        raise item[1]

    return _call, state


try:
    # 1) 一次成功 → 只调用 1 次
    w.api_call, st = stub([("ok", {"state": "idle"})])
    data, err = w._read_retry(w.TRAVEL_BASE, w.TRAVEL_STATUS_PATH, "t", waits=WAITS)
    check("首次成功 → 只请求 1 次", st["n"] == 1 and err is None, "请求 %d 次" % st["n"])
    check("  返回数据正确", data == {"state": "idle"})

    # 2) 前两次失败、第三次成功 → 调用 3 次
    w.api_call, st = stub([("code", 502, {"code": 502}),
                           ("exc", TimeoutError("timed out")),
                           ("ok", {"state": "traveling"})])
    data, err = w._read_retry(w.TRAVEL_BASE, w.TRAVEL_STATUS_PATH, "t", waits=WAITS)
    check("两次失败后成功 → 共请求 3 次", st["n"] == 3, "请求 %d 次" % st["n"])
    check("  重试后拿到数据", data == {"state": "traveling"} and err is None)

    # 3) 一直失败 → 返回 None + 具体原因
    w.api_call, st = stub([("code", 503, {"code": 503})])
    data, err = w._read_retry(w.TRAVEL_BASE, w.TRAVEL_STATUS_PATH, "t", waits=WAITS)
    check("始终失败 → 请求 3 次后放弃", st["n"] == 3 and data is None, "请求 %d 次" % st["n"])
    check("  错误原因含 HTTP 状态", err and "503" in err, str(err))

    # 4) 异常也要重试，且原因写明异常类型
    w.api_call, st = stub([("exc", TimeoutError("timed out"))])
    data, err = w._read_retry(w.GROWTH_BASE, w.GROWTH_HEATMAP_PATH, "t", waits=WAITS)
    check("网络异常同样重试 3 次", st["n"] == 3, "请求 %d 次" % st["n"])
    check("  原因含异常类型", err and "TimeoutError" in err, str(err))

    # 5) 业务错误码要带出来（便于区分 Token 失效）
    w.api_call, st = stub([("code", 200, {"code": 10001, "msg": "today already"})])
    data, err = w._read_retry(w.TRAVEL_BASE, w.TRAVEL_STATUS_PATH, "t", waits=WAITS)
    check("业务码非 0 → 原因里带 code", err and "code=10001" in err, str(err))

    # 6) travel_auto 的失败文案要含真实原因（不再笼统说「Token 失效」）
    w.api_call, st = stub([("code", 500, {"code": 500})])
    out = w.travel_auto("t", read_only=True)
    msg = " ".join(out.get("auto_log") or [])
    check("travel_auto 失败时写明 HTTP 500", "HTTP 500" in msg, msg[:60])
    check("  travel_auto 标记不可用", out.get("available") is False)
finally:
    w.api_call = ORIG

print()
print("=" * 60)
print(("全部通过 ✅" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
