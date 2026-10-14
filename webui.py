# -*- coding: utf-8 -*-
"""
巡检工具 · 本地网页服务（看结果 + 改配置，一个页面搞定）
========================================================
以前改配置要手工编辑 config.json，容易改坏（少个逗号就整个文件读不出来）。
现在把脚本跑成一个小服务，页面上点「设置」就能改应用信息、发版基线、
渠道清单与顺序、人工登记表：

    ┌─ 服务 ──────────────────────────────────────────────┐
    │  GET  /report       →  巡检结果页（每次访问现渲染） │
    │  GET  /api/config   →  当前配置（给弹窗填表单用）   │
    │  POST /api/config   →  保存配置（校验+备份+原子写） │
    │  POST /api/scan     →  后台重新巡检（不阻塞页面）   │
    │  GET  /api/state    →  巡检进度（页面轮询这个）     │
    │  GET  /healthz      →  存活探针（给监控/看门狗用）  │
    └─────────────────────────────────────────────────────┘

几种跑法：
- 默认：只监听 127.0.0.1，本机自己看。
- `--lan`（= `--host 0.0.0.0`）：开放给同网段，**用自己手机看结果时用它**。
  ⚠️ 该模式**没有任何登录校验**，同一网络下谁都能打开这个页面、改你的配置。
  只在可信的家用 Wi-Fi / 手机热点下用，别在公司或公共网络开。
- `--strict-port`：端口被占用就报错退出、不自动顺延。
- `--no-browser`：不自动开浏览器（无桌面会话的环境）。

几个刻意的设计（都不是随手写的）：

1. **默认只监听 127.0.0.1**，所以默认不需要账号密码——外人根本连不上。
   要用 `--lan` 显式开放给同网段（例如自己手机看结果）：那种模式下**同网段任何人都能
   改发版基线、渠道清单和人工登记表**，而这几样直接决定报告结论，所以才配套做了
   第 6、7 两条兜底——改动记日志（含来源 IP）、保存前自动备份。
2. **设置页只改白名单里的键**（应用信息 / 发版基线 / 渠道清单与顺序 / 各市场应用 ID /
   人工登记表）。其余结构性配置（timeout 等）不在界面上开放，改那些仍请直接编辑
   config.json，免得手滑把抓取配置改坏。
3. **保存前自动备份**：每次写入先把旧文件另存到 backup/config_<时间>.json，
   改错了能拿回来；写入用「先写临时文件再改名」的原子方式，
   避免写一半断电留下半个坏文件。
4. **先校验再落盘**：日期必须是合法的 年-月-日，构建号必须是数字，
   渠道 key 不能重复、抓取器必须真实存在。不合法就整条拒绝并说明原因，
   绝不留半截状态。
5. **纯标准库**（http.server），不需要 pip 装任何东西，也方便打包进 exe。
6. **保存配置全程加锁**。「读最新配置 → 校验 → 备份 → 写入」这一串必须在锁里跑完，
   否则两个人同时点保存（或同时开两个窗口）会出现「A 读到的基准里没有 B 的改动，
   A 写回去就把 B 的改动抹了」——而且**不报错、页面显示成功**，
   属于最难查的一类问题。服务是 ThreadingHTTPServer，并发是真会发生的。
7. **每次都写日志文件**（logs/server_<日期>.log）。出问题只能靠日志；
   多人访问模式下「谁改了配置」也只能靠它追溯，所以改配置的请求会把来源 IP 一起记下来。
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# check_review_status.py 会把自己注入进来（用注入而不是 import，
# 免得两个文件互相 import 绕成一个环）
CORE = None

LOCK = threading.RLock()
STATE = {
    "scanning": False,     # 是否正在巡检
    "progress": "",        # 当前进度文字（页面轮询显示）
    "rows": None,          # 最近一次巡检的结果（内存里留着，页面直接渲染）
    "rows_cfg": None,      # 那次巡检用的配置
    "render_time": None,   # 那次巡检的时间
    "last_run": None,
    "error": None,
    "lan": False,          # 是否处于「内网可访问」模式（页面据此提示）
    "log": None,           # 当前日志文件路径（页面显示，方便上服务器查）
    "notify_line": "",     # 最近一次巡检的推送结果（没配通知就是空字符串）
}

HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 8765
LAN_HOST = "0.0.0.0"

# 界面上允许改的键 —— 白名单，其余键一个都不碰
EDITABLE_KEYS = ("app_name", "android_pkg", "ios_track_id", "ios_bundle_id",
                 "release_date", "target_version", "target_codes",
                 "channels", "app_ids", "manual_reference", "notify")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 安卓包名：至少两段、只允许字母数字下划线（不做过分严格的校验，免得挡住真实存在的怪包名）
PKG_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")
MAX_BACKUPS = 30

LOG_DIR = "logs"
LOG_KEEP_DAYS = 30


# --------------------------------------------------------------------------
# 日志：控制台 + 文件双写
# --------------------------------------------------------------------------
_LOG_LOCK = threading.Lock()
_LOG_FILE = None          # serve() 启动时定下来，避免每个请求都拼一次路径


def _log(msg, level="info"):
    """同时打到控制台和当天的日志文件。

    服务化部署（systemd / 任务计划程序 / NSSM）后没有控制台可看，
    文件是唯一的排障线索，所以「写文件失败」不能反过来把服务搞挂。
    """
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, file=sys.stderr if level in ("warn", "error") else sys.stdout, flush=True)
    except Exception:  # noqa: BLE001
        pass                                  # 无控制台（被重定向/服务化）时忽略
    if not _LOG_FILE:
        return
    try:
        with _LOG_LOCK:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def _init_log():
    """建日志目录、定下今天的日志文件、顺手清掉过期日志"""
    global _LOG_FILE
    d = os.path.join(CORE.HERE, LOG_DIR)
    try:
        os.makedirs(d, exist_ok=True)
        _LOG_FILE = os.path.join(d, f"server_{datetime.now().strftime('%Y-%m-%d')}.log")
        cutoff = time.time() - LOG_KEEP_DAYS * 86400
        for x in os.listdir(d):
            p = os.path.join(d, x)
            if x.startswith("server_") and x.endswith(".log"):
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        _LOG_FILE = None
    return _LOG_FILE


def log_path():
    return _LOG_FILE


# --------------------------------------------------------------------------
# 配置：读取 / 校验 / 备份 / 保存
# --------------------------------------------------------------------------
def config_path():
    return os.path.join(CORE.HERE, "config.json")


def read_config():
    """每次都用磁盘上的最新内容 —— 所以你在外部手工改了 config.json，
    服务不用重启，下一次巡检就生效。"""
    return CORE.load_config()


def needs_setup(cfg=None):
    """「第一次使用，还没填过应用信息」→ True。

    **只看应用信息填了没有，不看 config.json 在不在**：那个文件在第一次打开页面时
    就会被自动生成，若拿「文件存在」当判据，用户开了页面一看、什么都没填就关掉，
    下次启动就会被当成"已配置"，然后拿默认的示例应用去跑一遍全量抓取——
    15 家市场全返回「查无此应用」，白等几分钟还看得一头雾水。

    判据同时认「默认样板值」（示例应用 / com.example.app），所以之后把这两项清空
    也会重新回到引导状态。
    """
    cfg = read_config() if cfg is None else cfg
    name = str(cfg.get("app_name") or "").strip()
    pkg = str(cfg.get("android_pkg") or "").strip()
    return name in ("", "示例应用") or pkg in ("", "com.example.app")


def _rev_of(editable):
    """给「可改的那些键」算一个指纹。

    用途：发现「你打开设置窗口之后，别人保存过配置」。没有这道检查的话，
    两个人同时改会以最后保存的那份为准，前一个人的改动被**静默**抹掉，
    而页面还显示保存成功——事后只能靠日志和 backup/ 才发现。
    """
    blob = json.dumps(editable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _public_config(cfg, scanned_at=None, from_snapshot=False):
    """给前端弹窗用的配置视图（只含需要展示的，不含 _说明_* 这类注释键）"""
    chs = CORE.channels_from_cfg(cfg, include_disabled=True)
    editable = {
        "app_name": cfg.get("app_name") or "",
        "android_pkg": cfg.get("android_pkg") or "",
        "ios_track_id": ("" if cfg.get("ios_track_id") in (None, "")
                         else cfg.get("ios_track_id")),
        "ios_bundle_id": cfg.get("ios_bundle_id") or "",
        "release_date": cfg.get("release_date") or "",
        "target_version": cfg.get("target_version") or "",
        "target_codes": {k: (cfg.get("target_codes") or {}).get(k)
                         for k in CORE.platform_keys(cfg)},
        "channels": [{k: c[k] for k in
                      ("key", "name", "platform", "fetcher", "enabled")} for c in chs],
        "app_ids": {k: v for k, v in (cfg.get("app_ids") or {}).items()
                    if v not in (None, "")},
        "manual_reference": {c["key"]: (cfg.get("manual_reference") or {}).get(c["key"]) or ""
                             for c in chs},
        # 消息通知也放进 editable：这样它会被算进「配置指纹 rev」，
        # 别人改了通知配置时这边能发现——否则并发校验会刚好漏掉这一块。
        # webhook 是**地址**不是密钥，但它等于「谁能往你群里发消息」，别外传。
        "notify": {
            "on": bool((cfg.get("notify") or {}).get("on")),
            "mode": str((cfg.get("notify") or {}).get("mode") or "always"),
            "providers": [{"type": str(p.get("type") or "wecom"),
                           "name": str(p.get("name") or ""),
                           "webhook": str(p.get("webhook") or "")}
                          for p in ((cfg.get("notify") or {}).get("providers") or [])
                          if isinstance(p, dict)],
        },
    }
    # 需要用户去各市场后台找的 ID：按渠道生成一个输入框；百度多一个配套的 pid
    id_keys = []
    for c in chs:
        if not c["fetcher"]:
            continue
        if c["key"] not in id_keys:
            id_keys.append(c["key"])
        if c["fetcher"] == "baidu" and "百度pid" not in id_keys:
            id_keys.append("百度pid")
    return {
        "editable": editable,
        "rev": _rev_of(editable),        # 保存时原样带回来，用于并发校验
        # 首次使用：前端据此自动摊开设置弹窗、并在里面显示填写引导
        # （不进 editable，所以不参与 rev 指纹）
        "need_setup": needs_setup(cfg),
        "manual_choices": list(CORE.MANUAL_CHOICES),
        # ⚠️ channels_from_cfg() 每项里带一个 "fn"（抓取函数），**不能**直接塞进
        # JSON 里。这里只挑前端要用的字段，顺序保持原样（前端按它渲染渠道表和排序）。
        "channels": [{k: c.get(k) for k in ("key", "name", "platform", "fetcher",
                                           "enabled", "report", "search_name", "note")}
                     for c in chs],
        "platforms": CORE.platform_keys(cfg),
        "id_keys": id_keys,
        "id_hints": CORE.FETCHER_ID_HINT,
        "fetchers": sorted(CORE.FETCHERS),
        "app_ids": cfg.get("app_ids") or {},
        "config_path": config_path(),
        "scanned_at": scanned_at,
        "from_snapshot": from_snapshot,
    }


def validate_patch(patch, cfg):
    """校验前端传来的改动。返回 (干净的改动, 错误清单)。

    原则：宁可整条拒绝并说清楚原因，也不要写进一个半对不对的值。

    **只校验「请求里真的带了的键」**（真·patch 语义）。没带的键保持原样，
    不会被清空。页面弹窗每次都会把 4 个键全传上来，所以清空是靠「传空值」表达的，
    两者不冲突。反过来，若不做这层判断，有人在服务器上用 curl 只发
    `{"release_date":"..."}`，另外三个键就会被静默清空——那是个很难查的坑。
    """
    errors = []
    clean = {}

    # ① 应用信息
    if "app_name" in patch:
        v = str(patch.get("app_name") or "").strip()
        if not v:
            errors.append("应用名不能为空——各市场的搜索和报告标题都要用它")
        elif len(v) > 60:
            errors.append("应用名太长了（60 字以内）")
        else:
            clean["app_name"] = v

    if "android_pkg" in patch:
        v = str(patch.get("android_pkg") or "").strip()
        if v and not PKG_RE.match(v):
            errors.append(f"安卓包名「{v}」看起来不对，应当形如 com.example.app"
                          "（只允许字母、数字、下划线和点）")
        else:
            clean["android_pkg"] = v or None

    if "ios_track_id" in patch:
        v = str(patch.get("ios_track_id") or "").strip()
        if not v:
            clean["ios_track_id"] = None
        elif not v.isdigit():
            errors.append(f"iOS trackId 要填纯数字（现在填的是「{v}」）")
        else:
            clean["ios_track_id"] = int(v)

    if "ios_bundle_id" in patch:
        v = str(patch.get("ios_bundle_id") or "").strip()
        clean["ios_bundle_id"] = v or None

    # ② 发版日期：可留空；填了就必须是合法的 年-月-日
    if "release_date" in patch:
        raw = str(patch.get("release_date") or "").strip()
        if raw:
            if not DATE_RE.match(raw):
                errors.append("发版日期要写成「2026-09-15」这样的格式")
            else:
                try:
                    datetime.strptime(raw, "%Y-%m-%d")
                    clean["release_date"] = raw
                except ValueError:
                    errors.append(f"发版日期「{raw}」不是真实存在的日期")
        else:
            clean["release_date"] = None

    # ② 目标版本：可留空（安卓 versionName 长期不变，留空只靠日期判定也行）
    if "target_version" in patch:
        ver = str(patch.get("target_version") or "").strip()
        if len(ver) > 40:
            errors.append("目标版本太长了（40 字以内）")
        else:
            clean["target_version"] = ver or None

    # ③ 构建号：可留空；填了必须是数字。允许哪些「端」，由渠道清单决定
    if "target_codes" in patch:
        codes = patch.get("target_codes") or {}
        if not isinstance(codes, dict):
            errors.append("构建号格式不对")
        else:
            keys = list(CORE.platform_keys(cfg))
            for k in codes:                      # 允许和渠道清单一起提交新的端
                if k not in keys:
                    keys.append(str(k))
            out_codes = {}
            for k in keys:
                v = codes.get(k)
                s = "" if v is None else str(v).strip()
                if not s:
                    out_codes[k] = None
                elif not s.isdigit():
                    errors.append(f"{k}构建号要填纯数字（现在填的是「{s}」）")
                else:
                    out_codes[k] = int(s)
            clean["target_codes"] = out_codes

    # ④ 渠道清单：顺序＝报告顺序；enabled=false 的不显示也不抓。
    #    UI 只提交 5 个字段，其余字段（search_name / report / note）从这里**按 key
    #    合并回来** —— 否则保存一次就会把搜狗的 report=false 之类悄悄丢掉。
    if "channels" in patch:
        raw = patch.get("channels")
        if not isinstance(raw, list) or not raw:
            errors.append("渠道清单不能是空的（至少留一家）")
        else:
            merged, seen = [], set()
            for item in raw:
                if not isinstance(item, dict):
                    errors.append("渠道清单格式不对")
                    continue
                key = str(item.get("key") or "").strip()
                if not key:
                    errors.append("渠道清单里有条目没写 key")
                    continue
                if key in seen:
                    errors.append(f"渠道「{key}」重复出现了")
                    continue
                seen.add(key)
                fname = item.get("fetcher") or None
                if fname and fname not in CORE.FETCHERS:
                    errors.append(f"渠道「{key}」指定的抓取器「{fname}」不存在"
                                  f"（可用：{'、'.join(sorted(CORE.FETCHERS))}）")
                    continue
                old = CORE.channel_of(cfg, key) or {}
                ch = {
                    "key": key,
                    "name": str(item.get("name") or old.get("name") or key).strip(),
                    "platform": str(item.get("platform") or old.get("platform")
                                    or CORE.DEFAULT_PLATFORM).strip(),
                    "fetcher": fname,
                    "enabled": item.get("enabled", True) is not False,
                }
                sn = str(item.get("search_name") or old.get("search_name") or "").strip()
                if sn:
                    ch["search_name"] = sn
                if old.get("report") is False:
                    ch["report"] = False
                note = str(item.get("note") or old.get("note") or "").strip()
                if note:
                    ch["note"] = note
                merged.append(ch)
            if merged and not any(c.get("enabled") for c in merged):
                errors.append("至少要启用一家渠道，否则报告里一家都没有")
            if merged:
                clean["channels"] = merged

    # ⑤ 各市场应用 ID：整表替换（清空某项＝删掉它）
    if "app_ids" in patch:
        raw = patch.get("app_ids")
        if not isinstance(raw, dict):
            errors.append("应用 ID 格式不对")
        else:
            out = {}
            for k, v in raw.items():
                k = str(k).strip()
                if not k or v in (None, ""):
                    continue
                s = str(v).strip()
                out[k] = int(s) if s.isdigit() else s
            clean["app_ids"] = out

    # ⑥ 人工登记表：渠道名必须真实存在，取值必须在可选范围内
    manual = patch.get("manual_reference") or {}
    if isinstance(clean.get("channels"), list):
        known = {c["key"] for c in clean["channels"]}      # 以本次同时提交的渠道为准
    else:
        known = {c["key"] for c in CORE.channels_from_cfg(cfg, include_disabled=True)}
    if "manual_reference" in patch:
        if not isinstance(manual, dict):
            errors.append("人工登记表格式不对")
        else:
            out_manual = {}
            for k, v in manual.items():
                if k not in known:
                    errors.append(f"人工登记表里出现了未知渠道「{k}」，已拒绝保存")
                    continue
                s = "" if v is None else str(v).strip()
                if s not in CORE.MANUAL_CHOICES:
                    errors.append(f"「{k}」的登记值「{s}」不在允许范围内"
                                  f"（只能填 {'/'.join(x for x in CORE.MANUAL_CHOICES if x)}）")
                    continue
                out_manual[k] = s or None
            # 顺带把漏掉的渠道补成「未登记」，保证表结构完整
            for k in known:
                out_manual.setdefault(k, None)
            clean["manual_reference"] = out_manual

    # ⑦ 消息通知：开关 / 推送时机 / 群机器人 webhook 列表
    if "notify" in patch:
        raw = patch.get("notify")
        if not isinstance(raw, dict):
            errors.append("消息通知的配置格式不对")
        else:
            mode = str(raw.get("mode") or "always")
            if mode not in CORE.NOTIFY_MODES:
                errors.append("推送时机只能是「每次都发」或「只在有变化时才发」")
                mode = "always"
            provs, seen_hooks = [], set()
            for item in (raw.get("providers") or []):
                if not isinstance(item, dict):
                    errors.append("消息通知里有一条机器人的格式不对")
                    continue
                hook = str(item.get("webhook") or "").strip()
                if not hook:
                    continue           # webhook 为空＝「没配这一条」，直接丢掉（不算错）
                kind = str(item.get("type") or "wecom")
                if kind not in CORE.NOTIFY_TYPES:
                    errors.append(f"群机器人类型「{kind}」不认识"
                                  f"（只能是 {'、'.join(CORE.NOTIFY_TYPES)}）")
                    continue
                if not hook.lower().startswith(("http://", "https://")):
                    errors.append(f"webhook 地址要以 http:// 或 https:// 开头"
                                  f"（现在填的是「{hook[:40]}」）")
                    continue
                if hook in seen_hooks:
                    errors.append("同一个 webhook 地址填了两遍")
                    continue
                seen_hooks.add(hook)
                # 域名与所选类型明显对不上时，以**域名**为准：这几乎必然是下拉选错了，
                # 与其让消息发不出去、还得回来猜，不如直接按网址纠正过来。
                host = hook.split("//", 1)[-1].split("/", 1)[0].lower()
                for k2, hosts in CORE.NOTIFY_HOSTS.items():
                    if any(h in host for h in hosts):
                        kind = k2
                        break
                provs.append({"type": kind,
                              "name": str(item.get("name") or "").strip()
                                      or CORE.NOTIFY_TYPES[kind],
                              "webhook": hook})
            on = raw.get("on") is True
            if on and not provs:
                errors.append("通知开关打开了，但一条 webhook 都没填"
                              "——要么填一个地址，要么把开关关掉")
            else:
                clean["notify"] = {"on": on, "mode": mode, "providers": provs}

    if not errors and not clean:
        errors.append("请求里没有任何可以修改的字段（可改的是："
                      + "、".join(EDITABLE_KEYS) + "）")

    return clean, errors


def _brief(v, limit=120):
    """把改动内容压成一行简写，给日志用。

    渠道清单这种结构化值整份打印会有好几 KB —— 既撑大日志，也可能把写日志
    的那一步卡住。所以超过 limit 就截断并注明原长度。
    """
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（共 {len(s)} 字，完整内容见 backup/ 里的备份）"


def backup_config():
    """保存前先把旧配置另存一份，改错了能拿回来"""
    src = config_path()
    if not os.path.exists(src):
        return None
    bdir = os.path.join(CORE.HERE, "backup")
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(bdir, f"config_{stamp}.json")
    # 同一秒内连存两次也不覆盖前一份，加个序号
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(bdir, f"config_{stamp}_{n}.json")
        n += 1
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data)
    # 只留最近 30 份，免得越攒越多
    olds = sorted(x for x in os.listdir(bdir)
                  if x.startswith("config_") and x.endswith(".json"))
    for x in olds[:-MAX_BACKUPS]:
        try:
            os.remove(os.path.join(bdir, x))
        except OSError:
            pass
    return os.path.relpath(dst, CORE.HERE)


def save_config(patch, actor=""):
    """校验 → 备份 → 原子写入。返回 (结果, 错误清单)

    整串操作必须在 LOCK 里跑完。服务用的是 ThreadingHTTPServer，两个人
    同时点「保存」是真的会并发进来的；不锁的话，「读基准 → 改 → 写回」
    三步之间会互相覆盖——而且页面照样显示保存成功，改动却没了。
    """
    with LOCK:
        cfg = read_config()                  # 以磁盘最新内容为基准，避免覆盖掉别处的改动

        # 并发校验：调用方带了指纹、而磁盘内容已经变了 → 说明「他打开设置窗口之后
        # 别人保存过」。此时照写会把别人的改动抹掉（页面还显示成功），所以直接拒绝。
        # 页面弹窗每次都会带 rev；用 curl 手动发请求不带也没关系，跳过这层检查。
        expect = patch.get("rev") if isinstance(patch, dict) else None
        if expect and expect != _rev_of(_public_config(cfg)["editable"]):
            _log(f"  [配置] {actor or '本机'} 保存被拒：期间已被他人修改", "warn")
            return None, ["配置在你打开设置窗口之后被改动过（可能是同事刚保存了）。"
                          "已阻止本次写入，以免覆盖对方的改动——"
                          "请关掉设置窗口重新打开，看到最新内容后再改。"]

        clean, errors = validate_patch(patch, cfg)
        if errors:
            _log(f"  [配置] 拒绝保存（来自 {actor or '本机'}）：{'；'.join(errors)}", "warn")
            return None, errors

        old = {k: cfg.get(k) for k in EDITABLE_KEYS}
        cfg.update(clean)                    # 只覆盖 EDITABLE_KEYS 白名单里的键

        bak = backup_config()
        path = config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)                # 原子替换：要么全新，要么全旧，不会半截

        changed = [k for k in EDITABLE_KEYS if old.get(k) != cfg.get(k)]
        # 无鉴权模式下，「谁改了什么」只能靠日志追溯，所以把来源和改动都记全
        detail = "；".join(
            f"{k}: {_brief(old.get(k))} → {_brief(cfg.get(k))}" for k in changed)
        _log(f"  [配置] {actor or '本机'} 已保存"
             f"{'（改动 ' + detail + '）' if detail else '（内容无变化）'}"
             f"；备份 {bak}")

        return {"backup": bak, "changed": changed, "config": _public_config(cfg)}, []


# --------------------------------------------------------------------------
# 巡检：后台线程跑，页面轮询进度
# --------------------------------------------------------------------------
def _set_progress(line):
    s = (line or "").strip()
    if not s or set(s) <= set("-= "):          # 分隔线、空行不算进度
        return
    if "各市场审核巡检" in s:          # 报告标题那一行，不是进度
        return
    with LOCK:
        STATE["progress"] = s[:160]


def _scan_worker(show_raw=False, push=True):
    with LOCK:
        if STATE["scanning"]:
            return
        STATE["scanning"] = True
        STATE["error"] = None
        STATE["progress"] = "正在巡检…"
    _log("  [巡检] 开始（全量抓取，通常要几分钟）")
    try:
        cfg = read_config()                   # 每次巡检都重新读配置
        res = CORE.run_scan(cfg, show_raw=show_raw, progress=_set_progress,
                            push=push)
        with LOCK:
            STATE["rows"] = res["rows"]
            STATE["rows_cfg"] = cfg
            STATE["render_time"] = res["now"]
            STATE["last_run"] = res["now"]
            STATE["notify_line"] = res.get("notify_line") or ""
        _log(f"  [巡检] 完成 @ {res['now']}；报告：{os.path.basename(res['html_path'])}")
        if res.get("notify_line"):
            bad = [n for n in (res.get("notify") or []) if not n[1]]
            _log(f"  [通知] {res['notify_line']}", "warn" if bad else "info")
    except Exception as exc:  # noqa: BLE001
        import traceback
        with LOCK:
            STATE["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"  [巡检] 失败：{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
             "error")
    finally:
        with LOCK:
            STATE["scanning"] = False
            STATE["progress"] = ""


def start_scan(show_raw=False, push=True):
    """启动一次后台巡检；已在跑就返回 False（不排队、不叠加）

    push=False 用在**服务启动时**的自动巡检上：打开页面看一眼不该往群里发消息。
    用户主动点「↻ 重新巡检」/「保存并重新巡检」时用默认的 True。
    """
    with LOCK:
        if STATE["scanning"]:
            return False
    threading.Thread(target=_scan_worker, args=(show_raw, push),
                     daemon=True).start()
    return True


def _rows_from_snapshot():
    """内存里没有结果时，用 history/latest.json 里的上一次快照 + 当前配置重算一遍。

    这样打开页面**立刻就有内容**，不用干等几分钟的全量巡检；
    改了配置点「仅保存」也能马上看到新口径下的结果。
    """
    path = os.path.join(CORE.HERE, "history", "latest.json")
    if not os.path.exists(path):
        return None, None, None
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None, None, None
    cfg = read_config()
    rows = CORE.rows_from_snapshot(snap.get("markets") or {}, cfg)
    return rows, cfg, snap.get("checked_at")


def current_result():
    """页面要渲染的数据：(rows, cfg, 时间, 是否来自上次快照)

    ⚠ cfg 一定要返回一个**真配置**。全新部署（history/ 还是空的）时快照取不到，
    若把 None 传下去，/api/config 会在 _public_config 里直接抛 AttributeError
    → 500 → 设置弹窗打不开。而「第一次上服务器」恰恰必须先设置才能巡检，
    这条链断了就死锁了。
    """
    with LOCK:
        if STATE["rows"]:
            return STATE["rows"], STATE["rows_cfg"], STATE["render_time"], False
    rows, cfg, t = _rows_from_snapshot()
    if cfg is None:
        cfg = read_config()
    return rows, cfg, t, bool(rows)


def render_report_page():
    rows, cfg, t, from_snap = current_result()
    if not rows:
        _set_progress("")
        return _waiting_page()
    with LOCK:
        lan, lp = STATE["lan"], STATE["log"]
        nl = STATE.get("notify_line") or ""
    bits = []
    if from_snap:
        bits.append(f"以下是「上次巡检」（{t}）的抓取结果，已用当前配置重新判定——"
                    f"抓取数据是旧的，判定口径是最新的。点右上角「重新巡检」抓最新数据。")
    if lan:
        # 无鉴权 + 内网可达，页面上必须说清楚，免得有人以为是「只有我能改」
        bits.append("当前为「内网可访问」模式：同网段任何人都能打开本页并修改配置，"
                    "每次改动都会记入日志。")
    if lp:
        bits.append(f"运行日志：{lp}")
    if nl:
        bits.append(f"📣 {nl}")
    return CORE.render_html(rows, cfg, t or "（未知）",
                            notice=" ".join(bits) if bits else None)


def _error_page():
    where = log_path() or "运行服务的那个窗口"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>巡检服务 · 出错了</title>
<style>body{{margin:0;padding:60px;background:#f7f8fa;color:#1a1a1a;
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
 .box{{max-width:600px;margin:0 auto;background:#fff;border:1px solid #e6e8eb;
 border-radius:12px;padding:26px 28px;line-height:1.8}}
 code{{background:#f2f3f5;padding:2px 5px;border-radius:4px}}</style></head>
<body><div class="box"><h2 style="margin:0 0 12px;font-size:17px">服务内部出错了</h2>
<p>具体报错（含调用栈）写在运行日志里：</p>
<p><code>{htmlmod.escape(where)}</code></p>
<p>看最后一段即可，照着改就行。</p>
<p><a href="/report">点这里重试</a></p></div></body></html>"""


