# -*- coding: utf-8 -*-
"""
应用市场「审核结果」自动巡检工具（通用版）
================================================
原理：不依赖任何市场的开发者后台，只抓**公开应用详情页／公开数据接口**上的
      「最新版本号」与「更新时间」，与本次发版基线（config.json 里的
      release_date / target_version / target_codes）对比，
      命中任一条件即判为「已上线」，否则为「审核中」。
      数据源明确答复「查无此应用」时判「已下架」。

适用对象：**你自己选的任意一个 App**。要巡检哪个应用、巡检哪些商店、
      按什么顺序显示，全部在 config.json 里配，代码不用改。

用法：
    python check_review_status.py              # ★ 启动网页服务（默认，只给本机）
    python check_review_status.py --cli        # 只跑一次巡检，控制台出表格
    python check_review_status.py --preflight  # 连通性体检：并发试一遍各渠道能不能抓通
    python check_review_status.py --show-raw   # 额外打印原始抓取片段（排障用）

    ★ 默认模式会启动一个小网页服务：浏览器里看结果，
      点右上角「⚙ 设置」即可改应用信息、发版基线、渠道清单与顺序、人工登记表，
      不用手工编辑 config.json。
      相关参数：--port 8765（默认端口，被占用会自动顺延）
                --no-browser    不自动开浏览器
                --no-scan       启动时不自动巡检（先看上次结果）
                --host 0.0.0.0  / --lan   开放给同网段（用手机看结果时用）
                --strict-port   端口被占用就报错退出、不自动顺延
      ⚠ --lan / --host 0.0.0.0 会把这个页面开放给同一网段的所有人，
        而它**没有任何登录校验**，别人可以改你的配置。别在公共 Wi-Fi 下开。

    ★ 打包成 exe 后（check_review_status.exe）：
      双击＝启动服务并自动打开浏览器；跑完/退出会在窗口停一下，方便看信息；
      若是被其他程序调用，加 --no-pause 就不会停。
      exe 模式下，工作目录 = **exe 自己所在的文件夹**，
      config.json / report / history 都相对它定位，改配置不用重新打包。

输出：
    控制台表格
    report/review_status_<日期>.md
    report/review_status_<日期>.html    ← 页面上带「设置」按钮
    history/latest.json  /  history/<日期>.json
    backup/config_<时间>.json           ← 每次在网页上保存配置前的自动备份

要加一个本工具还不支持的市场：见 README.md「怎么加一个新渠道」。
"""
from __future__ import annotations

import copy
import gzip
import html as htmlmod
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import webui            # 本地网页服务（纯标准库；它不反向 import 本文件，故无循环引用）

def _setup_output_encoding():
    """让中文在控制台正常显示。

    源码直接跑时，终端一般已经是 UTF-8，保持原样即可。
    但打包成 exe 双击运行时，Windows 传统控制台的代码页是 936(GBK)——
    这时若按 UTF-8 输出，中文会变成乱码。所以先把控制台切到 UTF-8(65001)。
    """
    if getattr(sys, "frozen", False) and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    try:
        # stdout 和 stderr 都要改。只改 stdout 的话，Windows 上重定向到文件时
        # stderr 仍然按 GBK 输出——于是同一个文件里混着 UTF-8 和 GBK 两种编码，
        # 而乱码的恰好是「端口被占」这类**报错行**，也就是出事时最需要看懂的那些。
        # line_buffering：输出被重定向到文件/被计划任务拉起时，默认是块缓冲，
        # 「还没填应用信息 → 这次不巡检」这类启动提示会一直卡在缓冲区里，
        # 进程一被结束就一个字都看不到。
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:  # noqa: BLE001
        pass


_setup_output_encoding()

