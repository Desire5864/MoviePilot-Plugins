import hashlib
import json
import os
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import qbittorrentapi
from qbittorrentapi import TorrentState

from app import schemas
from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# QB 活动状态：仅做种上传中（uploading）
# 命中监控分类且处于该状态的种子会被立即暂停（暂停后状态变为 stoppedUP）
ACTIVE_STATES = {
    TorrentState.UPLOADING.value,
}

# QB 已停止（暂停）状态
STOPPED_STATES = {
    TorrentState.STOPPED_UPLOAD.value,
    TorrentState.STOPPED_DOWNLOAD.value,
    TorrentState.PAUSED_UPLOAD.value,
    TorrentState.PAUSED_DOWNLOAD.value,
}

# 持久化停止时间记录的键名
STOP_TIME_DATA_KEY = "stopped_time_map"

# 持久化迁移任务队列的键名
MIGRATE_DATA_KEY = "migrate_jobs"

# 迁移任务「进行中」的状态集合（这些任务永远不会被裁剪）
MIGRATE_ACTIVE_STATES = ("queued", "copying", "verifying", "swapping")

# 迁移条目「状态」列的徽章配色（Vuetify 色名）。
# done 绿 / failed 红 / 进行中紫（面板主色）/ 排队灰；表里没有的取值兜底成 grey。
MIGRATE_STATE_COLORS = {
    "done": "success",
    "failed": "error",
    "queued": "grey",
    "copying": "primary",
    "verifying": "primary",
    "swapping": "primary",
}

# 迁移队列里最多保留多少条「已结束」（done / failed）历史记录。
# 采用**覆盖模式**：满了之后新记录挤掉最旧的，队列长度恒定在这个数。
# 渲染层也只取最近同样多条（见 __render_migrate_jobs），两边口径保持一致。
MIGRATE_KEEP_FINISHED = 20

# 🔴 迁移前的空间校验余量（字节）。
# 判断「装得下」用的是 `剩余空间 - 本次需要 >= MIGRATE_SPACE_RESERVE`，
# 而不是简单的 `剩余空间 > 本次需要`：文件系统元数据、目录项、以及复制过程中
# 目标侧的临时膨胀都要占地方，恰好卡满会把盘写爆 —— 写爆的后果是复制中途失败
# 且目标目录留下半份残文件。
MIGRATE_SPACE_RESERVE = 1024 ** 3  # 1 GiB

# 备用迁移目录的默认值（qB 视角路径）。
# 升级前的旧配置里**没有**这个键，此时按默认值启用备用目录；用户在面板里把它
# 清空成 "" 才是「明确不要备用」。这样「加备用路径」这个需求不需要用户再手动配一次。
DEFAULT_MIGRATE_TARGET_ALT = "/下载"

# qB 容器路径前缀 -> MoviePilot 容器路径前缀
# （两个容器的挂载点不同名，插件跑在 MP 容器里要靠这张表换算真实路径）
QB_TO_MP_PATH_MAP = (
    ("/保种", "/CloudNAS/CloudDrive/115open/保种"),
    ("/下载", "/下载"),
    ("/原盘", "/原盘"),
    ("/完结", "/完结"),
    ("/热更", "/热更"),
    ("/ISO", "/ISO"),
    ("/download", "/download"),
)


# ===========================================================================
# 详情页排版（方案 A1 + B1 + C3 + D4，2026-09-30 起）
#
# 为什么不用 VAlert / VTable：
#   · VAlert 的 `text` 只能给一段纯文本，做不了「彩色分段 + 右侧徽章」；
#   · VTable 表头固定 54px、行高由最高的那一列决定，长种子名会把整行撑高，
#     `table-layout` 再自适应也会让名称列右边留一大片空白；
#   · 实测内容宽 1240px / 可见高 587px 下，原结构总高 1744px，要滚 2.9 屏。
# 改用「div / span + inline style」自己拼版（面板渲染器对原生标签是直通的，
# 本仓库的 chdbitshrmonitor / scptrafficmonitor 都这么写，含 conic-gradient 环）。
#
# 四个板块：
#   A1  合并摘要条      配置 + 计数合进一条 48px 的蓝条（原本三条 alert = 216px）
#   B1  单行卡          正在上传：紫色竖条 + 分类 + 名称 + 徽章 + 动效点（48px）
#   C3  双行行卡        已停止：主行给眼睛看、副行给状态看（56px/条）
#   D4  队列压行        异常给完整两行，完成态压成一行，其余折成一条汇总
#
# 色板全部取自面板自身主题的实拍采样（真机截图逐像素取众数），不是估的。
# ===========================================================================

# 插件 ID：拼事件回调用（`events.click.api` 走的是 `plugin/<ID>/<method>`）
_PLUGIN_ID = "QbCategoryPause"

# 语义条：底色 / 图标底色 / 文字色 / 图标字符
_PAGE_BAR_COLORS = {
    "info": ("#DDEEF7", "#16B1FF", "#0E6A93", "i"),
    "success": ("#E4F2DD", "#56CA00", "#33691E", "\u2713"),
    "warning": ("#F6EEDD", "#FFB400", "#7A5A00", "!"),
    "error": ("#F5E4E5", "#FF4C51", "#9E2226", "i"),
}

_PAGE_TEXT = "#3A3541"
_PAGE_MUT = "#8B8794"
_PAGE_PRIMARY = "#8D51F9"

# 🔴 分组之间的竖直间距。面板原本靠 VRow/VCol 撑出 24px，改用裸 div 后要自己给。
_PAGE_GAP = 14

# 🔴 右下角那颗 56px 悬浮齿轮（距右下各 12px）的让位通道。
# 它是**固定在弹窗可见区右下角**的，不随内容滚动 —— 也就是说内容右侧那一条
# 68px 会被它扫过，凡是排到右边的元素（按钮、进度列、汇总行尾）都要让开。
_PAGE_FAB_CHANNEL = 68

# 完成态在队列里最多平铺几条，其余折成一行汇总（D4 的核心）
_PAGE_DONE_PREVIEW = 5

# 徽章配色（自带底色，不依赖面板主题）
_PAGE_CHIP = {
    "ok": "background:#E4F2DD;color:#3E8E1F;",
    "bad": "background:#F5E4E5;color:#C62828;",
    "solid": "background:#FF4C51;color:#FFFFFF;",
    "info": "background:#DDEEF7;color:#0E7FA8;",
    "warn": "background:#F6EEDD;color:#8A6500;",
    "pri": "background:#EDE4FE;color:#6B34C9;",
    "grey": "background:#ECEBEF;color:#6E6878;",
}

# 迁移任务状态 -> 徽章配色 / 中文名
_PAGE_STATE_CHIP = {
    "done": "ok",
    "failed": "bad",
    "queued": "grey",
    "copying": "pri",
    "verifying": "pri",
    "swapping": "pri",
}
_PAGE_STATE_LABEL = {
    "done": "完成",
    "failed": "失败",
    "queued": "排队",
    "copying": "复制中",
    "verifying": "校验中",
    "swapping": "切换中",
}

# qB 已停止状态 -> 中文名
_PAGE_STOPPED_LABEL = {
    "stoppedUP": "已停止",
    "stoppedDL": "已停止",
    "pausedUP": "已暂停",
    "pausedDL": "已暂停",
}

# 单行省略三件套。⚠️ `min-width:0` 不能省：flex 子项的默认 `min-width:auto`
# 会被 nowrap 的长名字顶开，省略号不生效、整行横向溢出。
_PAGE_ELL = "min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"


def _clip(text: Any, limit: int) -> str:
    """截断成一行，超出补省略号（用于已经写死长度的位置）。"""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\u2026"


def _pill(text: str, kind: str = "grey") -> dict:
    """小圆角徽章。"""
    return {
        "component": "span",
        "props": {"style": (
            "flex:0 0 auto;white-space:nowrap;font-size:12px;font-weight:600;"
            "padding:2px 9px;border-radius:11px;"
            + _PAGE_CHIP.get(kind, _PAGE_CHIP["grey"])
        )},
        "text": text,
    }


def _parse_categories(raw: Any) -> List[str]:
    """把「监控分类」的配置值统一解析成分类名列表（去空、去重、保序）。

    1.7.0 起配置页把这一项从手输框换成了多选控件，于是配置值的格式换代了：

    · 数组      —— 多选控件存下来的，如 ``["SSD", "辅种"]``
    · 字符串    —— 1.6.0 及以前的手输框存下来的，逗号或换行分隔，如 ``"SSD,辅种"``
    · JSON 串   —— 个别前端会把数组序列化成字符串，如 ``'["SSD", "辅种"]'``

    三种都要认。**不能只认数组** —— 老用户的配置是字符串，只认数组等于升级后
    监控分类被清空（表现为「插件忽然不监控任何分类了」）。

    :param raw: 配置里的原始值（可能是 None / str / list）
    :return: 干净的分类名列表
    """
    if raw is None:
        return []

    if isinstance(raw, (list, tuple, set)):
        candidates = [str(item) for item in raw]
    else:
        text = str(raw).strip()
        candidates = None
        # 只在「像 JSON 数组」时才尝试解析，避免把分类名里的中括号当语法
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                candidates = [str(item) for item in parsed]
        if candidates is None:
            candidates = text.replace("\n", ",").split(",")

    result: List[str] = []
    for candidate in candidates:
        name = str(candidate).strip()
        if name and name not in result:
            result.append(name)
    return result


def _sep() -> dict:
    """信息之间的中圆点分隔。"""
    return {"component": "span", "props": {"style": "margin:0 8px;color:#B7B4BE;"},
            "text": "\u00b7"}


def _vsep() -> dict:
    """条内竖分隔线。"""
    return {"component": "span", "props": {"style": (
        "flex:0 0 1px;width:1px;height:22px;background:rgba(0,0,0,.10);margin:0 2px;"
    )}}


def _bar_icon(kind: str) -> dict:
    """条左侧的实心圆图标。"""
    _, accent, _, glyph = _PAGE_BAR_COLORS[kind]
    italic = ";font-style:italic;" if kind in ("info", "error") else ""
    return {
        "component": "span",
        "props": {"style": (
            "flex:0 0 19px;width:19px;height:19px;border-radius:50%;"
            f"background:{accent};color:#fff;font-size:12px;line-height:19px;"
            f"text-align:center;font-weight:700{italic}"
        )},
        "text": glyph,
    }


