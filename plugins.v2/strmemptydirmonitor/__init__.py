"""strm 媒体库巡检插件：115 分享有效性检测 + 空目录巡检。

两件事，按「先检测 115 链接，再检查空文件夹」的顺序串成**组合巡检**：

1. **115 分享检测**（只读）—— 遍历库里所有 ``.strm``，解析出里面的 115 分享，
   按 ``(share_code, receive_code)`` 去重后并发查 115 接口，判定每个分享是
   **有效 / 已失效（取消·过期·违规·不存在·访问码错误） / 未知（风控或网络异常）**；
   并顺带算出「只剩失效 strm 的目录」——这批目录一旦清掉失效文件就会变成空目录。
2. **空目录巡检** —— 扫描「整棵子树里不含任何 .strm 文件」的目录（下称"死树"）。

**清理动作一律只放在详情页的手动按钮上，不做任何自动删除。**

设计前提（很重要，别搞错）：
    本插件服务的 ``/volume1/movie_strm`` **不是刮削库** —— 目录由用户手工整理、
    挂的是 115 分享链接，``.strm`` 由 NanShare 容器生成（内容是该分享的直链，
    形如 ``http://172.17.0.1:8115/api/?share_code=<分享码>&id=<文件id>&receive_code=<提取码>``）。
    所以空目录的成因在「115 分享链路」上（分享被取消/过期、分享里没有正片、
    转存或再分享失败），**不是刮削规则问题**。

115 检测的几个实测要点（都写进了代码，别凭感觉改）：
    * 检测接口 ``https://webapi.115.com/share/snap`` **匿名即可访问**，与带登录
      Cookie 返回完全一致，所以插件零配置；代价是风控概率略高。
    * 判定看 ``state`` + ``errno``：``state=true`` 且 ``share_state=1`` 才算有效；
      ``state=false`` 时 ``errno`` 给的就是原因（990002 参数错误 / 4100008 访问码错误 /
      4100012 请输入访问码 …）。
    * 🔴 ``state=false`` 且 ``errno=0`` 的是**服务端内部错误**（实测 115 用它表达
      "服务器开小差了"），**绝不能当成分享失效** —— 一律记为「未知」。
    * 必须**绕开系统代理**直连 115，走代理会被拒或超时。

🔴 定时任务注册的坑（1.0.0 翻过，1.1.1 修）：
    MoviePilot 的 scheduler 对 ``trigger="cron"`` 是 ``CronTrigger(**kwargs)``，
    kwargs 必须是 APScheduler 的字段名。传 ``{"cron": "0 30 3 * * *"}`` 会让
    ``get_service()`` 抛 ``unexpected keyword argument 'cron'``、**定时任务静默不注册**
    （页面正常、只有日志一条 ERROR）。正确做法见 :func:`_cron_kwargs`。

🔴 「清理失效点了像没反应」的坑（1.1.2 修，2026-10-01 用户实测反馈）：
    详情页上的「失效文件 N 个 / 失效分享清单 / 只剩失效 strm 的目录」**全部来自上一次
    检测的快照**，清理原本既不更新它们、又只搬每个分享的前 10 条样本
    （``SAMPLE_PER_SHARE``）—— 于是 14 个文件只搬走 10 个、页面数字纹丝不动，
    整页唯一的变化是页面**最底下**的「最近清理记录」多一行，用户自然认为点了没反应。
    现在的做法：清理时**按需重扫**目标分享、把**全部** strm 都搬走
    （``_collect_shares(capture_paths=...)``），结束后**当场扣减并回存检测快照**
    （``__prune_after_clean``）、页面上补一条「已于 xx 清理 N 个」的绿色回执；
    并且无论成功 / 空转 / 失败都会写一条清理记录 —— **点击必有回执**。

    ⚠️ 顺带记住：MP 前端的页面事件处理器（``PluginDataDialog`` 里的 ``PageRender``）
    是 ``try { axios... } catch(e) {}`` —— **把错误全部吞掉**，接口报错时页面上
    一点提示都没有。所以插件侧任何“点了没反应”的排查，第一步都应该是
    **去插件日志看那一步到底有没有执行**，而不是盯着前端。

清理的三道保险（都在代码里）：
    1. 删前对每个目标**重新实时复查**，只要子树里出现任何一个 ``.strm`` 就跳过；
    2. **零文件的子树只用 ``os.rmdir``**（非空必然失败）⇒ 物理上不可能删掉文件；
    3. 含文件的子树**不删除，整体移动到 ``<扫描路径>/@recycle/<时间戳>/``**
       （库内回收站，且 ``@*`` 默认被排除、不会被再次扫成死树）。
"""

import fnmatch
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode
from urllib.request import ProxyHandler, Request, build_opener

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# 插件 ID：拼事件回调用（`events.click.api` 走的是 `plugin/<ID>/<method>`）
_PLUGIN_ID = "StrmEmptyDirMonitor"

# 持久化键名
LAST_RESULT_KEY = "last_result"
CHECK_RESULT_KEY = "last_check115"
CLEAN_LOG_KEY = "clean_log"
# 清理日志最多保留条数
CLEAN_LOG_LIMIT = 20

# 默认配置
DEFAULT_SCAN_PATH = "/movie_strm"
# 默认排除规则：群晖的 @eaDir/@tmp/@unlink 与本插件的 @recycle 都以此覆盖
DEFAULT_EXCLUDE = "@*"
DEFAULT_CRON = "0 30 3 * * *"
# 清理时把含文件的死树挪进库内回收站的目录名
TRASH_DIR_NAME = "@recycle"

# 详情页最多列出的死树条数（再多就只在通知/日志里给总数）
MAX_PAGE_ROWS = 100
# 落库的死树明细条数上限（防止极端情况下把插件数据撑爆）
MAX_STORED_ROOTS = 500

# 详情页右下角那颗 56px 悬浮齿轮会扫过整页右侧，右端内容要让出通道
_FAB_CHANNEL = 56

# --------------------------------------------------------------- 115 检测常量

# 115 分享信息接口（匿名可访问）
CHECK_URL = "https://webapi.115.com/share/snap"
CHECK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 读 .strm 时只取前 N 字节：内容就是一行 URL，远小于这个值
STRM_READ_BYTES = 512
# 每个分享在「检测快照」里保留的样本路径条数（落库体积的闸门，只用于展示）
SAMPLE_PER_SHARE = 10
# 🔴 清理时按需重扫、每个分享最多抓多少条**完整**路径。
#    检测阶段不抓全量（22.9 万条太占内存），只有点「清理失效」时才对目标分享抓。
MAX_PATHS_PER_SHARE = 5000
# 落库的失效/未知分享条数上限
MAX_STORED_SHARES = 300
# 单个目录最多记几个分享键（防极端情况下 dir_keys 映射膨胀）
MAX_KEYS_PER_DIR = 8
# 疑似风控的响应特征：命中即判「未知」，**绝不当成失效**
RISK_HINTS = ("频繁", "限流", "稍后再试", "开小差", "服务器繁忙", "请稍后", "too many")
# 默认并发 / 超时
DEFAULT_CONCURRENCY = 3
DEFAULT_TIMEOUT = 15
# 对「未知」结果的额外重试次数（风控是瞬时的，重试一次能捞回不少）
DEFAULT_RETRY = 1
# 非 .strm 的注册扩展名（只用于展示，不影响判定）
_STRM_EXT = ".strm"


# --------------------------------------------------------------------- 工具


def _now_str() -> str:
    """当前时间字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_size(num_bytes: int) -> str:
    """人类可读的字节数。"""
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _parse_patterns(raw: Any) -> List[str]:
    """把配置里的排除规则解析成通配符列表（支持逗号 / 换行 / 空格分隔）。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        text = str(raw).replace("\n", ",").replace(" ", ",")
        items = text.split(",")
    return [p.strip() for p in items if p and p.strip()]


def _is_excluded(name: str, patterns: List[str]) -> bool:
    """目录名是否命中排除规则（支持 ``@*`` 这类通配）。"""
    if not name:
        return False
    for pat in patterns:
        if fnmatch.fnmatchcase(name, pat):
            return True
    return False


def _cron_kwargs(expr: str) -> Dict[str, str]:
    """把 cron 表达式转成 APScheduler ``CronTrigger`` 的关键字参数。

    🔴 **这是踩过的坑，别改回去**：MoviePilot 的 scheduler 对
    ``trigger="cron"`` 走的是 ``CronTrigger(**kwargs)``，所以 kwargs 必须是
    APScheduler 自己的字段名（``second`` / ``minute`` / ``hour`` / ``day`` /
    ``month`` / ``day_of_week``）。如果图省事传 ``{"cron": "0 30 3 * * *"}``，
    注册时会直接抛 ``CronTrigger.__init__() got an unexpected keyword
    argument 'cron'`` —— **定时任务静默注册不上**，插件看起来一切正常，
    只有日志里一条 ERROR。1.0.0 就是这么翻的。

    5 位（分 时 日 月 周）与 6 位（秒 分 时 日 月 周）都支持。

    :param expr: cron 表达式
    :return: 可直接 ``CronTrigger(**kwargs)`` 的字典
    :raises ValueError: 位数不是 5 或 6
    """
    fields = str(expr or "").split()
    if len(fields) == 5:
        minute, hour, day, month, dow = fields
        return {"minute": minute, "hour": hour, "day": day,
                "month": month, "day_of_week": dow}
    if len(fields) == 6:
        second, minute, hour, day, month, dow = fields
        return {"second": second, "minute": minute, "hour": hour,
                "day": day, "month": month, "day_of_week": dow}
    raise ValueError(f"cron 表达式需要 5 位或 6 位，当前 {len(fields)} 位：{expr!r}")


# ----------------------------------------------------------------- 扫描核心