# 工作根目录：config.json / report / history 都放在这里。
# 打包成 exe 后 __file__ 指向解包出来的临时目录（那里没有 config.json），
# 所以 exe 模式下一律以「exe 自己所在的目录」为准——
# 这样配置文件就摆在 exe 旁边，你改配置不需要重新打包。
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
M_UA = ("Mozilla/5.0 (Linux; Android 13; PGEM10) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def default_config() -> dict:
    """首次运行时的初始配置：结构齐全，应用信息留空等你自己填。

    ⚠️ 这里只是「初始值」。你在页面上保存过一次之后，一切以 config.json 为准。
    """
    return {
        "_说明_应用信息": "要巡检哪个 App，填这三行。安卓系市场靠「安卓包名」定位，"
                        "苹果靠「iOS trackId」；「应用名」是拿去各市场搜索用的名字，"
                        "也显示在报告标题上。用不到的留空即可（例如只发安卓就不填 iOS 的）。",
        "app_name": "示例应用",
        "android_pkg": "com.example.app",
        "ios_track_id": None,
        "ios_bundle_id": None,

        "_说明_本次发版": "判定只认两条硬条件：① 公开更新日期 >= release_date；"
                        "② 公开版本 == target_version，或 公开构建号 == target_codes 里对应端的值。"
                        "命中任一即『已上线』，其余一律『审核中』。"
                        "不知道的值保持 null，只靠日期也能判。",
        "release_date": None,
        "target_version": None,
        "target_codes": {"安卓": None, "苹果": None},

        "_说明_渠道": "★ 要巡检哪些商店、按什么顺序显示，全在这里。"
                    "数组顺序就是报告里的行顺序；enabled=false 的渠道不显示也不抓。"
                    "fetcher 是抓取器名字（见 check_review_status.py 里的 FETCHERS 表），"
                    "留 null 表示这家只能人工核验（没有可自动抓取的公开数据源）。"
                    "可选字段：search_name = 这家商店上的登记名（和 app_name 不一样时填）、"
                    "report=false = 只抓不算进报告（用来做交叉参考）、note = 说明文字。"
                    "这些都可以在网页的「⚙ 设置」里改，不用手工编辑本文件。",
        "channels": copy.deepcopy(DEFAULT_CHANNELS),

        "_说明_应用ID": "少数市场需要额外的 ID 才能定位到你的应用（不确定就留空，"
                      "程序会先按应用名去搜）："
                      "华为 = AppGallery 详情页 URL 末尾的 C 编号；"
                      "OPPO = 详情页 URL 里的 app_id；VIVO = appId；"
                      "360应用 = 详情页的 sid；百度 = docid（可留空，仅搜索不到时兜底）；"
                      "搜狗应用 = docid（必填）。"
                      "其余安卓市场靠上面的「安卓包名」定位，不用在这里填。",
        "app_ids": {
            "苹果": None, "华为": None, "OPPO": None, "VIVO": None,
            "360应用": None, "百度": None, "搜狗应用": None,
        },

        "_说明_通知": "★ 巡检结束后把结果推到群里，从「要人去看页面」变成「结果自己找人」。"
                    "on = 总开关；mode = always（每次巡检都发）/ change（只在结果相比"
                    "上次有变化时才发）；providers 里每条填一个群机器人的 webhook："
                    "type 可选 wecom（企业微信群机器人）/ feishu（飞书群机器人）/ "
                    "dingtalk（钉钉群机器人），name 只是显示用的名字。"
                    "在对应的群里「群设置 → 群机器人 → 添加」就能拿到 webhook 地址。"
                    "**webhook 留空的那条会被自动忽略**；全部留空＝不发。"
                    "这些都可以在网页的「⚙ 设置 → ⑥ 消息通知」里改。",
        "notify": {
            "on": False,
            "mode": "always",
            "providers": [
                {"type": "wecom", "name": "企业微信群", "webhook": ""},
                {"type": "feishu", "name": "飞书群", "webhook": ""},
            ],
        },

        "_说明_人工登记表": "① 登记为『未发版』的渠道＝本次没有上传包提审，报告直接显示未发版，"
                          "且不再去抓公开页；② 其余渠道，脚本抓不到公开数据时，"
                          "用这里的结果补进报告（以本表为准）。"
                          "可用值：『审核中』/『审核通过』/『审核未通过』/『未发版』/『已下架』。",
        "manual_reference": {},

        "timeout": 25,
    }


def load_config() -> dict:
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        # 首次运行：直接生成一份初始配置，省得用户还要先去别处找模板。
        # 先打印、再继续：双击运行时也是「先看到提示」的顺序。
        cfg = default_config()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)          # 原子写：不会留下半个坏文件
        print("没有找到配置文件，已自动生成一份初始配置：")
        print(f"  {path}\n")
        print("请先填上你要巡检的应用信息（在页面的「⚙ 设置」里改，或直接用文本编辑器"
              "打开上面的文件填），然后再跑一次。\n")
        return cfg
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def bust_url(url):
    """给 URL 追加一个防缓存参数，命中一个全新的 CDN 缓存键"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_t={int(time.time() * 1000)}"


def http_get(url, headers=None, timeout=25, ua=PC_UA, bust=False):
    h = {"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9",
         "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    h.update(headers or {})
    if bust:
        url = bust_url(url)
        h["Cache-Control"] = "no-cache"
        h["Pragma"] = "no-cache"
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def http_get_fresh(url, required=None, headers=None, timeout=25, ua=PC_UA,
                   delist_on=None):
    """抓公开详情页（含接口），优先拿到**源站最新内容**。

    背景：各商店的详情页都走 CDN。源站已更新、边缘节点还挂着旧缓存时，
    直接请求裸地址会拿到**过期数据**——表现为「明明已过审，报告还说审核中」，
    且不会报错，属于最危险的一类静默错误。

    做法：先带防缓存参数请求（命中全新缓存键 → 回源）；若返回正常但解析不到
    预期内容（required 正则不匹配），说明该站对多余参数敏感，退回裸地址再试。
    返回 (正文, 数据来源标记)。数据来源标记会写进报告，便于排查。

    delist_on：哪些 HTTP 状态码算「服务端明确答复没有这个资源」（一般是 404）。
      必须**两次请求都是**这个码才抛 DelistedError —— 只要有一次不是，
      就仍按「抓取故障」处理。这条是刻意的：把网络抖动说成「已下架」，
      比漏报更难查，也更吓人。
    """
    last_err = None
    codes = []
    for bust, tag in ((True, "源站直取"), (False, "CDN 缓存")):
        code, t = http_get(url, headers=headers, timeout=timeout, ua=ua, bust=bust)
        codes.append(code)
        if code != 200:
            last_err = f"HTTP {code}"
            continue
        if required and not re.search(required, t):
            last_err = "页面结构未匹配到预期字段"
            continue
        return t, tag
    if delist_on and codes and all(c in delist_on for c in codes):
        raise DelistedError(f"服务端明确答复 HTTP {codes[-1]}（该资源不存在）")
    raise RuntimeError(f"抓取失败（{last_err}）")


def http_post_form(url, params, headers=None, timeout=25, ua=M_UA):
    """POST 表单。用于 vivo 的 detailInfo 接口（JSON body 不认，必须表单）"""
    h = {"User-Agent": ua, "Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9",
         "Accept-Encoding": "gzip, deflate",
         "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
    h.update(headers or {})
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h,
                                                           method="POST"),
                                    timeout=timeout, context=CTX) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def http_post_json(url, payload, headers=None, timeout=25, ua=PC_UA):
    """POST JSON body。用于华为 webedge/getInterfaceCode 取接口码"""
    h = {"User-Agent": ua, "Accept": "application/json, text/plain, */*",
         "Accept-Language": "zh-CN,zh;q=0.9", "Accept-Encoding": "gzip, deflate",
         "Content-Type": "application/json;charset=UTF-8"}
    h.update(headers or {})
    data = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h,
                                                           method="POST"),
                                    timeout=timeout, context=CTX) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def norm_date(s):
    """把 2026.9.16 / 2026年9月16日 / 2026-09-16 / 时间戳 统一成 YYYY-MM-DD"""
    if not s:
        return None
    s = str(s).strip()
    if s.isdigit() and len(s) == 13:                      # 毫秒时间戳
        return time.strftime("%Y-%m-%d", time.localtime(int(s) / 1000))
    m = re.search(r"(\d{4})[-./年](\d{1,2})[-./月](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def norm_ver(v) -> str:
    """归一化版本号，便于比较：1.2.0 / v1.2.0 / 102 -> 去掉 v、去空格"""
    if v is None:
        return ""
    return re.sub(r"^[vV]", "", str(v).strip())


def ver_tuple(v):
    """把版本号拆成数字元组，用于大小比较；无法解析返回 None"""
    v = norm_ver(v)
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else None


def first(pattern, text, group=1, flags=re.S):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def app_id_of(cfg, key=None, default=None):
    """取某渠道在 config.app_ids 里登记的应用 ID；未登记（null/空）则回落到 default

    主流程逐渠道巡检时会往 cfg 里注入 "_key"，抓取器据此知道自己跑的是哪个渠道。
    """
    ids = cfg.get("app_ids") or {}
    v = ids.get(key or cfg.get("_key"))
    return v if v not in (None, "") else default


# --------------------------------------------------------------------------
# 各市场抓取器：返回 dict(version, date, code, url, note)
# 任何一家挂掉都不影响其他家
# --------------------------------------------------------------------------
def fetch_apple(cfg):
    tid = app_id_of(cfg, "苹果", cfg.get("ios_track_id"))
    if not tid:
        raise RuntimeError(
            "苹果未登记 trackId：请在设置页的「应用信息」里填 iOS trackId，"
            "或把这条渠道取消勾选（只发安卓的话就不需要它）")
    url = f"https://itunes.apple.com/lookup?id={tid}&country=cn"
    t, tag = http_get_fresh(url, required=r'"results"', timeout=cfg["timeout"])
    d = json.loads(t)
    if not d.get("results"):
        raise RuntimeError("iTunes 未返回结果")
    r = d["results"][0]
    # App Store 的 version 字段本身就是构建号（iOS 侧没有单独的 versionCode），
    # 所以 code 与 version 取同一个值。
    return {
        "version": norm_ver(r.get("version")),
        "date": norm_date(r.get("currentVersionReleaseDate")),
        "code": norm_ver(r.get("version")),
        "url": r.get("trackViewUrl", url),
        "note": (r.get("releaseNotes") or "").replace("\n", " ")[:60],
        "fetch_tag": tag,
    }


def fetch_googleplay(cfg):
    """Google Play（海外安卓；抓公开详情页，不需要任何开发者凭证）

    ⚠️ 两个必须知道的特性，决定了这个渠道的判定口径：

    1) **Google Play 的网页详情页不公开版本号**（既没有 versionName 也没有
       versionCode），只公开「更新日期」。所以这家只能按日期判定：
       公开更新日期 ≥ release_date ⇒ 已上线。给它填安卓构建号没有意义，
       报告里「公开版本」一列会是空的 —— 这与本工具「只有日期也能判」
       的整体设计一致，不算缺陷。
    2) **不存在的包名返回 HTTP 404**（实测带不带防缓存参数都是 404），
       属服务端明确答复「没有这个资源」⇒ 抛 DelistedError ⇒ 报告显示「已下架」。
       所以：**没在 Google Play 上架的 App，请把这条渠道取消勾选**，
       否则它会（正确地）显示成「已下架」。默认配置里这条就是关闭的。

    用 config 里的「安卓包名」定位，不需要额外登记 ID。
    """
    pkg = (cfg.get("android_pkg") or "").strip()
    if not pkg:
        raise RuntimeError(
            "Google Play 靠「安卓包名」定位应用，但设置页里的安卓包名还是空的："
            "请在「⚙ 设置 → ① 应用信息」里填上包名，或把这条渠道取消勾选")
    url = ("https://play.google.com/store/apps/details?id="
           + urllib.parse.quote(pkg) + "&hl=zh_CN&gl=CN")
    # required 同时兜住中英文两种页面（hl=zh_CN 给中文，兜底用英文标签）
    text, tag = http_get_fresh(url, required=r"更新日期|Updated on",
                               timeout=cfg["timeout"], delist_on=(404,))
    raw = (first(r"更新日期</div>\s*<div[^>]*>([^<]{4,24})</div>", text)
           or first(r"Updated on</div>\s*<div[^>]*>([^<]{4,24})</div>", text)
           or first(r"[\[,]\s*[uU]?[\"']?(\d{4}年\d{1,2}月\d{1,2}日)", text))
    date = norm_date(raw)
    if not date:
        raise RuntimeError(
            "Google Play 页面里没找到「更新日期」——可能页面结构改版了"
            "（加 --show-raw 看原始片段，改 fetch_googleplay 里的正则即可）")
    return {
        "version": None,          # 见上面第 1 条：网页版不公开版本号
        "date": date,
        "code": None,
        "url": url,
        "note": "Google Play 网页只公开更新日期，不公开版本号",
        "fetch_tag": tag,
    }


def fetch_yyb(cfg):
    """腾讯应用宝：页面服务端直出 AppInfo 区块"""
    pkg = cfg["android_pkg"]
    url = f"https://sj.qq.com/appdetail/{pkg}"
    t, tag = http_get_fresh(url, required=r"AppInfo_version", timeout=cfg["timeout"])
    # 第一个 AppInfo 区块即目标应用
    version = first(r'AppInfo_version__[A-Za-z0-9]+">V<!-- -->([0-9.]+)', t)
    date = first(r'更新时间</p><p title="([0-9.]+)"', t)
    return {"version": version, "date": norm_date(date), "url": url,
            "note": "页面「更新时间」会随商店刷新变动，仅供参考",
            "fetch_tag": tag}


def fetch_xiaomi(cfg):
    pkg = cfg["android_pkg"]
    url = f"https://app.mi.com/details?id={pkg}"
    t, tag = http_get_fresh(url, required=r"版本号", timeout=cfg["timeout"])
    version = first(r"版本号.{0,500}?float:right;\">\s*([0-9.]+)", t)
    date = first(r"更新时间.{0,500}?float:right;\">\s*([0-9-]+)", t)
    if not version:
        raise RuntimeError("小米页面未解析到版本号")
    return {"version": version, "date": norm_date(date), "url": url, "note": "",
            "fetch_tag": tag}


def fetch_vivo(cfg):
    """
    vivo 两步走：
      1. 拿到 appId：优先用 config.app_ids 里登记的 VIVO appId（省掉一次搜索，
         搜索接口偶发不稳）；未登记时才去搜索接口按应用名查（参数名是 key，不是 word）
      2. 详情接口 POST https://h5-api.appstore.vivo.com.cn/detailInfo（参数名是 appId），
         返回 upload_time —— 就是手机端 / H5 详情页显示的「更新时间」
    """
    pkg = cfg["android_pkg"]
    ref = {"Referer": "https://h5.appstore.vivo.com.cn/",
           "Origin": "https://h5.appstore.vivo.com.cn"}

    hit = {}
    app_id = app_id_of(cfg, "VIVO")
    if not app_id:
        u = ("https://search.appstore.vivo.com.cn/port/packages/?key="
             + urllib.parse.quote(search_name_of(cfg)) + "&start=0&limit=20")
        t, _tag = http_get_fresh(u, required=r'"value"', headers=ref,
                                 timeout=cfg["timeout"], ua=M_UA)
        hit = next((x for x in json.loads(t).get("value", [])
                    if x.get("package_name") == pkg), None) or {}
        if not hit:
            raise RuntimeError("vivo 未搜到该包名")
        app_id = hit.get("id")

    detail = {}
    code, t = http_post_form("https://h5-api.appstore.vivo.com.cn/detailInfo",
                             {"appId": str(app_id), "h5_websource": "h5appstore",
                              "frompage": "h5appstore"},
                             headers=ref, timeout=cfg["timeout"])
    if code == 200 and t.strip().startswith("{"):
        detail = json.loads(t)

    version = norm_ver(detail.get("version_name") or hit.get("version_name"))
    vcode = str(detail.get("version_code") or hit.get("version_code") or "")
    date = norm_date(detail.get("upload_time"))
    if not version:
        raise RuntimeError("vivo 详情接口未解析到版本号")
    return {
        "version": version,
        "date": date,
        "code": vcode,
        "url": f"https://h5.appstore.vivo.com.cn/#/details?appId={app_id}",
        "note": "" if date else "详情接口未返回 upload_time",
    }


def fetch_wandoujia(cfg):
    """阿里应用分发（豌豆荚）

    ⚠️ 这家是 CDN 旧缓存的高发渠道：源站已发布新版本后，边缘节点仍会返回旧页面，
       表现为「构建号/日期都还是上一版」且**不报错**。实测 2026-09-16 18:32 源站已更新
       （构建号 102→103），裸 URL 仍返回上一个版本的旧缓存。所以这里必须带防缓存参数。

    页面里同时有 data-app-vname / data-app-vcode 和 JSON-LD 的 pubDate：
        pubDate = 该版本进入商店的时间（精确到秒，如 2026-09-16T18:32:46）
    有些 App 的 versionName 长期不变、每次发版只跳 vcode，
    所以真正能区分新旧的是 vcode 和 pubDate。
    """
    pkg = cfg["android_pkg"]
    url = f"https://www.wandoujia.com/apps/{pkg}"
    t, tag = http_get_fresh(url, required=r'data-app-vname="', timeout=cfg["timeout"])
    version = first(r'data-app-vname="([^"]+)"', t)
    vcode = first(r'data-app-vcode="([^"]+)"', t)
    # pubDate 是权威字段；取不到再退回页面上的「更新时间」文案
    raw = first(r'"pubDate"\s*:\s*"([^"]+)"', t)
    if not raw:
        raw = first(r"更新(?:时间|于).{0,200}?(\d{4}-\d{2}-\d{2})", t)
    if not raw:
        cands = re.findall(r"(\d{4}-\d{2}-\d{2})", t)
        raw = cands[0] if cands else None
    if not version:
        raise RuntimeError("豌豆荚页面未解析到版本号")
    return {"version": version, "date": norm_date(raw), "code": vcode,
            "url": url, "note": "", "fetch_tag": tag}


def fetch_appchina(cfg):
    pkg = cfg["android_pkg"]
    url = f"http://www.appchina.com/app/{pkg}"
    t, tag = http_get_fresh(url, required=r"手机版免费下载", timeout=cfg["timeout"])
    # 页面标题形如「<应用名> 1.2.3 手机版免费下载」。先用 config 里登记的名字精确匹配；
    # 商店上的登记名与 config 里的写法常有出入（带后缀、英文名…），匹配不上就退回通用形态。
    version = first(re.escape(search_name_of(cfg)) + r"\s*([0-9]+\.[0-9.]+)\s*手机版免费下载", t)
    if not version:
        version = first(r"[^<>\"]{0,30}?\s*([0-9]+\.[0-9.]+)\s*手机版免费下载", t)
    date = first(r"更新(?:时间|日期).{0,200}?(\d{4}-\d{2}-\d{2})", t)
    if not date:
        cands = re.findall(r"(\d{4}-\d{2}-\d{2})", t)
        date = cands[0] if cands else None
    if not version:
        raise RuntimeError("应用汇页面未解析到版本号")
    return {"version": version, "date": norm_date(date), "url": url, "note": "",
            "fetch_tag": tag}


def fetch_lenovo(cfg):
    """联想乐商店：页面内嵌 JSON，publishDate 为毫秒时间戳"""
    pkg = cfg["android_pkg"]
    url = f"http://www.lenovomm.com/appdetail/{pkg}/0"
    t, tag = http_get_fresh(url, required=r'"appInfo":\{', timeout=cfg["timeout"])
    m = re.search(r'"appInfo":\{', t)
    scope = t[m.start(): m.start() + 4000] if m else t
    version = first(r'"version"\s*:\s*"([0-9][0-9.]*)"', scope)
    pub = first(r'"publishDate"\s*:\s*(\d{13})', scope)
    if not version:
        # 兜底：全页找第一个 4.x/5.x 形态版本号
        version = first(r">(\d+\.\d+\.\d+)<", t)
    return {"version": version, "date": norm_date(pub), "url": url,
            "note": "publishDate = 商店上架时间", "fetch_tag": tag}


class DelistedError(RuntimeError):
    """数据源**明确答复**「查无此应用」——是可信的否定结论，不是故障。

    必须与普通抓取失败（超时 / 对方改版 / 解析不到字段）严格区分：
      · 普通失败  ⇒ 数据不可信，退回人工登记表，报告里标 ⚠️ 点名；
      · 本异常    ⇒ 应用确实不在这个市场了，报告判「已下架」，**不**退人工登记。
    各渠道的 fetch_* 只有在拿到「服务端正常通信后给出的 not-found 语义」时才抛它。
    """


# 兜底用的 docid / packageid **不在代码里写死**，请在 config.json 的 app_ids 里登记：
#   "百度": "<docid>"        （可选；填了才能走「搜索不到时改用 docid 兜底」这条路）
#   "百度pid": "<packageid>" （可选，与 docid 配套）
BAIDU_SEARCH_API = "https://mobile.baidu.com/api/appsearch"   # 搜索：?word=<应用名>&pn=0
BAIDU_DOC_API = "https://mobile.baidu.com/api/appDocNew"      # 详情：?docid=<docid>&pid=<packageid>


def _baidu_api(url, cfg):
    """调百度手机助手的新版 JSON 接口（2026-10 改版后才有）。

    两个接口都必须在请求头里带 Accept: application/json 才返回 JSON；带 Referer 更稳。
    HTTP 非 200、或返回的不是 JSON，一律直接报错——不静默降级。

    取缓存：仍用 http_get(bust=True) 加无意义参数回源，避免中间层缓存给旧数据。
    """
    hc, t = http_get(url, headers={"Referer": "https://mobile.baidu.com/",
                                   "Accept": "application/json, text/plain, */*"},
                     timeout=cfg["timeout"], ua=M_UA, bust=True)
    if hc != 200:
        raise RuntimeError(f"百度接口 HTTP {hc}")
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        raise RuntimeError("百度接口返回的不是 JSON（站点可能又改版了）")


def fetch_baidu(cfg):
    """百度手机助手（mobile.baidu.com）：新版 JSON 接口。

    2026-10 起旧做法失效了：`/item?docid=` 详情页退化成**前端空壳**——页面里那份
    服务端直出的 appDoc JSON 没了（`__INITIAL_STATE__` 里 `resList:{}`），数据改由
    浏览器里的 JS 再发一次接口请求去取。所以这里改成**直接调它的数据接口**
    （两个接口名都是从它前端 bundle 里挖出来的）：

      ① 搜索：GET /api/appsearch?word=<应用名>&pn=0
         → data.data[] 每项含 sname / docid / packageid
      ② 详情：GET /api/appDocNew?docid=<docid>&pid=<packageid>
         → data.appDocNew.appDoc.versionname / versioncode / updatetime / package

    搜到候选后**逐个取详情并用包名核对**（`appDoc.package` 必须等于本应用的包名），
    防止同名应用撞车；搜索无候选时，退回 config/内置的 docid 兜底。
    候选筛选用 `_name_hit()` 做**双向**包含（商店登记名常常比你填的应用名短），
    免得「在架上却搜不到」。

    ⚠️ 失败时**明确报错、不静默**，而且要分清两种性质完全不同的失败：
      · 服务端正常通信后明确答复「资源不存在」（errorCode 1003）⇒ 抛 DelistedError，
        主流程据此判「已下架」（这是可信的否定结论）；
      · 超时 / 非 200 / 返回不是 JSON / 包名对不上 / 没有版本号 ⇒ 抛普通 RuntimeError，
        这才算「抓取失败、数据不可信」，退回人工登记表。
    """
    pkg = cfg["android_pkg"]
    name = search_name_of(cfg)

    # ① 搜索：拿到所有名字对得上的候选
    sug = f"{BAIDU_SEARCH_API}?word={urllib.parse.quote(name)}&pn=0"
    j = _baidu_api(sug, cfg)
    rows = ((j.get("data") or {}).get("data")) or []
    cand = [r for r in rows if _name_hit(name, r.get("sname"))]
    via = "搜索"
    if not cand:
        fallback = app_id_of(cfg, "百度")
        if not fallback:
            raise RuntimeError(
                f"百度手机助手搜索接口没有返回「{name}」；config 里也没登记 app_ids.百度"
                "（docid），无法走兜底路径——请核对搜索词，或把 docid 填进 config")
        cand = [{"docid": fallback,
                 "packageid": (cfg.get("app_ids") or {}).get("百度pid")}]
        via = "兜底 docid"

    # ② 逐个取详情，用包名校验（防同名撞车）
    last_err = ""
    tried = 0
    all_not_found = True          # 所有尝试都得到「服务端明确说没有」才算数
    for r in cand[:3]:
        docid, pid = r.get("docid"), r.get("packageid")
        url = f"{BAIDU_DOC_API}?docid={docid}&pid={pid}"
        j2 = _baidu_api(url, cfg)
        tried += 1
        if j2.get("errorCode"):
            ec = str(j2.get("errorCode"))
            em = str(j2.get("errorMessage") or "")
            last_err = f"docid {docid} 返回 errorCode={ec}（{em or '资源不存在'}）"
            # 只有 1003 / not found 才是「资源不存在」的可信语义；
            # 其它错误码（限流、参数错、接口又改版…）不能当作「已下架」的证据。
            if ec != "1003" and "not found" not in em.lower():
                all_not_found = False
            continue
        all_not_found = False     # 有一条能正常返回，就不是「查无此应用」
        ad = (((j2.get("data") or {}).get("appDocNew") or {}).get("appDoc")) or {}
        real_pkg = ad.get("package") or ""
        if real_pkg and real_pkg != pkg:
            last_err = f"docid {docid} 返回的包名 {real_pkg} 与预期 {pkg} 不符"
            continue
        ver = norm_ver(ad.get("versionname"))
        if not ver:
            last_err = f"docid {docid} 未返回版本号"
            continue
        note = "版本/更新时间取自百度接口 appDocNew.appDoc，与页面同源"
        if via == "兜底 docid":
            note += f"；本次搜索接口未返回该应用，已改用兜底 docid {docid}"
        return {"version": ver, "date": norm_date(ad.get("updatetime")),
                "code": norm_ver(ad.get("versioncode")) or None,
                "url": url, "note": note, "fetch_tag": None}

    # ③ 每个候选都被服务端**明确**否掉 ⇒ 可信的「这个市场里没有它」。
    #    抛专用异常，由主流程判「已下架」，与「抓取失败」彻底分开。
    if tried and all_not_found:
        raise DelistedError(
            f"百度手机助手查无「{name}」：搜索接口无同名结果，"
            f"且服务端明确答复资源不存在（{last_err}）")

    raise RuntimeError(
        f"百度手机助手检索不到「{name}」：搜索接口无同名结果，"
        f"逐个 docid 尝试也全部失败（{last_err or '无候选'}）"
        f"——该应用在此市场可能已下架/未收录，只能人工核验")


def fetch_sogou(cfg):
    """搜狗应用（数据镜像应用宝），作为交叉参考"""
    docid = app_id_of(cfg, "搜狗应用")
    if not docid:
        raise RuntimeError("搜狗应用需要在 config.app_ids 里登记 docid 才能抓")
    url = f"https://as.sogou.com/app/apkdetail.jsp?pid=34&cid=46&docid={docid}"
    t, tag = http_get_fresh(url, required=r"版本", timeout=cfg["timeout"])
    version = first(r"版本[：:]\s*([0-9.]+)", t)
    date = first(r"更新日期[：:]\s*([0-9-]+)", t)
    if not version:
        raise RuntimeError("搜狗页面未解析到版本号")
    return {"version": version, "date": norm_date(date), "url": url,
            "note": "镜像应用宝数据，可作交叉验证", "fetch_tag": tag}


def fetch_oppo(cfg):
    """OPPO 软件商店：详情页 app.cdo.oppomobile.com/home/detail?app_id=xxx（服务端渲染）。

    页面只公开「版本 / 大小 / 下载量」，不直接给更新时间；但「下载」按钮的 APK 链接形如
        https://storedl1.nearme.com.cn/apk/YYYYMM/DD/<hash>.apk
    路径里的 YYYYMM/DD 就是**包体上传 CDN 的日期**（实测与 APK 的 Last-Modified 一致）。
    它比手机端商店显示的「更新时间」早约 1~2 天——中间隔着提审/上架环节。

    APK 的 hash 随每次发版变化，作为「构建标识」纳入快照对比
    （注意：它不是安卓 versionCode，不能与 config 的 baseline_code 直接比）。
    """
    app_id = app_id_of(cfg, "OPPO", cfg.get("oppo_app_id"))
    if not app_id:
        raise RuntimeError(
            "OPPO 未登记 app_id：请到 OPPO 软件商店的详情页 URL 里找到 app_id="
            "后面的数字，填进设置页的「各市场应用 ID」")
    url = f"https://app.cdo.oppomobile.com/home/detail?app_id={app_id}"
    t, tag = http_get_fresh(url, required=r"版本：",
                            headers={"Referer": "https://store.oppomobile.com/"},
                            timeout=cfg["timeout"], ua=M_UA)
    ver = first(r"版本：\s*([0-9][0-9.]*)", t)
    if not ver:
        raise RuntimeError(
            f"OPPO 详情页未解析到版本号（app_id={app_id}：可能是该 ID 已失效，"
            "或这家商店改版了）")
    name = first(r'headline16">([^<]*)<', t) or "?"
    size = first(r"大小：\s*([^<]*)", t) or "?"
    apk = first(r'href="(https://[^"]*\.apk)"', t) or ""
    date, build = None, ""
    m = re.search(r"/apk/(\d{4})(\d{2})/(\d{2})/([0-9a-f]+)\.apk", apk)
    if m:
        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        build = m.group(4)[:8]
    return {"version": ver, "date": date, "code": build, "code_kind": "build",
            "url": url, "fetch_tag": tag,
            "note": f"名称「{name}」｜{size}｜日期＝APK 上传日（早于商店显示 1~2 天）"}


def fetch_samsung(cfg):
    """三星应用商店（中国站）：https://apps.galaxyappstore.com/api/detail/<包名>

    这是 CN 站前端 SPA 自己调的接口（前端代码里是 fetch(`/api/${type}/${id}`)），
    返回 JSON，DetailMain 里有：
        contentBinaryVersion → 版本名（形如 1.2.0）
        modifyDate           → 更新日期（2026.09.07.）＝ 页面显示的日期
    注意：全球站 galaxystore.samsung.com/api/detail/<包名> 按出口 IP 判地域，
    境外 IP 会返回 errCode 9904；CN 站这个接口不受影响。
    """
    pkg = cfg["android_pkg"]
    url = f"https://apps.galaxyappstore.com/api/detail/{pkg}"
    t, tag = http_get_fresh(url, required=r'"DetailMain"',
                            headers={"Referer": "https://apps.galaxyappstore.com/"},
                            timeout=cfg["timeout"])
    d = json.loads(t)
    if d.get("errCode"):
        raise RuntimeError(f"三星接口返回 {d.get('errCode')}：{d.get('errMsg')}")
    dm = d.get("DetailMain") or {}
    if dm.get("appId") != pkg:
        raise RuntimeError("三星接口返回的应用与目标包名不一致")
    ver = norm_ver(dm.get("contentBinaryVersion"))
    if not ver:
        raise RuntimeError("三星接口未解析到版本号")
    return {
        "version": ver,
        "date": norm_date(dm.get("modifyDate")),
        "url": f"https://apps.galaxyappstore.com/detail/{pkg}",
        "note": f"{dm.get('contentName') or ''}｜{dm.get('contentBinarySize') or ''}",
        "fetch_tag": tag,
    }


# ---- 以下几家（华为 / 360）的抓取实现 ----
# 华为：走 AppGallery 的 Interface-Code 两步握手（见 fetch_huawei），全自动。
#       ⚠️ 一个 appId 只对应一个**安卓**包。AppGallery 的公开接口只服务安卓内容，
#       没有鸿蒙维度——若让鸿蒙渠道沿用这个抓取器，只会把安卓数据原样抄一份，
#       产生假阳性。所以默认配置里鸿蒙是「人工渠道」（fetcher 留空），只走人工登记。
# 360：搜索接口不稳定，已加 sid 兜底（见 fetch_360）。
# 任何一家挂掉都不影响其他市场。

def _deep_find_app(obj, pkg):
    """在嵌套 JSON 中找出所有「值里含目标包名」的节点，从里到外返回"""
    hits = []

    def walk(o):
        if isinstance(o, dict):
            if any(v == pkg for v in o.values()
                   if isinstance(v, (str, int))):
                hits.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return hits


def _norm_key(s) -> str:
    """键名归一化：转小写并去掉 _ - 空格。

    各家接口命名风格不统一（360 用 version_name / update_time 蛇形，
    华为用 releaseDate 驼峰），归一化后同一条 pattern 可同时命中。
    """
    return re.sub(r"[_\-\s]", "", str(s)).lower()


def _pick(node, patterns):
    """按 patterns 的优先级，从节点里取第一个匹配键的可读值

    键名与 pattern 都先经 _norm_key 归一化再比对，因此
    versionName / version_name / VERSION-NAME 等价。
    """
    for p in patterns:
        pn = _norm_key(p)
        for k, v in (node or {}).items():
            if re.search(pn, _norm_key(k)) and isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
    return None


HUAWEI_EDGES = (
    "https://web-drcn.hispace.dbankcloud.com/edge",   # 华北
    "https://web-dra.hispace.dbankcloud.com/edge",    # 新加坡
    "https://web-dre.hispace.dbankcloud.com/edge",    # 欧洲
    "https://web-drru.hispace.dbankcloud.com/edge",   # 俄罗斯
)


def _hw_app_id(cfg):
    """华为的 appId

    取 config.app_ids.华为；再退到旧字段 huawei_app_id。两处都没有就直接报错——
    没有 appId 时华为的接口只会返回安卓包的数据，猜一个等于伪造结论。

    ⚠️ 一个 appId 只对应一个**安卓**包。若另有 HarmonyOS 版本，它在这个接口里
       是查不到的（详见 README「鸿蒙为什么只能人工」），别拿同一个 C 编号去判鸿蒙。
    """
    v = app_id_of(cfg, "华为", cfg.get("huawei_app_id"))
    if not v:
        raise RuntimeError(
            "华为未登记应用 ID：请把 AppGallery 详情页 URL 末尾的 C 编号填进 "
            "config.app_ids.华为")
    return str(v).strip()


def _hw_interface_code(base, timeout):
    """两步握手第一步：POST {edge}/webedge/getInterfaceCode 取一枚 JWT

    前端 7.js 的请求拦截器原文：
      e.headers["Interface-Code"] = sessionStorage.getItem("interfaceCode")
                                    + "_" + (new Date).getTime()
    JWT 的 username 声明绑定了 User-Agent，所以取码与用码必须用同一个 UA。
    """
    code, t = http_post_json(f"{base}/webedge/getInterfaceCode", {},
                             headers={"Referer": "https://appgallery.huawei.com/",
                                      "Origin": "https://appgallery.huawei.com"},
                             timeout=timeout)
    if code != 200 or not t.strip():
        return None
    s = t.strip()
    try:
        v = json.loads(s)
        if isinstance(v, str):
            return v
        return v.get("data") if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001
        return s.strip('"')


def _hw_extract(detail, pkg):
    """按包名从 layoutData 各卡片里提取 version / versionCode / releaseDate

    该接口返回的是「布局卡片」结构，版本号与更新日期分散在不同 layoutName：
      detailhiddencard  → versionName / versionCode
      detailappinfocard → version / releaseDate
    所以必须遍历全部卡片，不能只取第一个命中节点。
    """
    ver = code = date = None
    for block in detail.get("layoutData") or []:
        for item in block.get("dataList") or []:
            if not isinstance(item, dict):
                continue
            names = [item.get("package"), item.get("packageName"), item.get("pkgName")]
            if pkg not in [str(n) for n in names if n]:
                continue
            ver = ver or item.get("versionName") or item.get("version")
            code = code or item.get("versionCode")
            date = date or item.get("releaseDate") or item.get("updateTime")
    return ver, code, date


def fetch_huawei(cfg):
    """华为应用市场（安卓版）：AppGallery 官网前端的 two-step Interface-Code 握手

    ⚠️ 这里**不是**地域封锁。缺接口码时海内外 IP 一律返回
       HTTP 403 {"rtnCode":1002,"rtnDesc":"InterfaceCode Verification failed."}

    流程（纯标准库，无需浏览器内核、无需 JS 引擎）：
      ① POST {edge}/webedge/getInterfaceCode        → 一枚 JWT（约 15 分钟有效）
      ② GET  {edge}/uowap/index?method=internal.getTabDetail&serviceType=20
             &uri=app|<appId>&maxResults=25&zone=&locale=zh_CN
         请求头带：Interface-Code: <jwt>_<毫秒时间戳>

    坑位（2026-09-16 实测）：
      · 再附加 Identity-Id 头会被判 1002 校验失败 —— 只带 Interface-Code 即可
      · uri 必须用 appId（C 编号）；用包名会返回 200 但内容里没有该应用
      · 四个边缘节点（drcn/dra/dre/drru）均可用，按序兜底
    """
    pkg = cfg["android_pkg"]
    app_id = _hw_app_id(cfg)
    detail_url = f"https://appgallery.cloud.huawei.com/app/{app_id}"
    qs = urllib.parse.urlencode({
        "method": "internal.getTabDetail", "serviceType": "20",
        "reqPageNum": "1", "uri": f"app|{app_id}", "maxResults": "25",
        "zone": "", "locale": "zh_CN"})

    fails = []
    for base in HUAWEI_EDGES:
        token = _hw_interface_code(base, cfg["timeout"])
        if not token:
            fails.append(f"{base} 取接口码失败")
            continue
        hdr = {"Interface-Code": f"{token}_{int(time.time() * 1000)}",
               "Referer": "https://appgallery.huawei.com/",
               "Origin": "https://appgallery.huawei.com"}
        code, t = http_get(f"{base}/uowap/index?{qs}", headers=hdr,
                           timeout=cfg["timeout"])
        if code != 200 or not t.strip().startswith("{"):
            desc = f"HTTP {code}"
            if t.strip().startswith("{"):
                try:
                    desc += f" rtnCode={json.loads(t).get('rtnCode')}"
                except Exception:  # noqa: BLE001
                    pass
            fails.append(f"{base} {desc}")
            continue
        ver, code_no, date = _hw_extract(json.loads(t), pkg)
        if not ver:
            fails.append(f"{base} 未解析到版本号")
            continue
        return {"version": norm_ver(ver),
                "code": str(code_no) if code_no else None,
                "date": norm_date(date),
                "url": detail_url,
                "note": "AppGallery 官网接口（webedge 接口码握手 + uowap getTabDetail），取 releaseDate"}

    raise RuntimeError(
        "华为 AppGallery 接口码握手失败 —— " + "；".join(dict.fromkeys(fails)) +
        "。该接口不限制网络地域，失败多为 webedge/getInterfaceCode 取码不通，"
        "可在境内网络重试，或人工核验 " + detail_url)


def fetch_360(cfg):
    """360 手机助手：先按应用名搜索拿 sid，再取详情

    接口是 zhushou.360.cn 前端（Nuxt）自己调的，基址 openbox.mobilem.360.cn：
      搜索 GET /PcSearch/class?q=<应用名>&type=alltop&page=1
      详情 GET /PcHome/detail?sid=<sid>&src=zs
    两家都带固定参数 ch=200000。

    ⚠️ 搜索接口不稳定（2026-09-16 当天出现过：上午正常、下午返回
       {"code":0,"data":{"soft":[]}} 空结果），但**详情接口始终可用**。
       因此这里加了 sid 兜底：搜索拿不到时直接用内置/配置的 sid，
       并在 note 里标明本次是兜底路径，避免静默降级。
    """
    pkg = cfg["android_pkg"]
    ref = {"Referer": "https://zhushou.360.cn/", "ch": "200000"}

    u = ("https://openbox.mobilem.360.cn/PcSearch/class?type=alltop&page=1&q="
         + urllib.parse.quote(search_name_of(cfg)))
    code, t = http_get(u, headers=ref, timeout=cfg["timeout"], bust=True)
    soft = []
    if code == 200 and t.strip().startswith("{"):
        soft = ((json.loads(t).get("data") or {}).get("soft")) or []

    if soft:
        hit = next((x for x in soft if _pick(x, [r"packageName", r"^pkg$", r"package"])
                    == pkg), None)
        sid = (hit or soft[0]).get("id") or (hit or soft[0]).get("softid")
        if not sid:
            raise RuntimeError("360 搜索结果缺少 sid")
        via = "搜索"
    else:
        sid = app_id_of(cfg, "360应用", cfg.get("qihu_sid"))
        if not sid:
            raise RuntimeError(
                "360 搜索接口本次返回空结果，config 里也没登记 app_ids.360应用（sid），"
                "无法走兜底路径——该渠道本次不可信")
        via = "兜底 sid"

    u2 = f"https://openbox.mobilem.360.cn/PcHome/detail?sid={sid}&src=zs"
    code, t2 = http_get(u2, headers=ref, timeout=cfg["timeout"], bust=True)
    if code != 200 or not t2.strip().startswith("{"):
        raise RuntimeError(
            f"360 详情接口返回 {code}（搜索路径：{via}）——"
            f"搜索接口本次返回 {('HTTP ' + str(code)) if not soft else '有结果'}"
            f"，该渠道本次不可信")
    d2 = json.loads(t2)
    node = (_deep_find_app(d2, pkg) or [None])[0]
    if node is None:
        return {"version": None, "date": None, "code": None,
                "url": f"https://zhushou.360.cn/detail?id={sid}",
                "note": "360 详情接口未标明包名，需人工核对",
                "fetch_tag": "源站直取"}
    # 键名经 _pick 归一化后，version_name → versionname；pattern 也须写成归一化形式，
    # 否则 ^version_?name$ 里的下划线会被一并抹掉而永远匹配不上。
    ver = norm_ver(_pick(node, [r"^versionname$", r"^version$"]))
    code = norm_ver(_pick(node, [r"^versioncode$", r"^build$"]))
    # 360 详情的上线时间是蛇形键：update_time（与 edit_time 同值，精确到秒）
    date = norm_date(_pick(node, [r"^updatetime$", r"^edittime$", r"^publishtime$",
                                  r"^updatedate$", r"^date$"]))
    if not ver and code:                      # 只回传构建号时用它兜底
        ver = code
    if not ver:
        raise RuntimeError("360 详情接口未解析到版本号")
    note = "360 手机助手的更新时间精确到秒（update_time）"
    if via == "兜底 sid":
        note += "；本次搜索接口返回空结果，已改用兜底 sid " + str(sid) + "，数据仍取自详情接口"
    return {"version": ver, "date": date, "code": code or None,
            "url": f"https://zhushou.360.cn/detail?id={sid}",
            "note": note, "fetch_tag": "源站直取"}


# --------------------------------------------------------------------------
# 渠道清单：由 config.json 的 channels 描述，代码里只保留「解析器注册表」
# --------------------------------------------------------------------------
# 加一个市场 = ①写一个 fetch_xxx（返回 dict）+ ②在下面 FETCHERS 里注册
#              + ③在 config.channels 里加一行。详见 README.md。
#
# 「人工渠道」不需要解析器：channels 里那条把 fetcher 留空（null）即可，
# 程序不会去抓，结果直接取自人工登记表。
FETCHERS = {
    "apple":      fetch_apple,      # App Store（iOS）
    "googleplay": fetch_googleplay,  # Google Play（海外安卓）
    "huawei":    fetch_huawei,      # 华为应用市场（安卓版）
    "xiaomi":    fetch_xiaomi,      # 小米应用商店
    "oppo":      fetch_oppo,        # OPPO 软件商店
    "vivo":      fetch_vivo,        # vivo 应用商店
    "yyb":       fetch_yyb,         # 腾讯应用宝
    "samsung":   fetch_samsung,     # 三星应用商店
    "lenovo":    fetch_lenovo,      # 联想乐商店
    "baidu":     fetch_baidu,       # 百度手机助手
    "wandoujia": fetch_wandoujia,   # 阿里应用分发（豌豆荚）
    "360":       fetch_360,         # 360 手机助手
    "appchina":  fetch_appchina,    # 应用汇
    "sogou":     fetch_sogou,       # 搜狗应用（镜像应用宝，做交叉参考用）
}

# 每个解析器需要在 config.app_ids 里登记什么，用来给设置页生成提示文案
FETCHER_ID_HINT = {
    "apple":     "App Store trackId（纯数字，可从 App Store 链接里找到）",
    "googleplay": "不需要在此填写 —— 它用上面的「安卓包名」定位",
    "huawei":    "AppGallery 详情页 URL 末尾的 C 编号",
    "oppo":      "详情页 URL 里的 app_id（纯数字）",
    "vivo":      "vivo appId（纯数字；不填则按应用名去搜）",
    "baidu":     "docid（可留空，仅在搜索接口搜不到时用于兜底）",
    "360":       "sid（可留空，仅在搜索接口返回空结果时用于兜底）",
    "sogou":     "docid（必填）",
    "samsung":   "应用包名",
    "lenovo":    "应用包名",
    "xiaomi":    "应用包名",
    "yyb":       "应用包名",
    "wandoujia": "应用包名",
    "appchina":  "应用包名",
}

DEFAULT_PLATFORM = "安卓"

# 「端」的推荐顺序（也是设置页里构建号输入框的顺序）。不在这里的端会排在后面。
PLATFORM_ORDER = ["安卓", "苹果", "鸿蒙"]

# 首次运行（config 里还没有 channels 时）用的初始清单。
# 这**不是**写死的清单 —— 在设置页里改一次、保存之后，就以 config 里的为准了。
DEFAULT_CHANNELS = [
    {"key": "苹果", "name": "App Store", "platform": "苹果", "fetcher": "apple",
     "enabled": True},
    {"key": "谷歌", "name": "Google Play", "platform": "安卓", "fetcher": "googleplay",
     "enabled": False,
     "note": "默认关闭：只有确实在 Google Play 上架了才勾选。它用「安卓包名」定位；"
             "Google Play 的网页只公开更新日期、不公开版本号；"
             "没上架的包名会返回 404，报告里会显示「已下架」"},
    {"key": "华为", "name": "华为应用市场", "platform": "安卓", "fetcher": "huawei",
     "enabled": True},
    {"key": "鸿蒙", "name": "华为应用市场（HarmonyOS）", "platform": "鸿蒙",
     "fetcher": None, "enabled": True,
     "note": "华为没有公开的鸿蒙版网页数据源，只能人工核验"},
    {"key": "小米", "name": "小米应用商店", "platform": "安卓", "fetcher": "xiaomi",
     "enabled": True},
    {"key": "OPPO", "name": "OPPO 软件商店", "platform": "安卓", "fetcher": "oppo",
     "enabled": True},
    {"key": "VIVO", "name": "vivo 应用商店", "platform": "安卓", "fetcher": "vivo",
     "enabled": True},
    {"key": "应用宝", "name": "腾讯应用宝", "platform": "安卓", "fetcher": "yyb",
     "enabled": True},
    {"key": "三星", "name": "三星应用商店", "platform": "安卓", "fetcher": "samsung",
     "enabled": True},
    {"key": "联想", "name": "联想乐商店", "platform": "安卓", "fetcher": "lenovo",
     "enabled": True},
    {"key": "百度", "name": "百度手机助手", "platform": "安卓", "fetcher": "baidu",
     "enabled": True},
    {"key": "阿里", "name": "阿里应用分发（豌豆荚）", "platform": "安卓",
     "fetcher": "wandoujia", "enabled": True},
    {"key": "360应用", "name": "360 手机助手", "platform": "安卓", "fetcher": "360",
     "enabled": True},
    {"key": "应用汇", "name": "应用汇", "platform": "安卓", "fetcher": "appchina",
     "enabled": True},
    {"key": "华为荣耀", "name": "荣耀应用市场", "platform": "安卓", "fetcher": None,
     "enabled": True,
     "note": "荣耀应用市场没有可公开抓取的详情页（相关域名解析不到 / 需分享令牌），"
             "只能人工核验"},
    {"key": "搜狗应用", "name": "搜狗应用（镜像应用宝）", "platform": "安卓",
     "fetcher": "sogou", "enabled": False, "report": False,
     "note": "数据镜像应用宝，只作交叉参考、不进报告；需要在 app_ids 里填 docid 才启用"},
]


def channels_raw(cfg):
    """config 里声明的渠道清单（原样返回，含 enabled=false 的）"""
    chs = cfg.get("channels")
    return chs if isinstance(chs, list) and chs else DEFAULT_CHANNELS


def channel_of(cfg, key):
    """按渠道 key 取它的定义 dict；找不到返回 None"""
    if key is None:
        return None
    for ch in channels_raw(cfg):
        if isinstance(ch, dict) and str(ch.get("key")) == str(key):
            return ch
    return None


def channels_from_cfg(cfg, include_disabled=False):
    """把 config.channels 解析成运行时用的渠道列表。

    返回 [dict]，每项含 key / name / platform / fetcher / fn / enabled / report /
    search_name / note。规则：

      · **数组顺序就是报告里的行顺序**（这就是「排序」）；
      · enabled=false 的默认不返回（既不显示也不抓）；include_disabled=True 才带上；
      · fetcher 为空或不在 FETCHERS 里 ⇒ fn=None ⇒ 人工渠道，不抓；
      · report=false 的渠道照抓但不进报告（例：搜狗只是应用宝的镜像，拿来做交叉参考）。
    """
    out = []
    for ch in channels_raw(cfg):
        if not isinstance(ch, dict):
            continue
        key = str(ch.get("key") or "").strip()
        if not key:
            continue
        enabled = ch.get("enabled", True) is not False
        if not enabled and not include_disabled:
            continue
        fname = ch.get("fetcher")
        fn = FETCHERS.get(fname) if isinstance(fname, str) else None
        out.append({
            "key": key,
            "name": str(ch.get("name") or key),
            "platform": str(ch.get("platform") or DEFAULT_PLATFORM),
            "fetcher": fname if fn else None,
            "fn": fn,
            "enabled": enabled,
            "report": ch.get("report", True) is not False,
            "search_name": str(ch.get("search_name") or "").strip(),
            "note": str(ch.get("note") or ""),
        })
    return out


def platform_keys(cfg):
    """需要填构建号的「端」，按 PLATFORM_ORDER 排序（其余端排在后面）。

    只统计**有抓取器的渠道**用到的端：走人工的渠道没有公开数据可比，
    永远不会参与自动判定，给它一个构建号输入框只会让人纳闷该填什么
    （典型例子：鸿蒙只能人工核验，所以设置的构建号里不该出现「鸿蒙」）。
    """
    seen = []
    for ch in channels_from_cfg(cfg, include_disabled=True):
        if not ch["fetcher"]:
            continue
        p = ch["platform"]
        if p and p not in seen:
            seen.append(p)
    head = [p for p in PLATFORM_ORDER if p in seen]
    tail = [p for p in seen if p not in PLATFORM_ORDER]
    return head + tail


def app_name_of(cfg):
    """全局应用名（报告标题、控制台、各市场的搜索词都用它）"""
    return str(cfg.get("app_name") or "").strip() or "未命名应用"


def _name_hit(query, sname):
    """商店里的登记名 与 你填的应用名 是否算「对得上」。

    ⚠️ 必须**双向**包含，不能只判 `query in sname`：
       商店的登记名经常比你的应用名**短**。典型例子——你在设置里填「夸克浏览器」，
       百度手机助手里它登记的 sname 就是「夸克」。此时 `"夸克浏览器" in "夸克"`
       为假，候选清空，明明在架的应用会被判成「抓取失败」。

    放宽名字匹配**不会**带来误判：最终判据是后面的**包名核对**
    （详情接口返回的 package 必须等于 android_pkg），名字这一步只是用来
    把明显无关的结果挡掉、少发几次请求。
    """
    q = (query or "").strip()
    s = (sname or "").strip()
    if not q or not s:
        return False
    return q in s or s in q


def search_name_of(cfg):
    """本次拿去市场上搜的名字：渠道级 search_name 优先，否则用全局 app_name。

    同一个 App 在不同商店的登记名常常不一样（带后缀、英文名、全称缩写…），
    所以每个渠道都可以在 config.channels 里单独写一个 search_name 覆盖它。
    """
    ch = channel_of(cfg, cfg.get("_key"))
    if ch and str(ch.get("search_name") or "").strip():
        return str(ch["search_name"]).strip()
    return app_name_of(cfg)


def platform_of(cfg, key):
    """渠道 → 端（安卓 / 苹果 / 鸿蒙）。端决定拿 target_codes 里的哪一个构建号比对。"""
    ch = channel_of(cfg, key)
    if ch and ch.get("platform"):
        return str(ch["platform"])
    return DEFAULT_PLATFORM


# --------------------------------------------------------------------------
# 发版基线 / 人工登记表
# --------------------------------------------------------------------------
# 每个渠道的「端」由 config.channels[].platform 声明（见上面的 channels_from_cfg）。
# 端决定比对 target_codes 里的哪一个构建号：
#   苹果 → iOS 构建号；鸿蒙 → HarmonyOS 构建号；其余（含华为的安卓版）→ 安卓 versionCode。
# 走人工的渠道不参与自动比对，所以它对应的构建号留空即可。

# 人工登记表的取值 → (报告状态, 状态级别)。顺序有讲究：
# 「已下架」「未发版」「未通过」都必须先于「通过」判断，否则会被误判成已上线。
MANUAL_RULES = (
    (("已下架", "下架", "已移除", "移除"), "已下架", "gone"),
    (("未发版", "未上传", "未提审", "本期不发", "不发版"), "未发版", "skip"),
    (("不通过", "未通过", "驳回", "拒绝"), "审核未通过", "fail"),
    (("通过", "已上线", "已发布", "上线"), "已上线", "pass"),
    (("审核中", "审核", "未上线"), "审核中", "wait"),
)
NO_SUBMIT = "未发版"


def target_code_of(cfg, key):
    """取该渠道对应端的发版构建号（兼容旧的单值 target_code 字段）"""
    codes = cfg.get("target_codes") or {}
    return norm_ver(codes.get(platform_of(cfg, key)) or cfg.get("target_code") or "")


def manual_lookup(key, cfg):
    """查人工登记表。返回 (状态, 级别, 登记原文)；未登记返回 None"""
    raw = (cfg.get("manual_reference") or {}).get(key)
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    for keywords, status, level in MANUAL_RULES:
        if any(k in text for k in keywords):
            return status, level, text
    return text, "wait", text          # 认不出的登记值：原样展示，按未上线处理


# --------------------------------------------------------------------------
# 状态判定
# --------------------------------------------------------------------------
def judge(row, cfg):
    """
    判定顺序：
      1) 人工登记为「未发版」→ 直接显示未发版：该渠道本次没有上传包提审，
         公开页上挂的是旧版本，抓来的数据不适用
      2) 数据源**明确答复查无此应用** → 显示「已下架」（可信的否定结论，见 DelistedError）
      3) 有公开数据 → 按两条硬条件自动判定：① 公开更新日期 >= release_date；
         ② 公开版本 == target_version 或 公开构建号 == 该端 target_codes 值。
         命中任一即「已上线」，其余「审核中」
      4) 公开数据抓不到（且不是「已下架」）→ 用人工登记表的结果补进报告
    返回 (状态文本, 状态级别, 判据说明)，并把数据来源写回 row["source"]
    """
    key = row["key"]
    m = manual_lookup(key, cfg)

    # 1) 未发版：本次未上传包提审，公开页数据不参与判定
    if m and m[0] == NO_SUBMIT:
        row["source"] = "人工"
        return NO_SUBMIT, "skip", f"人工登记：{m[2]}（本次未上传包提审）"

    # 2) 已下架：抓取器拿到了「服务端明确答复资源不存在」，这是可信的否定结论，
    #    不再拿人工登记表兜底（否则会把「已经不在这个市场了」盖成「审核中」）。
    if row.get("delisted"):
        row["source"] = "自动"
        why = row["note"]
        if m:
            why += f"；人工登记表填的是「{m[2]}」，以公开数据为准"
        return "已下架", "gone", why

    # 3) 有公开数据就按硬条件自动判定
    if row["ok"]:
        row["source"] = "自动"
        return judge_auto(row, cfg)

    # 4) 抓不到公开数据 → 人工登记表兜底
    if m:
        row["source"] = "人工"
        why = f"人工登记：{m[2]}"
        if row["note"]:
            why += f"（公开页抓取失败：{row['note']}）"
        return m[0], m[1], why

    row["source"] = "—"
    return "无法自动获取", "unknown", row["note"]


def judge_auto(row, cfg):
    """只按公开数据判：日期阈值 + 版本/构建号一致性"""
    key = row["key"]
    ver = norm_ver(row.get("version"))
    code = norm_ver(row.get("code") or "")
    date = row.get("date")
    rel = cfg.get("release_date")
    target = norm_ver(cfg.get("target_version") or "")
    target_code = target_code_of(cfg, key)

    # ① 日期阈值
    if rel and date and date >= rel:
        return "已上线", "pass", f"更新日期 {date} ≥ 发版日 {rel}"

    # ② 版本号 / 构建号 与发版登记值一致
    if target and ver and ver == target:
        return "已上线", "pass", f"版本 {ver} = 发版版本 {target}"
    if target_code and code and code == target_code:
        return "已上线", "pass", \
            f"构建号 {code} = 发版{platform_of(cfg, key)}构建号 {target_code}"

    # 其余一律视为仍在审核中
    facts = []
    if ver:
        facts.append(f"版本 {ver}")
    if code and code != ver:
        facts.append(f"构建号 {code}")
    if date:
        facts.append(f"更新日期 {date}")
    return "审核中", "wait", "、".join(facts) + "，未达发版基线" if facts \
        else "未取到版本与日期，按未上线处理"


def history_last_ok(key):
    """返回该渠道在历史快照里**最近一次成功抓到**的 (版本, 日期, 巡检时间)；没有则 None。

    只读 history/*.json，不写任何东西。用途：给「已下架」那行补一句历史证据
    （「以前能抓到 X 版本 / 某日期」），让结论可追溯。history 不存在就返回 None。
    """
    hist = os.path.join(HERE, "history")
    if not os.path.isdir(hist):
        return None
    best = None
    for fn in os.listdir(hist):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(hist, fn), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001  坏掉的快照跳过，不影响本次判定
            continue
        s = (d.get("markets") or {}).get(key) or {}
        if not s.get("ok"):
            continue
        when = str(d.get("checked_at") or fn)
        if best is None or when > best[2]:
            best = (s.get("version"), s.get("date"), when)
    return best


def rows_from_snapshot(snapshot, cfg):
    """把 history/latest.json 里的上次抓取结果，用**当前配置**重新判一遍。

    用途：网页服务刚启动、或你只改了配置还没重新巡检时，页面能立刻有内容——
    抓取数据用上次的，判定口径用最新的。注意这只影响展示，
    report/ 下的历史报告文件不会被改写。
    """
    rows = []
    for ch in channels_from_cfg(cfg):
        if not ch["report"]:
            continue
        key, market = ch["key"], ch["name"]
        s = snapshot.get(key) or {}
        row = {"key": key, "market": market, "report": True,
               "ok": bool(s.get("ok")), "version": s.get("version"),
               "date": s.get("date"), "code": s.get("code"), "url": None,
               "code_kind": "versionCode", "source": s.get("source") or "自动",
               "fetch_tag": s.get("fetch_tag"), "note": s.get("note") or "",
               "auto_failed": bool(s.get("auto_failed")),
               "delisted": bool(s.get("delisted")),
               "status": "", "status_level": "unknown", "judge_note": ""}
        status, level, why = judge(row, cfg)
        row["status"], row["status_level"], row["judge_note"] = status, level, why
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# 输出渲染
# --------------------------------------------------------------------------
LEVEL_ICON = {"pass": "✅", "wait": "⏳", "gone": "⛔", "skip": "⊘",
              "fail": "❌", "unknown": "⚠️"}
# 汇总卡片顺序与标题
LEVEL_LABEL = [("pass", "已上线"), ("wait", "审核中"), ("gone", "已下架"),
               ("skip", "未发版"), ("fail", "审核未通过"), ("unknown", "无法自动获取")]


def ver_display(r) -> str:
    """「公开版本」列的显示值。

    不少 App 在安卓各市场的 versionName 长期不变，每次发版只跳构建号，
    所以只显示版本名会让「其实已经换成新包了」看起来和上一版一模一样。
    凡接口同时给了版本名和构建号，就把构建号一并显示：1.2.0 (102)。
    （OPPO 公开的是 APK 文件 hash，不是 versionCode，不在此列，故不显示。）
    """
    v = r.get("version") or ""
    c = str(r.get("code") or "")
    if c and c != v and r.get("code_kind", "versionCode") == "versionCode":
        return f"{v} ({c})" if v else c
    return v or "—"


def render_markdown(rows, cfg, now):
    L = []
    L.append(f"# {app_name_of(cfg)} · 各应用市场审核结果\n")
    L.append(f"- 巡检时间：{now}")
    ident = []
    if cfg.get("android_pkg"):
        ident.append(f"包名 `{cfg['android_pkg']}`")
    if cfg.get("ios_track_id"):
        ident.append(f"iOS trackId `{cfg['ios_track_id']}`")
    if ident:
        L.append("- " + " ｜ ".join(ident))
    tc = cfg.get("target_codes") or {}
    codes_txt = " · ".join(f"{p} `{v}`" for p, v in tc.items() if v) or "未配置"
    L.append(f"- 发版基线：日期 `{cfg.get('release_date') or '未配置'}`"
             f" ／ 版本 `{cfg.get('target_version') or '未配置'}`"
             f" ／ 构建号 {codes_txt}\n")
    L.append("| 平台 | 市场 | 公开版本 | 更新时间 | 状态 | 数据来源 |")
    L.append("|---|---|---|---|---|---|")
    for r in rows:
        icon = LEVEL_ICON[r["status_level"]]
        src = r.get("source") or "—"
        if r.get("auto_failed"):
            src += " ⚠️"
        L.append(f"| {r['key']} | {r['market']} | {ver_display(r)} | "
                 f"{r['date'] or '—'} | {icon} {r['status']} | {src} |")
    L.append("")
    counts = {lv: sum(1 for r in rows if r["status_level"] == lv)
              for lv, _ in LEVEL_LABEL}
    summary = " ｜ ".join(f"{name} {counts[lv]}" for lv, name in LEVEL_LABEL)
    L.append(f"**合计 {len(rows)} 家：{summary}**\n")
    manual = [r for r in rows if r.get("source") == "人工"]
    if manual:
        L.append("> 数据来源：`自动` ＝本次从该市场公开详情页抓取；`人工` ＝取自 config.json 的"
                 "人工登记表（该渠道没有可自动抓取的公开页 / 本次未上传包提审 /"
                 "本次抓取失败被兜底）。"
                 "本次走人工登记的渠道：" +
                 "、".join(f"**{r['key']}**（{r['status']}）" for r in manual) + "。\n")
    L.append("> 判定口径：公开更新日期 ≥ 发版日期，或公开版本（构建号）＝发版登记值，"
             "即视为**已上线**；其余视为**审核中**；登记为**未发版**的渠道＝本次未上传包提审；"
             "**数据源明确答复查无此应用**的渠道＝**已下架**（该市场里已经没有它了）。")
    cdn = [r for r in rows if r.get("fetch_tag") == "CDN 缓存"]
    if cdn:
        L.append("")
        L.append("> ⚠ **数据时效提示**：以下渠道本次未能直取源站（防缓存参数未生效），"
                 "取到的是 CDN 缓存页，数据可能滞后于源站："
                 + "、".join(f"**{r['key']}**" for r in cdn) + "。")
    gone = [r for r in rows if r["status_level"] == "gone"]
    if gone:
        L.append("")
        L.append(f"> ⛔ **本市场已无此应用（{len(gone)} 家）**：数据源**明确答复查无此应用**，"
                 "属可信的否定结论（不是抓取故障，也没有拿人工登记表兜底）：")
        L.append(">")
        for r in gone:
            L.append(f"> - **{r['key']}**（{r['market']}）：{r['judge_note']}")
    failed = [r for r in rows if r.get("auto_failed")]
    if failed:
        L.append("")
        L.append(f"> ⚠️ **本次有 {len(failed)} 家自动抓取失败，这几家本期的数据不可信**"
                 "（表里标 `⚠️` 的那几行，结果取自人工登记表，"
                 "**不是**脚本核验过的公开数据）：")
        L.append(">")
        for r in failed:
            L.append(f"> - **{r['key']}**（{r['market']}）：{r['note']}")
        L.append(">")
        L.append("> 说明该渠道的解析规则可能已失效（对方改版或换了接口），"
                 "需要人工去市场里核验这一家；规则本身要不要修，见 `README.md` 对应小节。")
    return "\n".join(L)


COLOR = {"pass": "#0a7d33", "wait": "#b26a00", "gone": "#7b1f2e",
         "skip": "#4a5568", "fail": "#c0392b", "unknown": "#8a8a8a"}
BG = {"pass": "#e7f7ec", "wait": "#fff4e0", "gone": "#f7e7ea",
      "skip": "#eef1f5", "fail": "#fdecea", "unknown": "#f2f2f2"}

# 人工登记表的可选值（网页「设置」面板的下拉框用它；空字符串 = 未登记）
MANUAL_CHOICES = ("", "未发版", "已下架", "审核中", "审核通过", "审核未通过")


# --------------------------------------------------------------------------
# 报告页里嵌的「设置」面板（右上角按钮 + 弹窗）
# --------------------------------------------------------------------------
# 注意：下面三个是**普通字符串**（不是 f-string），所以里面的 { } 都是字面量、
# 不需要写成 {{ }}；要动态填的地方留 __占位符__，由 _settings_panel() 替换。
# 这是刻意的——把 CSS/JS 塞进 f-string 里，每对大括号都要转义，极容易出错。
_SETTINGS_CSS = """
#rb-bar{position:fixed;top:16px;right:16px;display:flex;gap:8px;align-items:center;z-index:50}
.rb-btn{border:1px solid #d6dbe1;background:#fff;color:#2b3a4a;font-size:13px;
  padding:8px 14px;border-radius:8px;cursor:pointer;font-family:inherit;
  box-shadow:0 1px 3px rgba(0,0,0,.06)}
.rb-btn:hover{background:#f3f6fa;border-color:#b9c4d0}
.rb-btn.primary{background:#2b6cb0;border-color:#2b6cb0;color:#fff}
.rb-btn.primary:hover{background:#255c96}
.rb-btn:disabled{opacity:.55;cursor:not-allowed}
#rb-progress{font-size:12px;color:#666;background:#fff;border:1px solid #e6e8eb;
  border-radius:8px;padding:6px 10px;max-width:320px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;display:none;box-shadow:0 1px 3px rgba(0,0,0,.06)}
#rb-progress.on{display:inline-block}
#rb-mask{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(20,28,38,.45);
  display:none;z-index:100;padding:40px 20px;overflow:auto}
#rb-mask.on{display:block}
#rb-modal{max-width:780px;margin:0 auto;background:#fff;border-radius:12px;
  box-shadow:0 12px 40px rgba(0,0,0,.22);overflow:hidden}
.rb-head{padding:16px 20px;border-bottom:1px solid #eceff2;display:flex;
  justify-content:space-between;align-items:center}
.rb-head h3{margin:0;font-size:16px}
.rb-body{padding:18px 20px;max-height:64vh;overflow:auto}
.rb-foot{padding:14px 20px;border-top:1px solid #eceff2;display:flex;gap:10px;
  justify-content:flex-end;align-items:center;background:#fafbfc;flex-wrap:wrap}
.rb-sec{margin-bottom:22px}
.rb-sec>h4{font-size:13px;margin:0 0 4px;color:#2b3a4a}
.rb-hint{font-size:12px;color:#8a8a8a;margin:0 0 10px;line-height:1.65}
.rb-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px}
.rb-field label{display:block;font-size:12px;color:#666;margin-bottom:4px}
.rb-field input,.rb-field select{width:100%;padding:7px 9px;font-size:13px;
  border:1px solid #d6dbe1;border-radius:6px;font-family:inherit;background:#fff;color:#1a1a1a}
.rb-field input:focus,.rb-field select:focus{outline:none;border-color:#2b6cb0;
  box-shadow:0 0 0 3px rgba(43,108,176,.12)}
table.rb-tbl{width:100%;border-collapse:collapse}
table.rb-tbl th{font-size:12px;color:#666;text-align:left;padding:6px 8px;
  border-bottom:1px solid #eceff2;font-weight:600;background:none}
table.rb-tbl td{padding:5px 8px;border-bottom:1px solid #f4f5f7;font-size:13px}
table.rb-tbl tr:last-child td{border-bottom:none}
table.rb-tbl select{width:100%;padding:5px 7px;font-size:12.5px;border:1px solid #d6dbe1;
  border-radius:6px;font-family:inherit;background:#fff;color:#1a1a1a}
td.rb-k{font-weight:600}
.rb-ro{border:1px solid #eceff2;border-radius:8px;padding:10px 12px;background:#fafbfc}
.rb-ro summary{cursor:pointer;font-size:12.5px;color:#555}
.rb-ro pre{margin:8px 0 0;font-size:12px;color:#555;white-space:pre-wrap;
  font-family:ui-monospace,Consolas,monospace}
#rb-err{display:none;margin:0 0 16px;padding:10px 12px;border-radius:8px;
  background:#fdecea;color:#c0392b;font-size:12.5px;line-height:1.7}
#rb-err.on{display:block}
#rb-toast{position:fixed;left:50%;bottom:40px;transform:translateX(-50%);
  background:#1f2d3d;color:#fff;font-size:13px;padding:10px 18px;border-radius:8px;
  z-index:200;display:none;box-shadow:0 6px 20px rgba(0,0,0,.2);max-width:80vw}
#rb-toast.on{display:block}
.rb-note{font-size:12px;color:#8a8a8a;margin-right:auto;line-height:1.5}
#rb-notice{background:#eaf2fb;border:1px solid #cfe0f3;color:#2b5f96;font-size:12.5px;
  padding:10px 14px;border-radius:8px;margin-top:16px;line-height:1.7}
.rb-mv{white-space:nowrap}
.rb-mini{border:1px solid #d6dbe1;background:#fff;color:#2b3a4a;font-size:12px;
  width:26px;height:24px;border-radius:6px;cursor:pointer;font-family:inherit;padding:0}
.rb-mini:hover{background:#f3f6fa;border-color:#b9c4d0}
.rb-mini:disabled{opacity:.3;cursor:not-allowed}
/* 小号按钮：给**带文字**的小按钮用（「+ 添加一个机器人」「发一条测试消息」）。
   ⚠️ 文字按钮千万别挂 .rb-mini —— 那个类给 ↑↓ 箭头定死了 26×24 的宽高，
   文字会被挤成溢出的一坨。这里只改字号和内边距，宽度由内容撑开。 */