def _bar(kind: str, parts: List[dict], right: Optional[List[dict]] = None,
         height: int = 48, gap: int = _PAGE_GAP) -> dict:
    """一条彩色信息条（A1 / 组头 / 队列统计都用它）。

    :param kind: info / success / warning / error
    :param parts: 中间那段的 inline 内容（会被单行省略）
    :param right: 右端固定不缩的徽章或按钮
    """
    bg, _, fg, _ = _PAGE_BAR_COLORS[kind]
    content = [_bar_icon(kind), {
        "component": "div",
        "props": {"style": (
            "flex:1 1 auto;min-width:0;font-size:13px;"
            "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
        )},
        "content": parts,
    }]
    if right:
        content.append(_vsep())
        content.extend(right)
    # 🔴 右端有徽章 / 按钮时，右侧要多留一条齿轮通道。
    #    那颗齿轮是**固定在可见区右下角**的，会随滚动扫过整页 ——
    #    不是只有最后一块会被压：实测汇总条右端能到 x1360，而齿轮占 x1328~1384，
    #    滚到那一段时徽章尾巴会被啃掉。所以每条带右端内容的条都要让位。
    pad_right = 16 + _PAGE_FAB_CHANNEL if right else 16
    return {
        "component": "div",
        "props": {"style": (
            "display:flex;align-items:center;gap:12px;box-sizing:border-box;"
            f"background:{bg};border-radius:5px;padding:0 {pad_right}px 0 16px;"
            f"min-height:{height}px;font-size:13px;color:{fg};margin-bottom:{gap}px;"
        )},
        "content": content,
    }


def _kv(key: str, value: Any) -> List[dict]:
    """「键 值」两个同色片段（键加粗）。"""
    return [
        {"component": "span", "props": {"style": "font-weight:600;"}, "text": key},
        {"component": "span", "text": " " + str(value)},
    ]


# --------------------------------------------------------------- B1 正在上传

def _pulse() -> dict:
    """正在上传的动效点（静态三根竖条，面板里本来就只是装饰）。"""
    bars = [
        {"component": "span", "props": {"style": (
            f"display:inline-block;width:3px;height:{h}px;"
            f"background:{_PAGE_PRIMARY};border-radius:2px;"
        )}}
        for h in (6, 13, 9)
    ]
    return {"component": "span", "props": {"style": (
        "flex:0 0 auto;display:inline-flex;gap:3px;align-items:flex-end;height:14px;"
    )}, "content": bars}


def _group_caption(name: str, count: int) -> dict:
    """当有多个下载器 / 多条任务时，给这一组一个轻量标题。"""
    return {
        "component": "div",
        "props": {"style": (
            "display:flex;align-items:center;gap:8px;font-size:12px;"
            f"color:{_PAGE_MUT};font-weight:600;margin-bottom:8px;"
        )},
        "content": [
            {"component": "span", "text": name},
            _pill(str(count), "grey"),
        ],
    }


def _upload_card(item: Dict[str, str]) -> dict:
    """B1 · 单行卡：紫色竖条 + 分类徽章 + 种子名（单行省略）+ 状态 + 动效点。"""
    name = str(item.get("name") or "")
    category = str(item.get("category") or "(无分类)")
    return {
        "component": "div",
        "props": {"style": (
            "display:flex;align-items:center;gap:12px;box-sizing:border-box;"
            "height:48px;background:#FFFFFF;border:1px solid #E6E5E9;"
            f"border-radius:8px;padding-right:{14 + _PAGE_FAB_CHANNEL}px;"
            f"overflow:hidden;font-size:14px;"
            f"color:{_PAGE_TEXT};margin-bottom:{_PAGE_GAP}px;"
        )},
        "content": [
            {"component": "span", "props": {"style": (
                f"flex:0 0 3px;width:3px;height:48px;background:{_PAGE_PRIMARY};"
            )}},
            _pill(category, "pri"),
            {"component": "div", "props": {
                "style": "flex:1 1 auto;" + _PAGE_ELL + "font-size:14px;",
                "title": name,
            }, "text": name},
            _pill("上传中", "pri"),
            _pulse(),
        ],
    }


# --------------------------------------------------------------- C3 已停止

def _migrate_btn(item: Dict[str, str]) -> dict:
    """单条「迁移」按钮（保留 VBtn —— 只有它带得动面板那套事件回调）。"""
    return {
        "component": "VBtn",
        "props": {"size": "x-small", "color": "primary", "variant": "tonal",
                  "style": "flex:0 0 auto;"},
        "text": "迁移",
        "events": {"click": {
            "api": f"plugin/{_PLUGIN_ID}/migrate_torrent",
            "method": "get",
            "params": {"apikey": settings.API_TOKEN,
                       "hash": str(item.get("hash") or "")},
        }},
    }


def _stop_row(item: Dict[str, str], stop_time_map: Dict[str, float],
              now_ts: float, resume_minutes: int, show_migrate: bool,
              downloader: str = "") -> dict:
    """C3 · 双行行卡：主行给眼睛看（分类 / 名称 / 状态 / 迁移），副行给状态看。"""
    name = str(item.get("name") or "")
    category = str(item.get("category") or "(无分类)")
    stop_ts = stop_time_map.get(str(item.get("hash") or ""))
    if stop_ts:
        elapsed = int((now_ts - stop_ts) / 60)
        remain = max(0, resume_minutes - elapsed)
        # 「已停止」只在副行出现一次 —— 主行的状态徽章已经说了同一件事，
        # 原来写成「恢复倒计时 已停止 58 分钟，剩余 662 分钟」两处重复。
        countdown = f"已停止 {elapsed} 分钟 · 剩余 {remain} 分钟"
    else:
        countdown = "未记录停止时间"
    if downloader:
        countdown = f"{downloader} \u00b7 {countdown}"

    line1 = [
        {"component": "span", "props": {
            "style": f"flex:0 0 64px;width:64px;font-size:12px;color:{_PAGE_MUT};" + _PAGE_ELL,
            "title": category,
        }, "text": category},
        {"component": "div", "props": {
            "style": "flex:1 1 auto;" + _PAGE_ELL + f"font-size:14px;color:{_PAGE_TEXT};",
            "title": name,
        }, "text": name},
        _pill(_PAGE_STOPPED_LABEL.get(str(item.get("state") or ""), "已停止"), "warn"),
    ]
    if show_migrate:
        line1.append(_migrate_btn(item))

    return {
        "component": "div",
        "props": {"style": (
            "display:flex;flex-direction:column;justify-content:center;"
            "box-sizing:border-box;min-height:56px;padding:7px 0;"
            "border-bottom:1px solid #EFEEF1;"
        )},
        "content": [
            {"component": "div",
             "props": {"style": "display:flex;align-items:center;gap:12px;"},
             "content": line1},
            {"component": "div", "props": {
                "style": "font-size:11px;color:#A9A6AF;margin:3px 0 0 76px;" + _PAGE_ELL,
                "title": countdown,
            }, "text": countdown},
        ],
    }


# --------------------------------------------------------------- D4 迁移队列

def _queue_row(job: Dict[str, Any], summary: str, two_line: bool) -> dict:
    """队列一行。

    `two_line=True` 给「异常 / 进行中」用：第一行是种子名，第二行是原因或落地位置。
    完成态走 `two_line=False`：一条 36px 的单行，只留名字 + 徽章 + 百分比 ——
    20 条里 19 条都是 `完成 / 100.0%`，副标题也一字不差，没必要占两行。
    """
    state = str(job.get("state") or "")
    name = str(job.get("name") or "")
    total = job.get("total") or 0
    done_bytes = job.get("done") or 0
    percent = f"{done_bytes * 100.0 / total:.1f}%" if total else "-"

    if two_line and summary:
        sub_style = ("font-size:11px;margin-top:2px;"
                     + ("color:#C62828;" if state == "failed" else f"color:{_PAGE_MUT};"))
        left = {
            "component": "div",
            "props": {"style": "flex:1 1 auto;min-width:0;"},
            "content": [
                {"component": "div", "props": {
                    "style": _PAGE_ELL + f"font-size:14px;color:{_PAGE_TEXT};",
                    "title": name,
                }, "text": name},
                {"component": "div", "props": {
                    "style": _PAGE_ELL + sub_style, "title": summary,
                }, "text": summary},
            ],
        }
    else:
        left = {"component": "div", "props": {
            "style": "flex:1 1 auto;" + _PAGE_ELL + f"font-size:14px;color:{_PAGE_TEXT};",
            "title": name,
        }, "text": name}

    return {
        "component": "div",
        "props": {"style": (
            "display:flex;align-items:center;gap:12px;box-sizing:border-box;"
            f"min-height:{56 if two_line else 36}px;padding:6px 0;"
            "border-bottom:1px solid #EFEEF1;"
        )},
        "content": [
            left,
            _pill(_PAGE_STATE_LABEL.get(state, state or "\u2014"),
                  _PAGE_STATE_CHIP.get(state, "grey")),
            {"component": "span", "props": {"style": (
                "flex:0 0 68px;width:68px;text-align:right;font-size:12px;"
                f"color:{_PAGE_MUT};font-variant-numeric:tabular-nums;"
            )}, "text": percent},
        ],
    }