def _scan_dead_trees(root: str, patterns: List[str]) -> Dict[str, Any]:
    """扫描 ``root``，找出整棵子树不含 ``.strm`` 的目录（死树）。

    纯函数、只读、可离线单测。返回结构见文件末尾的返回值说明。

    :param root: 扫描根目录
    :param patterns: 排除的目录名通配列表（命中即整棵子树跳过、不统计也不判定）
    :return: 扫描结果字典
    """
    result: Dict[str, Any] = {
        "root": root,
        "exists": os.path.isdir(root),
        "total_dirs": 0,
        "total_files": 0,
        "strm_files": 0,
        "excluded_dirs": 0,
        "errors": [],
        "dead_all": [],
        "dead_roots": [],
        "detail": {},
    }
    if not result["exists"]:
        return result

    def _on_error(err: OSError) -> None:
        result["errors"].append(f"{type(err).__name__}: {err}")

    nodes: List[Dict[str, Any]] = []
    root = os.path.abspath(root)

    # 第一遍：topdown=True 只用来「剪枝 + 收集直属文件数」。
    # 剪枝必须在 topdown 模式下做，否则会白走一整棵被排除的子树。
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_on_error):
        kept: List[str] = []
        for sub in dirnames:
            if _is_excluded(sub, patterns):
                result["excluded_dirs"] += 1
            else:
                kept.append(sub)
        dirnames[:] = kept

        strm_here = 0
        exts_here: Dict[str, int] = {}
        for name in filenames:
            result["total_files"] += 1
            if name.lower().endswith(_STRM_EXT):
                strm_here += 1
            elif "." in name:
                ext = name.rsplit(".", 1)[-1].lower()
                exts_here[ext] = exts_here.get(ext, 0) + 1
        result["strm_files"] += strm_here
        result["total_dirs"] += 1
        nodes.append({
            "path": dirpath,
            "strm": strm_here,
            "files": len(filenames),
            "size": _dir_direct_size(dirpath, filenames),
            "exts": exts_here,
            "children": [os.path.join(dirpath, x) for x in kept],
        })

    # 第二遍：自底向上汇总。nodes 是 topdown 顺序（父在子前），
    # 反过来遍历就能保证「算到某个目录时它的所有子目录都已算完」。
    #
    # 🔴 扩展名只给「死树」汇总：一个目录只要自身或子树里有 .strm，它就不是死树、
    #    也就不需要 ext 统计。这样 ext_under 的体积被死树规模bound住，
    #    不会因为全库 3 万目录而爆内存。
    strm_under: Dict[str, int] = {}
    files_under: Dict[str, int] = {}
    size_under: Dict[str, int] = {}
    ext_under: Dict[str, Dict[str, int]] = {}
    for node in reversed(nodes):
        path = node["path"]
        strm = node["strm"]
        files = node["files"]
        size = node["size"]
        exts = dict(node["exts"])
        for child in node["children"]:
            strm += strm_under.get(child, 0)
            files += files_under.get(child, 0)
            size += size_under.get(child, 0)
            if strm_under.get(child, 1) == 0:
                for ext, cnt in ext_under.get(child, {}).items():
                    exts[ext] = exts.get(ext, 0) + cnt
        strm_under[path] = strm
        files_under[path] = files
        size_under[path] = size
        if strm == 0:
            ext_under[path] = exts

    dead_all = [p for p in strm_under if strm_under[p] == 0]
    dead_set = set(dead_all)
    # 死树根 = 自身 0-strm，且父目录**不是** 0-strm。
    # 直接把所有 0-strm 目录都列出来会严重灌水（后代必然也是 0-strm）。
    # 🔴 扫描根自己永远不算死树根 —— 否则「整库没 strm」时会把整库当目标删掉。
    for path in dead_all:
        if path == root:
            continue
        if os.path.dirname(path) in dead_set:
            continue
        result["dead_roots"].append(path)

    result["dead_all"] = sorted(dead_all)
    result["dead_roots"] = sorted(result["dead_roots"])

    for path in result["dead_roots"]:
        try:
            mtime = datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            mtime = ""
        result["detail"][path] = {
            "mtime": mtime,
            "files": files_under.get(path, 0),
            "size": size_under.get(path, 0),
            "subdirs": sum(1 for p in dead_all if p.startswith(path + os.sep)),
            "completely_empty": files_under.get(path, 0) == 0,
            "exts": dict(sorted((ext_under.get(path) or {}).items(),
                                key=lambda kv: -kv[1])[:6]),
            "rel": os.path.relpath(path, root),
        }

    return result


def _dir_direct_size(dirpath: str, filenames: List[str]) -> int:
    """目录内直属文件的总字节数（不递归，递归部分由自底向上汇总补上）。"""
    total = 0
    for name in filenames:
        try:
            total += os.path.getsize(os.path.join(dirpath, name))
        except OSError:
            pass
    return total


# ----------------------------------------------------------------- 清理核心


def _count_strm(tree: str) -> int:
    """实时数一遍 ``tree`` 子树里的 ``.strm`` 数量（删前复查用）。"""
    count = 0
    for dirpath, _dirnames, filenames in os.walk(tree, onerror=lambda _e: None):
        for name in filenames:
            if name.lower().endswith(_STRM_EXT):
                count += 1
                if count > 0:
                    return count
    return count


def _remove_empty_tree(tree: str) -> Tuple[int, int, List[str]]:
    """自底向上用 ``os.rmdir`` 删除全空的子树。

    ``os.rmdir`` 遇到非空目录必然抛 ``OSError`` ⇒ **物理上不可能删掉任何文件**，
    这是本插件最便宜的一道安全网。返回 (删掉的目录数, 失败数, 失败原因)。

    :param tree: 目标目录
    :return: (removed_dirs, failed, messages)
    """
    removed = 0
    failed = 0
    messages: List[str] = []
    for dirpath, _dirnames, _filenames in os.walk(tree, topdown=False):
        try:
            os.rmdir(dirpath)
            removed += 1
        except OSError as err:
            failed += 1
            if len(messages) < 3:
                messages.append(f"{dirpath}: {err}")
    return removed, failed, messages


def _clean_dead_root(scan_root: str, target: str, trash_root: str) -> Dict[str, Any]:
    """清理一个死树根（先复查、再按有无文件分流）。

    :param scan_root: 扫描根（用于边界校验）
    :param target: 要清理的死树根路径
    :param trash_root: 库内回收站目录
    :return: 单条清理结果
    """
    item: Dict[str, Any] = {"path": target, "ok": False, "action": "", "message": ""}

    real_root = os.path.realpath(scan_root)
    real_target = os.path.realpath(target)

    # 边界校验：只能删扫描根底下的东西，且不能是扫描根本身
    if real_target == real_root:
        item["message"] = "拒绝：目标是扫描根目录本身"
        return item
    if not real_target.startswith(real_root.rstrip("/") + os.sep):
        item["message"] = f"拒绝：目标不在扫描路径内（{real_target}）"
        return item
    if not os.path.isdir(real_target):
        item["action"] = "gone"
        item["ok"] = True
        item["message"] = "已不存在，跳过"
        return item

    # 保险 1：删前实时复查 —— 防止「扫描之后、点击之前」目录被填了内容
    strm_now = _count_strm(real_target)
    if strm_now > 0:
        item["message"] = f"跳过：复查发现 {strm_now}+ 个 .strm，目录已被填充"
        return item

    files, size = 0, 0
    for dirpath, _dirnames, filenames in os.walk(real_target, onerror=lambda _e: None):
        for name in filenames:
            files += 1
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass

    if files == 0:
        # 保险 2：零文件子树只用 rmdir
        removed, failed, messages = _remove_empty_tree(real_target)
        item["ok"] = removed > 0 or not os.path.exists(real_target)
        item["action"] = "rmdir"
        if item["ok"]:
            item["message"] = f"已删除 {removed} 个空目录"
            if failed:
                item["message"] += f"（{failed} 个非空已跳过）"
        else:
            item["message"] = "删除失败：" + ("；".join(messages) or "未知原因")
        return item

    # 保险 3：含文件的子树不删，挪进库内回收站
    rel = os.path.relpath(real_target, real_root)
    dest = os.path.join(trash_root, rel)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            dest = f"{dest}.{datetime.now().strftime('%H%M%S')}"
        shutil.move(real_target, dest)
        item["ok"] = True
        item["action"] = "trash"
        item["message"] = f"已移入回收站（{files} 个文件 / {_fmt_size(size)}）"
    except Exception as err:  # noqa: BLE001 - 清理失败只需如实回报
        item["message"] = f"移动到回收站失败：{err}"
    return item


def _move_strm_to_trash(scan_root: str, target: str, trash_root: str) -> Dict[str, Any]:
    """把一个无效的 ``.strm`` 移入库内回收站（**只移动、不删除**）。

    :param scan_root: 扫描根（用于边界校验）
    :param target: 目标 .strm 文件路径
    :param trash_root: 库内回收站目录
    :return: 单条处理结果
    """
    item: Dict[str, Any] = {"path": target, "ok": False, "message": ""}

    real_root = os.path.realpath(scan_root)
    real_target = os.path.realpath(target)

    if real_target == real_root:
        item["message"] = "拒绝：目标是扫描根目录本身"
        return item
    if not real_target.startswith(real_root.rstrip("/") + os.sep):
        item["message"] = "拒绝：目标不在扫描路径内"
        return item
    # 边界校验之后再看类型：只处理 .strm，绝不碰别的文件
    if not real_target.lower().endswith(_STRM_EXT):
        item["message"] = "拒绝：只处理 .strm 文件"
        return item
    if not os.path.isfile(real_target):
        item["ok"] = True
        item["message"] = "已不存在，跳过"
        return item

    rel = os.path.relpath(real_target, real_root)
    dest = os.path.join(trash_root, rel)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            dest = f"{dest}.{datetime.now().strftime('%H%M%S%f')}"
        shutil.move(real_target, dest)
        item["ok"] = True
        item["message"] = "已移入回收站"
    except Exception as err:  # noqa: BLE001
        item["message"] = f"移动失败：{err}"
    return item


# ------------------------------------------------------------- 115 检测核心


def _looks_like_risk(text: str) -> bool:
    """响应文本是否像「115 风控/限流」。"""
    if not text:
        return False
    low = text.lower()
    return any(hint in low for hint in RISK_HINTS)