.rb-btn.rb-sm{font-size:12.5px;padding:6px 12px;border-radius:6px;
  white-space:nowrap;box-shadow:none}
#rb-first{display:none;margin:0 0 16px;padding:12px 14px;border-radius:8px;
  background:#eaf7ee;border:1px solid #cbe8d5;color:#1f6b3a;
  font-size:12.5px;line-height:1.85}
#rb-first.on{display:block}
#rb-first b{color:#14653a}
#rb-first code{background:#fff;border:1px solid #cbe8d5;padding:1px 5px;border-radius:4px}
#rb-first .rb-first-why{display:block;margin-top:6px;color:#5b8a70}
@media (max-width:820px){#rb-bar{position:static;justify-content:flex-end;margin-bottom:14px}}
"""

_SETTINGS_HTML = """
<div id="rb-bar">
  <span id="rb-progress"></span>
  <button class="rb-btn" id="rb-rescan" type="button">&#8635; 重新巡检</button>
  <button class="rb-btn primary" id="rb-open" type="button">&#9881; 设置</button>
</div>
<div id="rb-mask">
  <div id="rb-modal">
    <div class="rb-head">
      <h3>&#9881; 巡检设置</h3>
      <button class="rb-btn" id="rb-close" type="button">关闭</button>
    </div>
    <div class="rb-body">
      <div id="rb-first">
        <b>第一次使用：先填下面这 3 处，再开始巡检。</b><br>
        ① <b>应用名 + 安卓包名</b>（必填，报告标题和各市场的搜索都用它）<br>
        ② <b>本次发版基线</b>——发版日期／版本／构建号，用来判定「已上线」还是「审核中」<br>
        ③ <b>渠道</b>里取消勾选你不发行的商店（默认是全部勾上）<br>
        填完点右下角「<b>保存并重新巡检</b>」，就会自动开始第一次抓取。<br>
        <span class="rb-first-why">这次没有自动巡检，是因为还不知道你要巡检哪个应用 ——
          拿默认的示例信息去抓，15 家市场只会全部显示「查无此应用」。
          填过一次之后，以后每次打开都会自动巡检，不用再管这里。</span>
      </div>
      <div id="rb-err"></div>
      <div class="rb-sec">
        <h4>① 应用信息</h4>
        <p class="rb-hint">各市场靠这些信息定位你的应用：安卓系市场用<b>包名</b>，苹果用 <b>trackId</b>。
          <b>应用名</b>是拿去市场上搜索用的名字，也会显示在报告标题上。</p>
        <div class="rb-grid">
          <div class="rb-field"><label>应用名</label>
            <input type="text" id="rb-name" placeholder="你的应用名"></div>
          <div class="rb-field"><label>安卓包名</label>
            <input type="text" id="rb-pkg" placeholder="com.example.app"></div>
          <div class="rb-field"><label>iOS trackId</label>
            <input type="text" id="rb-track" placeholder="纯数字，可留空"></div>
          <div class="rb-field"><label>iOS bundleId</label>
            <input type="text" id="rb-bundle" placeholder="可留空"></div>
        </div>
      </div>
      <div class="rb-sec">
        <h4>② 本次发版基线</h4>
        <p class="rb-hint">判定只认两条硬条件：公开更新日期 ≥ 发版日期；或公开版本（构建号）＝这里登记的值。
          命中任一即「已上线」，其余「审核中」。不确定的留空即可，只靠日期也能判。<br>
          构建号按「端」分开填，下面的框是按你当前渠道清单里<b>会自动抓取</b>的那几家生成的——
          只走人工的渠道（例如鸿蒙、荣耀）没有公开数据可比，不参与自动判定，
          所以这里不会为它们生成输入框，也不用填。</p>
        <div class="rb-grid">
          <div class="rb-field"><label>发版日期</label>
            <input type="date" id="rb-date"></div>
          <div class="rb-field"><label>目标版本（可留空）</label>
            <input type="text" id="rb-ver" placeholder="如 1.2.3"></div>
        </div>
        <div class="rb-grid" id="rb-code-grid" style="margin-top:12px"></div>
      </div>
      <div class="rb-sec">
        <h4>③ 渠道：显示哪些、按什么顺序</h4>
        <p class="rb-hint">取消勾选＝这条渠道不进报告，也不去抓。<b>上下的顺序就是报告里各行的顺序</b>。
          「自动」＝程序会去抓该市场的公开数据；「人工」＝该市场没有可自动抓取的公开数据源，
          结果取自下面的人工登记表。</p>
        <table class="rb-tbl">
          <thead><tr><th style="width:46px">显示</th><th style="width:96px">渠道</th><th>市场</th>
            <th style="width:56px">端</th><th style="width:64px">抓取</th>
            <th style="width:74px">排序</th></tr></thead>
          <tbody id="rb-ch-tbody"></tbody>
        </table>
      </div>
      <div class="rb-sec">
        <h4>④ 人工登记表</h4>
        <p class="rb-hint">登记为「未发版」的渠道＝本次没有上传包提审，脚本会跳过它、不再抓公开页；
          其余渠道抓不到公开数据时，用这里的结果补进报告。留空＝未登记。</p>
        <table class="rb-tbl">
          <thead><tr><th style="width:110px">渠道</th><th>市场</th>
          <th style="width:150px">登记结果</th></tr></thead>
          <tbody id="rb-manual-tbody"></tbody>
        </table>
      </div>
      <div class="rb-sec">
        <h4>⑤ 各市场应用 ID（可选）</h4>
        <p class="rb-hint">大多数市场靠<b>包名</b>定位，这里不用填。只有少数几家需要额外的 ID
          （华为的 C 编号、OPPO / vivo 的数字 id、百度 / 360 / 搜狗的 docid / sid 等）。
          不确定就留空——留空时程序会按应用名去市场上搜；搜不到会在报告里点名说明原因。
          输入框里的灰字写的是这家该填什么。</p>
        <div class="rb-grid" id="rb-ids-grid"></div>
      </div>
      <div class="rb-sec">
        <h4>&#9313; 消息通知（可选）</h4>
        <p class="rb-hint">巡检结束后把结果推到群里，不用再打开页面看。<br>
          在群聊里点「<b>群设置 &rarr; 群机器人 &rarr; 添加机器人</b>」，
          把它给的 <b>webhook 地址</b>粘到下面即可。<br>
          「每次都发」适合发版后手动跑一次；「只在有变化时才发」适合每天定时跑，免得每次都收到一样的一份。<br>
          <b>留空的行会被忽略</b>；开关关着时，下面填的地址只是存着、不会发。
          填完可以点「发一条测试消息」当场验证地址对不对。</p>
        <div class="rb-grid">
          <div class="rb-field"><label>通知开关</label>
            <select id="rb-nt-on">
              <option value="0">关闭（不推送）</option>
              <option value="1">开启</option>
            </select></div>
          <div class="rb-field"><label>推送时机</label>
            <select id="rb-nt-mode">
              <option value="always">每次都发</option>
              <option value="change">只在有变化时才发</option>
            </select></div>
        </div>
        <table class="rb-tbl" style="margin-top:12px">
          <thead><tr><th style="width:132px">类型</th><th>Webhook 地址</th>
            <th style="width:104px">备注名</th><th style="width:78px"></th></tr></thead>
          <tbody id="rb-nt-tbody"></tbody>
        </table>
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="rb-btn rb-sm" type="button" id="rb-nt-add">+ 添加一个机器人</button>
          <button class="rb-btn rb-sm" type="button" id="rb-nt-test">发一条测试消息</button>
          <span id="rb-nt-note" style="font-size:12px;color:#8a8a8a"></span>
        </div>
      </div>
    </div>
    <div class="rb-foot">
      <span class="rb-note" id="rb-note">保存前会自动备份 config.json 到 backup/</span>
      <button class="rb-btn" id="rb-cancel" type="button">取消</button>
      <button class="rb-btn" id="rb-save" type="button">仅保存</button>
      <button class="rb-btn primary" id="rb-save-run" type="button">保存并重新巡检</button>
    </div>
  </div>
</div>
<div id="rb-toast"></div>
"""

_SETTINGS_JS = """
(function () {
  var isFile = (location.protocol === 'file:');
  var scanTimer = null, scanBusy = false, waitTicks = 0;
  var FIRST_KEY = 'rb-need-setup-opened';   // 「首次使用」弹窗这次会话里是否已自动弹过
  var curRev = null;   // 打开弹窗时那份配置的指纹；保存时带回去，用于发现「别人抢先改过」

  function el(id) { return document.getElementById(id); }

  function toast(msg, ms) {
    var t = el('rb-toast'); if (!t) { return; }
    t.textContent = msg; t.classList.add('on');
    clearTimeout(t.__h);
    t.__h = setTimeout(function () { t.classList.remove('on'); }, ms || 2800);
  }

  function prog(text, on) {
    var p = el('rb-progress'); if (!p) { return; }
    p.textContent = text || '';
    if (on) { p.classList.add('on'); } else { p.classList.remove('on'); }
  }

  function setBusy(disabled) {
    scanBusy = disabled;
    var ids = ['rb-save', 'rb-save-run', 'rb-rescan'];
    for (var i = 0; i < ids.length; i++) {
      var b = el(ids[i]); if (b) { b.disabled = disabled; }
    }
  }

  function showErr(lines) {
    var box = el('rb-err'); if (!box) { return; }
    if (!lines || !lines.length) { box.classList.remove('on'); box.innerHTML = ''; return; }
    box.innerHTML = lines.join('<br>');
    box.classList.add('on');
  }

  function optText(v) { return v || '（未登记）'; }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function renderCodes(d, tc) {
    var ps = d.platforms || [], h = '';
    for (var i = 0; i < ps.length; i++) {
      var p = ps[i], v = tc[p];
      v = (v === null || v === undefined) ? '' : v;
      h += '<div class="rb-field"><label>' + esc(p) + '构建号</label>'
        + '<input type="text" class="rb-code" data-platform="' + esc(p)
        + '" value="' + esc(v) + '" placeholder="可留空"></div>';
    }
    el('rb-code-grid').innerHTML = h;
  }

  function renderChannels(d) {
    var chs = d.channels || [], h = '';
    for (var i = 0; i < chs.length; i++) {
      var c = chs[i];
      h += '<tr data-key="' + esc(c.key) + '" data-name="' + esc(c.name)
        + '" data-platform="' + esc(c.platform) + '" data-fetcher="' + esc(c.fetcher || '') + '">'
        + '<td><input type="checkbox" class="rb-ch-on"' + (c.enabled ? ' checked' : '') + '></td>'
        + '<td class="rb-k">' + esc(c.key) + '</td>'
        + '<td>' + esc(c.name) + '</td>'
        + '<td>' + esc(c.platform) + '</td>'
        + '<td>' + (c.fetcher ? '自动' : '人工') + '</td>'
        + '<td class="rb-mv"><button type="button" class="rb-mini rb-up">&uarr;</button>'
        + '<button type="button" class="rb-mini rb-down">&darr;</button></td></tr>';
    }
    el('rb-ch-tbody').innerHTML = h;
    refreshArrows();
  }

  function refreshArrows() {
    var rows = el('rb-ch-tbody').querySelectorAll('tr');
    for (var i = 0; i < rows.length; i++) {
      rows[i].querySelector('.rb-up').disabled = (i === 0);
      rows[i].querySelector('.rb-down').disabled = (i === rows.length - 1);
    }
  }

  function renderManual(d, mr) {
    var chs = d.channels || [], opts = d.manual_choices || [], h = '';
    for (var i = 0; i < chs.length; i++) {
      var c = chs[i], o = '';
      for (var j = 0; j < opts.length; j++) {
        o += '<option value="' + esc(opts[j]) + '">' + esc(opts[j] || '（未登记）') + '</option>';
      }
      var cur = mr[c.key] || '';
      h += '<tr><td class="rb-k">' + esc(c.key) + '</td><td>' + esc(c.name) + '</td>'
        + '<td><select data-key="' + esc(c.key) + '">' + o + '</select></td></tr>';
    }
    el('rb-manual-tbody').innerHTML = h;
    var sels = el('rb-manual-tbody').querySelectorAll('select');
    for (var k = 0; k < sels.length; k++) {
      sels[k].value = mr[sels[k].getAttribute('data-key')] || '';
    }
  }

  function renderIds(d) {
    var hints = d.id_hints || {}, ids = d.app_ids || {}, list = d.id_keys || [], h = '';
    for (var i = 0; i < list.length; i++) {
      var k = list[i], v = ids[k];
      v = (v === null || v === undefined) ? '' : v;
      h += '<div class="rb-field"><label>' + esc(k) + '</label>'
        + '<input type="text" class="rb-id" data-key="' + esc(k) + '" value="' + esc(v)
        + '" placeholder="' + esc(hints[k] || '可留空') + '"></div>';
    }
    el('rb-ids-grid').innerHTML = h;
  }

  var NT_TYPES = [['wecom', '企业微信群机器人'], ['feishu', '飞书群机器人'],
                  ['dingtalk', '钉钉群机器人']];

  function ntTypeOptions(cur) {
    var h = '';
    for (var i = 0; i < NT_TYPES.length; i++) {
      h += '<option value="' + NT_TYPES[i][0] + '"'
        + (cur === NT_TYPES[i][0] ? ' selected' : '') + '>' + NT_TYPES[i][1] + '</option>';
    }
    return h;
  }

  function ntRow(p) {
    p = p || {};
    return '<tr>'
      + '<td><select class="rb-nt-type">' + ntTypeOptions(p.type || 'wecom') + '</select></td>'
      + '<td><input type="text" class="rb-nt-hook" value="' + esc(p.webhook || '')
      + '" placeholder="https://...（从群机器人那里复制）"></td>'
      + '<td><input type="text" class="rb-nt-name" value="' + esc(p.name || '')
      + '" placeholder="可留空"></td>'
      + '<td><button type="button" class="rb-btn rb-sm rb-nt-del">移除</button></td></tr>';
  }

  function renderNotify(d) {
    var nt = d.notify || {}, ps = nt.providers || [], h = '';
    for (var i = 0; i < ps.length; i++) { h += ntRow(ps[i]); }
    el('rb-nt-tbody').innerHTML = h || ntRow(null);
    el('rb-nt-on').value = (nt.on === false || !nt.on) ? '0' : '1';
    el('rb-nt-mode').value = (nt.mode === 'change') ? 'change' : 'always';
    el('rb-nt-note').textContent = nt.on ? '' : '（开关是关着的，下面先填着不影响）';
  }

  // 只收集 webhook 非空的行 —— 空行等于「没配」，不该写进 config
  function collectNotify() {
    var rows = document.querySelectorAll('#rb-nt-tbody tr'), out = [];
    for (var i = 0; i < rows.length; i++) {
      var hook = rows[i].querySelector('.rb-nt-hook').value.trim();
      if (!hook) { continue; }
      out.push({
        type: rows[i].querySelector('.rb-nt-type').value,
        name: rows[i].querySelector('.rb-nt-name').value.trim(),
        webhook: hook
      });
    }
    return out;
  }

  function showFirst(on) {
    var f = el('rb-first');
    if (!f) { return; }
    if (on) { f.classList.add('on'); } else { f.classList.remove('on'); }
  }

  // pre：调用方已经把 /api/config 拿回来了就顺手传进来，省一次请求。
  // ⚠️ 必须判断它是不是「配置对象」——旁边的点击监听器如果直接把事件对象
  // 传进来（addEventListener('click', openModal) 就会这样），会拿不到 editable。
  function openModal(pre) {
    if (isFile) { toast('修改配置要通过本地服务打开：双击 check_review_status.exe'); return; }
    var got = (pre && pre.editable) ? Promise.resolve(pre)
      : fetch('/api/config', { cache: 'no-store' })
        .then(function (r) { return r.json(); });
    got
      .then(function (d) {
        var e = d.editable, mr = e.manual_reference || {};
        curRev = d.rev || null;   // 记下基准指纹，保存时带回去做并发校验
        el('rb-name').value = e.app_name || '';
        el('rb-pkg').value = e.android_pkg || '';
        el('rb-track').value = (e.ios_track_id === null || e.ios_track_id === undefined)
          ? '' : e.ios_track_id;
        el('rb-bundle').value = e.ios_bundle_id || '';
        el('rb-date').value = e.release_date || '';
        el('rb-ver').value = e.target_version || '';
        renderCodes(d, e.target_codes || {});
        renderChannels(d);
        renderManual(d, mr);
        renderIds(d);
        renderNotify(e);
        el('rb-note').textContent = '配置文件 ' + d.config_path + '，保存前自动备份到 backup/';
        showErr(null);
        showFirst(!!d.need_setup);
        el('rb-mask').classList.add('on');
      })
      .catch(function () { toast('读不到配置，请确认服务窗口还开着'); });
  }

  function closeModal() { var m = el('rb-mask'); if (m) { m.classList.remove('on'); } }

  function collect() {
    var manual = {}, channels = [], appIds = {}, codes = {}, i;

    // 渠道表：行的**先后顺序**就是报告里的顺序，勾选状态就是「显示哪些」
    var rows = document.querySelectorAll('#rb-ch-tbody tr');
    for (i = 0; i < rows.length; i++) {
      var tr = rows[i];
      channels.push({
        key: tr.getAttribute('data-key'),
        name: tr.getAttribute('data-name'),
        platform: tr.getAttribute('data-platform'),
        fetcher: tr.getAttribute('data-fetcher') || null,
        enabled: tr.querySelector('.rb-ch-on').checked
      });
    }
    var cs = document.querySelectorAll('#rb-code-grid input');
    for (i = 0; i < cs.length; i++) {
      codes[cs[i].getAttribute('data-platform')] = cs[i].value;
    }
    var ms = document.querySelectorAll('#rb-manual-tbody select');
    for (i = 0; i < ms.length; i++) {
      manual[ms[i].getAttribute('data-key')] = ms[i].value;
    }
    // 应用 ID：只上传填了值的（清空某项就等于把它删掉）
    var ais = document.querySelectorAll('#rb-ids-grid input');
    for (i = 0; i < ais.length; i++) {
      var v = ais[i].value.trim();
      if (v) { appIds[ais[i].getAttribute('data-key')] = v; }
    }
    return {
      rev: curRev,
      app_name: el('rb-name').value,
      android_pkg: el('rb-pkg').value,
      ios_track_id: el('rb-track').value,
      ios_bundle_id: el('rb-bundle').value,
      release_date: el('rb-date').value,
      target_version: el('rb-ver').value,
      target_codes: codes,
      channels: channels,
      app_ids: appIds,
      manual_reference: manual,
      notify: {
        on: el('rb-nt-on').value === '1',
        mode: el('rb-nt-mode').value,
        providers: collectNotify()
      }
    };
  }

  function save(thenScan) {
    if (scanBusy) { return; }
    setBusy(true);
    fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collect())
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        setBusy(false);
        if (!res.ok || !res.d.ok) {
          showErr(res.d.errors || [res.d.error || '保存失败']);
          return;
        }
        // 保存成功：把服务端回的新指纹收下，否则连续点两次保存会误判成「别人改过」
        if (res.d.config && res.d.config.rev) { curRev = res.d.config.rev; }
        showErr(null); closeModal();
        var n = (res.d.changed || []).length;
        toast(n ? ('已保存并备份（改动 ' + n + ' 项），正在刷新…') : '配置没有变化，正在刷新…');
        if (thenScan) { startScan(true); }
        else { setTimeout(function () { location.reload(); }, 700); }
      })
      .catch(function () {
        setBusy(false);
        showErr(['保存失败：连不上本地服务，请确认那个黑窗口还开着']);
      });
  }

  function startScan(fromModal) {
    if (fromModal) { closeModal(); }
    fetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { toast(d.error || '无法启动巡检'); return; }
        waitTicks = 3;
        prog('正在巡检…', true);
        setBusy(true);
        poll();
        toast(d.started ? '已开始巡检，跑完页面会自动刷新' : '已有一轮巡检在进行中');
      })
      .catch(function () { toast('无法启动巡检，服务可能已停止'); });
  }

  function poll() {
    if (scanTimer) { return; }
    scanTimer = setInterval(function () {
      fetch('/api/state', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.scanning) {
            waitTicks = 0;
            prog(s.progress || '正在巡检…', true);
            setBusy(true);
            return;
          }
          if (waitTicks > 0) { waitTicks--; return; }
          clearInterval(scanTimer); scanTimer = null;
          setBusy(false);
          if (s.error) { toast('上次巡检出错：' + s.error, 7000); }
          prog('巡检完成，正在刷新页面…', true);
          location.reload();
        })
        .catch(function () {
          clearInterval(scanTimer); scanTimer = null;
          setBusy(false); prog('', false);
        });
    }, 1500);
  }

  if (el('rb-open')) {
    el('rb-open').addEventListener('click', function () { openModal(); });
  }
  if (el('rb-close')) { el('rb-close').addEventListener('click', closeModal); }
  if (el('rb-cancel')) { el('rb-cancel').addEventListener('click', closeModal); }
  if (el('rb-save')) { el('rb-save').addEventListener('click', function () { save(false); }); }
  if (el('rb-save-run')) {
    el('rb-save-run').addEventListener('click', function () { save(true); });
  }
  if (el('rb-rescan')) {
    el('rb-rescan').addEventListener('click', function () {
      if (isFile) { toast('要重新巡检请双击 check_review_status.exe 从服务打开'); return; }
      startScan(false);
    });
  }
  if (el('rb-ch-tbody')) {
    // 「↑↓」调顺序：直接挪 DOM 行，collect() 按 DOM 顺序读出渠道数组
    el('rb-ch-tbody').addEventListener('click', function (ev) {
      var btn = ev.target;
      if (!btn || !btn.classList) { return; }
      var isUp = btn.classList.contains('rb-up');
      var isDown = btn.classList.contains('rb-down');
      if (!isUp && !isDown) { return; }
      var tr = btn.parentNode.parentNode;
      var tb = tr.parentNode;
      if (isUp && tr.previousElementSibling) {
        tb.insertBefore(tr, tr.previousElementSibling);
      } else if (isDown && tr.nextElementSibling) {
        tb.insertBefore(tr.nextElementSibling, tr);
      }
      refreshArrows();
    });
  }
  if (el('rb-nt-tbody')) {
    // 「移除」一行；只剩一行时不删行、只把内容抹掉（空行本来就不会被提交）
    el('rb-nt-tbody').addEventListener('click', function (ev) {
      var btn = ev.target;
      if (!btn || !btn.classList || !btn.classList.contains('rb-nt-del')) { return; }
      var tr = btn.parentNode.parentNode;
      var tb = el('rb-nt-tbody');
      if (tb.querySelectorAll('tr').length <= 1) {
        tr.querySelector('.rb-nt-hook').value = '';
        tr.querySelector('.rb-nt-name').value = '';
      } else {
        tb.removeChild(tr);
      }
    });
    el('rb-nt-add').addEventListener('click', function () {
      el('rb-nt-tbody').insertAdjacentHTML('beforeend', ntRow(null));
    });
    // 测试消息：不落盘，直接把当前填的地址发一条，验证 webhook 填对没有。
    // 发出去的标题是「【测试】XX · 通知测试」，和正式报告（「XX · 各市场审核巡检」）
    // 明显区分 —— 否则群里几条消息堆在一起，根本分不清哪条是真结果。
    el('rb-nt-test').addEventListener('click', function () {
      var list = collectNotify();
      if (!list.length) { showErr(['请先在上面填一条 webhook 地址，再点测试']); return; }
      showErr(null);
      prog('正在发送测试消息…', true);
      fetch('/api/notify-test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ providers: list })
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          prog('', false);
          if (res.ok) { toast('测试消息已发出（标题带【测试】字样），去群里看看'); }
          else { showErr(res.errors || ['测试消息发送失败']); }
        })
        .catch(function () { prog('', false); showErr(['连不上本地服务']); });
    });
  }
  if (el('rb-mask')) {
    el('rb-mask').addEventListener('click', function (ev) {
      if (ev.target === el('rb-mask')) { closeModal(); }
    });
  }
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { closeModal(); }
  });

  // ── 第一次使用：还没填应用信息（服务端 needs_setup() 的判断）───────────
  // 直接把设置弹窗摊开，而不是让人先看一张「15 家全都查无此应用」的空表。
  // sessionStorage 记一笔：关掉弹窗再刷新，不会又被弹一脸；
  // 保存过一次之后 need_setup 就变 false，这段逻辑自然失效。
  function firstOpened() {
    try { return sessionStorage.getItem(FIRST_KEY) === '1'; }
    catch (e) { return true; }        // 取不到就当「已弹过」，宁可少弹也别反复弹
  }
  function markFirstOpened() {
    try { sessionStorage.setItem(FIRST_KEY, '1'); } catch (e) {}
  }

  if (isFile) {
    prog('只读模式：双击 check_review_status.exe 打开才能改配置', true);
  } else {
    fetch('/api/config', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.need_setup && !firstOpened()) { markFirstOpened(); openModal(d); }
      })
      .catch(function () {});
    fetch('/api/state', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.scanning) { prog(s.progress || '正在巡检…', true); setBusy(true); poll(); }
      })
      .catch(function () {});
  }
})();
"""


def _settings_panel():
    """生成「设置」面板。

    骨架是静态的（见 _SETTINGS_HTML）。渠道表、人工登记表、构建号输入框、
    应用 ID 输入框这几块，都由页面里的 JS 在打开弹窗时按 /api/config 现场生成——
    这样渠道清单改了（增删渠道、改顺序）也不用重新渲染报告页，弹窗里拿到的总是最新配置。
    """
    return (_SETTINGS_HTML
            + "<style>" + _SETTINGS_CSS + "</style>"
            + "<script>" + _SETTINGS_JS + "</script>")


def render_html(rows, cfg, now, notice=None):
    """渲染报告页。

    notice：可选的提示条（网页服务用它说明「这是上次结果／正在后台刷新」）；
            传 None 就没有这一条。报告文件本身不带提示条。
    """
    tr = []
    for r in rows:
        lv = r["status_level"]
        src = r.get("source") or "—"
        if r.get("auto_failed"):
            src += " ⚠️"
        src_cls = "src-mat" if src.startswith("人工") else "src-auto"
        tr.append(
            f"<tr><td class='k'>{htmlmod.escape(r['key'])}</td>"
            f"<td>{htmlmod.escape(r['market'])}</td>"
            f"<td class='mono'>{htmlmod.escape(ver_display(r))}</td>"
            f"<td class='mono'>{htmlmod.escape(r['date'] or '—')}</td>"
            f"<td><span class='pill' style='color:{COLOR[lv]};"
            f"background:{BG[lv]}'>"
            f"{LEVEL_ICON[lv]} {htmlmod.escape(r['status'])}</span></td>"
            f"<td><span class='src {src_cls}'>{htmlmod.escape(src)}</span></td></tr>")
    counts = {lv: sum(1 for r in rows if r["status_level"] == lv)
              for lv, _ in LEVEL_LABEL}
    cards = "".join(
        f"<div class='card'><div class='n' style='color:{COLOR[lv]}'>{counts[lv]}</div>"
        f"<div class='t'>{name}</div></div>"
        for lv, name in LEVEL_LABEL if counts[lv] or lv in ("pass", "wait"))
    manual = [r for r in rows if r.get("source") == "人工"]
    mh = ""
    if manual:
        mh = ("<div class='muted'>数据来源：<b>自动</b> ＝本次从该市场公开详情页抓取；"
              "<b>人工</b> ＝取自 config.json 的人工登记表（该渠道没有可自动抓取的公开页 /"
              "本次未上传包提审 / 本次抓取失败被兜底）。本次走人工登记的渠道："
              + "、".join(f"<b>{htmlmod.escape(r['key'])}</b>（{htmlmod.escape(r['status'])}）"
                          for r in manual) + "。</div>")
    tc = cfg.get("target_codes") or {}
    cdn = [r for r in rows if r.get("fetch_tag") == "CDN 缓存"]
    ch = ""
    if cdn:
        ch = ("<div class='muted' style='color:#b26a00'>⚠ <b>数据时效提示</b>：以下渠道本次"
              "未能直取源站（防缓存参数未生效），取到的是 CDN 缓存页，数据可能滞后于源站："
              + "、".join(f"<b>{htmlmod.escape(r['key'])}</b>" for r in cdn) + "。</div>")
    gone = [r for r in rows if r["status_level"] == "gone"]
    gh = ""
    if gone:
        gh = ("<div class='muted' style='color:#7b1f2e'>⛔ <b>本市场已无此应用"
              f"（{len(gone)} 家）</b>：数据源<b>明确答复查无此应用</b>，"
              "属可信的否定结论（不是抓取故障，也没有拿人工登记表兜底）："
              + "".join(f"<br>· <b>{htmlmod.escape(r['key'])}</b>"
                        f"（{htmlmod.escape(r['market'])}）："
                        f"{htmlmod.escape(r['judge_note'])}" for r in gone)
              + "</div>")
    failed = [r for r in rows if r.get("auto_failed")]
    fh = ""
    if failed:
        fh = ("<div class='muted' style='color:#c0392b'>⚠️ <b>本次有 "
              f"{len(failed)} 家自动抓取失败，这几家本期的数据不可信</b>"
              "（表里数据来源标 ⚠️ 的那几行，结果取自人工登记表，"
              "<b>不是</b>脚本核验过的公开数据）："
              + "".join(f"<br>· <b>{htmlmod.escape(r['key'])}</b>"
                        f"（{htmlmod.escape(r['market'])}）："
                        f"{htmlmod.escape(r['note'])}" for r in failed)
              + "<br>说明该渠道的解析规则可能已失效（对方改版或换了接口），"
                "需要人工去市场里核验这一家。规则要不要修，见 README.md 对应小节。</div>")
    nh = ""
    if notice:
        nh = f"<div id='rb-notice'>{htmlmod.escape(str(notice))}</div>"
    panel = _settings_panel()
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{htmlmod.escape(app_name_of(cfg))} 各市场审核结果</title>
<style>
 *{{box-sizing:border-box}}
 body{{margin:0;padding:32px;background:#f7f8fa;color:#1a1a1a;
   font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
 .wrap{{max-width:1000px;margin:0 auto}}
 h1{{font-size:22px;margin:0 0 6px}}
 h2{{font-size:16px;margin:28px 0 12px;padding-left:10px;border-left:3px solid #2b6cb0}}
 .meta{{color:#666;font-size:13px;line-height:1.9}}
 .meta code{{background:#eef1f5;padding:1px 6px;border-radius:4px;font-size:12px}}
 .cards{{display:flex;gap:12px;margin:20px 0 8px;flex-wrap:wrap}}
 .card{{flex:1 1 140px;background:#fff;border:1px solid #e6e8eb;border-radius:10px;
   padding:14px 16px}}
 .card .n{{font-size:24px;font-weight:700}}
 .card .t{{font-size:12px;color:#666;margin-top:2px}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e6e8eb;
   border-radius:10px;overflow:hidden}}
 th{{background:#f0f2f5;font-size:12px;color:#555;font-weight:600;text-align:left;
   padding:10px 12px;border-bottom:1px solid #e6e8eb}}
 td{{padding:11px 12px;font-size:13px;border-bottom:1px solid #f0f0f0;vertical-align:top}}
 tr:last-child td{{border-bottom:none}}
 td.k{{font-weight:600}}
 .mono{{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}}
 .note{{color:#666;font-size:12px;line-height:1.55}}
 .pill{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;
   font-weight:600;white-space:nowrap}}
 .src{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11.5px;
   font-weight:600}}
 .src-auto{{color:#2b6cb0;background:#eaf2fb}}
 .src-mat{{color:#4a5568;background:#eef1f5}}
 .muted{{color:#888;font-size:12px;line-height:1.8;margin-top:22px}}
 .foot{{margin-top:10px;color:#8a8a8a;font-size:12px;line-height:1.8}}
</style></head><body><div class="wrap">
<h1>{htmlmod.escape(app_name_of(cfg))} · 各应用市场审核结果</h1>
<div class="meta">
  巡检时间：{now}<br>
  包名 <code>{cfg['android_pkg']}</code> ｜ iOS trackId <code>{cfg['ios_track_id']}</code><br>
  发版基线：日期 <code>{cfg.get('release_date')}</code>
  ／ 版本 <code>{cfg.get('target_version') or '未配置'}</code>
  ／ 构建号 安卓 <code>{tc.get('安卓') or '未配置'}</code>
  · 苹果 <code>{tc.get('苹果') or '未配置'}</code>
  · 鸿蒙 <code>{tc.get('鸿蒙') or '未配置'}</code>
</div>
{nh}
<div class="cards">{cards}</div>
<table><thead><tr><th>平台</th><th>市场</th><th>公开版本</th><th>更新时间</th>
<th>状态</th><th>数据来源</th></tr></thead><tbody>
{''.join(tr)}
</tbody></table>
{gh}
{fh}
{mh}
{ch}
<div class="muted">
  判定口径：公开更新日期 ≥ 发版日期，或公开版本（构建号）＝发版登记值，即视为
  <b>已上线</b>；其余视为 <b>审核中</b>；登记为 <b>未发版</b> 的渠道＝本次未上传包提审；
  <b>数据源明确答复查无此应用</b> 的渠道＝<b>已下架</b>（该市场里已经没有它了）。
</div>
<div class="foot">
  自动数据取自各应用市场公开详情页，不等同于开发者后台的官方审核状态；
  人工数据取自 config.json 的人工登记表。
</div>
</div>
{panel}
</body></html>"""


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 群机器人通知出口
# --------------------------------------------------------------------------
# 巡检结束后，把结果推到群里 —— 从「要人主动去看页面」变成「结果自己找人」。
#
# 三家群机器人的协议**各不相同**（字段名、成功判据都不一样），所以拆成：
#   · NOTIFY_TYPES  —— 支持哪几种
#   · _notify_payload() —— 每种怎么拼请求体
#   · _post_webhook()   —— 怎么发 + 怎么判断"真的发成功了"
# 要再加一种（Slack、Teams、自己的 HTTP 接口…）就是在这两处加一个分支。
#
# ⚠️ 两条硬规矩：
#   1) 通知**只发不读**，是单向的；
#   2) 推送失败**绝不**影响巡检本身 —— 报告已经落盘了，
#      推送只是"顺手告诉群里一声"，发不出去最多提示一句。

