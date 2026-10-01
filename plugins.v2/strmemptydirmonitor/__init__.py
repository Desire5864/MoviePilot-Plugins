"""strm 媒体库空目录巡检插件。

定时扫描 strm 库中「整棵子树里不含任何 .strm 文件」的目录（下称"死树"），
在插件详情页列出清单并可通过通知提醒；**清理动作只放在详情页的手动按钮上，
不做任何自动删除**。

设计前提（很重要，别搞错）：
    本插件服务的 ``/volume1/movie_strm`` **不是刮削库** —— 目录由用户手工整理、
    挂的是 115 分享链接，``.strm`` 由 NanShare 容器生成（内容是该分享的直链）。
    所以空目录的成因在「115 分享链路」上（分享被取消/过期、分享里没有正片、
    转存或再分享失败），**不是刮削规则问题**。

清理的三道保险（都在代码里）：
    1. 删前对每个目标**重新实时复查**，只要子树里出现任何一个 ``.strm`` 就跳过；
    2. **零文件的子树只用 ``os.rmdir``**（非空必然失败）⇒ 物理上不可能删掉文件；
    3. 含文件的子树**不删除，整体移动到 ``<扫描路径>/@recycle/<时间戳>/``**
       （库内回收站，且 ``@*`` 默认被排除、不会被再次扫成死树）。
"""

import fnmatch
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# 插件 ID：拼事件回调用（`events.click.api` 走的是 `plugin/<ID>/<method>`）
_PLUGIN_ID = "StrmEmptyDirMonitor"

# 持久化键名
LAST_RESULT_KEY = "last_result"
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
            if name.lower().endswith(".strm"):
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
            if name.lower().endswith(".strm"):
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


# ------------------------------------------------------------------- 插件