def _parse_strm_share(text: str) -> Optional[Dict[str, str]]:
    """从 ``.strm`` 内容里解析出 115 分享三要素。

    已知格式（NanShare 生成）::

        http://172.17.0.1:8115/api/?share_code=<分享码>&id=<文件id>&receive_code=<提取码>

    解析刻意写得很宽松：只要 query 里有 ``share_code`` 就认，``id`` /
    ``receive_code`` 缺了也照样返回（后面的检测能区分「缺访问码」）。

    :param text: 文件内容（前若干字节足够）
    :return: {"share_code", "file_id", "receive_code"}；不是 115 分享则 None
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    # 正常就一行；容错取第一条含 share_code 的行
    line = ""
    for candidate in raw.splitlines():
        if "share_code=" in candidate:
            line = candidate.strip()
            break
    if not line:
        return None

    if "?" in line:
        query = line.split("?", 1)[1]
    else:
        # 裸 query 形态：share_code=xxx&id=yyy
        query = line[line.find("share_code="):]

    params = parse_qs(query, keep_blank_values=True)
    code = (params.get("share_code") or [""])[0].strip()
    if not code:
        return None
    file_id = (params.get("id") or params.get("file_id") or [""])[0].strip()
    receive = (params.get("receive_code") or params.get("password") or [""])[0].strip()
    return {"share_code": code, "file_id": file_id, "receive_code": receive}


def _share_key(info: Dict[str, str]) -> str:
    """分享去重键：同分享码 + 同提取码才算同一个。"""
    return f"{info.get('share_code', '')}|{info.get('receive_code', '')}"


def _read_strm_share(path: str) -> Optional[Dict[str, str]]:
    """读一个 ``.strm`` 并解析出分享信息（只读前 :data:`STRM_READ_BYTES` 字节）。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(STRM_READ_BYTES)
    except OSError:
        return None
    return _parse_strm_share(raw.decode("utf-8", "replace"))


def _collect_shares(root: str, patterns: List[str],
                    capture_paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """遍历 strm 库，收集 115 分享并按 ``(share_code, receive_code)`` 去重。

    同时顺手记下每个目录的「直属 strm 数 / 直属非 strm 文件数 / 涉及的分享键」，
    供 :func:`_find_pending_dead` 算「只剩失效 strm 的目录」用。

    纯只读、可离线单测。

    :param root: 扫描根目录
    :param patterns: 排除的目录名通配列表
    :param capture_paths: 需要**抓全量路径**的分享键集合。
        🔴 默认 ``None`` ⇒ 每个分享只留 ``SAMPLE_PER_SHARE`` 条样本（检测阶段用，省内存）；
        点「清理失效」时传入目标分享键，只对这几个分享抓全量 —— 这样 22.9 万条 strm
        也不会在检测阶段常驻内存，而清理又能清干净（不再受 10 条样本上限限制）。
    :return: 采集结果字典
    """
    result: Dict[str, Any] = {
        "root": root,
        "exists": os.path.isdir(root),
        "strm_total": 0,
        "strm_115": 0,
        "strm_other": 0,
        "unreadable": 0,
        "errors": [],
        "shares": {},      # key -> {"share_code","receive_code","count","samples","paths"}
        "dir_strm": {},    # 目录 -> 直属 .strm 数
        "dir_other": {},   # 目录 -> 直属非 .strm 文件数
        "dir_keys": {},    # 目录 -> 直属 .strm 涉及的分享键（去重、封顶）
    }
    if not result["exists"]:
        return result

    capture: Set[str] = set(capture_paths or ())

    def _on_error(err: OSError) -> None:
        if len(result["errors"]) < 50:
            result["errors"].append(f"{type(err).__name__}: {err}")

    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_on_error):
        # 剪枝必须在 topdown 模式下改 dirnames[:]，否则白走整棵被排除子树
        dirnames[:] = [d for d in dirnames if not _is_excluded(d, patterns)]

        n_strm = 0
        n_other = 0
        keys_here: List[str] = []
        for name in filenames:
            lower = name.lower()
            if not lower.endswith(_STRM_EXT):
                n_other += 1
                continue
            n_strm += 1
            result["strm_total"] += 1
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as fh:
                    raw = fh.read(STRM_READ_BYTES)
            except OSError:
                result["unreadable"] += 1
                continue
            info = _parse_strm_share(raw.decode("utf-8", "replace"))
            if not info:
                result["strm_other"] += 1
                continue
            result["strm_115"] += 1
            key = _share_key(info)
            bucket = result["shares"].get(key)
            if bucket is None:
                bucket = {
                    "share_code": info["share_code"],
                    "receive_code": info["receive_code"],
                    "count": 0,
                    "samples": [],
                    "paths": [],
                }
                result["shares"][key] = bucket
            bucket["count"] += 1
            if len(bucket["samples"]) < SAMPLE_PER_SHARE:
                bucket["samples"].append(path)
            if key in capture and len(bucket["paths"]) < MAX_PATHS_PER_SHARE:
                bucket["paths"].append(path)
            if key not in keys_here and len(keys_here) < MAX_KEYS_PER_DIR:
                keys_here.append(key)

        if n_strm:
            result["dir_strm"][dirpath] = n_strm
            result["dir_other"][dirpath] = n_other
            result["dir_keys"][dirpath] = keys_here

    return result


def _check_share_115(share_code: str, receive_code: str,
                     timeout: int = DEFAULT_TIMEOUT, cookie: str = "") -> Dict[str, Any]:
    """查一个 115 分享的有效性（只读，不消耗任何账号权益）。

    :param share_code: 分享码
    :param receive_code: 提取码（可为空）
    :param timeout: 单次请求超时秒数
    :param cookie: 可选的 115 Cookie（匿名时留空，实测两者结果一致）
    :return: {"state": valid|invalid|unknown, "reason": str, "errno": Any, ...}
    """
    params = {"share_code": share_code, "offset": "0", "limit": "1"}
    if receive_code:
        params["receive_code"] = receive_code
    headers = {"User-Agent": CHECK_UA,
               "Accept": "application/json, text/plain, */*"}
    if cookie:
        headers["Cookie"] = cookie
    req = Request(f"{CHECK_URL}?{urlencode(params)}", headers=headers)

    # 🔴 必须显式绕开系统代理直连 115：容器里 PROXY_HOST 指向局域网代理，
    #    115 是直连站点，走代理会被拒或超时。
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
    except HTTPError as err:
        if err.code in (403, 429, 503):
            return {"state": "unknown", "reason": f"疑似风控（HTTP {err.code}）", "errno": err.code}
        return {"state": "unknown", "reason": f"HTTP {err.code}", "errno": err.code}
    except Exception as err:  # noqa: BLE001 - 网络异常只回报，不影响整体流程
        return {"state": "unknown",
                "reason": f"请求失败：{type(err).__name__}", "errno": None}

    try:
        data = json.loads(body)
    except ValueError:
        return {"state": "unknown", "reason": f"响应不是 JSON：{body[:60]}", "errno": None}
    if not isinstance(data, dict):
        return {"state": "unknown", "reason": "响应格式异常", "errno": None}

    state = data.get("state")
    errno = data.get("errno")
    error = str(data.get("error") or "").strip()
    shareinfo = (data.get("data") or {}).get("shareinfo") or {}

    if state:
        # shareinfo 缺失时不敢判失效，按有效处理（宁漏不误杀）
        if not shareinfo:
            return {"state": "valid", "reason": "", "errno": errno,
                    "title": "", "violation": False}
        share_state = shareinfo.get("share_state")
        title = str(shareinfo.get("share_title") or "")
        if share_state in (1, "1", None):
            violation = bool(shareinfo.get("have_vio_file"))
            return {"state": "valid", "errno": errno, "title": title,
                    "violation": violation,
                    "reason": "含违规文件" if violation else ""}
        return {"state": "invalid", "errno": errno, "title": title,
                "reason": f"分享状态异常（share_state={share_state}）"}

    # state 为假：先用文本特征挡掉风控，再用 errno 兜住「服务端内部错误」
    if _looks_like_risk(error):
        return {"state": "unknown", "reason": f"疑似风控：{error}", "errno": errno}
    if errno in (0, "0", None):
        # 🔴 实测：115 用它表达内部错误（state=false + errno=0），
        #    不能当成「分享失效」，否则会误报一大批。
        return {"state": "unknown", "reason": f"115 未明确原因：{error or '空响应'}",
                "errno": errno}
    return {"state": "invalid", "reason": error or f"errno={errno}", "errno": errno}


def _check_shares_concurrent(shares: Dict[str, Dict[str, Any]],
                             concurrency: int = DEFAULT_CONCURRENCY,
                             timeout: int = DEFAULT_TIMEOUT,
                             cookie: str = "",
                             retry: int = DEFAULT_RETRY) -> Dict[str, Dict[str, Any]]:
    """并发检测一批分享，只对「未知」结果做有限重试。

    :param shares: :func:`_collect_shares` 里的 ``shares`` 映射
    :param concurrency: 并发线程数（1~16）
    :param timeout: 单次请求超时
    :param cookie: 可选 115 Cookie
    :param retry: 对 unknown 的额外重试次数
    :return: {key: 判定结果}
    """
    keys = list(shares)
    results: Dict[str, Dict[str, Any]] = {}
    if not keys:
        return results

    workers = max(1, min(int(concurrency or DEFAULT_CONCURRENCY), 16))
    total = len(keys)
    counter = {"done": 0}
    lock = threading.Lock()

    def _one(key: str) -> Tuple[str, Dict[str, Any]]:
        info = shares[key]
        verdict = _check_share_115(info.get("share_code", ""),
                                   info.get("receive_code", ""),
                                   timeout=timeout, cookie=cookie)
        tries = 0
        while verdict.get("state") == "unknown" and tries < max(0, int(retry)):
            time.sleep(1.0 + tries)          # 风控是瞬时状态，退避一下再试
            verdict = _check_share_115(info.get("share_code", ""),
                                       info.get("receive_code", ""),
                                       timeout=timeout, cookie=cookie)
            tries += 1
        return key, verdict

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, k) for k in keys]
        for fut in as_completed(futures):
            try:
                key, verdict = fut.result()
            except Exception as err:  # noqa: BLE001
                logger.error(f"【strm空目录巡检】115 检测线程异常：{err}")
                continue
            results[key] = verdict
            with lock:
                counter["done"] += 1
                if counter["done"] % 50 == 0 or counter["done"] == total:
                    logger.info(f"【strm空目录巡检】115 检测进度 "
                                f"{counter['done']}/{total}")
    return results