NOTIFY_TYPES = {
    "wecom":    "企业微信群机器人",
    "feishu":   "飞书群机器人",
    "dingtalk": "钉钉群机器人",
}

# 各群机器人 webhook 的域名特征。**只用来在保存时给个提醒**，不做硬校验 ——
# 企业微信/飞书都可能有企业自建中转地址，拦死了反而挡住正当用法。
NOTIFY_HOSTS = {
    "wecom":    ("qyapi.weixin.qq.com",),
    "feishu":   ("open.feishu.cn", "open.larksuite.com"),
    "dingtalk": ("oapi.dingtalk.com",),
}

NOTIFY_MODES = ("always", "change")


def notify_summary(rows):
    """把报告行压成一句「已上线 9 ｜ 审核中 3 ｜ …」（没有的档位不出现）"""
    counts = {lv: sum(1 for r in rows if r["status_level"] == lv)
              for lv, _ in LEVEL_LABEL}
    parts = [f"{name} {counts[lv]}" for lv, name in LEVEL_LABEL if counts[lv]]
    return " ｜ ".join(parts) if parts else "（没有可显示的渠道）"


def build_notify_text(rows, cfg, now, changed=None):
    """拼给群机器人看的正文（markdown）。

    刻意**不用** markdown 表格：企业微信和飞书的群机器人 markdown 都不支持
    表格语法，发出去会变成一串竖线。用「粗体标题 + 每行一家」最稳，三家通吃。

    正文四段，**顺序别随意挪**：
      ① 标题 + 巡检时间 + 汇总计数
      ② 已上线清单 —— 只用短名并成一行。它们没有待办含义，逐条铺开会把群消息
         撑得很长；但完全不列又只能看到一个数字、「到底哪几家上了」无从核对。
      ③ 需要关注 —— 逐条列并带判据（这才是要人动手的部分）
      ④ 相比上次的变化（可选）
    """
    L = [f"**{app_name_of(cfg)} · 各市场审核巡检**",
         f"巡检时间：{now}",
         "",
         notify_summary(rows)]

    done = [r for r in rows if r["status_level"] == "pass"]
    todo = [r for r in rows if r["status_level"] != "pass"]

    if done:
        L.append("")
        L.append(f"**已上线（{len(done)} 家）**")
        L.append("✅ " + " ｜ ".join(f"**{r['key']}**" for r in done))

    L.append("")
    if not todo:
        L.append("✅ 所有渠道均已上线。")
    else:
        L.append(f"**需要关注（{len(todo)} 家）**")
        for r in todo:
            icon = LEVEL_ICON.get(r["status_level"], "•")
            why = r.get("judge_note") or r.get("note") or ""
            L.append(f"{icon} **{r['key']}**（{r['market']}）：{r['status']}"
                     + (f" —— {why}" if why else ""))
    if changed:
        L.append("")
        L.append("**相比上次的变化**")
        L.extend(changed)
    return "\n".join(L)