def _queue_blocks(ordered: List[Dict[str, Any]], make_summary) -> List[dict]:
    """D4 · 队列区块 = 统计条 + 异常/进行中完整行 + 最近完成态 + 折叠汇总行。

    :param ordered: 已按时间倒序排列的任务列表（调用方已截到最近 20 条）
    :param make_summary: `(job) -> str`，取第二行的小字摘要
    """
    def state_of(job):
        return str(job.get("state") or "")

    active = [j for j in ordered if state_of(j) in MIGRATE_ACTIVE_STATES]
    failed = [j for j in ordered if state_of(j) == "failed"]
    done = [j for j in ordered if state_of(j) == "done"]
    # ⚠️ 按状态分流，不要用 `j not in active` —— dict 的 `in` 走值比较，
    #    两条内容完全一样的记录会被误判成「已出现过」而漏掉。
    others = [j for j in ordered
              if state_of(j) not in MIGRATE_ACTIVE_STATES
              and state_of(j) not in ("failed", "done")]
    shown_done = done[:_PAGE_DONE_PREVIEW]
    rest_done = len(done) - len(shown_done)

    # 统计条：只要有失败就整条转红（和原版行为一致），右端挂总数
    parts: List[dict] = []
    parts += [{"component": "span", "text": "进行中 "},
              {"component": "span", "props": {"style": "font-weight:700;"},
               "text": str(len(active))}, _sep()]
    parts += [{"component": "span", "text": "已完成 "},
              {"component": "span", "props": {"style": "font-weight:700;"},
               "text": str(len(done))}]
    if failed:
        parts += [_sep(), {"component": "span", "text": "失败 "},
                  {"component": "span", "props": {"style": "font-weight:700;"},
                   "text": str(len(failed))}]

    blocks = [_bar("error" if failed else "info", parts,
                   [_pill(f"共 {len(ordered)} 条", "grey")], height=46)]

    rows = [_queue_row(j, make_summary(j), True) for j in failed + active + others]
    rows += [_queue_row(j, make_summary(j), False) for j in shown_done]
    if rows:
        blocks.append({
            "component": "div",
            "props": {"style": (
                f"margin-bottom:{_PAGE_GAP}px;padding-right:{_PAGE_FAB_CHANNEL}px;"
            )},
            "content": rows,
        })

    if rest_done:
        fold: List[dict] = [
            {"component": "span", "props": {"style": "font-weight:700;"},
             "text": "\u2713"},
            {"component": "span", "text": f" 其余 {rest_done} 条已完成"},
        ]
        if shown_done:
            latest = str(shown_done[0].get("name") or "")
            fold.append({"component": "span", "props": {
                "style": "font-weight:400;margin-left:12px;opacity:.72;" + _PAGE_ELL,
                "title": latest,
            }, "text": "最近：" + _clip(latest, 42)})
        blocks.append({
            "component": "div",
            "props": {"style": (
                "display:flex;align-items:center;box-sizing:border-box;"
                "min-height:36px;border-radius:6px;background:#E4F2DD;"
                f"color:#33691E;font-size:13px;font-weight:600;margin-bottom:0;"
                f"padding-left:14px;padding-right:{_PAGE_FAB_CHANNEL}px;"
            )},
            "content": fold,
        })

    return blocks