def _find_pending_dead(collect: Dict[str, Any],
                       verdicts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """算出「只剩失效 strm 的目录」—— 失效文件清掉后就会变成空目录的那批。

    判定条件（三条同时成立，**未知状态一律不参与**，宁可不报也不误报）：

    1. 目录自身有 ``.strm``；
    2. 该目录所有 ``.strm`` 都属于「已确认失效」的分享；
    3. 目录里没有别的文件（有的话清完仍不空，单独标出来）。

    :param collect: :func:`_collect_shares` 的返回
    :param verdicts: :func:`_check_shares_concurrent` 的返回
    :return: {"items": [...], "count": n, "will_empty": n}
    """
    invalid_keys = {k for k, v in verdicts.items() if v.get("state") == "invalid"}
    dir_strm = collect.get("dir_strm") or {}
    dir_other = collect.get("dir_other") or {}
    dir_keys = collect.get("dir_keys") or {}
    root = collect.get("root") or ""

    pending = set()
    for path, count in dir_strm.items():
        if not count:
            continue
        keys = dir_keys.get(path) or []
        if not keys:
            continue
        if all(k in invalid_keys for k in keys):
            pending.add(path)

    items: List[Dict[str, Any]] = []
    for path in sorted(pending):
        # 只保留「根」：父目录也在集合里就不用重复报
        if os.path.dirname(path) in pending:
            continue
        keys = dir_keys.get(path) or []
        other = dir_other.get(path, 0)
        items.append({
            "path": path,
            "rel": os.path.relpath(path, root) if root else path,
            "strm": dir_strm.get(path, 0),
            "shares": len(keys),
            "other": other,
            "will_empty": other == 0,
        })

    return {
        "items": items,
        "count": len(items),
        "will_empty": sum(1 for x in items if x["will_empty"]),
    }


# ------------------------------------------------------------------- 插件


class StrmEmptyDirMonitor(_PluginBase):
    """strm 媒体库巡检：115 分享有效性检测 + 空目录巡检。

    组合巡检按「先检测 115 链接、再检查空文件夹」的顺序执行；
    所有清理动作（空目录、失效 strm）都只放在详情页的手动按钮上，
    带边界校验 / 复查 / rmdir / 库内回收站四道保险。
    """

    # 插件名称
    plugin_name = "strm库空目录巡检"
    # 插件描述
    plugin_desc = "检测115分享有效性、扫描strm库空目录并通知，可手动一键清理。"
    # 插件图标
    plugin_icon = "world.png"
    # 插件版本
    plugin_version = "1.1.2"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "strmemptydirmonitor_"
    # 加载顺序
    plugin_order = 40
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    # 仅当发现死树时才通知
    _notify_only_when_found: bool = True
    _scan_path: str = DEFAULT_SCAN_PATH
    _exclude: str = DEFAULT_EXCLUDE
    _cron: str = DEFAULT_CRON
    # 清理开关：打开后点「清理」只预览、不真删
    _dry_run: bool = False
    # 115 分享检测
    _check_115: bool = True
    _check_115_on_cron: bool = True
    _check_concurrency: int = DEFAULT_CONCURRENCY
    _check_timeout: int = DEFAULT_TIMEOUT
    # 可选的 115 Cookie（留空 = 匿名检测，实测结果一致）
    _cookie_115: str = ""
    # 最近一次扫描结果（内存缓存，供详情页免扫展示）
    _last_result: Optional[Dict[str, Any]] = None
    # 最近一次 115 检测结果
    _last_check: Optional[Dict[str, Any]] = None
    # 禁用重入：扫描可能跑十几秒，防止定时任务与手动按钮叠在一起
    _scan_lock = threading.RLock()

    # ------------------------------------------------------------ 生命周期

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        self.stop_service()

        self._enabled = False
        self._notify = False
        self._notify_only_when_found = True
        self._scan_path = DEFAULT_SCAN_PATH
        self._exclude = DEFAULT_EXCLUDE
        self._cron = DEFAULT_CRON
        self._dry_run = False
        self._check_115 = True
        self._check_115_on_cron = True
        self._check_concurrency = DEFAULT_CONCURRENCY
        self._check_timeout = DEFAULT_TIMEOUT
        self._cookie_115 = ""
        self._last_result = None
        self._last_check = None

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._notify_only_when_found = bool(config.get("notify_only_when_found", True))
        self._dry_run = bool(config.get("dry_run"))
        self._check_115 = bool(config.get("check_115", True))
        self._check_115_on_cron = bool(config.get("check_115_on_cron", True))
        self._cookie_115 = str(config.get("cookie_115") or "").strip()
        self._scan_path = str(config.get("scan_path") or DEFAULT_SCAN_PATH).strip() or DEFAULT_SCAN_PATH
        self._exclude = str(config.get("exclude") or DEFAULT_EXCLUDE).strip()
        self._cron = str(config.get("cron") or DEFAULT_CRON).strip() or DEFAULT_CRON
        try:
            self._check_concurrency = min(max(int(config.get("check_concurrency")
                                                  or DEFAULT_CONCURRENCY), 1), 16)
        except (TypeError, ValueError):
            self._check_concurrency = DEFAULT_CONCURRENCY
        try:
            self._check_timeout = min(max(int(config.get("check_timeout")
                                              or DEFAULT_TIMEOUT), 3), 120)
        except (TypeError, ValueError):
            self._check_timeout = DEFAULT_TIMEOUT

        # 恢复最近一次结果，重载后详情页立刻有东西看
        self._last_result = self.get_data(LAST_RESULT_KEY) or None
        self._last_check = self.get_data(CHECK_RESULT_KEY) or None

        if config.get("run_once"):
            logger.info("【strm空目录巡检】立即执行一次")
            self.check()
            stored = self.get_config() or {}
            stored.update({k: v for k, v in {
                "enabled": self._enabled,
                "notify": self._notify,
                "notify_only_when_found": self._notify_only_when_found,
                "scan_path": self._scan_path,
                "exclude": self._exclude,
                "cron": self._cron,
                "dry_run": self._dry_run,
                "check_115": self._check_115,
                "check_115_on_cron": self._check_115_on_cron,
                "check_concurrency": self._check_concurrency,
                "check_timeout": self._check_timeout,
                "cookie_115": self._cookie_115,
                "run_once": False,
            }.items()})
            self.update_config(stored)

    def get_state(self) -> bool:
        """插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """停止插件后台服务（定时任务由宿主按插件名统一回收）。"""
        return None

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """远程命令列表（本插件不提供）。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时巡检服务。

        🔴 kwargs 必须拆成 APScheduler 的字段名，**不能**传 ``{"cron": "..."}``
        —— 详见 :func:`_cron_kwargs` 的说明，1.0.0 正是死在这里。
        """
        if not (self._enabled and self._cron):
            return []
        try:
            kwargs = _cron_kwargs(self._cron)
        except ValueError as err:
            logger.error(f"【strm空目录巡检】定时周期不合法，定时服务未注册：{err}")
            return []
        return [{
            "id": _PLUGIN_ID,
            "name": "strm库空目录巡检",
            "trigger": "cron",
            "func": self.check,
            "kwargs": kwargs,
        }]

    # ------------------------------------------------------------------ API

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET"],
                "summary": "立即扫描空目录",
                "description": "只读扫描一次扫描路径，列出整棵子树不含 .strm 的死树目录。",
            },
            {
                "path": "/check115",
                "endpoint": self.api_check115,
                "methods": ["GET"],
                "summary": "检测 115 分享有效性",
                "description": "解析库里所有 .strm 的 115 分享，去重后并发检测，"
                               "列出已失效 / 已取消 / 已过期 / 违规 / 不存在的分享，只报告不清理。",
            },
            {
                "path": "/inspect",
                "endpoint": self.api_inspect,
                "methods": ["GET"],
                "summary": "组合巡检（先测 115 再查空目录）",
                "description": "先跑一遍 115 分享检测，再扫一遍空目录，结果一起返回。",
            },
            {
                "path": "/clean",
                "endpoint": self.api_clean,
                "methods": ["GET"],
                "summary": "清理死树目录",
                "description": "清理指定死树（path 参数）或全部死树（all=1）。"
                               "零文件目录用 rmdir，含文件目录移入库内回收站。",
            },
            {
                "path": "/clean_invalid",
                "endpoint": self.api_clean_invalid,
                "methods": ["GET"],
                "summary": "清理失效的 strm 文件",
                "description": "把失效分享对应的 .strm 移入库内 @recycle 回收站"
                               "（key 指定单个分享，all=1 表示全部）。只移动、不删除。",
            },
        ]

    def api_scan(self) -> Dict[str, Any]:
        """API：立即扫描一次空目录（只读）。"""
        result = self.check(manual=True, with_115=False)
        return {"success": True, "data": result or {}}

    def api_check115(self) -> Dict[str, Any]:
        """API：立即执行一次 115 分享检测（只读，不清理）。"""
        result = self.check_115(manual=True)
        return {"success": True, "data": result or {}}

    def api_inspect(self) -> Dict[str, Any]:
        """API：组合巡检 —— 先检测 115 链接，再检查空文件夹。"""
        check = self.check_115(manual=True)
        scan = self.check(manual=True, with_115=False)
        return {"success": True, "data": {"check115": check or {}, "scan": scan or {}}}

    def api_clean(self, path: str = "", all: str = "", apikey: str = "") -> Dict[str, Any]:
        """API：清理死树。

        :param path: 单个死树根路径
        :param all: 传 "1" 表示清理全部死树
        :param apikey: 面板事件回调自动带上的鉴权参数
        :return: 清理结果
        """
        return self.clean(path=path, clean_all=str(all) in ("1", "true", "True"))

    def api_clean_invalid(self, key: str = "", all: str = "", apikey: str = "") -> Dict[str, Any]:
        """API：清理失效的 strm（只移动到库内回收站）。

        :param key: 单个分享键 ``share_code|receive_code``
        :param all: 传 "1" 表示清理全部失效分享下的 strm
        :param apikey: 面板事件回调自动带上的鉴权参数
        :return: 处理结果
        """
        return self.clean_invalid(key=key, clean_all=str(all) in ("1", "true", "True"))

    # ------------------------------------------------------------- 表单与页面

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-2",
                            "text": "① 115 分享检测：解析库里每个 .strm 的分享码，"
                                    "去重后并发查 115 接口，列出已失效（取消/过期/违规/"
                                    "不存在/访问码错误）的分享与受影响的文件，**只报告不清理**。"
                                    "② 空目录巡检：扫描「整棵子树不含任何 .strm」的目录。"
                                    "两者的清理动作都只在详情页手动触发，"
                                    "且一律移入库内 @recycle 回收站或 rmdir 空目录，绝不直接删文件。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "enabled", "label": "启用插件"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "notify", "label": "发送通知"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "notify_only_when_found",
                                    "label": "仅在发现问题时通知"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "scan_path",
                                        "label": "扫描路径",
                                        "placeholder": DEFAULT_SCAN_PATH,
                                        "hint": "容器内的路径，需在 compose 里挂载",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "exclude",
                                        "label": "排除的目录名",
                                        "placeholder": DEFAULT_EXCLUDE,
                                        "hint": "逗号分隔，支持 @* 这类通配",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "巡检周期",
                                        "placeholder": "5位cron表达式",
                                        "hint": DEFAULT_CRON,
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "check_115", "label": "启用 115 分享检测"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "check_115_on_cron",
                                    "label": "定时任务里也检测 115"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "dry_run", "label": "清理时只预览不删除"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "check_concurrency",
                                        "label": "115 检测并发数",
                                        "placeholder": str(DEFAULT_CONCURRENCY),
                                        "hint": "1~16，太大容易被 115 风控，建议 3~5",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "check_timeout",
                                        "label": "115 请求超时（秒）",
                                        "placeholder": str(DEFAULT_TIMEOUT),
                                        "hint": "3~120",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cookie_115",
                                        "label": "115 Cookie（可选）",
                                        "placeholder": "留空 = 匿名检测",
                                        "hint": "实测匿名与带 Cookie 结果一致；"
                                                "填写可略微降低风控概率",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "run_once", "label": "立即巡检一次"}}],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "notify_only_when_found": True,
            "scan_path": DEFAULT_SCAN_PATH,
            "exclude": DEFAULT_EXCLUDE,
            "cron": DEFAULT_CRON,
            "dry_run": False,
            "check_115": True,
            "check_115_on_cron": True,
            "check_concurrency": DEFAULT_CONCURRENCY,
            "check_timeout": DEFAULT_TIMEOUT,
            "cookie_115": "",
            "run_once": False,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页组件树。"""
        result = self._last_result or {}
        check = self._last_check or {}
        page: List[dict] = []

        page.append({"component": "VAlert", "props": {
            "type": "info", "variant": "tonal", "density": "comfortable",
            "class": "mb-2",
            "text": f"扫描路径 {self._scan_path} ｜ 周期 {self._cron} ｜ "
                    f"排除 {self._exclude or '(无)'} ｜ "
                    f"115 检测 {'开' if self._check_115 else '关'}"
                    f"（并发 {self._check_concurrency}） ｜ "
                    f"清理模式 {'预览（不删）' if self._dry_run else '实际执行'}",
        }})

        if not self._enabled:
            page.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mb-2",
                "text": "插件未启用，定时巡检不会执行（详情页的手动按钮仍可用）。",
            }})

        if not result and not check:
            page.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mb-2",
                "text": "尚未巡检过。建议点「组合巡检」（先检测 115 链接、再查空目录）。",
            }})
        elif not result.get("exists", True) or (check and not check.get("exists", True)):
            page.append({"component": "VAlert", "props": {
                "type": "error", "variant": "tonal", "class": "mb-2",
                "text": f"扫描路径不存在：{result.get('root') or check.get('root')}。"
                        f"该路径需要在 moviepilot 容器的 compose 里挂载后才能扫描。",
            }})

        # ---- 操作行：第一排是 115 检测，第二排是空目录（呼应「先测链接再查目录」）
        page.append({
            "component": "VRow",
            "props": {"dense": True, "class": "mb-1"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [_inspect_btn()]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [_check115_btn(self._check_115)]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [_clean_invalid_btn((check.get("share_invalid") or 0)
                                                if check else 0)]},
            ],
        })
        page.append({
            "component": "VRow",
            "props": {"dense": True, "class": "mb-2"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [_scan_btn()]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [_clean_all_btn(len(result.get("dead_roots") or []))]},
            ],
        })

        # ---- 115 检测区块
        page.extend(self.__render_check_block(check))

        # ---- 空目录区块
        page.extend(self.__render_scan_block(result))

        page.append(self.__render_clean_log())

        # 右下角 56px 悬浮齿轮会扫过整页右侧，最后一块要让出通道
        _avoid_fab(page)
        return page

    def __render_check_block(self, check: Dict[str, Any]) -> List[dict]:
        """渲染 115 检测区块。"""
        blocks: List[dict] = []
        if not check:
            blocks.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "density": "comfortable",
                "class": "mb-2",
                "text": "还没有做过 115 分享检测。点上面的「检测 115 分享」跑一次。",
            }})
            return blocks

        if not check.get("exists"):
            return blocks

        invalid = check.get("share_invalid") or 0
        unknown = check.get("share_unknown") or 0
        violation = check.get("share_violation") or 0
        valid = check.get("share_valid") or 0
        head_type = "error" if invalid else ("warning" if unknown else "success")
        head_text = (f"115 检测：{check.get('share_total', 0):,} 个分享中 "
                     f"失效 {invalid:,} ｜ 未知 {unknown:,} ｜ 有效 {valid:,}"
                     + (f" ｜ 含违规文件 {violation:,}" if violation else ""))
        blocks.append({"component": "VAlert", "props": {
            "type": head_type, "variant": "tonal", "density": "comfortable",
            "class": "mb-2", "text": head_text,
        }})

        blocks.append({"component": "VAlert", "props": {
            "type": "info", "variant": "tonal", "density": "compact", "class": "mb-2",
            "text": f"最近检测 {check.get('time') or '—'} ｜ 耗时 {check.get('cost', 0)}s ｜ "
                    f"扫到 .strm {check.get('strm_total', 0):,} 个"
                    f"（其中 115 格式 {check.get('strm_115', 0):,}） ｜ "
                    f"失效文件 {check.get('strm_invalid', 0):,} 个",
        }})

        # 🔴 清理过就要有回执：否则「失效文件 N 个」纹丝不动，用户会以为点了没反应。
        if check.get("cleaned_at"):
            blocks.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal", "density": "compact",
                "class": "mb-2",
                "text": f"已于 {check.get('cleaned_at')} 清理 "
                        f"{check.get('cleaned_total', 0):,} 个失效 strm"
                        f"（移入库内 @recycle）；上面这些数字已按清理结果同步扣减。",
            }})

        if unknown:
            blocks.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "density": "compact",
                "class": "mb-2",
                "text": f"有 {unknown} 个分享返回「未知」（115 风控或网络异常），"
                        f"**未计入失效**。稍后重扫一遍即可。",
            }})

        blocks.extend(self.__render_pending_list(check))
        blocks.extend(self.__render_invalid_list(check))
        return blocks

    def __render_pending_list(self, check: Dict[str, Any]) -> List[dict]:
        """渲染「只剩失效 strm 的目录」清单。"""
        items = check.get("pending_dead") or []
        if not items:
            return []
        will_empty = sum(1 for x in items if x.get("will_empty"))
        body: List[dict] = [{
            "component": "div", "props": {"class": "text-subtitle-2 mb-1"},
            "text": f"只剩失效 strm 的目录（{len(items)} 个，其中 {will_empty} 个清完就空）",
        }, {
            "component": "div", "props": {
                "class": "text-caption text-medium-emphasis mb-1",
                "text": "这些目录里有 .strm，所以「空目录巡检」抓不到；"
                        "但里面的分享已全部失效，清掉失效文件后就会变成空目录。",
            },
        }]
        for info in items[:MAX_PAGE_ROWS]:
            body.append(_pending_row(info))
        if len(items) > MAX_PAGE_ROWS:
            body.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis",
                "text": f"其余 {len(items) - MAX_PAGE_ROWS} 个未在页面列出。",
            }})
        return [{"component": "VRow", "props": {"dense": True}, "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": body}]}]

    def __render_invalid_list(self, check: Dict[str, Any]) -> List[dict]:
        """渲染失效分享清单（每个分享一行 + 单个「移除」按钮）。"""
        shares = check.get("invalid_shares") or []
        unknown_shares = check.get("unknown_shares") or []
        if not shares and not unknown_shares:
            return []
        body: List[dict] = [{
            "component": "div", "props": {"class": "text-subtitle-2 mb-1"},
            "text": f"失效分享清单（{len(shares)} 条）",
        }]
        if not shares:
            body.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis",
                "text": "没有失效分享。",
            }})
        for info in shares[:MAX_PAGE_ROWS]:
            body.append(_share_row(info))
        if len(shares) > MAX_PAGE_ROWS:
            body.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis",
                "text": f"其余 {len(shares) - MAX_PAGE_ROWS} 条未在页面列出。",
            }})
        if unknown_shares:
            body.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mt-2",
                "text": f"另有 {len(unknown_shares)} 个分享状态未知（未判定为失效）："
                        + "、".join(f"{x.get('share_code', '')}"
                                   for x in unknown_shares[:8]),
            }})
        return [{"component": "VRow", "props": {"dense": True}, "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": body}]}]

    def __render_scan_block(self, result: Dict[str, Any]) -> List[dict]:
        """渲染空目录巡检区块。"""
        blocks: List[dict] = []
        if not result:
            return blocks

        errors = result.get("errors") or []
        summ = (
            f"空目录巡检：{result.get('time') or '—'} ｜ "
            f"目录 {result.get('total_dirs', 0):,} ｜ 文件 {result.get('total_files', 0):,} ｜ "
            f".strm {result.get('strm_files', 0):,} ｜ "
            f"排除目录 {result.get('excluded_dirs', 0):,}"
        )
        blocks.append({"component": "VAlert", "props": {
            "type": "success" if not errors else "warning",
            "variant": "tonal", "density": "comfortable", "class": "mb-2",
            "text": summ + (f" ｜ 权限错误 {len(errors)}" if errors else " ｜ 权限错误 0"),
        }})

        roots = result.get("dead_roots") or []
        head_type = "error" if roots else "success"
        head_text = (f"发现 {len(roots)} 个死树目录（整棵子树不含 .strm）"
                     if roots else "没有发现死树目录，媒体库是干净的。")
        blocks.append({"component": "VAlert", "props": {
            "type": head_type, "variant": "tonal", "density": "comfortable",
            "class": "mb-2", "text": head_text,
        }})

        if roots:
            blocks.append(self.__render_dead_list(result))
        return blocks

    def __render_dead_list(self, result: Dict[str, Any]) -> dict:
        """渲染死树清单（每行一个路径 + 大小 + 单个「清理」按钮）。"""
        roots: List[str] = list(result.get("dead_roots") or [])
        detail: Dict[str, Any] = result.get("detail") or {}
        rows: List[dict] = []
        for path in roots[:MAX_PAGE_ROWS]:
            info = detail.get(path) or {}
            rows.append(_dead_row(path, info))

        body: List[dict] = [{
            "component": "div",
            "props": {"class": "text-subtitle-2 mb-1"},
            "text": f"死树清单（前 {min(len(roots), MAX_PAGE_ROWS)} / 共 {len(roots)} 条）",
        }]
        if len(roots) > MAX_PAGE_ROWS:
            body.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis mb-1",
                "text": f"清单较长，页面只列前 {MAX_PAGE_ROWS} 条，"
                        f"其余 {len(roots) - MAX_PAGE_ROWS} 条请用「清理全部」。",
            }})
        body.extend(rows)

        return {"component": "VRow", "props": {"dense": True}, "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": body}]}

    def __render_clean_log(self) -> dict:
        """渲染最近清理记录。"""
        logs = self.get_data(CLEAN_LOG_KEY) or []
        body: List[dict] = [{
            "component": "div", "props": {"class": "text-subtitle-2 mb-1"},
            "text": f"最近清理记录（保留最近 {CLEAN_LOG_LIMIT} 次）",
        }]
        if not logs:
            body.append({"component": "div", "props": {
                "class": "text-caption text-medium-emphasis",
                "text": "还没有清理记录。",
            }})
        else:
            for entry in list(reversed(logs))[:10]:
                body.append({"component": "div",
                             "props": {"class": "text-caption",
                                       "style": "display:block;"},
                             "content": [
                                 {"component": "span",
                                  "props": {"style": "opacity:0.62;"},
                                  "text": f"{entry.get('time', '')}　"},
                                 {"component": "span",
                                  "text": f"成功 {entry.get('ok', 0)} / 跳过 {entry.get('skip', 0)}"
                                          f"　{entry.get('summary', '')}"},
                             ]})
        return {"component": "VRow", "props": {"dense": True}, "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": body}]}

    # ---------------------------------------------------------------- 业务

    def check(self, manual: bool = False,
              with_115: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """扫一遍空目录并落库 / 通知。

        :param manual: 是否由详情页按钮触发（手动触发时忽略「未启用」）
        :param with_115: 是否连带跑 115 检测；None 表示按配置（定时任务用）
        :return: 扫描结果
        """
        if not self._enabled and not manual:
            return None

        with self._scan_lock:
            root = os.path.abspath(self._scan_path)
            logger.info(f"【strm空目录巡检】开始扫描：{root}")
            started = datetime.now()

            result = _scan_dead_trees(root, _parse_patterns(self._exclude))
            result["time"] = _now_str()
            result["cost"] = round((datetime.now() - started).total_seconds(), 2)
            # 明细只落前 N 条，避免极端情况下把插件数据撑爆；总数单独记
            roots = list(result.get("dead_roots") or [])
            result["dead_root_total"] = len(roots)
            result["dead_roots"] = roots[:MAX_STORED_ROOTS]
            # dead_all 只是算 subdirs 的中间产物，体积可能很大，别落库
            result.pop("dead_all", None)

            if not result["exists"]:
                logger.error(f"【strm空目录巡检】扫描路径不存在：{root}"
                             f"（需要在 moviepilot 容器的 compose 里挂载该目录）")
            else:
                logger.info(
                    f"【strm空目录巡检】扫描完成：目录 {result['total_dirs']:,} / "
                    f"文件 {result['total_files']:,} / .strm {result['strm_files']:,} / "
                    f"排除 {result['excluded_dirs']:,} / 死树 {len(roots)} / "
                    f"权限错误 {len(result['errors'])} / 耗时 {result['cost']}s")

            self._last_result = result
            self.save_data(LAST_RESULT_KEY, result)

        # 🔴 115 检测放在锁外：它自己不带锁、耗时长，串在锁里会把整段占死
        want_115 = self._check_115 and (self._check_115_on_cron if with_115 is None
                                        else bool(with_115))
        if want_115:
            self.check_115(manual=manual)

        if self._notify:
            self.__notify(result, self._last_check)
        return result

    def check_115(self, manual: bool = False) -> Optional[Dict[str, Any]]:
        """检测库里所有 115 分享的有效性（只读，不做任何清理）。

        :param manual: 是否由详情页按钮触发（手动触发时忽略「未启用」）
        :return: 检测结果
        """
        if not self._enabled and not manual:
            return None

        with self._scan_lock:
            root = os.path.abspath(self._scan_path)
            logger.info(f"【strm空目录巡检】开始 115 分享检测：{root}")
            started = datetime.now()

            collect = _collect_shares(root, _parse_patterns(self._exclude))
            shares = collect.get("shares") or {}

            if not collect["exists"]:
                logger.error(f"【strm空目录巡检】115 检测跳过：扫描路径不存在 {root}")
                result: Dict[str, Any] = {
                    "root": root, "exists": False, "time": _now_str(), "cost": 0.0,
                    "strm_total": 0, "strm_115": 0, "strm_other": 0, "unreadable": 0,
                    "share_total": 0, "share_valid": 0, "share_invalid": 0,
                    "share_unknown": 0, "share_violation": 0,
                    "strm_invalid": 0, "strm_unknown": 0,
                    "invalid_shares": [], "unknown_shares": [], "violation_shares": [],
                    "pending_dead": [], "pending_dead_will_empty": 0,
                }
                self._last_check = result
                self.save_data(CHECK_RESULT_KEY, result)
                return result

            logger.info(f"【strm空目录巡检】115 检测：扫到 .strm {collect['strm_total']:,} 个，"
                        f"其中 115 分享格式 {collect['strm_115']:,} 个，"
                        f"去重后 {len(shares):,} 个分享，开始并发检测"
                        f"（并发 {self._check_concurrency}）")
            verdicts = _check_shares_concurrent(
                shares,
                concurrency=self._check_concurrency,
                timeout=self._check_timeout,
                cookie=self._cookie_115,
            )

            invalid_shares: List[Dict[str, Any]] = []
            unknown_shares: List[Dict[str, Any]] = []
            violation_shares: List[Dict[str, Any]] = []
            strm_invalid = 0
            strm_unknown = 0
            for key, bucket in shares.items():
                verdict = verdicts.get(key) or {"state": "unknown",
                                                "reason": "未返回结果", "errno": None}
                state = verdict.get("state")
                record = {
                    "key": key,
                    "share_code": bucket.get("share_code", ""),
                    "receive_code": bucket.get("receive_code", ""),
                    "files": bucket.get("count", 0),
                    "reason": verdict.get("reason") or "",
                    "errno": verdict.get("errno"),
                    "title": verdict.get("title") or "",
                    "samples": bucket.get("samples") or [],
                }
                if state == "invalid":
                    invalid_shares.append(record)
                    strm_invalid += bucket.get("count", 0)
                elif state == "unknown":
                    unknown_shares.append(record)
                    strm_unknown += bucket.get("count", 0)
                elif verdict.get("violation"):
                    violation_shares.append(record)

            invalid_shares.sort(key=lambda x: -x["files"])
            unknown_shares.sort(key=lambda x: -x["files"])
            violation_shares.sort(key=lambda x: -x["files"])

            pending = _find_pending_dead(collect, verdicts)

            result = {
                "root": root,
                "exists": True,
                "time": _now_str(),
                "cost": round((datetime.now() - started).total_seconds(), 2),
                "strm_total": collect["strm_total"],
                "strm_115": collect["strm_115"],
                "strm_other": collect["strm_other"],
                "unreadable": collect["unreadable"],
                "share_total": len(shares),
                "share_valid": sum(1 for v in verdicts.values() if v.get("state") == "valid"),
                "share_invalid": len(invalid_shares),
                "share_unknown": len(unknown_shares),
                "share_violation": len(violation_shares),
                "strm_invalid": strm_invalid,
                "strm_unknown": strm_unknown,
                "invalid_shares": invalid_shares[:MAX_STORED_SHARES],
                "unknown_shares": unknown_shares[:MAX_STORED_SHARES],
                "violation_shares": violation_shares[:MAX_STORED_SHARES],
                "pending_dead": pending["items"][:MAX_STORED_ROOTS],
                "pending_dead_will_empty": pending["will_empty"],
                "errors": collect.get("errors") or [],
            }

            logger.info(
                f"【strm空目录巡检】115 检测完成：分享 {result['share_total']} / "
                f"有效 {result['share_valid']} / 失效 {result['share_invalid']} / "
                f"未知 {result['share_unknown']} / 含违规 {result['share_violation']} / "
                f"失效文件 {result['strm_invalid']} / "
                f"只剩失效的目录 {len(result['pending_dead'])} / 耗时 {result['cost']}s")
            for info in invalid_shares[:10]:
                logger.info(f"【strm空目录巡检】失效分享 {info['share_code']}"
                            f"（{info['files']} 个文件）：{info['reason']}")

            self._last_check = result
            self.save_data(CHECK_RESULT_KEY, result)
            return result

    def clean(self, path: str = "", clean_all: bool = False) -> Dict[str, Any]:
        """清理死树目录（详情页按钮 / API 调用的唯一入口）。

        :param path: 单个死树根路径；为空且 clean_all 时清理全部
        :param clean_all: 是否清理全部死树
        :return: {"success": bool, "preview": bool, "items": [...], "summary": str}
        """
        result = self._last_result or self.get_data(LAST_RESULT_KEY) or {}
        roots = list(result.get("dead_roots") or [])
        root = os.path.abspath(self._scan_path or DEFAULT_SCAN_PATH)

        if clean_all:
            targets = roots
        elif path:
            targets = [path]
        else:
            return {"success": False, "preview": False, "items": [],
                    "summary": "没有指定要清理的目录。"}

        if not targets:
            return {"success": False, "preview": False, "items": [],
                    "summary": "当前没有可清理的死树（请先扫描一次）。"}

        if self._dry_run:
            items = []
            for target in targets[:MAX_STORED_ROOTS]:
                info = (result.get("detail") or {}).get(target) or {}
                items.append({
                    "path": target, "ok": True, "action": "preview",
                    "message": ("将用 rmdir 删除空目录" if info.get("completely_empty")
                                else f"将移入回收站（{info.get('files', '?')} 个文件）"),
                })
            logger.info(f"【strm空目录巡检】预览模式，未执行删除，共 {len(items)} 个目标")
            return {"success": True, "preview": True, "items": items,
                    "summary": f"预览模式：{len(items)} 个目标未做任何改动。"}

        trash_root = os.path.join(root, TRASH_DIR_NAME,
                                  datetime.now().strftime("%Y%m%d_%H%M%S"))
        items: List[Dict[str, Any]] = []
        ok = skip = 0
        for target in targets[:MAX_STORED_ROOTS]:
            item = _clean_dead_root(root, target, trash_root)
            items.append(item)
            if item.get("ok"):
                ok += 1
            else:
                skip += 1
            logger.info(f"【strm空目录巡检】清理 {target}：{item.get('message')}")

        summary = (f"处理 {len(items)} 个：成功 {ok}、跳过 {skip}"
                   f"{'，含文件目录已移入 ' + trash_root if ok else ''}")
        self.__append_clean_log({
            "time": _now_str(), "ok": ok, "skip": skip, "summary": summary,
        })
        # 清理完立刻重扫，避免页面还挂着已经不存在的目标
        self.check(manual=True, with_115=False)
        return {"success": True, "preview": False, "items": items, "summary": summary}

    def clean_invalid(self, key: str = "", clean_all: bool = False) -> Dict[str, Any]:
        """把失效分享对应的 ``.strm`` 移入库内回收站（**只移动、不删除**）。

        1.1.2 起修了两个「点了像没反应」的坑：

        * **清不干净** —— 检测快照里每个分享只留 10 条样本，旧实现只搬这 10 条，
          分享实际有 14 个文件时就剩 4 个一直挂着。现在**按需重扫**，抓目标分享的
          **全部** strm 再搬（``capture_paths``）。
        * **看不出变化** —— 页面上的「失效文件 N 个 / 失效分享清单 / 只剩失效 strm 的目录」
          全部来自上一次检测的快照，清理原本不动它们，于是整页毫无变化、只有页面最底下
          的「最近清理记录」多一行 ⇒ 用户以为没反应。现在清理完会**当场把快照里对应条目
          扣掉并回存**（:meth:`__prune_after_clean`），页面刷新即反映真实状态。

        另外：无论成功、空转还是失败，**都会往「最近清理记录」写一条**，保证点击必有回执。

        :param key: 单个分享键 ``share_code|receive_code``
        :param clean_all: 是否清理全部失效分享下的 strm
        :return: {"success": bool, "preview": bool, "items": [...], "summary": str}
        """
        check = self._last_check or self.get_data(CHECK_RESULT_KEY) or {}
        shares = check.get("invalid_shares") or []
        root = os.path.abspath(self._scan_path or DEFAULT_SCAN_PATH)

        if clean_all:
            targets = list(shares)
        elif key:
            targets = [x for x in shares if x.get("key") == key]
            if not targets:
                return self.__clean_refuse("没有找到该分享的最新检测结果，请先重新检测一次。")
        else:
            return self.__clean_refuse("没有指定要清理的分享。")

        if not targets:
            return self.__clean_refuse(
                "当前没有已确认失效的分享（可能已经清理完，或还没做过 115 检测）。")

        target_keys = {x.get("key") for x in targets if x.get("key")}

        if self._dry_run:
            total = sum(int(x.get("files") or 0) for x in targets)
            summary = (f"预览模式：{len(targets)} 个失效分享、共 {total} 个 strm "
                       f"将被移入回收站，未做任何改动。")
            self.__append_clean_log({"time": _now_str(), "ok": 0, "skip": 0,
                                     "preview": True, "summary": summary})
            return {"success": True, "preview": True, "items": [], "summary": summary}

        # ---- 按需重扫：只对目标分享抓**全量**路径（22.9 万条也不会常驻内存）
        collect = _collect_shares(root, _parse_patterns(self._exclude),
                                  capture_paths=target_keys)
        bucket_map = collect.get("shares") or {}
        plan: List[Tuple[str, str]] = []
        for k in target_keys:
            for p in ((bucket_map.get(k) or {}).get("paths") or []):
                plan.append((k, p))
        if not plan:
            # 扫不到（库结构变了 / 分享已整批消失）→ 退回快照里的样本，至少不全无作为
            for x in targets:
                for p in (x.get("samples") or []):
                    plan.append((x.get("key"), p))

        if not plan:
            summary = "重新扫描后没有找到可清理的失效 strm（可能已被清理过，或库已变化）。"
            self.__append_clean_log({"time": _now_str(), "ok": 0, "skip": 0,
                                     "summary": summary})
            return {"success": True, "preview": False, "items": [], "summary": summary}

        trash_root = os.path.join(root, TRASH_DIR_NAME,
                                  datetime.now().strftime("%Y%m%d_%H%M%S"))
        items: List[Dict[str, Any]] = []
        ok = skip = 0
        moved: List[str] = []
        per_key: Dict[str, int] = {}
        for k, path in plan:
            item = _move_strm_to_trash(root, path, trash_root)
            items.append(item)
            if item.get("ok"):
                ok += 1
                moved.append(os.path.realpath(path))
                per_key[k] = per_key.get(k, 0) + 1
            else:
                skip += 1

        summary = f"失效分享 {len(target_keys)} 个：移动 {ok} 个 .strm、跳过 {skip} 个"
        self.__prune_after_clean(check, moved, per_key)
        self.__append_clean_log({
            "time": _now_str(), "ok": ok, "skip": skip, "summary": summary,
        })
        logger.info(f"【strm空目录巡检】{summary}（回收站 {trash_root}）")
        return {"success": True, "preview": False, "items": items, "summary": summary}

    def __clean_refuse(self, summary: str) -> Dict[str, Any]:
        """「没能开始清理」也要留一条可见记录，否则页面上看起来就是『点了没反应』。"""
        self.__append_clean_log({"time": _now_str(), "ok": 0, "skip": 0,
                                 "summary": summary})
        return {"success": False, "preview": False, "items": [], "summary": summary}

    def __prune_after_clean(self, check: Dict[str, Any], moved: List[str],
                            per_key: Dict[str, int]) -> None:
        """清理成功后，把检测快照里对应的条目**扣掉并回存**，让详情页立刻反映真实状态。

        * 失效分享清单：按 ``per_key`` 减掉已清理条数，减到 0 的直接移除；
        * 「只剩失效 strm 的目录」：按**直属** strm 数扣减（子目录的条目由它自己承载），
          减到 0 的说明这批失效文件已清空、该目录不再属于「只剩失效 strm」，一并移除；
        * 重算 ``share_invalid`` / ``strm_invalid`` / ``pending_dead_will_empty``。
        """
        if not check or not moved:
            return
        try:
            moved_set = set(moved)

            kept: List[Dict[str, Any]] = []
            for info in (check.get("invalid_shares") or []):
                gone = per_key.get(info.get("key"), 0)
                if gone:
                    info = dict(info)
                    info["files"] = max(0, int(info.get("files") or 0) - gone)
                    info["samples"] = [p for p in (info.get("samples") or [])
                                       if os.path.realpath(p) not in moved_set]
                if int(info.get("files") or 0) <= 0:
                    continue
                kept.append(info)
            check["invalid_shares"] = kept
            check["share_invalid"] = len(kept)
            check["strm_invalid"] = sum(int(x.get("files") or 0) for x in kept)

            pending: List[Dict[str, Any]] = []
            for info in (check.get("pending_dead") or []):
                d = os.path.realpath(info.get("path") or "")
                if not d:
                    continue
                gone = sum(1 for p in moved_set if os.path.dirname(p) == d)
                info = dict(info)
                info["strm"] = max(0, int(info.get("strm") or 0) - gone)
                if info["strm"] <= 0:
                    continue
                pending.append(info)
            check["pending_dead"] = pending
            check["pending_dead_will_empty"] = sum(
                1 for x in pending if x.get("will_empty"))

            check["cleaned_at"] = _now_str()
            check["cleaned_total"] = int(check.get("cleaned_total") or 0) + len(moved)
            self._last_check = check
            self.save_data(CHECK_RESULT_KEY, check)
        except Exception as err:  # noqa: BLE001 - 扣减失败不能影响清理结果本身
            logger.error(f"【strm空目录巡检】清理后同步检测快照失败：{err}")

    # ---------------------------------------------------------------- 内部

    def __notify(self, result: Dict[str, Any],
                 check: Optional[Dict[str, Any]] = None) -> None:
        """按配置发送巡检结果通知（115 检测 + 空目录）。"""
        try:
            lines: List[str] = []

            if check and check.get("exists"):
                invalid = check.get("share_invalid") or 0
                unknown = check.get("share_unknown") or 0
                if invalid:
                    lines.append(f"115 分享：{check.get('share_total', 0)} 个中 "
                                 f"{invalid} 个已失效（涉及 "
                                 f"{check.get('strm_invalid', 0)} 个 .strm）")
                    for info in (check.get("invalid_shares") or [])[:5]:
                        lines.append(f"· {info.get('share_code')}"
                                     f"（{info.get('files')} 文件）：{info.get('reason')}")
                    if invalid > 5:
                        lines.append(f"…… 其余 {invalid - 5} 个见插件详情页")
                elif unknown:
                    lines.append(f"115 分享：{check.get('share_total', 0)} 个全部未判定，"
                                 f"其中 {unknown} 个返回未知（疑似风控），建议稍后重扫。")
                elif check.get("share_total"):
                    lines.append(f"115 分享：{check.get('share_total', 0)} 个全部有效。")

            if not result.get("exists"):
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【strm库巡检】",
                    text="扫描路径不存在：{}\n该目录需要在 moviepilot 容器的 compose 里挂载。"
                    .format(result.get("root")),
                )
                return

            roots = result.get("dead_roots") or []
            if roots:
                lines.append(f"空目录：{len(roots)} 个死树目录（整棵子树不含 .strm）")
                lines.extend(f"· {p}" for p in roots[:5])
                if len(roots) > 5:
                    lines.append(f"…… 其余 {len(roots) - 5} 个见插件详情页")

            if not lines:
                if self._notify_only_when_found:
                    return
                lines.append("没有发现问题，媒体库是干净的。")

            lines.append("")
            lines.append("清理请到插件详情页手动执行。")
            self.post_message(mtype=NotificationType.SiteMessage,
                              title="【strm库巡检】", text="\n".join(lines))
        except Exception as err:  # noqa: BLE001 - 通知失败不能影响结果落库
            logger.error(f"【strm空目录巡检】发送通知失败：{err}")

    def __append_clean_log(self, entry: Dict[str, Any]) -> None:
        """追加一条清理记录（只保留最近 CLEAN_LOG_LIMIT 条）。"""
        try:
            logs = self.get_data(CLEAN_LOG_KEY) or []
            logs.append(entry)
            self.save_data(CLEAN_LOG_KEY, logs[-CLEAN_LOG_LIMIT:])
        except Exception as err:  # noqa: BLE001
            logger.error(f"【strm空目录巡检】写入清理记录失败：{err}")