def notify_changes(rows, prev_snapshot):
    """跟上次快照比，哪些渠道的状态变了（新出现的渠道也算）。返回 [文本行]。

    只用于**通知正文**里那一小段「相比上次的变化」。
    报告表格里没有这一块（那是刻意去掉的），两者互不影响。
    """
    if not prev_snapshot:
        return []
    out = []
    for r in rows:
        old = prev_snapshot.get(r["key"])
        if not isinstance(old, dict):
            out.append(f"🆕 **{r['key']}**：本次新加入巡检（{r['status']}）")
            continue
        old_lv = old.get("status_level")
        old_txt = old.get("status")
        # 早期快照两者都没存（本函数上线前的历史文件）⇒ 没有可比的基准。
        # 这种情况**什么都不说**：宁可漏报一次，也不要每次巡检都凭空报一条
        # 「（未知）→ 无法自动获取」，那会把 change 模式变成 always。
        if not old_lv and not old_txt:
            continue
        same = (old_lv == r["status_level"]) if old_lv else (old_txt == r["status"])
        if not same:
            out.append(f"🔁 **{r['key']}**：{old_txt or '（未知）'}"
                       f" → {r['status']}")
    return out


def _notify_payload(kind, title, text):
    """按群机器人类型拼请求体（三种协议字段名都不一样）"""
    if kind == "feishu":
        # 飞书：用交互式卡片，正文走 lark_md（支持 **粗体**、换行）
        return {"msg_type": "interactive",
                "card": {"config": {"wide_screen_mode": True},
                         "header": {"template": "blue",
                                    "title": {"tag": "plain_text", "content": title}},
                         "elements": [{"tag": "div",
                                       "text": {"tag": "lark_md", "content": text}}]}}
    if kind == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    # 企业微信群机器人（也是默认）
    return {"msgtype": "markdown", "markdown": {"content": text}}


