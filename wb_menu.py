#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号签到 · 交互式菜单（CLI 向导）

面向不想记命令行参数的场景：双击 启动菜单.bat，或运行
    python wb_menu.py
    python wb_checkin_multi.py --menu

菜单只做「选择 + 展示」，所有业务逻辑仍在 wb_checkin_multi.py 里，两者共用同一实现，
不存在两套代码走偏的问题。计划任务的无人值守行为不受影响。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import wb_checkin_multi as core  # noqa: E402

TASK_NAME = "WorkBuddy-MultiCheckin"
LOCATION_NAMES = {1: "咖啡馆", 2: "商场店铺", 3: "健身房", 4: "古镇客栈"}


# ---------------- 终端适配 ----------------
class C:
    """ANSI 配色；不支持时自动降级为无色。"""
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[36m"
    _on = True

    @classmethod
    def w(cls, text, color):
        if not cls._on:
            return str(text)
        return "%s%s%s" % (color, text, cls.RESET)


def setup_console():
    """Windows 控制台切到 UTF-8 + 启用 ANSI，保证中文与颜色正常。"""
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
            handle = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(handle, ctypes.byref(mode)):
                k.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:  # noqa: BLE001
            C._on = False
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        C._on = False


def clear_screen():
    if sys.stdout.isatty():
        os.system("cls" if sys.platform == "win32" else "clear")


def line(char="─", width=78):
    print(C.w(char * width, C.DIM))


def pause(msg="按回车返回…"):
    try:
        input("\n" + C.w(msg, C.DIM))
    except (EOFError, KeyboardInterrupt):
        print()


def ask(prompt, default=None):
    """读取一行输入；直接回车取默认值。"""
    suffix = " [%s]" % default if default not in (None, "") else ""
    try:
        raw = input("%s%s: " % (prompt, suffix)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return raw or default


def ask_int(prompt, default, lo, hi):
    raw = ask(prompt, default)
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        print(C.w("  输入不是整数，使用默认值 %s" % default, C.YELLOW))
        return default
    if not (lo <= v <= hi):
        print(C.w("  超出范围 %s-%s，使用默认值 %s" % (lo, hi, default), C.YELLOW))
        return default
    return v


def confirm(prompt):
    try:
        return input("%s (y/N): " % prompt).strip().lower() in ("y", "yes", "是")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------- 展示 ----------------
def header():
    clear_screen()
    cfg = core.load_config()
    accounts = cfg["accounts"]
    enabled = [a for a in accounts if a.get("enabled", True)]
    warn = 0
    for a in accounts:
        ok, left, _ = core.token_status(core.clean_token(a.get("token")))
        if not ok or (left is not None and left < core.EXPIRY_WARN_DAYS * 86400):
            warn += 1
    line("═")
    print("  %s   %s" % (C.w("WorkBuddy 多账号签到助手 · 交互菜单", C.BOLD),
                         C.w("v%s" % core.VERSION, C.DIM)))
    line("═")
    print("  账号 %s 个（启用 %s）  ｜  桌面通知 %s  ｜  定时任务 %s"
          % (C.w(len(accounts), C.BOLD), len(enabled),
             C.w("开" if cfg["settings"].get("desktop_notify", True) else "关", C.GREEN),
             task_short_state()))
    if accounts:
        if warn:
            print("  Token：%s" % C.w("有 %d 项需处理（过期 / 临近过期）" % warn, C.YELLOW))
        else:
            print("  Token：%s" % C.w("全部正常", C.GREEN))
    else:
        print("  %s" % C.w("尚未配置账号 → 请选「6 → 1」从本机登录态提取", C.YELLOW))
    line("═")


def print_accounts():
    cfg = core.load_config()
    accounts = cfg["accounts"]
    if not accounts:
        print(C.w("  还没有账号。请先选「6 → 1」从本机登录态提取，或「6 → 2」手工添加。", C.YELLOW))
        return
    print("  %s %s %s %s %s %s"
          % (core.pad("编号", 6), core.pad("账号名", 16), core.pad("uid", 11),
             core.pad("状态", 8), core.pad("派遣地点", 12), "Token"))
    line()
    for idx, acc in enumerate(accounts, 1):
        ok, left, exp_str = core.token_status(core.clean_token(acc.get("token")))
        if not acc.get("token"):
            tstate, color = "未配置", C.RED
        elif not ok:
            tstate, color = "已过期", C.RED
        elif left is not None and left < core.EXPIRY_WARN_DAYS * 86400:
            tstate, color = "即将过期", C.YELLOW
        else:
            tstate, color = "正常", C.GREEN
        loc = acc.get("location_id")
        print("  %s %s %s %s %s %s"
              % (core.pad(idx, 6), core.pad(acc.get("label"), 16),
                 core.pad(core.mask_id(acc.get("uid")), 11),
                 core.pad("启用" if acc.get("enabled", True) else "停用", 8),
                 core.pad(LOCATION_NAMES.get(loc, "随机"), 12),
                 C.w(tstate, color) + ("   %s 到期（剩 %s）" % (exp_str, core.fmt_duration(left))
                                       if exp_str and ok else "")))
    print("  %s" % C.w("配置：%s" % core.CONFIG_PATH, C.DIM))


def run_and_show(check_only=False, do_check=True, travel_mode="auto",
                 accounts=None, location_id=None, title="执行结果"):
    cfg = core.load_config()
    accs = accounts if accounts is not None else core.select_accounts(cfg)
    if not accs:
        print(C.w("  没有可执行的账号。", C.YELLOW))
        return
    print()
    print(C.w("  ▶ %s（%d 个账号）" % (title, len(accs)), C.BOLD))
    print()
    core.execute_run(cfg, accs, do_check=do_check, check_only=check_only,
                     travel_mode=travel_mode, location_id=location_id,
                     show_progress=True,
                     notify=bool(cfg["settings"].get("desktop_notify", True)))


# ---------------- 各菜单动作 ----------------
def act_run_full():
    run_and_show(title="签到 + 派小猫（全部启用账号）")


def act_run_checkin_only():
    run_and_show(travel_mode="off", title="只签到（不派小猫）")


def act_run_travel_only():
    run_and_show(do_check=False, travel_mode="auto", title="只派小猫旅行")


def act_run_status_only():
    run_and_show(check_only=True, do_check=True, travel_mode="readonly", title="只查询状态（只读）")


def act_run_selected():
    cfg = core.load_config()
    accounts = cfg["accounts"]
    if not accounts:
        print(C.w("  还没有账号。", C.YELLOW))
        return
    print_accounts()
    raw = ask("\n  输入要执行的编号（逗号分隔，如 1,3；直接回车=全部启用账号）", "")
    if not raw:
        run_and_show(title="签到 + 派小猫（全部启用账号）")
        return
    picked = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(accounts):
            picked.append(accounts[int(part) - 1])
        else:
            m = core.find_account(cfg, part)
            if m:
                picked.append(m)
    if not picked:
        print(C.w("  没有匹配到账号。", C.YELLOW))
        return
    print(C.w("  已选：%s" % "，".join(a.get("label") for a in picked), C.GREEN))
    mode = ask("  模式 1=签到+派小猫  2=只签到  3=只派小猫  4=只查询", "1")
    table = {"1": (True, False, "auto", "签到 + 派小猫"),
             "2": (True, False, "off", "只签到"),
             "3": (False, False, "auto", "只派小猫"),
             "4": (True, True, "readonly", "只查询状态")}
    do_check, check_only, travel_mode, label = table.get(str(mode).strip(), table["1"])
    run_and_show(check_only=check_only, do_check=do_check, travel_mode=travel_mode,
                 accounts=picked, title=label)


# --- 账号管理 ---
def act_capture():
    print()
    print(C.w("  从本机 WorkBuddy 登录态文件中提取 Token（只读，按账号归组）…", C.DIM))
    print()
    code = core.cmd_capture(argparse.Namespace(capture_dir=None, label_prefix=None, keep_label=False))
    if code == 0:
        print(C.w("\n  提示：该文件含明文 Token，请勿外传。", C.YELLOW))


def act_add_account():
    print()
    label = ask("  账号名（如 小号A）")
    if not label:
        print(C.w("  已取消。", C.YELLOW))
        return
    print(C.w("  Token 获取方式：登录该账号的 WorkBuddy 客户端后，用「从本机提取」；", C.DIM))
    print(C.w("  或从任意机器复制 accessToken 粘贴到这里。", C.DIM))
    token = ask("  粘贴 accessToken（回车取消）")
    if not token:
        print(C.w("  已取消。", C.YELLOW))
        return
    domain = ask("  接口域名", "www.workbuddy.cn")
    code = core.cmd_add(argparse.Namespace(add=True, label=label, token=token,
                                           domain=domain, uid=None))
    if code == 0:
        print(C.w("  已添加。建议接着做「4 → 只查询状态」验证 Token 可用。", C.GREEN))


def act_set_token():
    cfg = core.load_config()
    if not cfg["accounts"]:
        print(C.w("  还没有账号。", C.YELLOW))
        return
    print_accounts()
    key = ask("\n  要更新哪个账号（编号或名称）")
    if not key:
        print(C.w("  已取消。", C.YELLOW))
        return
    acc = None
    if str(key).isdigit() and 1 <= int(key) <= len(cfg["accounts"]):
        acc = cfg["accounts"][int(key) - 1]
    else:
        acc = core.find_account(cfg, key)
    if not acc:
        print(C.w("  未找到账号。", C.YELLOW))
        return
    token = ask("  粘贴 %s 的新 accessToken（回车取消）" % acc.get("label"))
    if not token:
        print(C.w("  已取消。", C.YELLOW))
        return
    core.cmd_set_token(argparse.Namespace(set_token=acc.get("id"), token=token, domain=None))


def act_toggle_account():
    cfg = core.load_config()
    if not cfg["accounts"]:
        print(C.w("  还没有账号。", C.YELLOW))
        return
    print_accounts()
    key = ask("\n  要启用/停用哪个账号（编号或名称）")
    if not key:
        return
    acc = cfg["accounts"][int(key) - 1] if (str(key).isdigit() and 1 <= int(key) <= len(cfg["accounts"])) \
        else core.find_account(cfg, key)
    if not acc:
        print(C.w("  未找到账号。", C.YELLOW))
        return
    target = not acc.get("enabled", True)
    core.toggle_account(cfg, acc.get("id"), target)
    print(C.w("  %s 已%s。" % (acc.get("label"), "启用" if target else "停用"), C.GREEN))


def act_remove_account():
    cfg = core.load_config()
    if not cfg["accounts"]:
        print(C.w("  还没有账号。", C.YELLOW))
        return
    print_accounts()
    key = ask("\n  要删除哪个账号（编号或名称）")
    if not key:
        return
    acc = cfg["accounts"][int(key) - 1] if (str(key).isdigit() and 1 <= int(key) <= len(cfg["accounts"])) \
        else core.find_account(cfg, key)
    if not acc:
        print(C.w("  未找到账号。", C.YELLOW))
        return
    if not confirm("  确认从配置中删除「%s」？（仅删本地配置，不影响该账号本身）" % acc.get("label")):
        print(C.w("  已取消。", C.DIM))
        return
    core.remove_account(cfg, acc.get("id"))
    print(C.w("  已删除。", C.GREEN))


# --- 定时任务 ---
def _run_ps(command, timeout=90):
    """执行 PowerShell 命令，返回 (退出码, 输出文本)。全部按 ASCII 解析，规避编码问题。"""
    if sys.platform != "win32":
        return 1, "仅 Windows 支持"
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out.strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


_task_cache = {"at": 0.0, "installed": False, "info": {}}
_TASK_TTL = 20  # 秒：避免每次刷新菜单都启动一次 PowerShell


def task_state(force=False):
    """返回 (是否已安装, 状态字典)。结果缓存 20 秒；只取 ASCII 值，规避控制台编码问题。"""
    if sys.platform != "win32":
        return False, {}
    now = time.time()
    if not force and (now - _task_cache["at"]) < _TASK_TTL and _task_cache["at"] > 0:
        return _task_cache["installed"], _task_cache["info"]
    cmd = ("$t=Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue;"
           "if($t){$i=Get-ScheduledTaskInfo -TaskName '%s';"
           "'State='+$t.State;'LastRun='+$i.LastRunTime;'LastResult='+$i.LastTaskResult;"
           "'NextRun='+$i.NextRunTime}else{'NOT_INSTALLED'}" % (TASK_NAME, TASK_NAME))
    code, out = _run_ps(cmd, timeout=40)
    if code != 0 or "NOT_INSTALLED" in out or not out:
        _task_cache.update(at=now, installed=False, info={})
        return False, {}
    info = {}
    for ln in out.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            info[k.strip()] = v.strip()
    _task_cache.update(at=now, installed=True, info=info)
    return True, info


def task_short_state():
    if sys.platform != "win32":
        return C.w("不支持（非 Windows）", C.DIM)
    installed, info = task_state()
    if not installed:
        return C.w("未安装", C.YELLOW)
    nxt = info.get("NextRun", "")
    if nxt and len(nxt) > 16:
        nxt = nxt[:16]
    return C.w("已安装", C.GREEN) + C.w("（下次 %s）" % nxt if nxt else "", C.DIM)


def act_task_install():
    cur = core.load_config()["settings"].get("task_time", "09:00")
    t = ask("  每日执行时间（HH:MM，24 小时制）", cur)
    try:
        datetime.strptime(str(t), "%H:%M")
    except ValueError:
        print(C.w("  时间格式不对，应为 HH:MM。", C.RED))
        return
    ps1 = os.path.join(HERE, "install_task.ps1")
    if not os.path.isfile(ps1):
        print(C.w("  未找到 install_task.ps1。", C.RED))
        return
    was_installed, _ = task_state(force=True)
    print(C.w("  正在%s计划任务（当前用户上下文，无需管理员）…"
              % ("更新" if was_installed else "注册"), C.DIM))
    code, _ = _run_ps("& '%s' -Time '%s'" % (ps1, t), timeout=120)
    installed, info = task_state(force=True)
    if code == 0 and installed:
        core.set_setting(core.load_config(), "task_time", str(t))
        print(C.w("  ✓ 已%s定时任务：每日 %s 自动签到（无需 WorkBuddy 运行）"
                  % ("更新" if was_installed else "安装", t), C.GREEN))
        print("    下次执行：%s" % info.get("NextRun", "-"))
    else:
        print(C.w("  ✗ 注册失败（退出码 %s）。可手动执行：" % code, C.RED))
        print("    powershell -ExecutionPolicy Bypass -File \"%s\" -Time %s" % (ps1, t))


def act_task_uninstall():
    ps1 = os.path.join(HERE, "install_task.ps1")
    if not confirm("  确认卸载定时任务 %s？" % TASK_NAME):
        print(C.w("  已取消。", C.DIM))
        return
    code, _ = _run_ps("& '%s' -Uninstall" % ps1, timeout=90)
    installed, _info = task_state(force=True)
    if not installed:
        print(C.w("  ✓ 定时任务已卸载。", C.GREEN))
    else:
        print(C.w("  ✗ 卸载失败（退出码 %s）。" % code, C.RED))


def act_task_status():
    if sys.platform != "win32":
        print(C.w("  定时任务功能仅支持 Windows。其它系统可用 cron 调用脚本。", C.YELLOW))
        return
    installed, info = task_state(force=True)
    if not installed:
        print(C.w("  未安装定时任务。可在「7 → 1」安装。", C.YELLOW))
        return
    print("  任务名    ：%s" % TASK_NAME)
    print("  状态      ：%s" % C.w(info.get("State", "-"), C.GREEN))
    print("  上次执行  ：%s" % info.get("LastRun", "-"))
    print("  上次结果  ：%s%s"
          % (info.get("LastResult", "-"),
             C.w("  （0 = 成功）", C.DIM)))
    print("  下次执行  ：%s" % info.get("NextRun", "-"))


def act_task_run_now():
    if sys.platform != "win32":
        print(C.w("  定时任务功能仅支持 Windows。", C.YELLOW))
        return
    installed, _ = task_state(force=True)
    if not installed:
        print(C.w("  尚未安装定时任务，请先安装。", C.YELLOW))
        return
    print(C.w("  正在触发一次试跑…", C.DIM))
    _run_ps("Start-ScheduledTask -TaskName '%s'" % TASK_NAME, timeout=40)
    wait = ask_int("  等待多少秒后查看结果", 10, 3, 120)
    time.sleep(wait)
    _installed, info = task_state(force=True)
    print("  上次结果：%s" % info.get("LastResult", "-"))
    act_last_result(show_header=False)


# --- 日志 ---
def act_last_result(show_header=True):
    path = os.path.join(core.LOG_DIR, "last_run.json")
    if not os.path.isfile(path):
        print(C.w("  还没有运行记录。先执行一次签到。", C.YELLOW))
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(C.w("  读取失败：%s" % e, C.RED))
        return
    if show_header:
        print("  最近一次运行：%s" % C.w(data.get("time", "-"), C.BOLD))
    line()
    for r in data.get("results", []):
        mark = C.w("✓", C.GREEN) if r.get("status") == "ok" else C.w("✗", C.RED)
        print("  %s [%s] %s" % (mark, r.get("id"), r.get("label")))
        if r.get("status") != "ok":
            print("      %s" % C.w(r.get("msg", ""), C.RED))
            continue
        bits = []
        if r.get("action") == "signed":
            bits.append("新签到 +%s" % (r.get("points") or 0))
        elif r.get("action") == "already_signed":
            bits.append("今日已签")
        elif r.get("action") == "check_only":
            bits.append("状态已查")
        if r.get("balance") is not None:
            bits.append("余额 %s" % r["balance"])
        if r.get("streak_days"):
            bits.append("连续 %s 天" % r["streak_days"])
        tv = r.get("travel") or {}
        if tv.get("state_text"):
            t = "小猫 %s" % tv["state_text"]
            if tv.get("location_name"):
                t += "·%s" % tv["location_name"]
            if tv.get("remaining_text"):
                t += "（还需 %s）" % tv["remaining_text"]
            bits.append(t)
        print("      %s" % "｜".join(str(b) for b in bits))
    csv_path = os.path.join(core.LOG_DIR, "checkin-%s.csv" % datetime.now().strftime("%Y-%m"))
    print("\n  %s" % C.w("完整明细：%s" % csv_path, C.DIM))


# --- 设置 ---
def act_settings_concurrency():
    cfg = core.load_config()
    cur = cfg["settings"].get("concurrency", 4)
    v = ask_int("  并发线程数（1-10，账号多可调大）", cur, 1, 10)
    core.set_setting(core.load_config(), "concurrency", v)
    print(C.w("  ✓ 已设为 %d" % v, C.GREEN))


def act_settings_notify():
    cfg = core.load_config()
    cur = bool(cfg["settings"].get("desktop_notify", True))
    core.set_setting(core.load_config(), "desktop_notify", not cur)
    print(C.w("  ✓ 桌面通知已%s" % ("关闭" if cur else "开启"), C.GREEN))


def act_settings_location():
    cfg = core.load_config()
    if not cfg["accounts"]:
        print(C.w("  还没有账号。", C.YELLOW))
        return
    print_accounts()
    key = ask("\n  给哪个账号设置固定派遣地点（编号或名称）")
    if not key:
        return
    acc = cfg["accounts"][int(key) - 1] if (str(key).isdigit() and 1 <= int(key) <= len(cfg["accounts"])) \
        else core.find_account(cfg, key)
    if not acc:
        print(C.w("  未找到账号。", C.YELLOW))
        return
    print("  地点：1 咖啡馆 / 2 商场店铺 / 3 健身房 / 4 古镇客栈 / random 随机")
    v = ask("  选择", "random")
    try:
        loc = None if str(v).strip().lower() in ("random", "r", "0", "") else int(v)
        if loc is not None and loc not in (1, 2, 3, 4):
            raise ValueError
    except ValueError:
        print(C.w("  输入无效。", C.RED))
        return
    core.set_account_location(cfg, acc.get("id"), loc)
    print(C.w("  ✓ %s 的派遣地点：%s" % (acc.get("label"), LOCATION_NAMES.get(loc, "随机")), C.GREEN))


def act_diagnose():
    print()
    core.cmd_diagnose(argparse.Namespace(json=False, capture_dir=None))


def act_help():
    print("""
  %s

  · 首次使用：进「账号管理 → 从本机登录态提取」，一次性把本机登录过的账号全抓进来。
  · 日常使用：直接用「1」一键签到 + 派小猫；想定时自动跑就装「7 → 1」定时任务。
  · Token 有效期约 30 天，到期前一周菜单顶部会提示；届时登录一次客户端再「6 → 1」即可。
  · 计划任务与本菜单互不影响：任务跑的是无参数模式，不会弹出交互界面。

  %s
""" % (C.w("使用要点", C.BOLD), C.w("完整说明见 README.md", C.DIM)))


# ---------------- 菜单框架 ----------------
def submenu(title, items):
    """构造一个子菜单入口，供上层菜单列表使用（子菜单返回时不重复停顿）。"""
    def _open():
        menu(title, items, back_key="0")
    _open._is_submenu = True
    return _open


def menu(title, items, back_key="0", back_label="返回上级"):
    """渲染并循环处理一个菜单。

    items: [(key, 标签, 可调用对象)]。
    按下 back_key 返回 True（主菜单=退出程序，子菜单=回上级）；stdin 结束返回 False。
    """
    while True:
        header()
        print(C.w("  %s" % title, C.BOLD))
        line()
        for key, label, _fn in items:
            print("   %s. %s" % (C.w(str(key).rjust(2), C.BLUE), label))
        print("   %s. %s" % (C.w(str(back_key).rjust(2), C.BLUE), back_label))
        try:
            choice = input("\n  请选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if choice == back_key or choice == "":
            return True
        hit = [fn for k, _l, fn in items if k == choice]
        if not hit:
            print(C.w("  无效选择：%s" % choice, C.YELLOW))
            time.sleep(0.8)
            continue
        fn = hit[0]
        try:
            fn()
        except KeyboardInterrupt:
            print()
        except Exception as e:  # noqa: BLE001
            print(C.w("  执行出错：%s" % e, C.RED))
        if not getattr(fn, "_is_submenu", False) and sys.stdin.isatty():
            pause()


def main():
    setup_console()
    sub_accounts = submenu("账号管理", [
        ("1", "从本机登录态提取 Token（批量，推荐）", act_capture),
        ("2", "手工添加账号", act_add_account),
        ("3", "更新某个账号的 Token", act_set_token),
        ("4", "启用 / 停用账号", act_toggle_account),
        ("5", "删除账号", act_remove_account),
        ("6", "查看账号列表", lambda: print_accounts()),
    ])
    sub_task = submenu("定时任务（Windows 计划任务，独立于 WorkBuddy）", [
        ("1", "安装 / 修改每日自动签到时间", act_task_install),
        ("2", "卸载定时任务", act_task_uninstall),
        ("3", "查看任务状态", act_task_status),
        ("4", "立即试跑一次", act_task_run_now),
    ])
    sub_settings = submenu("设置", [
        ("1", "并发线程数", act_settings_concurrency),
        ("2", "切换桌面通知", act_settings_notify),
        ("3", "设置某账号的派遣地点", act_settings_location),
    ])
    items = [
        ("1", "立即签到 + 派小猫（全部启用账号）", act_run_full),
        ("2", "只签到（不派小猫）", act_run_checkin_only),
        ("3", "只派小猫旅行", act_run_travel_only),
        ("4", "只查询状态（只读，不领取）", act_run_status_only),
        ("5", "选择账号执行…", act_run_selected),
        ("6", "账号管理…", sub_accounts),
        ("7", "定时任务…（每日自动签到）", sub_task),
        ("8", "查看账号与 Token 有效期", lambda: print_accounts()),
        ("9", "查看最近一次运行结果 / 日志", act_last_result),
        ("10", "设置…（并发 / 桌面通知 / 派遣地点）", sub_settings),
        ("11", "环境自检", act_diagnose),
        ("12", "使用帮助", act_help),
    ]
    menu("主菜单", items, back_key="0", back_label="退出")
    print(C.w("\n  已退出。\n", C.DIM))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