# --------------------------------------------------------------- 组件小件
# 一律用 `_` 前缀的模块级函数：类里不带 `_` 的方法会被注册成插件 API 端点。


def _scan_btn() -> dict:
    """「扫描空目录」按钮。"""
    return {
        "component": "VBtn",
        "props": {"size": "small", "color": "primary", "variant": "tonal",
                  "prepend-icon": "mdi-magnify", "style": "flex:0 0 auto;"},
        "text": "扫空目录",
        "events": {"click": {
            "api": f"plugin/{_PLUGIN_ID}/scan",
            "method": "get",
            "params": {"apikey": settings.API_TOKEN},
        }},
    }


def _inspect_btn() -> dict:
    """「组合巡检」按钮（先测 115 再查空目录）。"""
    return {
        "component": "VBtn",
        "props": {"size": "small", "color": "primary", "variant": "flat",
                  "prepend-icon": "mdi-playlist-check", "style": "flex:0 0 auto;"},
        "text": "组合巡检",
        "events": {"click": {
            "api": f"plugin/{_PLUGIN_ID}/inspect",
            "method": "get",
            "params": {"apikey": settings.API_TOKEN},
        }},
    }


def _check115_btn(enabled: bool) -> dict:
    """「检测 115 分享」按钮。"""
    return {
        "component": "VBtn",
        "props": {"size": "small", "color": "indigo", "variant": "tonal",
                  "prepend-icon": "mdi-link-variant",
                  "disabled": not enabled, "style": "flex:0 0 auto;"},
        "text": "检测 115 分享",
        "events": {"click": {
            "api": f"plugin/{_PLUGIN_ID}/check115",
            "method": "get",
            "params": {"apikey": settings.API_TOKEN},
        }},
    }