def _waiting_page():
    with LOCK:
        scanning = STATE["scanning"]
        err = STATE["error"]
    if err:
        body = (f"<p>巡检出错：</p><pre style='color:#c0392b'>{htmlmod.escape(err)}</pre>"
                f"<p>修好后点右上角「&#8635; 重新巡检」再试一次。</p>")
        auto = ""
    elif scanning:
        body = "<p>正在执行第一次巡检，各家应用市场页面抓取需要几分钟，页面会自动刷新…</p>"
        auto = "<script>setTimeout(function(){location.replace('/report');}, 2500);</script>"
    elif needs_setup():
        # 第一次使用：服务启动时**故意没巡检**（见 serve() 里的 needs_setup 判断）。
        # 说明清楚「为什么没有结果」+ 下一步点哪里，别让人以为是坏了。
        body = ("<p><b>第一次使用：还没有填过应用信息。</b></p>"
                "<p>已经为你打开「&#9881; 设置」——填好 <b>① 应用信息</b>"
                "（应用名 + 安卓包名）和 <b>② 本次发版基线</b>，"
                "再点右下角「<b>保存并重新巡检</b>」，就会开始第一次抓取。</p>"
                "<p class='muted'>刚才没有自动巡检，是因为还不知道要巡检哪个应用 ——"
                "拿默认的示例信息去抓，各家市场只会全部显示「查无此应用」。"
                "填过一次之后，以后每次打开都会自动巡检。</p>")
        auto = ""
    else:
        # 全新部署（history/ 还没有数据）会走到这里。
        # 注意**不要**自动刷新：那时页面会无限自我重载，什么也做不了。
        # 给出可操作的顺序：先设置基线（弹窗里的配置来自 config.json，不依赖快照），再巡检。
        body = ("<p>这台机器上还没有巡检数据（history/ 目录是空的）。</p>"
                "<p>建议顺序：先点右上角「&#9881; 设置」确认发版基线与人工登记表，"
                "再点「&#8635; 重新巡检」抓一遍各市场公开数据。</p>"
                "<p class='muted'>第一次抓取通常要 1～3 分钟，跑完本页会自动变成结果表。</p>")
        auto = ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>巡检服务 · 准备中</title>