class QbCategoryPause(_PluginBase):
    """QB 分类做种上传监控暂停插件。

    监控指定 QB 下载器中指定分类的种子，一旦发现处于做种上传中
    （uploading）状态的种子，立即将其暂停（暂停后状态变为 stoppedUP）。
    """

    # 插件名称
    plugin_name = "QB分类活动暂停"
    # 插件描述
    plugin_desc = "监控QB指定分类并暂停活动种子，支持把已停止种子迁移到本地做种。"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.7.1"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "qbcategorypause_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _downloaders: List[str] = []
    _categories: List[str] = []
    _interval: int = 30
    # 是否启用自动恢复
    _resume_enabled: bool = False
    # 停止多久后自动恢复（分钟）
    _resume_minutes: int = 60
    # 记录上一次暂停的种子哈希，避免重复通知
    _last_paused_hashes: List[str] = []

    # ---- 迁移相关（把种子内容搬到另一处存储后重新做种）----
    # 目标根目录（qB 容器视角路径，如 /download）
    _migrate_target: str = ""
    # 备用目标根目录（qB 容器视角路径，如 /下载）：主目录剩余空间不足时自动改用
    _migrate_target_alt: str = ""
    # 迁移后归入的新分类
    _migrate_category: str = ""
    # 重新添加时跳过哈希校验
    _migrate_skip_check: bool = False
    # 删除原任务时是否连文件一起删
    _migrate_delete_files: bool = False
    # 复制完成后是否逐块校验完整性
    _migrate_verify: bool = True
    # 迁移工作线程是否在跑
    _migrate_running: bool = False
    # 🔴 迁移队列的读写锁：`__enqueue`（用户点按钮）与 worker（复制中的进度回写）
    # 都会「读队列 → 改 → 写回」，没有这把锁两个线程会互相覆盖。
    # 用 RLock（可重入）：`__load_jobs` / `__save_jobs` 自己也上锁，
    # 于是「一整段读-改-写」既能被锁保护、又不会自己把自己锁死。
    # ⚠️ 复制/校验这种长耗时的动作**绝不能**持锁，否则用户点迁移会卡住整个线程。
    _migrate_lock: threading.RLock = threading.RLock()

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        # 停止现有任务
        self.stop_service()

        # 重置状态
        self._enabled = False
        self._notify = False
        self._downloaders = []
        self._categories = []
        self._interval = 30
        self._resume_enabled = False
        self._resume_minutes = 60
        self._last_paused_hashes = []
        self._migrate_target = ""
        self._migrate_target_alt = ""
        self._migrate_category = ""
        self._migrate_skip_check = False
        self._migrate_delete_files = False
        self._migrate_verify = True

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._downloaders = config.get("downloaders") or []
        # 监控分类：支持「数组」（1.7.0 多选控件）与「逗号/换行分隔的字符串」（旧手输框）
        categories_raw = config.get("categories")
        self._categories = _parse_categories(categories_raw)
        # 🔴 格式迁移：配置页的多选控件值必须是数组，喂字符串进去会渲染成**空白**，
        # 用户会以为「监控分类被清空了」。所以发现是旧格式就顺手回写成数组。
        # 只改 categories 这一个键，其余字段以库里现存的原样带上，不做任何顺带修改。
        # 写一次之后库里就是数组了，条件不再成立 ⇒ 幂等，不会每次加载都写库。
        if not isinstance(categories_raw, (list, tuple, set)):
            stored = self.get_config()
            base = stored if isinstance(stored, dict) else dict(config)
            if not isinstance(base.get("categories"), (list, tuple, set)):
                try:
                    self.update_config({**base, "categories": self._categories})
                    logger.info(
                        f"QB分类活动暂停：监控分类配置格式已迁移为数组：{self._categories}"
                    )
                except Exception as err:
                    # 迁移失败不影响运行：解析结果已经在 self._categories 里了，
                    # 只是下次打开配置弹窗时多选框仍会显示为空（需要用户重选一次）。
                    logger.error(f"QB分类活动暂停：监控分类格式迁移失败：{err}")
        try:
            self._interval = max(5, int(config.get("interval") or 30))
        except (TypeError, ValueError):
            self._interval = 30
        self._resume_enabled = bool(config.get("resume_enabled"))
        try:
            self._resume_minutes = max(1, int(config.get("resume_minutes") or 60))
        except (TypeError, ValueError):
            self._resume_minutes = 60

        # 迁移配置
        self._migrate_target = str(config.get("migrate_target") or "").strip()
        # 键缺失（老配置）→ 用默认备用目录；显式空串 → 尊重「关闭备用」的意图
        alt_raw = config.get("migrate_target_alt")
        self._migrate_target_alt = (
            DEFAULT_MIGRATE_TARGET_ALT if alt_raw is None else str(alt_raw).strip()
        )
        self._migrate_category = str(config.get("migrate_category") or "").strip()
        self._migrate_skip_check = bool(config.get("migrate_skip_check"))
        self._migrate_delete_files = bool(config.get("migrate_delete_files"))
        self._migrate_verify = bool(config.get("migrate_verify", True))

    def get_state(self) -> bool:
        """获取插件启用状态。

        :return: 插件是否启用
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。

        :return: 命令定义列表
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。

        :return: API 定义列表
        """
        return [
            {
                "path": "/migrate_torrent",
                "endpoint": self.api_migrate_torrent,
                "methods": ["GET"],
                "summary": "迁移单个种子",
                "description": "把指定已停止种子的内容复制到目标目录，"
                               "校验通过后删除原任务并以新分类重新添加。",
            },
            {
                "path": "/migrate_all",
                "endpoint": self.api_migrate_all,
                "methods": ["GET"],
                "summary": "迁移全部已停止种子",
                "description": "把当前所有已停止的种子批量加入迁移队列。",
            },
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。

        :return: Vuetify 表单结构与默认配置
        """
        # 「监控分类」的候选项 = qB 里**实际存在**的分类。这一步是只读的，
        # 失败也给空列表、绝不抛异常 —— get_form 是打开配置弹窗时的同步路径，
        # 异常抛出去弹窗就直接打不开了。
        category_options = self.__category_options()
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval",
                                            "label": "检查间隔（秒）",
                                            "placeholder": "默认30，最小5",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "downloaders",
                                            "label": "下载器",
                                            "items": [
                                                {"title": config.name, "value": config.name}
                                                for config in DownloaderHelper().get_configs().values()
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        # 候选项取自 qB 实际分类，从「手输」改成「勾选」：
                                        # 名字只能从真实分类里挑，杜绝手敲错别字/空格导致的
                                        # 「配了但永远匹配不上」的静默失效。
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "categories",
                                            "label": "监控分类",
                                            "items": [
                                                {"title": name, "value": name}
                                                for name in category_options
                                            ],
                                            "placeholder": (
                                                "从 qB 实际分类中勾选"
                                                if category_options else "未能读取 qB 分类"
                                            ),
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "resume_enabled",
                                            "label": "启用自动恢复",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "resume_minutes",
                                            "label": "停止多久后自动恢复（分钟）",
                                            "placeholder": "默认60，最小1",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "【种子迁移】填好目标目录与新分类后，"
                                                    "插件详情页的已停止列表会多出「迁移」按钮。"
                                                    "点击后先把内容复制到目标目录，"
                                                    "校验通过再删除原任务并以新分类重新添加，"
                                                    "过程在后台进行，不阻塞页面。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "migrate_target",
                                            "label": "迁移主目标目录（qB 视角路径）",
                                            "placeholder": "例如 /download，留空则不显示迁移按钮",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "migrate_target_alt",
                                            "label": "迁移备用目标目录（主目录空间不足时自动使用）",
                                            "placeholder": "例如 /下载，留空则空间不足时不迁移",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "migrate_category",
                                            "label": "迁移后归入的分类",
                                            "placeholder": "例如 本地保种，不存在会自动创建",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "migrate_verify",
                                            "label": "复制后逐块校验",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "migrate_skip_check",
                                            "label": "重加时跳过哈希校验",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "migrate_delete_files",
                                            "label": "删除原任务时连文件一起删",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件会按设定间隔检查所选下载器中指定分类的种子，"
                                                    "一旦发现处于做种上传中（uploading）状态的种子，"
                                                    "立即将其暂停（暂停后状态变为 stoppedUP）。"
                                                    "启用自动恢复后，所有处于 stoppedUP 状态的任务"
                                                    "在停止超过设定时间后会自动重新开始。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "downloaders": [],
            "categories": [],
            "interval": 30,
            "resume_enabled": False,
            "resume_minutes": 60,
            "migrate_target": "",
            "migrate_target_alt": DEFAULT_MIGRATE_TARGET_ALT,
            "migrate_category": "",
            "migrate_skip_check": False,
            "migrate_delete_files": False,
            "migrate_verify": True,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面（方案 A1 + B1 + C3 + D4）。

        排版取向：**不再用 VAlert / VTable**，整页由四个板块的 div 拼成 ——
        A1 合并摘要条 / B1 正在上传单行卡 / C3 已停止双行行卡 / D4 队列压行。
        原结构（3 条 alert + 3 张表）实测总高 1744px、可见区只有 587px，要滚 2.9 屏；
        现在把「每条信息只讲一遍」作为准则，配置、计数、状态各归其位。

        :return: Vuetify 详情页结构
        """
        if not self._enabled:
            return None

        page_content: List[dict] = []

        downloaders_text = "、".join(self._downloaders) if self._downloaders else "未配置"
        categories_text = "、".join(self._categories) if self._categories else "未配置"

        # 实时查询各下载器中 uploading（正在上传）的种子
        uploading_info = self.__collect_uploading_torrents()
        fetch_failed = uploading_info is None
        if fetch_failed:
            # 取不到数据时只降级提示，不要 return —— 否则「已停止」列表与
            # 迁移队列也会被一起吞掉，下载器短暂掉线就看不到迁移入口了。
            uploading_info = {}
        total_uploading = sum(len(items) for items in uploading_info.values())

        stopped_info = self.__collect_stopped_torrents() or {}
        total_stopped = sum(len(items) for items in stopped_info.values())

        job_list = sorted(self.__load_jobs().values(),
                          key=lambda job: job.get("ts") or 0, reverse=True)
        failed_jobs = sum(1 for job in job_list
                          if str(job.get("state") or "") == "failed")

        # ---------------- 板块 A1 · 合并摘要条 ----------------
        # 配置原本独占一条 56px 的 alert、两条统计各占一条 —— 三条加起来 216px，
        # 而它们讲的都是「静态配置 + 两个数字」。合并成一条，数字做成右侧徽章。
        cfg_parts: List[dict] = []
        cfg_parts += _kv("监控", downloaders_text)
        cfg_parts.append(_sep())
        cfg_parts += _kv("分类", categories_text)
        cfg_parts.append(_sep())
        cfg_parts += _kv("间隔", f"{self._interval}s")
        cfg_parts.append(_sep())
        cfg_parts += _kv("自动恢复",
                         f"{self._resume_minutes}min" if self._resume_enabled else "未启用")
        if fetch_failed:
            cfg_parts.append(_sep())
            cfg_parts.append({"component": "span",
                              "props": {"style": "font-weight:600;"},
                              "text": "下载器数据获取失败"})

        count_pills = [_pill(f"上传 {total_uploading}",
                             "ok" if total_uploading else "grey")]
        if total_stopped:
            count_pills.append(_pill(f"已停止 {total_stopped}", "warn"))
        if failed_jobs:
            count_pills.append(_pill(f"失败 {failed_jobs}", "solid"))
        page_content.append(
            _bar("warning" if fetch_failed else "info", cfg_parts, count_pills))

        # ---------------- 板块 B1 · 正在上传（单行卡） ----------------
        if fetch_failed:
            page_content.append(_bar("warning", [{
                "component": "span",
                "text": "未能获取下载器数据，请检查下载器配置与连接状态。",
            }], height=44))
        else:
            groups = [(name, items) for name, items in uploading_info.items() if items]
            # 只有「一个下载器一条任务」时才不分组标题 —— 那是最常见的形态，
            # 让它保持 48px 一张卡，不为一个标签多花 30px。
            need_caption = len(groups) > 1 or any(len(items) > 1 for _, items in groups)
            for group_name, items in groups:
                if need_caption:
                    page_content.append(_group_caption(group_name, len(items)))
                for item in items:
                    page_content.append(_upload_card(item))

        # ---------------- 板块 C3 · 已停止任务（双行行卡） ----------------
        if total_stopped:
            show_migrate = bool(self._migrate_target)
            stop_time_map: Dict[str, float] = self.get_data(STOP_TIME_DATA_KEY) or {}
            now_ts = datetime.now().timestamp()

            stop_parts: List[dict] = [
                {"component": "span", "props": {"style": "font-weight:700;"},
                 "text": str(total_stopped)},
                {"component": "span", "text": " 个任务已停止"},
            ]
            if self._resume_enabled:
                stop_parts.append(_sep())
                stop_parts.append({
                    "component": "span",
                    "text": f"超 {self._resume_minutes} 分钟自动恢复"})
            if show_migrate:
                stop_parts.append(_sep())
                stop_parts.append({
                    "component": "span", "props": {"style": "opacity:.8;"},
                    "text": f"目标 {self._migrate_target} → "
                            f"{self._migrate_category or '（未设置）'}"})

            stop_right: List[dict] = []
            if show_migrate:
                # 批量迁移入口。⚠️ 这是这个页面存在的意义所在，任何降级分支
                # 都不能把它吞掉（1.5.1 修过一次，别退回去）。
                stop_right.append({
                    "component": "VBtn",
                    "props": {"size": "small", "color": "primary",
                              "variant": "flat", "style": "flex:0 0 auto;"},
                    "text": f"迁移全部已停止（{total_stopped} 个）",
                    "events": {"click": {
                        "api": f"plugin/{_PLUGIN_ID}/migrate_all",
                        "method": "get",
                        "params": {"apikey": settings.API_TOKEN},
                    }},
                })
            page_content.append(_bar("warning", stop_parts, stop_right))

            stop_rows = [
                _stop_row(item, stop_time_map, now_ts, self._resume_minutes,
                          show_migrate, downloader_name)
                for downloader_name, items in stopped_info.items()
                for item in items
            ]
            if stop_rows:
                page_content.append({
                    "component": "div",
                    "props": {"style": (
                        f"margin-bottom:{_PAGE_GAP}px;"
                        f"padding-right:{_PAGE_FAB_CHANNEL}px;"
                    )},
                    "content": stop_rows,
                })

        # ---------------- 板块 D4 · 迁移队列 ----------------
        page_content.extend(self.__render_migrate_jobs())

        # 面板在对话框右下角内侧 12px 固定着一个 56px 的齿轮悬浮按钮（VFab），它不在
        # 插件页面树里、插件改不了它。它**不随内容滚动**，所以内容右侧那一条 68px
        # 始终会被它扫过 —— 页面里排到右边的元素都得让开，不只是最后一块。
        # （各板块自己已经用 _PAGE_FAB_CHANNEL 让过位；这里再兜底一次，防止某个
        #   分支下最后一块是条信息条、右端徽章正好落在齿轮下面。）
        self.__avoid_fab(page_content)

        return page_content

    @staticmethod
    def __avoid_fab(page_content: List[dict], gap: int = _PAGE_FAB_CHANNEL) -> None:
        """把页面最下面那一块往左让出 gap 像素，避开右下角的齿轮悬浮按钮。

        :param page_content: get_page 的页面结构（原地修改）
        :param gap: 让出的宽度；实测压住区宽 48px，取 68 留余量
        """
        if not page_content:
            return
        block = page_content[-1]
        if not isinstance(block, dict):
            return

        def _pad(props: dict) -> bool:
            style = (props.get("style") or "").strip()
            if "padding-right" in style:
                return True
            props["style"] = (style + f";padding-right:{gap}px;").lstrip(";")
            return True

        # 新结构：最后一块自己就是容器 div
        if block.get("component") == "div":
            _pad(block.setdefault("props", {}))
            return

        # 旧结构兼容：VRow -> content[VCol]（VCol 直接是 VRow 的孩子，
        # 别再往 VCol 的 content 里找一层，那里是 VTable / VAlert）
        for col in block.get("content") or []:
            if isinstance(col, dict) and col.get("component") == "VCol":
                _pad(col.setdefault("props", {}))
                return

    def __collect_stopped_torrents(self) -> Optional[Dict[str, List[Dict[str, str]]]]:
        """收集各下载器中已停止（stoppedUP）状态的种子。

        :return: 下载器名称到种子明细列表的映射；获取失败时返回 None
        """
        if not self._downloaders:
            return {}

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            return None

        result: Dict[str, List[Dict[str, str]]] = {}
        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                continue

            torrents, error = downloader_obj.get_torrents()
            if error:
                continue

            items: List[Dict[str, str]] = []
            for torrent in torrents:
                if torrent.get("state") not in STOPPED_STATES:
                    continue
                items.append(
                    {
                        "category": torrent.get("category") or "(无分类)",
                        "state": torrent.get("state") or "",
                        "name": torrent.get("name") or "",
                        "hash": torrent.get("hash") or "",
                    }
                )
            result[service_name] = items

        return result

    def __collect_uploading_torrents(self) -> Optional[Dict[str, List[Dict[str, str]]]]:
        """收集各下载器中「监控分类」里 uploading（正在上传）的种子。

        只统计 self._categories 里的分类 —— 与 __pause_active_torrents 保持同一口径。
        （1.6.0 修复：此前不筛分类，别的分类里正在上传的种子也会被列出来，
        看着像「插件在监控它」，实际那些种子永远不会被暂停。）

        :return: 下载器名称到种子明细列表的映射；获取失败时返回 None
        """
        if not self._downloaders:
            return {}

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            return None

        result: Dict[str, List[Dict[str, str]]] = {}
        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                continue

            torrents, error = downloader_obj.get_torrents()
            if error:
                continue

            items: List[Dict[str, str]] = []
            for torrent in torrents:
                # 🔴 只认监控分类（与 __pause_active_torrents 同一口径）——
                # 漏了这句，详情页会把「压根本不会被暂停」的其它分类种子也列出来。
                if (torrent.get("category") or "") not in self._categories:
                    continue
                if torrent.get("state") not in ACTIVE_STATES:
                    continue
                items.append(
                    {
                        "category": torrent.get("category") or "(无分类)",
                        "state": torrent.get("state") or "",
                        "name": torrent.get("name") or "",
                    }
                )
            result[service_name] = items

        return result

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        services: List[Dict[str, Any]] = []
        if not self._enabled or not self._downloaders:
            return services

        # 暂停服务：需要配置监控分类
        if self._categories:
            services.append(
                {
                    "id": "QbCategoryPause",
                    "name": "QB分类活动监控暂停",
                    "trigger": "interval",
                    "func": self.check_and_pause,
                    "kwargs": {"seconds": self._interval},
                }
            )

        # 自动恢复服务：需要启用自动恢复
        if self._resume_enabled:
            services.append(
                {
                    "id": "QbCategoryResume",
                    "name": "QB已停止任务自动恢复",
                    "trigger": "interval",
                    "func": self.check_and_resume,
                    "kwargs": {"seconds": self._interval},
                }
            )

        return services

    def check_and_pause(self) -> None:
        """检查指定分类的活动种子并立即暂停。"""
        if not self._enabled:
            return

        if not self._downloaders or not self._categories:
            logger.warning("QB分类活动暂停：未配置下载器或监控分类，跳过检查")
            return

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("QB分类活动暂停：获取下载器实例失败，请检查配置")
            return

        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                logger.warning(f"QB分类活动暂停：下载器 {service_name} 不是 QB 类型，跳过")
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"QB分类活动暂停：下载器 {service_name} 未连接，跳过")
                continue

            self.__pause_active_torrents(service_name, downloader_obj)

    def check_and_resume(self) -> None:
        """检查所有已停止（stoppedUP）任务，超过设定时间后自动恢复。"""
        if not self._enabled or not self._resume_enabled:
            return

        if not self._downloaders:
            logger.warning("QB已停止任务自动恢复：未配置下载器，跳过检查")
            return

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("QB已停止任务自动恢复：获取下载器实例失败，请检查配置")
            return

        # 读取持久化的停止时间记录
        stop_time_map: Dict[str, float] = self.get_data(STOP_TIME_DATA_KEY) or {}
        now_ts = datetime.now().timestamp()
        threshold_seconds = self._resume_minutes * 60

        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"QB已停止任务自动恢复：下载器 {service_name} 未连接，跳过")
                continue

            torrents, error = downloader_obj.get_torrents()
            if error:
                logger.error(f"QB已停止任务自动恢复：获取下载器 {service_name} 种子失败")
                continue

            stopped_hashes: List[str] = []
            resume_hashes: List[str] = []
            resume_names: List[str] = []
            for torrent in torrents:
                torrent_hash = torrent.get("hash")
                if not torrent_hash:
                    continue
                if torrent.get("state") not in STOPPED_STATES:
                    continue

                stopped_hashes.append(torrent_hash)
                # 首次发现该停止任务时记录时间；已存在则沿用原记录
                if torrent_hash not in stop_time_map:
                    # 优先使用 QB 的最后活动时间作为停止起始时间
                    last_activity = torrent.get("last_activity")
                    stop_time_map[torrent_hash] = (
                        float(last_activity) if last_activity and last_activity > 0 else now_ts
                    )

                # 停止时间超过阈值则恢复
                if now_ts - stop_time_map[torrent_hash] >= threshold_seconds:
                    resume_hashes.append(torrent_hash)
                    resume_names.append(torrent.get("name") or torrent_hash)

            # 清理已不再处于停止状态的记录
            for torrent_hash in list(stop_time_map.keys()):
                if torrent_hash not in stopped_hashes:
                    stop_time_map.pop(torrent_hash, None)

            if not resume_hashes:
                continue

            logger.info(
                f"QB已停止任务自动恢复：下载器 {service_name} 有 {len(resume_hashes)} 个任务"
                f"停止超过 {self._resume_minutes} 分钟，准备恢复"
            )
            if downloader_obj.start_torrents(ids=resume_hashes):
                logger.info(f"QB已停止任务自动恢复：下载器 {service_name} 已恢复 {len(resume_hashes)} 个任务")
                for torrent_hash in resume_hashes:
                    stop_time_map.pop(torrent_hash, None)
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【QB已停止任务自动恢复】",
                        text=f"下载器：{service_name}\n"
                             f"停止超过 {self._resume_minutes} 分钟，已恢复 {len(resume_hashes)} 个任务：\n"
                             + "\n".join(f"- {name}" for name in resume_names[:20]),
                    )
            else:
                logger.error(f"QB已停止任务自动恢复：下载器 {service_name} 恢复任务失败")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【QB已停止任务自动恢复】",
                        text=f"下载器 {service_name} 恢复任务失败，请检查下载器状态。",
                    )

        # 持久化停止时间记录
        self.save_data(STOP_TIME_DATA_KEY, stop_time_map)

    def __pause_active_torrents(self, downloader_name: str, downloader_obj: Any) -> None:
        """暂停指定下载器中监控分类的活动种子。

        :param downloader_name: 下载器名称
        :param downloader_obj: 下载器实例
        """
        torrents, error = downloader_obj.get_torrents()
        if error:
            logger.error(f"QB分类活动暂停：获取下载器 {downloader_name} 种子失败")
            return

        if not torrents:
            return

        # 筛选出监控分类中处于做种上传中（uploading）状态的种子
        active_hashes: List[str] = []
        active_names: List[str] = []
        active_categories: List[str] = []
        for torrent in torrents:
            category = torrent.get("category") or ""
            if category not in self._categories:
                continue
            # 仅做种上传中（uploading）视为活动状态
            if torrent.get("state") in ACTIVE_STATES:
                active_hashes.append(torrent.get("hash"))
                active_names.append(torrent.get("name"))
                active_categories.append(category)

        if not active_hashes:
            # 无活动种子时清空记录
            self._last_paused_hashes = []
            return

        logger.info(
            f"QB分类活动暂停：下载器 {downloader_name} 发现 {len(active_hashes)} 个活动种子，准备暂停"
        )

        if downloader_obj.stop_torrents(ids=active_hashes):
            logger.info(f"QB分类活动暂停：下载器 {downloader_name} 已暂停 {len(active_hashes)} 个种子")
            if self._notify and active_hashes != self._last_paused_hashes:
                # 按分类统计暂停数量
                category_counter: Dict[str, int] = {}
                for category in active_categories:
                    category_counter[category] = category_counter.get(category, 0) + 1
                category_summary = "；".join(
                    f"已暂停{category} {count} 个活动种子"
                    for category, count in sorted(
                        category_counter.items(), key=lambda pair: pair[1], reverse=True
                    )
                )
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【QB分类活动暂停】",
                    text=f"下载器：{downloader_name}\n"
                         f"监控分类：{'、'.join(self._categories)}\n"
                         f"{category_summary}：\n"
                         + "\n".join(f"- {name}" for name in active_names[:20]),
                )
            self._last_paused_hashes = active_hashes
        else:
            logger.error(f"QB分类活动暂停：下载器 {downloader_name} 暂停种子失败")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【QB分类活动暂停】",
                    text=f"下载器 {downloader_name} 暂停种子失败，请检查下载器状态。",
                )

    # ------------------------------------------------------------------
    # 种子迁移：复制内容到目标目录 → 校验 → 删原任务 → 以新分类重加
    # ------------------------------------------------------------------

    @staticmethod
    def __map_qb_path(path: str) -> str:
        """把 qB 容器视角的路径换算成 MoviePilot 容器里的真实路径。

        两个容器的挂载点不同名（qB 里是 /保种，MP 里是 /CloudNAS/CloudDrive/...），
        插件跑在 MP 容器里，必须换算后才能读写。
        """
        if not path:
            return path
        for qb_prefix, mp_prefix in QB_TO_MP_PATH_MAP:
            if path == qb_prefix or path.startswith(qb_prefix + "/"):
                return mp_prefix + path[len(qb_prefix):]
        return path

    @staticmethod
    def __fmt_size(num: Any) -> str:
        """把字节数格式化成人类可读文本（用于任务说明与日志）。"""
        try:
            size = float(num or 0)
        except (TypeError, ValueError):
            return "0 B"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024:
                return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} PB"

    @staticmethod
    def __disk_free(path: str) -> Optional[Tuple[int, int, str]]:
        """取 path 所在文件系统的剩余空间。

        :param path: 允许尚不存在（例如首次迁移到某个新分类目录），
                     这时上溯到最近存在的祖先目录再探测 —— 同一分区结论一致。
        :return: (剩余字节, 总量字节, 实际用于探测的路径)；完全探测不到时返回 None
        """
        probe = path
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if not parent or parent == probe:
                break
            probe = parent
        try:
            usage = shutil.disk_usage(probe)
            return usage.free, usage.total, probe
        except Exception as e:
            logger.warning(f"QB分类活动暂停：读取 {path} 所在分区剩余空间失败：{e}")
            return None

    @staticmethod
    def __path_dev(path: str) -> Optional[int]:
        """取路径所在文件系统的设备号（用于判断两个目录是否同一分区）。"""
        try:
            return os.stat(path).st_dev
        except OSError:
            return None

    @staticmethod
    def __same_fs(dev: Optional[int], total: int, free: int,
                  primary: Optional[Tuple[Optional[int], int, int]]) -> bool:
        """判断候选目录与主目录是否落在同一文件系统。

        同一文件系统 = 备用目录带不来额外空间，配了也没用（还可能误导用户
        以为「换了个地方就有地方了」）。两个判据取或：

        1. 总量与剩余都一致 —— 同一文件系统在同一时刻读出来必然一致。
           🔴 这个判据不能省：本环境实测 `/` 与 `/下载` 的 `st_dev` **不同**
           （1048618 vs 124）却是同一块盘，只看设备号会漏判。
        2. 设备号相同。
        """
        if primary is None:
            return False
        primary_dev, primary_total, primary_free = primary
        if total == primary_total and abs(free - primary_free) < 1024 * 1024:
            return True
        return dev is not None and primary_dev is not None and dev == primary_dev

    @staticmethod
    def __scan_tree(path: str) -> Tuple[int, List[Tuple[str, str]]]:
        """统计待复制内容的**真实**字节数，顺带产出文件清单供复制阶段复用。

        用真实文件大小而不是种子报告的 size：稀疏文件、部分下载、硬链接都会
        让两者不一致，而空间校验必须按实际要写过去的字节算。
        清单一起返回是为了避免复制时再遍历一遍目录（大目录遍历很贵）。

        :return: (总字节数, [(源文件绝对路径, 相对路径)；单文件时为空列表])
        """
        if os.path.isdir(path):
            pairs: List[Tuple[str, str]] = []
            total = 0
            for root, _dirs, names in os.walk(path):
                for filename in names:
                    full = os.path.join(root, filename)
                    try:
                        total += os.path.getsize(full)
                    except OSError:
                        # 单个文件取不到大小不中断统计，复制阶段会报出具体失败
                        continue
                    pairs.append((full, os.path.relpath(full, path)))
            return total, pairs
        try:
            return os.path.getsize(path), []
        except OSError:
            return 0, []

    def __pick_target(self, src_mp: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """迁移前挑一个装得下的目标目录：主目录优先，不够就改用备用目录。

        判断标准是 `剩余空间 - 本次需要 >= MIGRATE_SPACE_RESERVE`（留安全余量，
        不是恰好卡满）。两边都不够、没配备用、或备用与主目录同分区时返回 None，
        说明文本直接写进任务记录，让用户看得见差多少。

        :return: (选中方案 或 None, 说明文本)
        """
        need, pairs = self.__scan_tree(src_mp)
        notes: List[str] = []
        candidates: List[Dict[str, Any]] = []
        primary: Optional[Tuple[Optional[int], int, int]] = None

        # 配置项本身就是 qB 视角路径，要换算成 MP 视角才能探测剩余空间
        # （两个容器挂载点不同名）。
        for index, (label, qb_path) in enumerate((("主目录", self._migrate_target),
                                                  ("备用目录", self._migrate_target_alt))):
            if not qb_path:
                continue
            mp_path = self.__map_qb_path(qb_path)
            probe = self.__disk_free(mp_path)
            if not probe:
                notes.append(f"{label} {qb_path} 读不到剩余空间")
                continue
            free, total, real = probe
            dev = self.__path_dev(real)
            if index > 0 and self.__same_fs(dev, total, free, primary):
                # 同一块盘：换个名字并不会多出空间，留着只会误导用户
                notes.append(f"{label} {qb_path} 与主目录在同一分区，按无备用处理")
                continue
            if index == 0:
                primary = (dev, total, free)
            candidates.append({"label": label, "qb": qb_path, "mp": mp_path,
                               "free": free, "total": total, "real": real,
                               "need": need})

        if not candidates:
            return None, "；".join(notes) or "没有可用的迁移目标目录"

        for candidate in candidates:
            spare = candidate["free"] - need
            if spare >= MIGRATE_SPACE_RESERVE:
                detail = (f"{candidate['label']} {candidate['qb']} 剩余 "
                          f"{self.__fmt_size(candidate['free'])}，本次需要 "
                          f"{self.__fmt_size(need)}")
                if not os.path.exists(candidate["mp"]):
                    detail += f"（目标目录尚不存在，按 {candidate['real']} 估算）"
                candidate["plan"] = (need, pairs)
                return candidate, detail
            notes.append(f"{candidate['label']} {candidate['qb']} 剩余 "
                         f"{self.__fmt_size(candidate['free'])}，需要 "
                         f"{self.__fmt_size(need)}，差 {self.__fmt_size(-spare)}")

        return None, (f"空间不足：{'；'.join(notes)}"
                      f"（另需预留 {self.__fmt_size(MIGRATE_SPACE_RESERVE)}）")

    def __qb_client(self, downloader_name: str) -> Optional[qbittorrentapi.Client]:
        """构造指定下载器的原生 qbittorrentapi 客户端。

        用原生客户端而不是 MP 的封装，是因为本流程需要两个它没暴露的能力：
        导出 .torrent（torrents_export）与跳过哈希校验（is_skip_checking）。
        """
        try:
            conf = DownloaderHelper().get_configs().get(downloader_name)
        except Exception as e:
            logger.error(f"QB分类活动暂停：读取下载器配置失败：{e}")
            return None
        if not conf:
            return None
        cfg = conf.config or {}
        try:
            client = qbittorrentapi.Client(
                host=cfg.get("host"),
                username=cfg.get("username"),
                password=cfg.get("password"),
            )
            client.auth_log_in()
            return client
        except Exception as e:
            logger.error(f"QB分类活动暂停：连接下载器 {downloader_name} 失败：{e}")
            return None

    def __category_options(self) -> List[str]:
        """配置页「监控分类」的候选项：qB 里的实际分类，外加当前已配置的分类。

        🔴 为什么要带上「当前已配置的」：qB 掉线时这里只能拿到空列表，若不兜底，
        用户原本勾好的分类会从控件里消失、看着像配置被清了。已配置的一律保留。

        查询范围：选了下载器就只查已选的；一个都没选（首次打开配置页）时查全部 ——
        否则第一次配置时下拉是空的，等于让人无从选起。

        本方法在 get_form() 里被同步调用，所以只读、不建连接、不抛异常。

        :return: 候选分类名列表（qB 里的在前，仅存在于配置里的在后）
        """
        try:
            configs = DownloaderHelper().get_configs()
        except Exception as err:
            logger.error(f"QB分类活动暂停：读取下载器列表失败：{err}")
            configs = {}

        # 已配置的下载器里，只保留确实还存在的（配置可能指向已被删掉的下载器）
        names = [name for name in self._downloaders if name in configs] or list(configs.keys())

        found: List[str] = []
        for name in names:
            for category in self.__qb_categories(name):
                if category not in found:
                    found.append(category)
        # 已在配置里的分类永远保留 —— 哪怕它在 qB 里已被改名或删除，
        # 也不能让用户的选择凭空消失（否则一保存就把监控项弄丢了）
        for category in self._categories:
            if category not in found:
                found.append(category)
        return found

    def __qb_categories(self, downloader_name: str) -> List[str]:
        """读单个下载器在 qB 里的实际分类名（升序）。

        走面板**已经缓存**的那个连接（`Qbittorrent.qbc`），不自己新建客户端 ——
        qbittorrentapi 每 auth_log_in 一次都会在 qB 侧留一个会话，而 get_form
        是「每打开一次配置弹窗就执行一次」，自己建会白白堆会话。
        只认 qbittorrent 类型：其它下载器没有「分类」这个概念。
        任何异常都吞掉并返回空列表（原因见 __category_options 的说明）。

        :param downloader_name: 下载器名称
        :return: 分类名列表；失败时空列表
        """
        try:
            services = DownloaderHelper().get_services(name_filters=[downloader_name])
        except Exception as err:
            logger.error(f"QB分类活动暂停：获取下载器 {downloader_name} 失败：{err}")
            return []
        for service_info in services.values():
            if not DownloaderHelper().is_downloader(service_type="qbittorrent",
                                                    service=service_info):
                continue
            client = getattr(service_info.instance, "qbc", None)
            if client is None:
                continue
            try:
                return sorted(str(name) for name in client.torrents_categories().keys())
            except Exception as err:
                logger.error(f"QB分类活动暂停：读取下载器 {downloader_name} 的分类失败：{err}")
        return []

    def __load_jobs(self) -> Dict[str, Dict[str, Any]]:
        with self._migrate_lock:
            return self.get_data(MIGRATE_DATA_KEY) or {}

    def __save_jobs(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        with self._migrate_lock:
            self.save_data(MIGRATE_DATA_KEY, jobs)

    def __get_job(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """读取队列里的单条任务（副本，改了不会影响存储）。"""
        if not torrent_hash:
            return None
        job = self.__load_jobs().get(torrent_hash)
        return dict(job) if job else None

    def __update_job(self, torrent_hash: str, **patch) -> None:
        """只更新队列里的**一条**任务，其余记录原样保留。

        🔴 必须「写前重新读取 + 只合并自己那几个字段」，绝不能拿一份旧快照整表回写 ——
        worker 在复制开始前 load 的那份快照不含后来新入队的任务，
        整表写回会把它们抹掉：1.5.3 及之前「复制期间点迁移，任务活不过 1 秒」就是此因。
        整个读-改-写在 `_migrate_lock` 内完成，同时点多个迁移也不会互相覆盖。
        """
        if not torrent_hash:
            return
        if patch.get("state") in ("done", "failed") and "end_ts" not in patch:
            # 记下结束时刻，供裁剪时按「真正结束的先后」保留最近若干条
            patch["end_ts"] = datetime.now().timestamp()
        with self._migrate_lock:
            jobs = self.__load_jobs()
            job = jobs.get(torrent_hash)
            if not job:
                job = {"hash": torrent_hash}
                jobs[torrent_hash] = job
            job.update(patch)
            self.__save_jobs(self.__trim_jobs(jobs, keep_hash=torrent_hash))

    @staticmethod
    def __trim_jobs(jobs: Dict[str, Dict[str, Any]],
                    keep_hash: str = None) -> Dict[str, Dict[str, Any]]:
        """收敛队列体积：进行中的任务全部保留，已结束的只留最近 MIGRATE_KEEP_FINISHED 条。

        :param keep_hash: 本条任务一定会保留（刚结束的那条，别让它被自己挤掉）
        """
        active: Dict[str, Dict[str, Any]] = {}
        finished: List[Tuple[int, Dict[str, Any]]] = []
        for index, (torrent_hash, job) in enumerate(jobs.items()):
            if job.get("state") in MIGRATE_ACTIVE_STATES:
                active[torrent_hash] = job
            else:
                finished.append((index, job))
        # 时间相同时（同一轮里批量结束）用入队次序兜底：后入队的算更新。
        # 少了这个兜底，稳定排序会把「并列里最旧的」当成最新的留下。
        finished.sort(key=lambda item: (item[1].get("end_ts")
                                        or item[1].get("ts") or 0, item[0]),
                      reverse=True)
        kept: Dict[str, Dict[str, Any]] = {}
        for _index, job in finished[:MIGRATE_KEEP_FINISHED]:
            torrent_hash = job.get("hash")
            if torrent_hash:
                kept[torrent_hash] = job
        kept.update(active)
        if keep_hash and keep_hash in jobs:
            kept[keep_hash] = jobs[keep_hash]
        return kept

    def __torrent_brief(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """在已配置的下载器里定位种子，返回它所属下载器与关键字段。"""
        services = DownloaderHelper().get_services(
            name_filters=self._downloaders or None)
        for service_name, service_info in (services or {}).items():
            if not DownloaderHelper().is_downloader(
                    service_type="qbittorrent", service=service_info):
                continue
            obj = service_info.instance
            if not obj or obj.is_inactive():
                continue
            torrents, error = obj.get_torrents(ids=[torrent_hash])
            if error or not torrents:
                continue
            torrent = torrents[0]
            return {
                "downloader": service_name,
                "name": torrent.get("name"),
                "category": torrent.get("category"),
                "size": torrent.get("size"),
                "save_path": torrent.get("save_path"),
                "content_path": torrent.get("content_path"),
            }
        return None

    def __resolve_source(self, brief: Dict[str, Any], name: str) -> str:
        """定位种子内容在磁盘上的**根**（映射成 MoviePilot 容器视角）。

        🔴 为什么不直接用 `content_path`：qB 对「种子内只有 1 个文件」的种子返回的
        是*文件*路径（哪怕该文件在磁盘上还带一层以种子名命名的顶层目录）。直接拿它
        当源根，复制阶段会被判成「单文件」而丢掉顶层目录，校验阶段再按
        `<目标>/<种子内相对路径>` 去找就报「N 个文件缺失」。

        取法（三形态通吃）：
        1. 优先 `save_path/种子名` —— 多文件种子、以及「单文件但带顶层目录」的种子，
           这一层都是磁盘上真实存在的目录；
        2. 取不到时退回 `content_path` —— 对应「文件直接躺在 save_path 下」的真单文件种子。

        :return: 源根路径；两者都取不到时返回空串
        """
        save_path = self.__map_qb_path(brief.get("save_path") or "")
        if save_path and name:
            root = os.path.join(save_path, name)
            if os.path.isdir(root):
                return root
        return self.__map_qb_path(brief.get("content_path") or "")

    def api_migrate_torrent(self, hash: str = None,
                            apikey: str = None) -> schemas.Response:
        """API：把单个种子加入迁移队列。"""
        if not self._migrate_target:
            return schemas.Response(success=False, message="未配置迁移目标目录")
        if not hash:
            return schemas.Response(success=False, message="缺少 hash 参数")
        added, _skipped = self.__enqueue([hash])
        if not added:
            return schemas.Response(success=False,
                                    message="未加入队列：该种子已在队列中，或已不存在")
        return schemas.Response(success=True,
                                message=f"已加入迁移队列，共 {added} 个任务")

    def api_migrate_all(self, apikey: str = None) -> schemas.Response:
        """API：把所有已停止的种子加入迁移队列。"""
        if not self._migrate_target:
            return schemas.Response(success=False, message="未配置迁移目标目录")
        stopped = self.__collect_stopped_torrents() or {}
        hashes = [item["hash"] for items in stopped.values()
                  for item in items if item.get("hash")]
        if not hashes:
            return schemas.Response(success=False, message="当前没有已停止的种子")
        added, skipped = self.__enqueue(hashes)
        return schemas.Response(
            success=bool(added),
            message=f"新加入 {added} 个，跳过 {skipped} 个（已在队列或已不存在）")

    def __enqueue(self, hashes: List[str]) -> Tuple[int, int]:
        """写入迁移队列，并确保后台线程在跑。

        🔴 整段「读队列 → 判断 → 写回」都在 `_migrate_lock` 内：没有锁时
        「同时点好几个迁移」的并发请求会各自读到同一份旧值，后写的赢（前面的任务消失）。
        """
        added = skipped = 0
        with self._migrate_lock:
            jobs = self.__load_jobs()
            for torrent_hash in hashes:
                if not torrent_hash:
                    continue
                exist = jobs.get(torrent_hash)
                if exist and exist.get("state") in MIGRATE_ACTIVE_STATES:
                    skipped += 1
                    continue
                brief = self.__torrent_brief(torrent_hash)
                if not brief:
                    skipped += 1
                    continue
                jobs[torrent_hash] = {
                    "hash": torrent_hash,
                    "name": brief.get("name") or torrent_hash,
                    "downloader": brief.get("downloader") or "",
                    "category": brief.get("category") or "",
                    "state": "queued",
                    "total": int(brief.get("size") or 0),
                    "done": 0,
                    "message": "排队中",
                    "ts": datetime.now().timestamp(),
                }
                added += 1
            if added:
                self.__save_jobs(self.__trim_jobs(jobs))
                self.__start_worker()
        return added, skipped

    def __start_worker(self) -> None:
        """启动后台迁移线程（已有线程在跑时不重复启动）。"""
        with self._migrate_lock:
            if self._migrate_running:
                return
            self._migrate_running = True
        threading.Thread(target=self.__migrate_worker,
                         name="QbCategoryPauseMigrate", daemon=True).start()

    def __migrate_worker(self) -> None:
        """后台工作线程：按入队顺序逐个迁移。

        🔴 「没待办 → 退出」的判断与把 `_migrate_running` 置回 False 必须在同一把锁内，
        否则中间那一小段窗口里入队的任务会两边都漏：worker 判定队列为空准备退出，
        而入队的线程此刻看到 `_migrate_running` 还是 True 就不再另起线程。
        （复制期间入队的任务由此能正常续跑，不会再丢。）
        """
        try:
            while True:
                with self._migrate_lock:
                    pending = [job for job in self.__load_jobs().values()
                               if job.get("state") == "queued"]
                    if not pending:
                        self._migrate_running = False
                        return
                pending.sort(key=lambda job: job.get("ts") or 0)
                job = pending[0]
                torrent_hash = job.get("hash")
                try:
                    self.__migrate_one(torrent_hash)
                except Exception as e:
                    logger.error(f"QB分类活动暂停：迁移 {job.get('name')} 异常：{e}")
                    self.__update_job(torrent_hash, state="failed",
                                      message=f"异常：{e}")
        finally:
            with self._migrate_lock:
                self._migrate_running = False

    def __migrate_one(self, torrent_hash: str) -> None:
        """迁移单个种子。任一步失败都保留原任务，不制造"两头空"。

        🔴 只认 hash，**不接收也不持有队列快照**：每一步写入都走 `__update_job`
        （写前重读、只改自己那条），否则会把复制期间新入队的任务整表抹掉。
        """
        job = self.__get_job(torrent_hash) or {}
        name = job.get("name") or torrent_hash

        brief = self.__torrent_brief(torrent_hash)
        if not brief:
            self.__update_job(torrent_hash, state="failed",
                              message="找不到该种子，可能已被删除")
            return

        client = self.__qb_client(brief["downloader"])
        if not client:
            self.__update_job(torrent_hash, state="failed",
                              message=f"无法连接下载器 {brief['downloader']}")
            return

        # 🔴 源根不能直接用 content_path：qB 对「种子内只有 1 个文件」的种子返回的是
        #    *文件*路径，而不是它所在的那层目录。照搬会让复制走「单文件」分支、丢掉
        #    顶层目录，随后校验按「<目标>/<种子内相对路径>」找不到文件，报「N 个文件缺失」。
        #    所以优先取「save_path/种子名」这层真目录，取不到才退回 content_path。
        src_mp = self.__resolve_source(brief, name)
        if not src_mp or not os.path.exists(src_mp):
            self.__update_job(torrent_hash, state="failed",
                              message=f"源路径不存在：{src_mp or '无法定位种子内容'}")
            return

        # 源路径正好就是主目标下的同名位置时，搬了等于原地不动。
        # ⚠️ 这是「无事可做」而不是「出错」—— 记成 done，别去占顶部那个失败计数，
        #    否则用户看到红字「失败 N」会以为插件坏了（1.7.1 修正）。
        primary_mp = self.__map_qb_path(self._migrate_target)
        if os.path.abspath(src_mp) == os.path.abspath(
                os.path.join(primary_mp, os.path.basename(src_mp.rstrip("/")))):
            self.__update_job(torrent_hash, state="done",
                              message="无需迁移：源已在主目标位置")
            return

        # ① 空间校验：剩余空间必须大于本次迁移所需（并留安全余量），
        #    主目录装不下就自动改用备用目录；两边都装不下就干脆不动 ——
        #    宁可这次不迁，也不能写爆目标盘或留下半份残文件。
        chosen, space_detail = self.__pick_target(src_mp)
        if not chosen:
            self.__update_job(torrent_hash, state="failed",
                              message=f"{space_detail}，本次未迁移")
            return
        target_qb = chosen["qb"]
        target_mp = chosen["mp"]
        # 目标名必须跟源的实际形态对齐：目录型用种子名，真单文件型用文件名。
        # 一律用种子名的话，真单文件会变成「无扩展名的裸文件」，校验必然失败。
        dst_mp = os.path.join(target_mp, os.path.basename(src_mp.rstrip("/")))
        if os.path.abspath(src_mp) == os.path.abspath(dst_mp):
            # 同上：无事可做 = done，不计入失败
            self.__update_job(torrent_hash, state="done",
                              message="无需迁移：源已在选中的目标位置")
            return

        # ② 复制（复用空间校验时已扫好的文件清单，不重复遍历目录）
        used_alt = chosen["label"] == "备用目录"
        self.__update_job(
            torrent_hash, state="copying", target=target_mp, used_alt=used_alt,
            space_need=chosen["need"], space_free=chosen["free"],
            message=f"复制到 {dst_mp}（{chosen['label']}：{space_detail}）")
        if not self.__copy_tree(src_mp, dst_mp, torrent_hash,
                                chosen.get("plan")):
            return

        # ③ 逐块校验（只比大小验不出静默损坏）
        if self._migrate_verify:
            self.__update_job(torrent_hash, state="verifying", message="逐块校验中")
            ok, detail = self.__verify_pieces(client, torrent_hash, target_mp)
            if not ok:
                self.__update_job(torrent_hash, state="failed",
                                  message=f"校验未通过（{detail}），原任务未动")
                return
            self.__update_job(torrent_hash, message=f"校验通过：{detail}")

        # ④ 导出 .torrent 备份 —— 删掉原任务后这是唯一的回滚凭据
        try:
            torrent_bytes = client.torrents_export(torrent_hash=torrent_hash)
        except Exception as e:
            self.__update_job(torrent_hash, state="failed",
                              message=f"导出种子失败：{e}")
            return
        if not torrent_bytes:
            self.__update_job(torrent_hash, state="failed",
                              message="导出种子为空，原任务未动")
            return
        try:
            backup_dir = os.path.join(self.get_data_path(), "torrents")
            os.makedirs(backup_dir, exist_ok=True)
            with open(os.path.join(backup_dir, f"{torrent_hash}.torrent"), "wb") as fp:
                fp.write(torrent_bytes)
        except Exception as e:
            logger.warning(f"QB分类活动暂停：备份种子文件失败（不阻断流程）：{e}")

        # ⑤ 确保目标分类存在
        if self._migrate_category:
            try:
                client.torrents_create_category(
                    name=self._migrate_category, save_path=target_qb)
            except Exception as e:
                logger.warning(f"QB分类活动暂停：创建分类 {self._migrate_category} "
                               f"失败（可能已存在）：{e}")

        # ⑥ 删除原任务
        self.__update_job(torrent_hash, state="swapping", message="删除原任务")
        try:
            client.torrents_delete(delete_files=self._migrate_delete_files,
                                   torrent_hashes=torrent_hash)
        except Exception as e:
            self.__update_job(torrent_hash, state="failed",
                              message=f"删除原任务失败：{e}（内容已复制，原任务未动）")
            return

        # ⑦ 以新分类重新添加
        self.__update_job(torrent_hash, message="重新添加中")
        try:
            client.torrents_add(
                torrent_files=torrent_bytes,
                save_path=target_qb,
                category=self._migrate_category or None,
                is_skip_checking=self._migrate_skip_check,
                is_paused=False,
            )
        except Exception as e:
            self.__update_job(
                torrent_hash, state="failed",
                message=(f"重新添加失败：{e}；种子已备份到插件数据目录 "
                         f"torrents/{torrent_hash}.torrent，可手动恢复"))
            return

        # ⑧ 复查生效状态
        # 🔴 结尾的说明必须带上「实际落在哪个目录、是主目录还是备用目录」：
        # 这四个阶段（copying/verifying/swapping/重新添加）会反复覆盖 message，
        # 不在这里补一句，用户翻队列就只看到「已重新添加」，根本不知道换过盘。
        where = f"{chosen['label']} {target_mp}"
        time.sleep(2)
        try:
            infos = client.torrents_info(torrent_hashes=torrent_hash)
        except Exception:
            infos = []
        if infos:
            info = infos[0]
            progress = float(info.get("progress") or 0)
            # 🔴 括号里只留「主目录 / 备用目录」这个标签：`save_path` 已经写在
            #    前一段，再把 `where`（标签 **+ 路径**）整段塞进来，页面上就会
            #    出现 `/download（主目录 /download）` 这种同一路径写两遍的冗余
            #    （2026-09-30 在真机截图上发现的）。
            self.__update_job(
                torrent_hash, state="done",
                message=(f"{info.get('state')} / {progress * 100:.0f}% / "
                         f"{info.get('category')} / {info.get('save_path')}"
                         f"（{chosen['label']}）"))
        else:
            self.__update_job(torrent_hash, state="done",
                              message=f"已重新添加（{where}），请刷新页面确认")
        logger.info(f"QB分类活动暂停：迁移完成 {name} → {dst_mp}"
                    f"（{chosen['label']}）")

    def __copy_tree(self, src: str, dst: str, torrent_hash: str,
                    plan: Optional[Tuple[int, List[Tuple[str, str]]]] = None) -> bool:
        """把源目录（或单文件）复制到目标位置，并把进度写进任务记录。

        :param plan: 空间校验阶段已扫好的 (总字节数, 文件清单)，传进来就不再重复
                     遍历目录；为 None 时自己扫一遍（留作兜底）。
        🔴 进度只回写**自己那一条**任务的 total / done（`__update_job` 写前重读）。
        不要持有队列快照整表回写 —— 那正是「复制期间点迁移，任务活不过 1 秒」的成因。
        """
        saved_at = [0.0]

        def flush(force: bool = False, **patch) -> None:
            now = time.time()
            if force or now - saved_at[0] >= 1.0:
                saved_at[0] = now
                self.__update_job(torrent_hash, **patch)

        try:
            total, pairs = plan if plan else self.__scan_tree(src)
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                flush(force=True, total=total, done=0)
                copied = 0
                for source, rel in pairs:
                    target = os.path.join(dst, rel)
                    parent = os.path.dirname(target)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    if not self.__copy_file(source, target):
                        raise IOError(f"复制失败：{source}")
                    copied += os.path.getsize(target)
                    flush(done=copied)
                flush(force=True, done=copied)
            else:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if not self.__copy_file(src, dst):
                    raise IOError(f"复制失败：{src}")
                flush(force=True, total=total, done=os.path.getsize(dst))
            return True
        except Exception as e:
            self.__update_job(torrent_hash, state="failed",
                              message=f"复制失败：{e}")
            return False

    @staticmethod
    def __copy_file(src: str, dst: str) -> bool:
        """复制单个文件。网盘挂载点上 copystat 可能失败，降级为纯内容复制。"""
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            try:
                shutil.copyfile(src, dst)
                return True
            except Exception as e:
                logger.error(f"QB分类活动暂停：复制 {src} → {dst} 失败：{e}")
                return False

    @staticmethod
    def __verify_pieces(client: qbittorrentapi.Client, torrent_hash: str,
                        files_root: str) -> Tuple[bool, str]:
        """按种子的 piece 哈希逐块校验目标目录里的文件。

        :param files_root: 目标父目录（种子内文件名是相对它的路径）
        :return: (是否通过, 说明)
        """
        try:
            props = client.torrents_properties(torrent_hash=torrent_hash)
            piece_size = int(props.get("piece_size") or 0)
            hashes = list(client.torrents_piece_hashes(torrent_hash=torrent_hash))
            files = sorted(client.torrents_files(torrent_hash=torrent_hash),
                           key=lambda f: f.get("index", 0))
        except Exception as e:
            return False, f"读取校验数据失败：{e}"
        if not piece_size or not hashes:
            return False, "种子没有可用的 piece 信息"

        buf = bytearray()
        index = missing = bad = 0
        for finfo in files:
            path = os.path.join(files_root, finfo.get("name") or "")
            if not os.path.exists(path):
                missing += 1
                continue
            with open(path, "rb") as fp:
                while True:
                    chunk = fp.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= piece_size and index < len(hashes):
                        if hashlib.sha1(bytes(buf[:piece_size])).hexdigest() \
                                != str(hashes[index]).lower():
                            bad += 1
                        del buf[:piece_size]
                        index += 1
        if buf and index < len(hashes):
            if hashlib.sha1(bytes(buf)).hexdigest() != str(hashes[index]).lower():
                bad += 1
            index += 1

        if missing:
            return False, f"{missing} 个文件缺失"
        if bad:
            return False, f"{bad} / {len(hashes)} 个数据块不一致"
        return True, f"{index} 个数据块全部一致"

    @staticmethod
    def __job_summary(job: Dict[str, Any]) -> str:
        """迁移条目第二行的小字摘要。

        「状态」「进度」已经各自成列，完成态 message 开头那两段
        （`stoppedUP / 100%`）就是重复信息，这里剥掉只留分类与落地位置：

            `stoppedUP / 100% / 本地保种 / /download（主目录）`
                -> `本地保种 · /download（主目录）`

        其余形态（排队中、逐块校验中、各种失败原因）原样保留。
        """
        msg = str(job.get("message") or "").strip()
        parts = msg.split(" / ")
        if len(parts) >= 3 and parts[1].endswith("%"):
            # 分类为空时上游会写出字面量 `None`，这里一并滤掉，别让它在页面上露脸。
            parts = [part for part in parts[2:] if part and part != "None"]
            # 🔴 1.5.8 之前写进队列的**老记录**，末段是 `/download（主目录 /download）`
            #    —— 路径写了两遍（新写入的已经只留标签）。历史记录不改库，在渲染层折叠：
            #    括号内形如「标签 路径」、且路径与括号外那段重合时，只留标签。
            if parts:
                head, sep, tail = parts[-1].rpartition("（")
                if sep and tail.endswith("）"):
                    label, _, inner = tail[:-1].partition(" ")
                    if inner and head.endswith(inner):
                        parts[-1] = f"{head}（{label}）"
            msg = " · ".join(parts)
        return msg or "—"

    def __render_migrate_jobs(self) -> List[dict]:
        """渲染迁移队列区块（方案 D4，2026-09-30 起）。

        排版取向：**异常给完整两行，正常态压成一行**。

        旧版是「一张表 20 行、每行两行文字」，实测 986px —— 占可见区 587px 的 168%，
        光这一块就要滚一屏半。而 20 行里 19 行状态是 `done`、进度是 `100.0%`，
        16 行的副标题一字不差都是「本地保种 · /download（主目录）」：
        病根不是行高，是**信息重复**。所以这里按状态分流：

          · 失败 / 进行中 → 两行（种子名 + 原因或落地位置），进度只留百分比；
          · 完成态       → 一行 36px，只留名字 + 徽章 + 百分比，最多平铺 5 条；
          · 其余完成态   → 折成一条绿色汇总行（「✓ 其余 N 条已完成 · 最近：…」）。

        折叠行是**静态文案**，没有做「点击展开」：面板的页面树没有可用的展开组件，
        而插件自己翻转状态后页面不会自动重取，做出来会是个点了没反应的假按钮。
        """
        jobs = self.__load_jobs()
        if not jobs:
            return []

        ordered = sorted(jobs.values(),
                         key=lambda job: job.get("ts") or 0, reverse=True)[:20]
        return _queue_blocks(ordered, self.__job_summary)

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        self._last_paused_hashes = []