def _clean_invalid_btn(count: int) -> dict:
    """「清理失效 strm」按钮（移入回收站）。"""
    return {
        "component": "VBtn",
        "props": {"size": "small", "color": "deep-orange", "variant": "tonal",
                  "prepend-icon": "mdi-broom",
                  "disabled": count <= 0, "style": "flex:0 0 auto;"},
        "text": f"清理失效（{count}）",
        "events": {"click": {
            "api": f"plugin/{_PLUGIN_ID}/clean_invalid",
            "method": "get",
            "params": {"apikey": settings.API_TOKEN, "all": "1"},
        }},
    }


def _clean_all_btn(count: int) -> dict:
    """「清理全部死树」按钮。"""
    return {
        "component": "VBtn",
        "props": {"size": "small", "color": "error", "variant": "tonal",
                  "prepend-icon": "mdi-delete-sweep", "disabled": count <= 0,
                  "style": "flex:0 0 auto;"},
        "text": f"清理空目录（{count}）",
        "events": {"click": {
            "api": f"plugin/{_PLUGIN_ID}/clean",
            "method": "get",
            "params": {"apikey": settings.API_TOKEN, "all": "1"},
        }},
    }


def _dead_row(path: str, info: Dict[str, Any]) -> dict:
    """死树清单里的一行：双行卡（上行路径 + 清理按钮，下行规模信息）。"""
    files = info.get("files", 0)
    size = info.get("size", 0)
    empty = bool(info.get("completely_empty"))
    tag_text = "完全空白" if empty else f"{files} 个文件 · {_fmt_size(size)}"
    tag_color = "#2E7D32" if empty else "#E08A17"

    return {
        "component": "div",
        "props": {
            "style": "display:flex; align-items:center; gap:8px; "
                     "padding:6px 0; border-bottom:1px solid "
                     "rgba(var(--v-border-color), var(--v-border-opacity)); "
                     f"padding-right:{_FAB_CHANNEL}px;",
        },
        "content": [
            {
                "component": "div",
                "props": {"style": "flex:1 1 auto; min-width:0;"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 text-truncate",
                                  "style": "min-width:0;", "title": path},
                        "text": info.get("rel") or path,
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-caption",
                                  "style": "display:block; margin-top:2px;"},
                        "content": [
                            {"component": "span",
                             "props": {"style": f"color:{tag_color}; font-weight:600;"},
                             "text": tag_text},
                            {"component": "span",
                             "props": {"style": "color:rgba(var(--v-theme-on-surface),0.62);"},
                             "text": f"　{info.get('mtime') or ''}"
                                     f"　子目录 {info.get('subdirs', 0)}"},
                        ],
                    },
                ],
            },
            {
                "component": "VBtn",
                "props": {"size": "x-small", "color": "error", "variant": "text",
                          "style": "flex:0 0 auto;"},
                "text": "清理",
                "events": {"click": {
                    "api": f"plugin/{_PLUGIN_ID}/clean",
                    "method": "get",
                    "params": {"apikey": settings.API_TOKEN, "path": path},
                }},
            },
        ],
    }