<style>body{{margin:0;padding:60px;background:#f7f8fa;color:#1a1a1a;
 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
 .box{{max-width:560px;margin:0 auto;background:#fff;border:1px solid #e6e8eb;
 border-radius:12px;padding:26px 28px;line-height:1.8}}
 .muted{{color:#8a8a8a;font-size:13px}}</style></head>
<body><div class="box"><h2 style="margin:0 0 12px;font-size:17px">应用市场审核巡检</h2>
{body}
{auto}
</div>
{CORE._settings_panel()}
</body></html>"""


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AppStoreReviewMonitor"
    _responded = False

    # ---- 兜底：任何意外都要回一个看得懂的响应，而不是直接把连接掐断 ----
    # （默认行为是异常直接冒泡、连接无响应关闭，浏览器只会显示
    #  「无法访问 / 连接被重置」，看不出真实原因，排查很痛苦。）
    def handle_one_request(self):
        self._responded = False
        try:
            super().handle_one_request()
        except Exception:  # noqa: BLE001
            import traceback
            # 服务化部署（systemd / 任务计划程序）没有控制台可看，
            # 所以调用栈必须同时进日志文件，否则真出问题时「一个字都查不到」。
            _log(f"[内部错误] 处理请求时出错：{self.path}\n{traceback.format_exc()}",
                 "error")
            if not self._responded:
                try:
                    if self.path.startswith("/api/"):
                        self._json({"ok": False,
                                    "error": "服务内部错误，详见运行日志"}, 500)
                    else:
                        self._send(500, _error_page(), "text/html; charset=utf-8")
                except Exception:  # noqa: BLE001
                    pass
            self.close_connection = True

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        pass                                  # 关掉默认的啰嗦日志（访问日志由 _log 按需打）

    def _actor(self):
        """这次请求是谁发来的 —— 无鉴权模式下，「谁改了配置」只能靠它追溯"""
        ip = (self.client_address or ("?",))[0]
        return "本机" if ip in ("127.0.0.1", "::1", "localhost") else ip

    def _same_origin(self):
        """挡一下「别的网站在同事浏览器里偷偷 POST 过来改配置」。

        浏览器发跨站请求一定会带 Origin，而同源请求的 Origin 与 Host 必然一致，
        所以这条检查对正常使用零影响，却能挡掉 CSRF。
        没有 Origin 的请求（curl、脚本）放行 —— 本来就能直接改文件，拦它没意义。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.split("://", 1)[-1] == (self.headers.get("Host") or "")

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._responded = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # 报告页绝不能被浏览器缓存住
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("请求体是空的")
        if n > 1_000_000:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._responded = True
            self.send_response(302)
            self.send_header("Location", "/report")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/report":
            return self._send(200, render_report_page(), "text/html; charset=utf-8")
        if path == "/api/config":
            rows, cfg, t, from_snap = current_result()
            return self._json(_public_config(cfg, t, from_snap))
        if path == "/api/state":
            with LOCK:
                return self._json({"scanning": STATE["scanning"],
                                   "progress": STATE["progress"],
                                   "last_run": STATE["last_run"],
                                   "error": STATE["error"]})
        if path == "/healthz":
            # 给监控/看门狗用的存活探针：只回极简 JSON，不渲染报告（渲染有成本）
            with LOCK:
                return self._json({"ok": True, "service": "appstore-review-monitor",
                                   "scanning": STATE["scanning"],
                                   "last_run": STATE["last_run"],
                                   "error": STATE["error"]})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._json({"ok": False, "error": "没有这个地址"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._same_origin():
            _log(f"  [安全] 拒绝跨站请求：Origin={self.headers.get('Origin')} "
                 f"Host={self.headers.get('Host')}，来自 {self._actor()}", "warn")
            return self._json({"ok": False, "error": "只接受本页发来的请求"}, 403)
        try:
            payload = self._body_json()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"ok": False, "error": f"请求内容读不出来：{exc}"}, 400)

        actor = self._actor()
        if path == "/api/config":
            try:
                result, errors = save_config(payload, actor=actor)
            except OSError as exc:
                _log(f"  [配置] {actor} 写配置失败：{exc}", "error")
                return self._json({"ok": False,
                                   "error": f"写配置失败：{exc}"}, 500)
            if errors:
                return self._json({"ok": False, "errors": errors}, 400)
            return self._json({"ok": True, **result})

        if path == "/api/scan":
            started = start_scan(show_raw=bool(payload.get("show_raw")))
            _log(f"  [巡检] {actor} 触发" + ("开始" if started else "（已有一轮在跑，忽略）"))
            return self._json({"ok": True, "started": started})

        if path == "/api/notify-test":
            # 只发一条测试消息、**不落盘**：让人在保存之前就确认 webhook 填对了。
            # 走的是和正式推送完全同一条 send_notifications 通道，
            # 所以「测试通过」就等于「正式推送也通」。
            cfg_now = read_config()
            clean, errors = validate_patch(
                {"notify": {"on": True, "mode": "always",
                            "providers": payload.get("providers") or []}}, cfg_now)
            if errors:
                _log(f"  [通知] {actor} 发的测试消息被拒：{'；'.join(errors)}", "warn")
                return self._json({"ok": False, "errors": errors}, 400)
            try:
                out = CORE.send_notifications(
                    [],                       # 测试消息不渲染真实结果，用固定文案
                    {"app_name": cfg_now.get("app_name") or "示例应用",
                     "notify": clean["notify"]},
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    testing=True)             # 标题带【测试】，和正式报告一眼可分
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False,
                                   "errors": [f"{type(exc).__name__}: {exc}"]}, 500)
            line = CORE.notify_report(out)
            bad = [n for n in out if not n[1]]
            _log(f"  [通知] {actor} 测试消息：{line}", "warn" if bad else "info")
            if bad:
                return self._json({"ok": False,
                                   "errors": [f"{n[0]} 发送失败：{n[2]}" for n in bad]})
            return self._json({"ok": True, "result": line})

        self._json({"ok": False, "error": "没有这个地址"}, 404)


def _pick_port(host, preferred, strict=False):
    """端口被占用就顺延找一个空的，免得每次都去查是谁占了。

    strict=True 时**不顺延**：服务化部署必须这样。否则端口悄悄漂到 8766，
    而大家照旧访问 8765——那是上一个没退干净的老实例，两个进程各写各的 config。
    """
    for p in ([preferred] if strict else range(preferred, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return None


def local_ips():
    """列出本机内网 IP，好把地址发给同事（纯标准库，不依赖 psutil）"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))     # 只为让内核挑一张出口网卡，不真发包
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    # Windows 上 SO_REUSEADDR 的语义是「允许重复绑定」——两个进程能同时绑住同一
    # 个端口，请求随机分给其中一个。服务端绝不该这样，所以 Windows 上关掉它，
    # 让第二个实例老老实实报「端口被占用」，而不是悄悄共存、两边配置互相覆盖。
    allow_reuse_address = (os.name != "nt")


def _install_stop_signals(httpd):
    """收到停止信号时干净退出（systemd 的 stop、NSSM 的 stop 都靠这个）"""
    def _stop(signum, frame):
        _log(f"收到停止信号 {signum}，正在退出…")
        # 必须另起线程调用 shutdown()：它要等 serve_forever 退出，
        # 而信号处理函数就跑在 serve_forever 那个线程里，直接调会自己等自己（卡死）。
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, RuntimeError):
            pass                              # 非主线程或不支持的平台，跳过即可


def serve(core_module, host=HOST_DEFAULT, port=PORT_DEFAULT,
          open_browser=True, auto_scan=True, show_raw=False, strict_port=False):
    """启动本地服务。core_module 就是 check_review_status 模块本身。"""
    global CORE
    CORE = core_module

    real_port = _pick_port(host, port, strict=strict_port)
    lp = _init_log()
    _log("=" * 64)
    _log(f"服务启动请求：host={host} port={port} strict_port={strict_port} "
         f"auto_scan={auto_scan} open_browser={open_browser}")

    if real_port is None:
        if strict_port:
            _log(f"端口 {port} 已被占用，而 --strict-port 要求不顺延。"
                 f"多半是上一个实例没退干净，先停掉它；或改用别的端口：--port 8770",
                 "error")
        else:
            _log(f"端口 {port}~{port + 19} 都被占用了，请用 --port 指定别的端口。", "error")
        return 1

    url_local = f"http://127.0.0.1:{real_port}/report"

    try:
        httpd = _Server((host, real_port), Handler)
    except OSError as exc:
        _log(f"绑定 {host}:{real_port} 失败：{exc}", "error")
        _log("  常见原因：端口被占用、防火墙/安全软件拦截、或无权限绑定该地址。", "error")
        return 1

    print("\n" + "=" * 70)
    print("应用市场审核巡检 · 网页服务已启动")
    print("=" * 70)
    if host == LAN_HOST:
        print("  同网段访问地址（用手机浏览器打开这个）：")
        ips = local_ips()
        if ips:
            for ip in ips:
                print(f"    http://{ip}:{real_port}/report")
        else:
            print("    （没探到内网 IP，用 ipconfig / ifconfig 看一下本机地址）")
        print(f"  本机访问：{url_local}")
        print()
        print("  ⚠ 当前是「同网段可访问」模式：同一网络下的任何人都能打开页面并修改配置，")
        print("    而发版基线／渠道清单／人工登记表直接决定报告结论。已做的兜底：")
        print("      · 每次改动都记进日志（含来源 IP、改前改后的值）")
        print("      · 每次保存前自动备份到 backup/，改错了能还原")
        print("    请只在可信的网络（家里 Wi-Fi、手机热点）下使用。")
    else:
        print(f"  结果页面：{url_local}")
    print(f"  配置文件：{config_path()}")
    print(f"  运行日志：{lp or '（写不了日志文件，本次只打控制台）'}")
    print()
    print("  用法：页面上点右上角「⚙ 设置」改应用信息／发版基线／渠道清单／人工登记表，")
    print("        点「↻ 重新巡检」重新抓取各家市场的公开数据。")
    print("  停止：在这个窗口按 Ctrl+C（关掉窗口也等于停止服务）。")
    print("=" * 70 + "\n")
    _log(f"服务已就绪：host={host} port={real_port} url={url_local} 日志={lp}")
    with LOCK:
        STATE["lan"] = (host == LAN_HOST)
        STATE["log"] = lp

    if auto_scan and not needs_setup():
        # push=False：开机/双击之后自动抓的那一遍**不推群**，
        # 免得「每天开一次网页」变成「每天群里多一条」。
        start_scan(show_raw=show_raw, push=False)
    elif auto_scan:
        # 第一次使用：还不知道要巡检哪个应用 → 先不抓，等人填。
        # 硬抓的话，用的是模板里的「示例应用 / com.example.app」，
        # 15 家市场一路「查无此应用」，白等几分钟还看得一头雾水。
        # 页面上会自动把设置弹窗摊开（见 needs_setup / /api/config 的 need_setup）。
        _log("  [启动] 还没填应用信息（首次使用）→ 跳过自动巡检，等页面上完成设置")
        print("  还没填应用信息，这次「不自动巡检」——因为还不知道要巡检哪个应用。")
        print("  请在打开的页面上填好「① 应用信息 / ② 发版基线」，")
        print("  再点「保存并重新巡检」——填过一次之后，以后每次打开都会自动巡检。")
        print()

    if open_browser:
        def _open():
            try:
                webbrowser.open(url_local)
            except Exception:  # noqa: BLE001
                _log("打不开浏览器（服务化运行时常见），请手动访问上面的地址", "warn")
        threading.Timer(0.8, _open).start()

    _install_stop_signals(httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        _log("服务已停止。")
        print("\n服务已停止。")
    return 0