class StrmEmptyDirMonitor(_PluginBase):
    """strm 媒体库空目录巡检。

    定时扫描指定目录，列出「整棵子树不含 .strm」的死树目录并通知；
    清理动作全部放在详情页的手动按钮上，带复查 / rmdir / 回收站三道保险。
    """

    # 插件名称
    plugin_name = "strm库空目录巡检"
    # 插件描述
    plugin_desc = "扫描strm媒体库，列出不含.strm的空目录并通知，可手动一键清理。"
    # 插件图标
    plugin_icon = "world.png"
    # 插件版本
    plugin_version = "1.0.0"
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
    # 最近一次扫描结果（内存缓存，供详情页免扫展示）
    _last_result: Optional[Dict[str, Any]] = None
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
        self._last_result = None

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._notify_only_when_found = bool(config.get("notify_only_when_found", True))
        self._dry_run = bool(config.get("dry_run"))
        self._scan_path = str(config.get("scan_path") or DEFAULT_SCAN_PATH).strip() or DEFAULT_SCAN_PATH
        self._exclude = str(config.get("exclude") or DEFAULT_EXCLUDE).strip()
        self._cron = str(config.get("cron") or DEFAULT_CRON).strip() or DEFAULT_CRON

        # 恢复最近一次结果，重载后详情页立刻有东西看
        self._last_result = self.get_data(LAST_RESULT_KEY) or None

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
        """注册定时扫描服务。"""
        if self._enabled and self._cron:
            return [{
                "id": _PLUGIN_ID,
                "name": "strm库空目录巡检",
                "trigger": "cron",
                "func": self.check,
                "kwargs": {"cron": self._cron},
            }]
        return []

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
                "path": "/clean",
                "endpoint": self.api_clean,
                "methods": ["GET"],
                "summary": "清理死树目录",
                "description": "清理指定死树（path 参数）或全部死树（all=1）。"
                               "零文件目录用 rmdir，含文件目录移入库内回收站。",
            },
        ]

    def api_scan(self) -> Dict[str, Any]:
        """API：立即扫描一次（只读）。"""
        result = self.check(manual=True)
        return {"success": True, "data": result or {}}

    def api_clean(self, path: str = "", all: str = "", apikey: str = "") -> Dict[str, Any]:
        """API：清理死树。

        :param path: 单个死树根路径
        :param all: 传 "1" 表示清理全部死树
        :param apikey: 面板事件回调自动带上的鉴权参数
        :return: 清理结果
        """
        return self.clean(path=path, clean_all=str(all) in ("1", "true", "True"))

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
                            "text": "扫描「整棵子树不含任何 .strm 文件」的目录（死树）。"
                                    "清理动作只在详情页手动触发：零文件目录用 rmdir 删除，"
                                    "含文件目录移动到库内 @recycle 回收站，绝不直接删文件。",
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
                                    "label": "仅在发现死树时通知"}}],
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
                                        "label": "扫描周期",
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
                                    "model": "dry_run", "label": "清理时只预览不删除"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "run_once", "label": "立即扫描一次"}}],
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
            "run_once": False,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页组件树。"""
        result = self._last_result or {}
        page: List[dict] = []

        page.append({"component": "VAlert", "props": {
            "type": "info", "variant": "tonal", "density": "comfortable",
            "class": "mb-2",
            "text": f"扫描路径 {self._scan_path} ｜ 周期 {self._cron} ｜ "
                    f"排除 {self._exclude or '(无)'} ｜ "
                    f"清理模式 {'预览（不删）' if self._dry_run else '实际执行'}",
        }})

        if not self._enabled:
            page.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "class": "mb-2",
                "text": "插件未启用，定时扫描不会执行（详情页的手动按钮仍可用）。",
            }})

        if not result:
            page.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mb-2",
                "text": "尚未扫描过。点下面的「立即扫描」跑一次。",
            }})
        elif not result.get("exists"):
            page.append({"component": "VAlert", "props": {
                "type": "error", "variant": "tonal", "class": "mb-2",
                "text": f"扫描路径不存在：{result.get('root')}。"
                        f"该路径需要在 moviepilot 容器的 compose 里挂载后才能扫描。",
            }})

        # 操作行
        page.append({
            "component": "VRow",
            "props": {"dense": True, "class": "mb-2"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [_scan_btn()]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    _clean_all_btn(len(result.get("dead_roots") or []))]},
            ],
        })

        # 汇总
        if result:
            errors = result.get("errors") or []
            summ = (
                f"最近扫描：{result.get('time') or '—'} ｜ "
                f"目录 {result.get('total_dirs', 0):,} ｜ 文件 {result.get('total_files', 0):,} ｜ "
                f".strm {result.get('strm_files', 0):,} ｜ "
                f"排除目录 {result.get('excluded_dirs', 0):,}"
            )
            page.append({"component": "VAlert", "props": {
                "type": "success" if not errors else "warning",
                "variant": "tonal", "density": "comfortable", "class": "mb-2",
                "text": summ + (f" ｜ 权限错误 {len(errors)}" if errors else " ｜ 权限错误 0"),
            }})

            roots = result.get("dead_roots") or []
            head_type = "error" if roots else "success"
            head_text = (f"发现 {len(roots)} 个死树目录（整棵子树不含 .strm）"
                         if roots else "没有发现死树目录，媒体库是干净的。")
            page.append({"component": "VAlert", "props": {
                "type": head_type, "variant": "tonal", "density": "comfortable",
                "class": "mb-2", "text": head_text,
            }})

            if roots:
                page.append(self.__render_dead_list(result))
            page.append(self.__render_clean_log())

        # 右下角 56px 悬浮齿轮会扫过整页右侧，最后一块要让出通道
        _avoid_fab(page)
        return page

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

    def check(self, manual: bool = False) -> Optional[Dict[str, Any]]:
        """扫描一次并把结果落库 / 通知。

        :param manual: 是否由详情页按钮触发（手动触发时忽略「未启用」）
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

            if self._notify:
                self.__notify(result)
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
        self.check(manual=True)
        return {"success": True, "preview": False, "items": items, "summary": summary}

    # ---------------------------------------------------------------- 内部

    def __notify(self, result: Dict[str, Any]) -> None:
        """按配置发送扫描结果通知。"""
        try:
            roots = result.get("dead_roots") or []
            if not result.get("exists"):
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【strm空目录巡检】",
                    text=f"扫描路径不存在：{result.get('root')}\n"
                         f"该目录需要在 moviepilot 容器的 compose 里挂载。",
                )
                return
            if not roots and self._notify_only_when_found:
                return

            if roots:
                preview = "\n".join(f"· {p}" for p in roots[:5])
                more = f"\n…… 其余 {len(roots) - 5} 个见插件详情页" if len(roots) > 5 else ""
                text = (f"发现 {len(roots)} 个死树目录（整棵子树不含 .strm）：\n"
                        f"{preview}{more}\n\n"
                        f"清理请到插件详情页手动执行。")
            else:
                text = "没有发现死树目录，媒体库是干净的。"

            self.post_message(mtype=NotificationType.SiteMessage,
                              title="【strm空目录巡检】", text=text)
        except Exception as err:  # noqa: BLE001 - 通知失败不能影响扫描结果落库
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
    """「立即扫描」按钮。"""
    return {
        "component": "VBtn",
        "props": {"size": "small", "color": "primary", "variant": "tonal",
                  "prepend-icon": "mdi-magnify", "style": "flex:0 0 auto;"},
        "text": "立即扫描",
        "events": {"click": {
            "api": f"plugin/{_PLUGIN_ID}/scan",
            "method": "get",
            "params": {"apikey": settings.API_TOKEN},
        }},
    }


def _clean_all_btn(count: int) -> dict:
    """「清理全部死树」按钮。"""
    return {
        "component": "VBtn",
        "props": {"size": "small", "color": "error", "variant": "tonal",
                  "prepend-icon": "mdi-delete-sweep", "disabled": count <= 0,
                  "style": "flex:0 0 auto;"},
        "text": f"清理全部（{count}）",
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