def _pending_row(info: Dict[str, Any]) -> dict:
    """「只剩失效 strm 的目录」一行。"""
    will_empty = bool(info.get("will_empty"))
    strm = info.get("strm", 0)
    other = info.get("other", 0)
    if will_empty:
        tag_text = f"{strm} 个失效 strm · 清完即空"
        tag_color = "#C62828"
    else:
        tag_text = f"{strm} 个失效 strm · 另有 {other} 个其它文件"
        tag_color = "#E08A17"
    return {
        "component": "div",
        "props": {
            "style": "display:flex; align-items:center; gap:8px; "
                     "padding:6px 0; border-bottom:1px solid "
                     "rgba(var(--v-border-color), var(--v-border-opacity)); "
                     f"padding-right:{_FAB_CHANNEL}px;",
        },
        "content": [
            {
                "component": "div",
                "props": {"style": "flex:1 1 auto; min-width:0;"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 text-truncate",
                                  "style": "min-width:0;",
                                  "title": info.get("path") or ""},
                        "text": info.get("rel") or info.get("path") or "",
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-caption",
                                  "style": "display:block; margin-top:2px;"},
                        "content": [
                            {"component": "span",
                             "props": {"style": f"color:{tag_color}; font-weight:600;"},
                             "text": tag_text},
                            {"component": "span",
                             "props": {"style": "color:rgba(var(--v-theme-on-surface),0.62);"},
                             "text": f"　涉及分享 {info.get('shares', 0)} 个"},
                        ],
                    },
                ],
            },
        ],
    }