def _post_webhook(url, payload, timeout=10):
    """POST 一个 JSON 到群机器人 webhook。返回 (是否成功, 说明)。

    ⚠️ 三家都是「HTTP 200 但 body 里带错误码」的套路（比如 webhook 失效、
       被限流、内容超长），**只看 HTTP 状态码会把失败当成功**。所以必须再解析
       一次 body 里的 errcode / code / StatusCode。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "User-Agent": PC_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}（webhook 地址可能不对，或机器人被停用了）"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"

    try:
        d = json.loads(body)
    except ValueError:
        return True, body[:80]
    if not isinstance(d, dict):
        return True, str(d)[:80]
    code = d.get("errcode", d.get("code", d.get("StatusCode")))
    if code in (0, "0", None):
        return True, "ok"
    msg = d.get("errmsg") or d.get("msg") or d.get("StatusMessage") or ""
    return False, f"{code} {msg}".strip()


def send_notifications(rows, cfg, now, prev_snapshot=None, testing=False):
    """按 config.notify 把巡检结果推到群里。返回 [(名称, 是否成功, 说明), ...]

    **绝不抛异常** —— 通知只是顺手告诉群里一声，不该把一次成功的巡检搞成失败。

    testing=True：发**测试消息**（设置页那个「发一条测试消息」按钮走的就是这条）。
    它和正式推送共用同一段发送逻辑，只是标题与正文换成测试专用文案 ——
    ⚠️ 标题必须一眼看出是测试：否则它和正式报告同名（都叫「XX · 各市场审核巡检」），
    群成员收到 4 条消息时分不清哪条要当真。
    """
    nt = cfg.get("notify") or {}
    if not isinstance(nt, dict) or not nt.get("on"):
        return []

    providers = [p for p in (nt.get("providers") or [])
                 if isinstance(p, dict) and str(p.get("webhook") or "").strip()]
    if not providers:
        return [("（未配置 webhook）", False,
                 "通知开关是开着的，但一条 webhook 都没填")]

    if testing:
        # 测试消息：标题带【测试】，正文也明说是测试 —— 让它和正式报告一眼可分。
        title = f"【测试】{app_name_of(cfg)} · 通知测试"
        text = ("这是一条**测试消息**，用来确认群机器人的地址填对了没有。\n"
                "看到它就说明通知链路是通的，可以把设置保存下来了。\n\n"
                f"正式报告的标题是「{app_name_of(cfg)} · 各市场审核巡检」，"
                "那条里才有各市场的真实结果；这条可以直接忽略。")
    else:
        changed = notify_changes(rows, prev_snapshot)
        mode = str(nt.get("mode") or "always")
        if mode == "change" and not changed:
            return [("（本次未推送）", True,
                     "结果与上次相比没有变化（mode=change，只推变化）")]
        title = f"{app_name_of(cfg)} · 各市场审核巡检"
        text = build_notify_text(rows, cfg, now, changed)

    out = []
    for p in providers:
        kind = str(p.get("type") or "wecom")
        label = str(p.get("name") or "").strip() or NOTIFY_TYPES.get(kind, kind)
        ok, msg = _post_webhook(str(p["webhook"]).strip(),
                                _notify_payload(kind, title, text))
        out.append((label, ok, msg))
    return out


def notify_report(notify_out):
    """把通知结果压成一行，给控制台 / 报告页 / 日志用；没配就返回 ""（空）"""
    if not notify_out:
        return ""
    ok = [n for n in notify_out if n[1]]
    bad = [n for n in notify_out if not n[1]]
    bits = []
    if ok:
        bits.append("已推送：" + "、".join(n[0] for n in ok))
    if bad:
        bits.append("推送失败：" + "；".join(f"{n[0]}（{n[2]}）" for n in bad))
    return " ｜ ".join(bits)


def _prev_snapshot():
    """读上一次巡检的快照（history/latest.json 里的 markets）。没有就返回 {}。

    ⚠️ 必须在**覆盖** latest.json **之前**调用，否则读到的是刚写进去的自己。
    """
    p = os.path.join(HERE, "history", "latest.json")
    try:
        with open(p, encoding="utf-8") as f:
            return (json.load(f) or {}).get("markets") or {}
    except Exception:  # noqa: BLE001
        return {}


def run_scan(cfg, show_raw=False, progress=None, push=True):
    """跑一次完整巡检：抓取 → 判定 → 落盘（history 快照 + md/html 报告）。

    抽成函数是为了让网页服务能反复调用它（命令行模式也是调它）。
    progress：可选回调，每换一个渠道就调一次，网页服务用它显示「正在巡检 小米（6/15）…」。
    push：巡检完要不要按 config.notify 推群机器人。命令行（自己主动跑）默认推；
          网页服务**启动时**那次自动巡检传 push=False —— 用户只是打开页面看一眼，
          不该因此往群里发消息（否则「每天双击一次 exe」＝每天群里多一条）。
    返回 dict(rows=报告用行, all_rows=含搜狗的全部行, now=巡检时间,
              today=日期, md_path=..., html_path=...)。
    """
    def emit(line):
        print(line)

    def notify(text):
        """只给网页服务看的进度（不打印，免得控制台被进度刷屏）"""
        if progress:
            progress(text)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")

    hist_dir = os.path.join(HERE, "history")
    os.makedirs(hist_dir, exist_ok=True)
    latest = os.path.join(hist_dir, "latest.json")

    rows = []
    snapshot = {}
    emit(f"\n{app_name_of(cfg)} 各市场审核巡检  {now}")
    emit("=" * 104)
    emit(f"{'平台':<10}{'公开版本':<24}{'更新时间':<13}{'状态':<12}{'来源':<6}说明")
    emit("-" * 104)

    live_ch = channels_from_cfg(cfg)
    total = len(live_ch)
    for idx, ch in enumerate(live_ch, 1):
        key, market, fn = ch["key"], ch["name"], ch["fn"]
        row = {"key": key, "market": market, "ok": True, "report": ch["report"],
               "version": None, "date": None, "code": None, "url": None,
               "code_kind": "versionCode", "source": "自动", "fetch_tag": None,
               "note": "", "status": "", "status_level": "unknown", "judge_note": "",
               "auto_failed": False, "delisted": False}

        # 人工渠道（config 里这条渠道没配 fetcher）：没有可自动抓取的公开数据源，
        # 不抓、不发请求，结果完全取自人工登记表。报告里标「人工」，
        # 也**不会**被算进「抓取失败」——它本来就没有可失败的东西。
        if fn is None:
            row["ok"] = False
            row["note"] = ch["note"] or "该渠道没有可自动抓取的公开数据源，只能人工核验"
            status, level, why = judge(row, cfg)
            row["status"], row["status_level"], row["judge_note"] = status, level, why
            rows.append(row)
            # ⚠ status / status_level 必须写进来：通知里的「相比上次的变化」
            #   靠它跟上次比。漏了的话这两家每次都会被当成「有变化」，
            #   change 模式就形同虚设（每天都推一条「（未知）→ 无法自动获取」）。
            snapshot[key] = {"version": None, "date": None, "code": None, "ok": False,
                             "source": row["source"], "fetch_tag": None,
                             "note": row["note"], "auto_failed": False,
                             "delisted": False,
                             "status": row["status"],
                             "status_level": row["status_level"]}
            if ch["report"]:
                notify(f"跳过 {key}（人工渠道，{idx}/{total}）")
                emit(f"{key:<10}{'—':<12}{'—':<14}"
                     f"{LEVEL_ICON[level]} {status:<8}{row['source']:<6}{why}")
            continue

        # 人工登记为「未发版」：本次没上传包提审，公开页上挂的是旧版本，不必抓
        m = manual_lookup(key, cfg)
        if m and m[0] == NO_SUBMIT:
            row["ok"] = False
            row["note"] = f"人工登记：{m[2]}"
            status, level, why = judge(row, cfg)
            row["status"], row["status_level"], row["judge_note"] = status, level, why
            rows.append(row)
            snapshot[key] = {"version": None, "date": None, "code": None, "ok": False,
                             "source": row["source"], "fetch_tag": None,
                             "note": row["note"], "auto_failed": False,
                             "delisted": False,
                             "status": row["status"],
                             "status_level": row["status_level"]}
            if ch["report"]:
                notify(f"跳过 {key}（人工登记为未发版，{idx}/{total}）")
                emit(f"{key:<10}{'—':<12}{'—':<14}"
                     f"{LEVEL_ICON[level]} {status:<8}{row['source']:<6}{why}")
            continue

        # 抓取器需要知道自己跑的是哪个渠道（渠道不同，appId 也不同）
        cfg_view = {**cfg, "_key": key}
        notify(f"正在巡检 {key}（{idx}/{total}）…")
        # 每个渠道最多试 3 次：网络抖动不应被误报成「抓不到」
        info, err = None, None
        for attempt in range(3):
            try:
                info = fn(cfg_view)
                break
            except DelistedError as e:
                err = e
                break          # 服务端已给出明确否定答复，重试没有意义
            except Exception as e:  # noqa: BLE001
                err = e
                if attempt < 2:
                    time.sleep(1.5)
        if info is not None:
            row.update({k: info.get(k) for k in ("version", "date", "code", "url")})
            row["code_kind"] = info.get("code_kind", "versionCode")
            row["note"] = info.get("note") or ""
            row["fetch_tag"] = info.get("fetch_tag")
            if show_raw:
                emit(f"    [raw] {key}: {info}")
            if row["fetch_tag"] == "CDN 缓存":
                emit(f"    ⚠ {key}：防缓存参数未生效，本次走的是 CDN 缓存，"
                     f"数据可能滞后于源站")
        else:
            row["ok"] = False
            row["note"] = str(err)
            if isinstance(err, DelistedError):
                # 可信的否定结论：应用已不在该市场 → 判「已下架」，不算抓取失败
                row["delisted"] = True
                last = history_last_ok(key)
                if last and last[0]:
                    row["note"] += f"（历史快照里最近一次正常抓到的是 {last[0]}"
                    row["note"] += f" / {last[1]}" if last[1] else ""
                    row["note"] += f"，巡检时间 {last[2]}）"
            # 走到这里的都是**本该自动抓**的渠道（人工渠道在上面的 fn is None
            # 分支里已经处理掉了）。超时 / 对方改版 / 解析不到字段 ⇒ 报告里必须
            # 点名，不能让它悄悄退化成一行看不出区别的「人工登记」。
            else:
                row["auto_failed"] = True

        status, level, why = judge(row, cfg)
        row["status"], row["status_level"], row["judge_note"] = status, level, why
        rows.append(row)
        snapshot[key] = {"version": row["version"], "date": row["date"],
                         "code": row["code"], "ok": row["ok"],
                         "source": row["source"], "fetch_tag": row["fetch_tag"],
                         "note": row.get("note") or "",
                         "auto_failed": bool(row.get("auto_failed")),
                         "delisted": bool(row.get("delisted")),
                         # 状态与级别：群机器人通知里的「相比上次的变化」要用
                         "status": row["status"],
                         "status_level": row["status_level"]}
        if ch["report"]:
            emit(f"{key:<10}{ver_display(row):<24}"
                 f"{str(row['date'] or '—'):<13}"
                 f"{LEVEL_ICON[level]} {status:<8}{row['source']:<6}{why}")

    main_rows = [r for r in rows if r["report"]]

    # 上一次的快照：通知里的「相比上次的变化」要用，必须在覆盖 latest.json 之前读
    prev = _prev_snapshot()

    # 落盘（保留历史快照备查，报告本身不再展示变化）
    with open(latest, "w", encoding="utf-8") as f:
        json.dump({"checked_at": now, "app_name": app_name_of(cfg),
                   "config": cfg, "markets": snapshot},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(hist_dir, f"{today}.json"), "w", encoding="utf-8") as f:
        json.dump({"checked_at": now, "markets": snapshot},
                  f, ensure_ascii=False, indent=2)

    rep_dir = os.path.join(HERE, "report")
    os.makedirs(rep_dir, exist_ok=True)
    md_path = os.path.join(rep_dir, f"review_status_{today}.md")
    html_path = os.path.join(rep_dir, f"review_status_{today}.html")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(main_rows, cfg, now))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(main_rows, cfg, now))

    emit("-" * 104)
    counts = {lv: sum(1 for r in main_rows if r["status_level"] == lv)
              for lv, _ in LEVEL_LABEL}
    emit(f"合计 {len(main_rows)} 家：" +
         " ｜ ".join(f"{name} {counts[lv]}" for lv, name in LEVEL_LABEL))
    manual = [r for r in main_rows if r.get("source") == "人工"]
    if manual:
        emit("取自人工登记表：" +
             "、".join(f"{r['key']}（{r['status']}）" for r in manual))
    emit(f"\n报告已生成：\n  {md_path}\n  {html_path}")

    # 群机器人通知：配了才发；**发失败也绝不让巡检失败**（报告已经落盘了）
    if not push:
        # 网页服务启动时的自动巡检走这里：开页面看一眼而已，不该惊动群里。
        notify_out, line = [], ""
    else:
        try:
            notify_out = send_notifications(main_rows, cfg, now, prev)
        except Exception as exc:  # noqa: BLE001
            notify_out = [("（通知模块出错）", False,
                           f"{type(exc).__name__}: {exc}")]
        line = notify_report(notify_out)
        if line:
            emit(line + "\n")

    return {"rows": main_rows, "all_rows": rows, "now": now, "today": today,
            "md_path": md_path, "html_path": html_path,
            "notify": notify_out, "notify_line": line}


def preflight(cfg, workers=5, budget=90):
    """连通性体检：并发试一遍各渠道，只为回答「这台机器能不能抓通各市场」。

    为什么单独做一个模式：部署到内网服务器时，公司服务器常常只放通了部分外网
    出口。等手工跑一次全量巡检（好几分钟）才发现有一半渠道连不上，很浪费时间。
    这个模式把十几家并发跑，几十秒内出结论，且**不生成报告、不写 history**。

    ⚠ 能连通 ≠ 数据是对的。这只验证网络与接口可达；
      字段解析是否正确、版本号有没有抓错，仍然要看正式巡检的结果。
    """
    jobs, skipped, unsupported = [], [], []
    live_ch = channels_from_cfg(cfg)
    no_report = [c["key"] for c in live_ch if not c["report"]]
    # 只试**报告里会出现**的渠道。report=false 的（如搜狗的镜像）不进报告，
    # 算进来会让「共 N 家」和报告的行数对不上号，反而添乱。
    for ch in live_ch:
        key, market, fn = ch["key"], ch["name"], ch["fn"]
        if not ch["report"]:
            continue
        if fn is None:
            unsupported.append(key)      # 人工渠道：结构性没有数据源，体检无从谈起
            continue
        m = manual_lookup(key, cfg)
        if m and m[0] == NO_SUBMIT:
            skipped.append(key)          # 未发版渠道正式巡检里不发请求，此处也不发
            continue
        jobs.append((key, market, fn))

    print(f"\n连通性体检  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)
    extra = []
    if skipped:
        extra.append(f"{len(skipped)} 家登记为「未发版」，不发请求")
    if unsupported:
        extra.append(f"{len(unsupported)} 家没有公开数据源"
                     f"（{'、'.join(unsupported)}）")
    print(f"共 {len(jobs)} 个渠道要试" +
          (f"（另有 {'；'.join(extra)}，此处跳过）" if extra else "") +
          (f"；{'、'.join(no_report)} 不进报告，不参与本次体检" if no_report else ""))
    print(f"并发 {workers} 路，整体限时 {budget} 秒")
    print("-" * 100)
    print(f"{'平台':<14}{'结果':<8}{'公开版本':<18}{'更新时间':<14}说明")
    print("-" * 100)

    results = {}
    sem = threading.Semaphore(workers)

    def probe(i, fn):
        with sem:
            try:
                results[i] = (fn({**cfg, "_key": jobs[i][0]}), None)
            except Exception as exc:  # noqa: BLE001
                results[i] = (None, exc)

    threads = []
    for i, (_key, _market, fn) in enumerate(jobs):
        t = threading.Thread(target=probe, args=(i, fn), daemon=True)
        t.start()
        threads.append(t)

    deadline = time.time() + budget
    for t in threads:
        t.join(max(0.1, deadline - time.time()))

    ok = fail = 0
    for i, (key, _market, _fn) in enumerate(jobs):
        got = results.get(i)
        if got is None:                  # 限时内没回来；线程是 daemon，直接放弃它
            fail += 1
            print(f"{key:<14}{'超时':<8}{'—':<18}{'—':<14}接口没在限时内返回")
            continue
        info, exc = got
        if isinstance(exc, DelistedError):
            # 接口通了，只是该市场里查无此应用。**不能算「不通」**，否则排错会被带偏
            ok += 1
            print(f"{key:<14}{'通':<8}{'—':<18}{'—':<14}"
                  f"接口可达，但查无此应用（正式校验会判「已下架」）")
            continue
        if exc is not None:
            fail += 1
            print(f"{key:<14}{'不通':<8}{'—':<18}{'—':<14}"
                  f"{type(exc).__name__}: {str(exc)[:52]}")
            continue
        ok += 1
        v, c = info.get("version"), info.get("code")
        vs = f"{v} ({c})" if (v and c) else str(v or "—")
        print(f"{key:<14}{'通':<8}{vs:<18}{str(info.get('date') or '—'):<14}"
              f"{info.get('fetch_tag') or '直取'}")

    print("-" * 100)
    if skipped:
        print(f"跳过（未发版，不发请求）：{'、'.join(skipped)}")
    if unsupported:
        print(f"不计入（无公开数据源，只能人工核验）：{'、'.join(unsupported)}")
    print(f"结论：{ok} 家连通，{fail} 家不通。")

    if fail:
        print("\n连不上的排查顺序（按可能性从高到低）：")
        print("  1. 这台机器没有外网出口，或只放通了部分域名 → 找网管把这些域名加白名单")
        print("  2. 内网 DNS 不解析公网域名 → 试试 nslookup mobile.baidu.com")
        print("  3. 出网必须走代理 → 本脚本没有代理支持，需要时才去改 http_get")
        print("  ⚠ 连不上的渠道在正式巡检里会退回 config.json 的人工登记表：报告照样出得来，")
        print("    但「数据来源」会标成「人工」，新鲜度得自己维护。")
    else:
        print("全部连通，可以放心把服务放到这台机器上。")
    print()
    return {"ok": ok, "fail": fail, "skipped": skipped,
            "unsupported": unsupported}


def main():
    """命令行模式：跑一次巡检、出报告，然后退出（老用法，未改动）"""
    res = run_scan(load_config(), show_raw="--show-raw" in sys.argv)
    print("（不加 --cli 时启动的是网页界面：可以看结果、点『设置』改配置）\n")
    return res


def _arg_value(name, default):
    """取 `--xxx 值` 形式的命令行参数"""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _dispatch():
    """决定这次是「启动网页服务」还是「跑一次命令行巡检」。

    返回值会作为进程退出码（0=正常）。服务化部署时这点很重要：
    启动失败（比如端口被占）必须以非 0 退出，systemd / 任务计划程序 / NSSM
    才知道「这次没起来」，从而按策略重试，而不是当成正常结束。
    """
    if "--preflight" in sys.argv:
        preflight(load_config())
        return 0
    # 默认启动网页服务；显式加 --cli（或 --show-raw）才是老的单次巡检
    if "--cli" in sys.argv or "--show-raw" in sys.argv:
        main()
        return 0

    # --lan 是「开放给内网」的简写，等价于 --host 0.0.0.0
    lan = "--lan" in sys.argv
    host = _arg_value("--host", webui.LAN_HOST if lan else webui.HOST_DEFAULT)
    port = _arg_value("--port", str(webui.PORT_DEFAULT))
    try:
        port = int(port)
    except ValueError:
        port = webui.PORT_DEFAULT

    # 开放到内网时，默认**不**开浏览器：
    # 服务化运行（systemd / 任务计划程序 / NSSM）下没有桌面会话，硬开可能报错或卡住；
    # 真有人在本机手动跑、又想顺手开一下，显式加 --browser 即可。
    if "--browser" in sys.argv:
        open_browser = True
    elif "--no-browser" in sys.argv:
        open_browser = False
    else:
        open_browser = host in ("127.0.0.1", "localhost", "::1")

    return webui.serve(
        sys.modules[__name__],
        host=host,
        port=port,
        open_browser=open_browser,
        auto_scan="--no-scan" not in sys.argv,
        show_raw="--show-raw" in sys.argv,
        strict_port="--strict-port" in sys.argv,
    )


def _pause_before_exit():
    """双击 exe 运行时，跑完停一下，别让窗口一闪而过、看不见结果。

    只在「打包后的 exe + 交互式控制台」下暂停：
    - 被其他程序调用、或加了 --no-pause 时，直接退出，不影响自动化；
    - 输出被重定向到文件时（stdin 不是终端），也不暂停。
    """
    if not getattr(sys, "frozen", False) or "--no-pause" in sys.argv:
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车键关闭窗口 ...")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    code = 0
    try:
        code = _dispatch() or 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已中断。")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"\n程序运行出错：{type(exc).__name__}: {exc}")
        code = 1
    finally:
        _pause_before_exit()
    sys.exit(code)