def _share_row(info: Dict[str, Any]) -> dict:
    """失效分享一行（上行分享码 + 移除按钮，下行文件数与原因）。"""
    files = info.get("files", 0)
    code = info.get("share_code", "")
    receive = info.get("receive_code", "")
    errno = info.get("errno")

    meta = f"{files} 个文件"
    if receive:
        meta += f"　提取码 {receive}"
    if errno not in (None, ""):
        meta += f"　errno {errno}"

    return {
        "component": "div",
        "props": {
            "style": "display:flex; align-items:center; gap:8px; "
                     "padding:6px 0; border-bottom:1px solid "
                     "rgba(var(--v-border-color), var(--v-border-opacity)); "
                     f"padding-right:{_FAB_CHANNEL}px;",
        },
        "content": [
            {
                "component": "div",
                "props": {"style": "flex:1 1 auto; min-width:0;"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 text-truncate",
                                  "style": "min-width:0;",
                                  "title": info.get("samples") and
                                  (info.get("samples") or [""])[0] or code},
                        "content": [
                            {"component": "span",
                             "props": {"style": "font-weight:600;"}, "text": code},
                            {"component": "span",
                             "props": {"style": "opacity:0.7;"},
                             "text": f"　{info.get('reason') or '已失效'}"},
                        ],
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-caption",
                                  "style": "display:block; margin-top:2px; opacity:0.72;"},
                        "text": meta,
                    },
                ],
            },
            {
                "component": "VBtn",
                "props": {"size": "x-small", "color": "deep-orange", "variant": "text",
                          "style": "flex:0 0 auto;"},
                "text": "移除",
                "events": {"click": {
                    "api": f"plugin/{_PLUGIN_ID}/clean_invalid",
                    "method": "get",
                    "params": {"apikey": settings.API_TOKEN, "key": info.get("key", "")},
                }},
            },
        ],
    }


def _avoid_fab(page_content: List[dict], gap: int = _FAB_CHANNEL) -> None:
    """给页面最后一块的 VCol 留出右下角悬浮齿轮的通道。

    ⚠️ 结构是 ``VRow -> content[VCol]``：VCol 直接就是 VRow 的孩子。
    多写一层去 VCol.content 里找 VCol 会「静默什么都不做」——
    不报错、页面毫无变化，1.5.2 那次就是这么翻的。
    """
    if not page_content:
        return
    block = page_content[-1]
    if not isinstance(block, dict):
        return
    for col in block.get("content") or []:
        if isinstance(col, dict) and col.get("component") == "VCol":
            props = col.setdefault("props", {})
            style = (props.get("style") or "").strip()
            if "padding-right" not in style:
                props["style"] = (style + f";padding-right:{gap}px;").lstrip(";")
            return
