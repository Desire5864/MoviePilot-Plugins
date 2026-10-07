import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils

# 完成记录持久化键名
COMPLETED_DATA_KEY = "hr_completed_map"
# H&R 周期记录键名：记录每个任务最近一次在站点 H&R 页面看到的保种周期。
# 站点「未列出某任务」并不等于「该任务已完成保种」——站点从下载达标到登记
# H&R 记录存在延迟，因此必须用保种周期作为删除前的硬闸门。
HR_CYCLE_DATA_KEY = "hr_cycle_map"
# HR 统计阈值：站点规则为「HR 种子下载大于等于 50% 时需完成规定保种时间」
HR_PROGRESS_THRESHOLD = 0.5
# 兜底 H&R 周期（小时）：站点从未给出周期时使用，默认 5 天。
# 宁可多保种，也不要在保种未达标时误删任务。
HR_DEFAULT_CYCLE_HOURS = 120.0
# 保种时长闸门的安全余量：闸门要求「周期 + 余量」才放行，兜住站点登记延迟与
# 标题匹配瞬时失败两类风险。
# 原为固定 2 小时（顾及本站点 H&R 计数比 QB seeding_time 慢约 2 小时），
# v2.0.6 起按用户要求下调为 0.5 小时 —— 5 天周期门槛即 120.5 小时。
# 注意：余量越小，越依赖「站点登记不延迟」这一前提；下方保险闸门②③
# （匹配失效冻结 / 必须曾在站点出现过）仍是主要防线。
# （v1.9.1/v1.9.2 曾按周期的 5% 追加余量，5 天周期要拖到 126 小时才删，过于保守。）
HR_CYCLE_MARGIN_RATIO = 0.0
HR_CYCLE_MARGIN_MIN_HOURS = 0.5
# UHD原盘自动下载 插件ID（用于读取副标题与种子标题）
UHD_PLUGIN_ID = "UhdBlurayAutoDownload"
# UHD原盘自动下载 插件的已处理记录键名
UHD_PROCESSED_DATA_KEY = "uhd_processed_map"

# ─────────────────────── 通知正文长度控制 ───────────────────────
# 单条通知里最多列出的任务数
NOTIFY_NAME_LIMIT = 20
# 通知正文长度上限：各通知渠道上限不一（Telegram 为 4096），此处留出余量。
# 正常情况下任务名**完整展示、不做单条截断**；只有整体超长时才减少列出条数。
NOTIFY_TEXT_MAX = 3600

# ── 删除保险 ②：匹配失效冻结 ──────────────────────────────────────
# 「已完成」只能靠「站点 H&R 页面查无」反推，一旦站点与本地的标题匹配失效
# （站点改版／标题格式变化），所有任务会同时变成「查无」并成批进入删除流程，
# 而「解析 0 条」「条数骤降」两道保护只看站点条数，拦不住这种情况。
# 判据：上一轮还能正常匹配到若干任务，本轮参与比对的任务一个都匹配不上。
MATCH_STATS_DATA_KEY = "hr_match_stats"
MATCH_FAIL_MIN_PREV = 2      # 上一轮至少匹配到 N 个，才说明「此前匹配是正常的」
MATCH_FAIL_MIN_LOCAL = 2     # 本轮参与比对的本地任务至少 N 个，判定才有意义

# ── 标题匹配：片名特征（v2.0.1） ────────────────────────────────────
# 原盘种子名里绝大部分是规格词（分辨率 / 容器 / 编码 / 音轨 / 发布组），片名只
# 占 2~4 个词。若直接按「全词交集比例」评分，同规格、同发布组的任意两部电影都
# 能拿到 0.8 以上 —— 实测 Evil.Dead.Burn.2026 与 Made 2001 的共有词有 9 个、
# 全部是规格词（9 / min(13, 11) = 0.818 ≥ 0.8），于是**已完成**的种子被误配到
# 别人的 H&R 记录上，表现为「站点仍在考核」而永远不进删除流程（漏删）。
# 规则：片名特征 = 第一个规格锚点之前的词（剔除停用词）；匹配时要求两侧片名
# 特征至少共享一个词，「片名相等 / 互含」两条快路径同样受此约束。
SPEC_ANCHOR_TOKENS = frozenset({
    # 分辨率 / 片源
    "2160p", "1080p", "720p", "576p", "480p", "4k", "8k", "uhd", "hd", "sd",
    "bluray", "blu", "bd", "bdrip", "brrip", "web", "webdl", "webrip",
    "remux", "hdtv", "dvdrip", "dvd", "hdrip", "iso", "mkv", "mp4", "ts", "avi",
    # 画质 / 编码
    "hdr", "hdr10", "hdr10plus", "dovi", "dv", "sdr", "10bit", "8bit",
    "hevc", "h264", "h265", "avc", "x264", "x265", "av1", "mpeg2", "vc1",
})
NAME_STOP_TOKENS = frozenset({
    "the", "a", "an", "of", "on", "in", "and", "or", "to", "for", "with",
    "at", "by", "from", "v2", "v3", "repack", "proper",
})
# 片名区里的纯年份 token（如 2026 / 2018）：不参与片名特征计算。
# 它虽然出现在规格锚点之前，却完全没有区分度 —— 同一年、同规格、同发布组的
# 两部片子能仅凭这一个词通过片名闸门，再被兜底评分判成同一个种子
# （实测「揭秘日」与「火遮眼」共有 14 个 token、评分 0.875 ≥ 0.8，
#   导致前者读到后者的站点做种时间，且永远不进完成流程）。v2.0.3 修。
YEAR_TOKEN_RE = re.compile(r"^(?:19|20)\d{2}$")

# ── 删除保险 ⑤：删除留档 ─────────────────────────────────────────
DELETE_LOG_DATA_KEY = "hr_deleted_log"
DELETE_LOG_LIMIT = 50        # 存储条数（保证事后可追溯）
DELETE_LOG_DISPLAY = 5       # 页面展示条数（v2.0.10 曾提到 10；v2.0.11 回到 5 —— 内容区改成等高，10 条会把弹窗撑太高）

# ── 详情页视图切换（保种任务 / 最近删除）────────────────────────────
# 详情页组件树的 props 是**单向 v-bind、没有 v-model**（见 PageRender.vue），
# 所以「点一下换一个界面」这类状态只能落在后端：点分段 → 调 switch_view API 存值
# → 前端整页重载 → get_page() 按存下的值只渲染一个视图。
PAGE_VIEW_DATA_KEY = "page_view"
PAGE_VIEW_HR = "hr"          # 视图①保种任务（默认，也是值非法时的回落）
PAGE_VIEW_DEL = "del"        # 视图②最近删除


def format_name_list_text(names: List[str]) -> str:
    """把任务名列表渲染成通知正文。

    优先完整展示每个任务名（不再按 60 字截断）；仅当整体长度超过
    NOTIFY_TEXT_MAX 时，才从末尾递减列出条数，并追加「…等 N 个」说明。
    """
    total = len(names)
    shown = list(names[:NOTIFY_NAME_LIMIT])
    while shown:
        hidden = total - len(shown)
        body = "\n".join(f"- {name}" for name in shown)
        if hidden > 0:
            body += f"\n…等 {hidden} 个"
        if len(body) <= NOTIFY_TEXT_MAX:
            return body
        shown.pop()
    return f"（共 {total} 个任务，名称过长未逐条列出）"

# ─────────────────────── 详情页卡片样式（清爽风） ───────────────────────
# 状态色：与明暗主题均有足够对比度的中间调
COLOR_OK = "#2e9e5b"      # 达标 / 已完成
COLOR_WARN = "#e08a00"    # 进行中 / 未达标
COLOR_DANGER = "#e5484d"  # 紧急 / 即将删除 / 失败
COLOR_INFO = "#3b82f6"    # 提示
COLOR_IDLE = "#8a8f98"    # 无数据 / 已跳过
# 待复核（站点首次未列出、尚未开始计时）。用靛紫而非蓝：进度环的 0% 端就是蓝色，
# 同色会让「刚下载完」和「等待复核」两种完全不同的状态撞色。
COLOR_PENDING = "#7c5cff"

# 完成度环形配色（方案 ⑥ · 莫兰迪 雾蓝 → 雾青 → 橄榄绿）
# 越接近完成越绿；红色不再用于进度，只留给「即将删除」的倒计时那类真紧急。
# 整条色阶统一低饱和（30%~34%）：相邻完成度仍可分辨，但不会像高饱和色那样抢眼，
# 与 Vuetify 明暗主题都比较协调，长时间盯着不刺眼。
# 每档为 (完成度%, 色相, 饱和度%, 明度%)，区间内线性插值。
RING_COLOR_STOPS = (
    (0.0, 205.0, 30.0, 58.0),    # 雾蓝：刚起步
    (55.0, 170.0, 30.0, 52.0),   # 雾青：过半
    (100.0, 135.0, 34.0, 44.0),  # 橄榄绿：达标
)

# 卡片外壳：浅描边 + 低饱和底，去掉重度毛玻璃与投影
CARD_STYLE = (
    "padding: 12px 14px; margin-bottom: 10px; border-radius: 12px; "
    "background: rgba(var(--v-theme-surface-variant), 0.10); "
    "border: 1px solid rgba(var(--v-theme-on-surface), 0.08);"
)
# 卡片标题：允许换行，但不在单词中间断开
CARD_TITLE_STYLE = (
    "flex: 1 1 auto; min-width: 0; font-size: 14px; font-weight: 600; "
    "line-height: 1.45; word-break: break-word; overflow-wrap: anywhere;"
)
# 卡片次级说明行
CARD_CAPTION_STYLE = (
    "margin-top: 4px; font-size: 12px; line-height: 1.5; opacity: 0.6; "
    "word-break: break-word; overflow-wrap: anywhere;"
)
# 底部指标胶囊容器
CARD_PILLS_STYLE = "margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px;"

# 「待删除」专属卡片外壳：整卡红色描边 + 淡红底。
# 待删除是唯一「会真正删除本地文件、且只剩抢救窗口」的状态，只让右上角 chip
# 变色不足以在长列表里被一眼扫到；这里把整张卡片染红，滚屏时余光即可发现。
# 透明度刻意压低（底色 7%、描边 42%），保证醒目但不刺眼，明暗主题都成立。
# 注：此处写死 rgba 字面量而非调用 _rgba()，因为 _rgba 定义在本常量之后。
CARD_STYLE_DANGER = (
    "padding: 12px 14px; margin-bottom: 10px; border-radius: 12px; "
    "background: rgba(229, 72, 77, 0.07); "
    "border: 1px solid rgba(229, 72, 77, 0.42);"
)
# 「待删除」标题：跟随卡片转浅红，与普通卡片的默认色标题区分开。
# 用偏亮的浅红（#ffd9da）而非纯 #e5484d：纯红字压在淡红底上会发闷，
# 浅红既有警示意味又保证可读性。
CARD_TITLE_STYLE_DANGER = (
    "flex: 1 1 auto; min-width: 0; font-size: 14px; font-weight: 600; "
    "line-height: 1.45; word-break: break-word; overflow-wrap: anywhere; "
    "color: #ffd9da;"
)

# 「最近删除」时间轴的轴线 / 节点 / 计数强调色。
# 用字面量而非 var(--v-theme-primary)：插件其余强调色一律字面量，
# 且主题变量万一缺失会让整条 rgb() 声明失效、时间轴节点直接消失。
COLOR_TIMELINE = "#8d51f9"


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化为便于阅读的时长文本（如 "3天04:30"、"5.2h"）。"""
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "-"
    if total < 3600:
        return f"{total / 60:.0f}分钟"
    hours = total / 3600
    if hours < 24:
        return f"{hours:.1f}h"
    days = int(hours // 24)
    return f"{days}天{hours - days * 24:04.1f}h"


def _parse_duration_hours(text: Any) -> Optional[float]:
    """把时长文本尽力解析为小时数，**仅用于排序**，解析失败返回 None。

    比 `__parse_seeding_hours` 更宽松，因为同一处「做种」字段有两种来源：
      1. 站点 H&R 页面原文 —— `"3天02:48:29"`、`"14:38:16"`；
      2. 插件自身的 `_fmt_duration()` 输出 —— `"3天04.5h"`、`"5.2h"`、`"42分钟"`。
    后者中的 `h` / `分钟` 形式原解析器不认，若沿用会导致「保种中」这类记录
    被误判成数据缺失而全部沉底，排序结果与直觉不符。

    :param text: 时长文本（可为 None / 数字 / 字符串）
    :return: 小时数；无法解析返回 None
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw or raw == "-":
        return None
    rest = raw
    total = 0.0
    matched = False
    # 天
    match = re.search(r'([\d.]+)\s*天', rest)
    if match:
        total += float(match.group(1)) * 24
        rest = rest[match.end():]
        matched = True
    # 小时
    match = re.search(r'([\d.]+)\s*(?:小时|h|H)', rest)
    if match:
        total += float(match.group(1))
        rest = rest[match.end():]
        matched = True
    # 分钟
    match = re.search(r'([\d.]+)\s*分钟?', rest)
    if match:
        total += float(match.group(1)) / 60
        rest = rest[match.end():]
        matched = True
    # 时:分[:秒]
    match = re.search(r'(\d+):(\d+)(?::(\d+))?', rest)
    if match:
        total += int(match.group(1))
        total += int(match.group(2)) / 60
        if match.group(3):
            total += int(match.group(3)) / 3600
        matched = True
    return total if matched else None


def _rgba(hex_color: str, alpha: float) -> str:
    """把 #RRGGBB 转成 rgba() 字符串。"""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        return hex_color
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha})"


def _hsl_to_hex(hue: float, saturation: float, lightness: float) -> str:
    """HSL → #RRGGBB。

    环形配色在 HSL 空间插值最自然（色相单调推进），但最终要输出 hex，
    因为 `_rgba()` 只解析 6 位 hex，chip / pill 的半透明底色都依赖它。

    :param hue: 色相 0-360
    :param saturation: 饱和度 0-100
    :param lightness: 明度 0-100
    :return: `#rrggbb`
    """
    h = (hue % 360) / 360.0
    s = max(0.0, min(100.0, saturation)) / 100.0
    l = max(0.0, min(100.0, lightness)) / 100.0

    def hue2rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1
        elif t > 1:
            t -= 1
        if t < 1.0 / 6.0:
            return p + (q - p) * 6.0 * t
        if t < 0.5:
            return q
        if t < 2.0 / 3.0:
            return p + (q - p) * (2.0 / 3.0 - t) * 6.0
        return p

    if s <= 0:
        red = green = blue = l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2 * l - q
        red = hue2rgb(p, q, h + 1.0 / 3.0)
        green = hue2rgb(p, q, h)
        blue = hue2rgb(p, q, h - 1.0 / 3.0)
    return "#%02x%02x%02x" % (round(red * 255), round(green * 255), round(blue * 255))


def _ring_color(percent: float) -> str:
    """完成度 → 环形颜色（方案 ⑥ · 莫兰迪 雾蓝 → 雾青 → 橄榄绿）。

    连续插值，不做分档：相邻进度的颜色差异肉眼可辨，也不会出现
    「24% 与 26% 差一档」这类边界争议。

    :param percent: 完成度 0-100
    :return: `#rrggbb`
    """
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    for i in range(len(RING_COLOR_STOPS) - 1):
        p0, h0, s0, l0 = RING_COLOR_STOPS[i]
        p1, h1, s1, l1 = RING_COLOR_STOPS[i + 1]
        if p0 <= pct <= p1:
            t = 0.0 if p1 == p0 else (pct - p0) / (p1 - p0)
            return _hsl_to_hex(h0 + (h1 - h0) * t,
                               s0 + (s1 - s0) * t,
                               l0 + (l1 - l0) * t)
    last = RING_COLOR_STOPS[-1]
    return _hsl_to_hex(last[1], last[2], last[3])


def _status_chip(text: str, color: str, solid: bool = False) -> dict:
    """卡片右上角状态徽标。

    :param text: 徽标文字
    :param color: 主题色
    :param solid: True 时用实心底 + 白字（用于「待删除」这类必须一眼看到的状态），
                  False 时用 15% 透明底 + 同色字（默认，较克制）
    """
    if solid:
        style = ("flex: 0 0 auto; padding: 2px 10px; border-radius: 999px; "
                 "font-size: 11px; line-height: 18px; font-weight: 600; "
                 "white-space: nowrap; color: #fff; "
                 f"background: {color};")
    else:
        style = ("flex: 0 0 auto; padding: 2px 10px; border-radius: 999px; "
                 "font-size: 11px; line-height: 18px; font-weight: 600; "
                 "white-space: nowrap; "
                 f"color: {color}; background: {_rgba(color, 0.15)};")
    return {
        "component": "div",
        "props": {"style": style},
        "text": text,
    }


def _caption(text: str) -> dict:
    """卡片次级说明行。"""
    return {
        "component": "div",
        "props": {"style": CARD_CAPTION_STYLE},
        "text": text,
    }


def _pill(text: str, color: str = "", strong: bool = False) -> dict:
    """底部指标胶囊；传 color 则高亮，传 strong 则加粗。"""
    style = ("padding: 2px 9px; border-radius: 999px; font-size: 11px; "
             "line-height: 16px; white-space: nowrap; ")
    if color:
        style += (f"font-weight: 600; color: {color}; "
                  f"background: {_rgba(color, 0.14)};")
    elif strong:
        style += ("font-weight: 600; "
                  "background: rgba(var(--v-theme-on-surface), 0.10);")
    else:
        style += ("opacity: 0.8; "
                  "background: rgba(var(--v-theme-on-surface), 0.06);")
    return {"component": "div", "props": {"style": style}, "text": text}


def _ring(percent: float, color: str, text: str, size: int = 46) -> dict:
    """完成度／倒计时环形。

    纯 div + conic-gradient 实现，不依赖 SVG 组件（MP 详情页只渲染
    Vuetify 组件名，自定义 SVG 标签无法渲染）。外圈画环带，内圈用
    主题背景色遮出「甜甜圈」空心，中心显示数字。

    :param percent: 环带填充比例，自动裁剪到 0-100
    :param color: 环带颜色
    :param text: 中心文字（如 "85%" / "1.2h"）
    :param size: 外径（px）
    :return: 环形组件结构
    """
    ratio = max(0.0, min(100.0, float(percent)))
    inner = size - 10
    return {
        "component": "div",
        "props": {
            "style": f"flex: 0 0 {size}px; width: {size}px; height: {size}px; "
                     f"margin-top: 2px; border-radius: 50%; "
                     f"background: conic-gradient({color} 0 {ratio:.1f}%, "
                     f"rgba(var(--v-theme-on-surface), 0.10) {ratio:.1f}% 100%);",
        },
        "content": [
            {
                "component": "div",
                "props": {
                    "style": f"margin: 5px; height: {inner}px; border-radius: 50%; "
                             f"background: rgb(var(--v-theme-surface)); "
                             f"display: flex; align-items: center; "
                             f"justify-content: center; font-size: 11px; "
                             f"font-weight: 700; color: {color}; line-height: 1;",
                },
                "text": text,
            }
        ],
    }


def _ring_card(ring: dict, title: str, chip: Optional[dict] = None,
               captions: Optional[List[str]] = None,
               pills: Optional[List[dict]] = None,
               danger: bool = False) -> dict:
    """环形卡片：左侧环形 + 右侧（标题 + 状态徽标 / 次级说明 / 指标胶囊）。

    :param danger: True 时整卡套用「待删除」配色（红描边 + 淡红底 + 浅红标题）
    """
    title_style = CARD_TITLE_STYLE_DANGER if danger else CARD_TITLE_STYLE
    header = [{"component": "div", "props": {"style": title_style}, "text": title}]
    if chip:
        header.append(chip)

    body: List[dict] = [
        {
            "component": "div",
            "props": {"style": "display: flex; align-items: flex-start; gap: 8px;"},
            "content": header,
        }
    ]
    for text in captions or []:
        body.append(_caption(text))
    if pills:
        body.append({
            "component": "div",
            "props": {"style": CARD_PILLS_STYLE},
            "content": pills,
        })

    return {
        "component": "div",
        "props": {"style": CARD_STYLE_DANGER if danger else CARD_STYLE},
        "content": [
            {
                "component": "div",
                "props": {"style": "display: flex; gap: 12px; align-items: flex-start;"},
                "content": [
                    ring,
                    {
                        "component": "div",
                        "props": {"style": "flex: 1 1 auto; min-width: 0;"},
                        "content": body,
                    },
                ],
            }
        ],
    }



# ─────────────────── 「最近删除」时间轴版式（v2.0.7） ───────────────────
# 区块外壳刻意不用信息蓝：它是「历史流水」，不是需要立刻处理的告警，
# 视觉重量必须明确低于上方任务卡，但仍沿用同一套卡片语言（同底色、同描边）。
CARD_TIMELINE_STYLE = (
    "padding: 12px 16px 4px; border-radius: 12px; "
    "background: rgba(var(--v-theme-surface-variant), 0.10); "
    "border: 1px solid rgba(var(--v-theme-on-surface), 0.08);"
)
TIMELINE_HEAD_STYLE = "display: flex; align-items: center; gap: 9px; margin-bottom: 8px;"
TIMELINE_TITLE_STYLE = (
    "flex: 1 1 auto; min-width: 0; font-size: 14px; font-weight: 700; "
    "line-height: 1.5; word-break: break-word;"
)
TIMELINE_HINT_STYLE = "flex: 0 0 auto; font-size: 11.5px; line-height: 1.5; opacity: 0.45;"
TIMELINE_ROW_STYLE = "display: flex; align-items: stretch;"
# 日期列：日期在上、时间在下，右对齐 + 等宽数字，5 条记录的时刻能扫成一列
TIMELINE_DATE_STYLE = (
    "flex: 0 0 78px; padding: 1px 14px 0 0; text-align: right; "
    "font-variant-numeric: tabular-nums;"
)
TIMELINE_DATE_MAIN = "font-size: 12.5px; font-weight: 700; line-height: 1.6; opacity: 0.78;"
TIMELINE_DATE_SUB = "font-size: 11px; line-height: 1.5; opacity: 0.45;"
# 轴线：竖向 flex 的「上段｜圆点｜下段」三段拼成，不用绝对定位与伪元素。
# `align-self: stretch` 让轴线列撑满整行高度，下段用 flex-grow 吃掉剩余空间。
TIMELINE_RAIL_STYLE = (
    "flex: 0 0 13px; align-self: stretch; display: flex; "
    "flex-direction: column; align-items: center; box-sizing: border-box;"
)
TIMELINE_RAIL_LINE_TOP = (
    "flex: 0 0 5px; width: 1px; "
    "background: rgba(var(--v-theme-on-surface), 0.18);"
)
TIMELINE_RAIL_LINE_BOTTOM = (
    "flex: 1 1 auto; width: 1px; "
    "background: rgba(var(--v-theme-on-surface), 0.18);"
)
TIMELINE_RAIL_DOT = (
    "flex: 0 0 7px; width: 7px; height: 7px; border-radius: 50%; "
    f"background: {COLOR_TIMELINE}; "
    f"box-shadow: 0 0 0 3px {_rgba(COLOR_TIMELINE, 0.14)};"
)
TIMELINE_BODY_STYLE = "flex: 1 1 auto; min-width: 0; padding: 0 0 11px 12px;"
# 片名单行省略：4K 原盘名动辄 90+ 字符，折行正是旧版对不齐列的病根
TIMELINE_NAME_STYLE = (
    "font-size: 13px; font-weight: 600; line-height: 1.45; "
    "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
)
TIMELINE_META_STYLE = (
    "margin-top: 2px; font-size: 11.5px; line-height: 1.5; opacity: 0.55; "
    "font-variant-numeric: tabular-nums;"
)


def _clock_glyph(size: int = 15) -> dict:
    """小号时钟图标（纯 div 绘制）。

    MP 详情页只渲染 Vuetify 组件与原生标签，自绘 `<svg>` 不会渲染
    （详见 `_ring()` 的说明），所以图标也用 div 拼：一个圆环 + 两根指针。
    指针长度 / 粗细全部按 `size` 等比推算，改尺寸不用同步改坐标。

    :param size: 图标外径（px）
    :return: 图标组件结构
    """
    stroke = "rgb(var(--v-theme-on-surface))"
    hand = round(size / 10.0, 1)          # 15px → 1.5px
    center = size / 2.0
    axis = f"{center - hand / 2:g}"
    return {
        "component": "div",
        "props": {
            "style": f"position: relative; flex: 0 0 {size}px; "
                     f"width: {size}px; height: {size}px; opacity: 0.55;",
        },
        "content": [
            {
                "component": "div",
                "props": {
                    "style": f"position: absolute; top: 0; left: 0; "
                             f"width: {size}px; height: {size}px; "
                             f"box-sizing: border-box; border: {hand}px solid {stroke}; "
                             f"border-radius: 50%;",
                },
            },
            {
                # 竖直指针：从 22% 处往下扎，末端停在圆心
                "component": "div",
                "props": {
                    "style": f"position: absolute; left: {axis}px; "
                             f"top: {size * 0.22:g}px; width: {hand}px; "
                             f"height: {size * 0.30:g}px; background: {stroke};",
                },
            },
            {
                # 水平指针：从圆心往右
                "component": "div",
                "props": {
                    "style": f"position: absolute; left: {axis}px; top: {axis}px; "
                             f"width: {size * 0.26:g}px; height: {hand}px; "
                             f"background: {stroke};",
                },
            },
        ],
    }


def _deleted_log_block(rows: List[Dict[str, Any]], total: int) -> dict:
    """构造「最近删除」留档区块（时间轴版式）。

    为什么不再用 `VAlert` + 多行文本：`white-space: pre-line` 下长片名一折行，
    紧跟其后的「删除时间／本地做种／周期」就被挤到下一行，5 条记录会渲染成
    11 行，且字段与字段之间对不上列。这里改成三段式「日期列｜轴线｜名称 + 指标」：
    日期与指标各自成列、片名超长走省略号，**每条恒定两行**。

    :param rows: 待展示的留档记录（已按删除时间倒序裁剪）
    :param total: 留档总条数（含未展示的）
    :return: VRow 组件结构
    """
    items: List[dict] = []
    last_index = len(rows) - 1
    for index, rec in enumerate(rows):
        stamp = str(rec.get("deleted_time") or "")
        # 「2026-10-05 02:43:46」→ 日期列拆成上下两行「10-05」「02:43:46」
        date_text = stamp[5:10] if len(stamp) >= 10 else (stamp or "-")
        time_text = stamp[11:19] if len(stamp) >= 19 else ""
        date_lines: List[dict] = [
            {"component": "div", "props": {"style": TIMELINE_DATE_MAIN}, "text": date_text}
        ]
        if time_text:
            date_lines.append(
                {"component": "div", "props": {"style": TIMELINE_DATE_SUB}, "text": time_text}
            )

        # 最后一条不画「下段」：竖线自然收在节点处，不会孤零零拖到区块底部
        rail: List[dict] = [
            {"component": "div", "props": {"style": TIMELINE_RAIL_LINE_TOP}},
            {"component": "div", "props": {"style": TIMELINE_RAIL_DOT}},
        ]
        if index != last_index:
            rail.append({"component": "div", "props": {"style": TIMELINE_RAIL_LINE_BOTTOM}})

        items.append(
            {
                "component": "div",
                "props": {"style": TIMELINE_ROW_STYLE},
                "content": [
                    {
                        "component": "div",
                        "props": {"style": TIMELINE_DATE_STYLE},
                        "content": date_lines,
                    },
                    {
                        "component": "div",
                        "props": {"style": TIMELINE_RAIL_STYLE},
                        "content": rail,
                    },
                    {
                        "component": "div",
                        "props": {"style": TIMELINE_BODY_STYLE},
                        "content": [
                            {
                                "component": "div",
                                "props": {"style": TIMELINE_NAME_STYLE},
                                "text": str(rec.get("name") or "-"),
                            },
                            {
                                "component": "div",
                                "props": {"style": TIMELINE_META_STYLE},
                                "text": f"本地做种 {rec.get('local_seeding') or '-'}"
                                        f" · 周期 {rec.get('hr_cycle') or '-'}",
                            },
                        ],
                    },
                ],
            }
        )

    hint = f"留档上限 {DELETE_LOG_LIMIT} 条"
    # `rows` 为空时不能写「显示最新 0 条」：调用方只在留档非空时才进来，
    # 但这个函数本身要能安全处理空输入（回归 C 组专门盯这一条）。
    if rows and total > len(rows):
        hint += f" · 显示最新 {len(rows)} 条"

    return {
        "component": "VRow",
        "content": [
            {
                "component": "VCol",
                "props": {"cols": 12},
                "content": [
                    {
                        "component": "div",
                        "props": {"style": CARD_TIMELINE_STYLE},
                        "content": [
                            {
                                "component": "div",
                                "props": {"style": TIMELINE_HEAD_STYLE},
                                "content": [
                                    _clock_glyph(),
                                    {
                                        "component": "div",
                                        "props": {"style": TIMELINE_TITLE_STYLE},
                                        "content": [
                                            {"component": "span", "text": "最近删除 "},
                                            {
                                                "component": "span",
                                                "props": {
                                                    "style": f"color: {COLOR_TIMELINE};"
                                                },
                                                "text": str(total),
                                            },
                                            {"component": "span", "text": " 个任务"},
                                        ],
                                    },
                                    {
                                        "component": "div",
                                        "props": {"style": TIMELINE_HINT_STYLE},
                                        "text": hint,
                                    },
                                ],
                            },
                            *items,
                        ],
                    }
                ],
            }
        ],
    }


# ─────── 详情页视图切换条（分段胶囊 · v2.0.8，2.0.9 修字色，2.0.11 视图等高） ───────
# 观感对齐 iOS segmented：整条做成一个胶囊容器，选中项白底浮起。
# 配色沿用卡片那一族（surface-variant 低透明度），视觉重量低于任务卡。
SEG_TRACK_STYLE = (
    "display: inline-flex; align-items: center; gap: 2px; padding: 3px; "
    "border-radius: 10px; background: rgba(var(--v-theme-on-surface), 0.06); "
    "border: 1px solid rgba(var(--v-theme-on-surface), 0.06);"
)
# 两个分段共用同一套骨架；选中/未选中的差异（背景、字色、字重、投影）追加在后面，
# 同一属性后写的胜出 —— 所以不用拆成两套常量，也不会出现「改了骨架漏改一半」。
SEG_BTN_STYLE = (
    "min-width: 0; min-height: 28px; height: 28px; padding: 0 16px; "
    "border-radius: 8px; font-size: 13px; font-weight: 600; "
    "letter-spacing: 0; text-transform: none; box-shadow: none; "
    "font-variant-numeric: tabular-nums;"
)
SEG_BTN_STYLE_ON = (
    " background: rgb(var(--v-theme-surface)); "
    # 🔴 必须带 !important：Vuetify 会给 VBtn 自动加 text-primary 类，
    #    而 `.text-primary { color: rgb(var(--v-theme-primary)) !important }`
    #    带 !important，会压过这里没加 !important 的 color（2026-10-07 真机实测）。
    "color: rgb(var(--v-theme-primary)) !important; font-weight: 700; "
    "box-shadow: 0 1px 3px rgba(0, 0, 0, 0.16);"
)
SEG_BTN_STYLE_OFF = (
    # 🔴 同上：未选中是「透明底 + 62% 灰」，字色不加 !important 会被染成主色，
    #    真机上看着像两段都是选中态（只剩底色与字重两处差异）。
    " background: transparent; "
    "color: rgba(var(--v-theme-on-surface), 0.62) !important;"
)


def _seg_button(label: str, view: str, active: bool, plugin_id: str) -> dict:
    """分段胶囊里的一个分段。

    🔴 必须用 `VBtn`：MP 详情页只给 `VBtn` 绑得动 `events.click`，
       自己用 `div` / `span` 画的「按钮」点了没反应。
    🔴 `params` 是静态字典，事件回调解不到「点的是哪个」⇒ 每个分段写死自己的 `view`。
    🔴 路由默认鉴权是 `apikey`（`_update_plugin_api_routes` 里
       `api.pop("auth", "apikey")`），所以必须显式带上 `apikey`。
    """
    return {
        "component": "VBtn",
        "props": {
            "variant": "text",
            "ripple": False,
            "style": SEG_BTN_STYLE + (SEG_BTN_STYLE_ON if active else SEG_BTN_STYLE_OFF),
        },
        "text": label,
        "events": {
            "click": {
                "api": f"plugin/{plugin_id}/switch_view",
                "method": "get",
                "params": {"view": view, "apikey": settings.API_TOKEN},
            }
        },
    }


def _page_view_switch(active: str, hr_count: int, del_count: int, plugin_id: str) -> dict:
    """详情页顶部的「保种任务 / 最近删除」切换条。

    两个分段各自带计数 —— 一眼能看出**另一页有没有东西**，这是折叠面板做不到的
    （也是这一版选分段胶囊而非手风琴的主要理由）。
    """
    return {
        "component": "VRow",
        "content": [
            {
                "component": "VCol",
                "props": {"cols": 12},
                "content": [
                    {
                        "component": "div",
                        "props": {"style": SEG_TRACK_STYLE},
                        "content": [
                            _seg_button(f"保种任务 {hr_count}", PAGE_VIEW_HR,
                                        active == PAGE_VIEW_HR, plugin_id),
                            _seg_button(f"最近删除 {del_count}", PAGE_VIEW_DEL,
                                        active == PAGE_VIEW_DEL, plugin_id),
                        ],
                    }
                ],
            }
        ],
    }


# ── 详情页内容区固定高度（v2.0.11）──────────────────────────────
# 弹窗是 `<VDialog scrollable>` 且**垂直居中**（PluginDataDialog.vue），内容一变高
# 整个卡片就重新居中 —— 切换条跟着上下跳。真机实测（视口 1574×1060）：
# 视图① 内容 504px、视图② 内容 702px，点一下分段切换条位移 99px、弹窗高度差 198px。
# 把「切换条以下」的正文锁成同一个高度，两个视图就永远等高 ⇒ 切换零位移。
# 用 vh 比例而不是写死像素：不同窗口尺寸下都不会顶满屏幕或空掉半屏。
#
# 🔴 上下各 12px 内边距是**必须的**：里面那层 VRow 带 `margin: -12px`，
#    在滚动容器上不补回来，第一块的顶部会被裁掉 12px。
VIEW_BODY_VH = 45            # 正文高度 = 视口高度的 45%
VIEW_BODY_MIN_H = 300        # 小窗口兜底（保证 5 条时间轴放得下）
VIEW_BODY_MAX_H = 560        # 大屏上限（别把弹窗撑满整屏）
VIEW_BODY_STYLE = (
    f"height: {VIEW_BODY_VH}vh; min-height: {VIEW_BODY_MIN_H}px; "
    f"max-height: {VIEW_BODY_MAX_H}px; padding: 12px 0; box-sizing: border-box; "
    "overflow-y: auto; overflow-x: hidden;"
)


def _view_body(blocks: List[dict]) -> dict:
    """把视图正文块包进固定高度的容器。

    两个视图用**同一份 style 串**，高度必然相等 —— 这就是「切换不跳动」的判据，
    回归里直接比对两个视图的容器 style 是否逐字节相同。
    """
    return {"component": "div", "props": {"style": VIEW_BODY_STYLE},
            "content": blocks}

def _empty_view_hint(text: str) -> dict:
    """当前视图没有内容时的占位提示。

    加了切换条之后，空视图如果什么都不渲染，整页就只剩那条切换条 ——
    看着像插件坏了。这里补一条中性提示（不是错误态）。
    """
    return {
        "component": "VRow",
        "content": [
            {
                "component": "VCol",
                "props": {"cols": 12},
                "content": [
                    {
                        "component": "VAlert",
                        "props": {"type": "info", "variant": "tonal", "text": text},
                    }
                ],
            }
        ],
    }


class ChdbitsHrMonitor(_PluginBase):
    """彩虹岛 H&R 种子监控插件。

    定时抓取彩虹岛 H&R 页面，解析未完成的 HR 任务，与本地 QB 指定分类的
    任务比对。站点 H&R 页面已不存在（即已完成保种要求）的任务，在等待
    设定时间后自动从 QB 删除，并同时删除本地文件。
    """

    # 插件名称
    plugin_name = "彩虹岛HR监控"
    plugin_desc = "监控彩虹岛H&R任务，完成后延迟自动删除QB任务与本地文件。"
    # 插件图标
    plugin_icon = "CHDBits.png"
    # 插件版本
    plugin_version = "2.0.11"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = "https://github.com/Desire5864"
    # 插件配置项ID前缀
    plugin_config_prefix = "chdbitshrmonitor_"
    # 加载顺序
    plugin_order = 25
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _downloader: str = ""
    _category: str = "彩虹岛&HR"
    _hr_url: str = ""
    _delete_delay_hours: float = 4.0
    _fallback_cycle_hours: float = HR_DEFAULT_CYCLE_HOURS
    _interval_minutes: int = 30
    _run_once: bool = False
    # 最近一次检查时间
    _last_check_time: Optional[str] = None
    # 最近一次检查错误
    _last_error: str = ""
    # 最近一次站点 HR 任务
    _last_hr_tasks: List[Dict[str, Any]] = []
    # 上次站点 H&R 任务数，用于检测骤降异常
    _last_hr_count: int = 0
    # 最近一次 QB 与站点比对明细
    _last_compare_items: List[Dict[str, Any]] = []
    # 最近一次「本地任务 × 站点记录」的互斥分配结果
    # key = 本地任务 hash，value = (站点任务, 站点序号)
    # None = 本轮还没算过（例如刚 reload），此时详情页回落到逐条匹配
    _site_assignment: Optional[Dict[str, Tuple[Dict[str, Any], int]]] = None

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        # 停止现有任务
        self.stop_service()

        # 重置状态
        self._enabled = False
        self._notify = False
        self._downloader = ""
        self._category = "彩虹岛&HR"
        self._hr_url = ""
        self._delete_delay_hours = 4.0
        self._fallback_cycle_hours = HR_DEFAULT_CYCLE_HOURS
        self._interval_minutes = 30
        self._run_once = False
        self._last_check_time = None
        self._last_error = ""
        self._last_hr_tasks = []
        self._last_hr_count = 0
        self._last_compare_items = []
        self._site_assignment = None

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._downloader = str(config.get("downloader") or "").strip()
        self._category = str(config.get("category") or "彩虹岛&HR").strip()
        self._hr_url = str(config.get("hr_url") or "").strip()
        try:
            # v2.0.5：支持小数（例如 0.5 小时）。旧版 int() 强转会把 0.5 变成 0
            # （表现为「立刻删除」），字符串 "0.5" 则抛错回落到 4 小时，都不是本意。
            self._delete_delay_hours = max(
                0.0, float(config.get("delete_delay_hours") or 4))
        except (TypeError, ValueError):
            self._delete_delay_hours = 4.0
        try:
            fallback = float(config.get("fallback_cycle_hours") or HR_DEFAULT_CYCLE_HOURS)
            self._fallback_cycle_hours = fallback if fallback > 0 else HR_DEFAULT_CYCLE_HOURS
        except (TypeError, ValueError):
            self._fallback_cycle_hours = HR_DEFAULT_CYCLE_HOURS
        try:
            self._interval_minutes = max(5, int(config.get("interval_minutes") or 30))
        except (TypeError, ValueError):
            self._interval_minutes = 30
        self._run_once = bool(config.get("run_once"))

        # 立即执行一次：执行后自动关闭开关
        if self._run_once:
            logger.info("彩虹岛HR监控：立即执行一次检查")
            self.check_hr()
            self.update_config(
                {
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "downloader": self._downloader,
                    "category": self._category,
                    "hr_url": self._hr_url,
                    "delete_delay_hours": self._delete_delay_hours,
                    "fallback_cycle_hours": self._fallback_cycle_hours,
                    "interval_minutes": self._interval_minutes,
                    "run_once": False,
                }
            )

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
                "path": "/switch_view",
                "endpoint": self.api_switch_view,
                "methods": ["GET"],
                # 不写 auth：路由默认就是 apikey（见 _update_plugin_api_routes），
                # 与仓库里 tvfirstwatch / qbcategorypause 的写法一致。
                "summary": "切换详情页视图（保种任务 / 最近删除）",
                "description": "详情页顶部分段胶囊点击后调用：把选中的视图写入插件数据，"
                               "随后前端重载整页，get_page() 按该值只渲染一个视图。",
            }
        ]

    def api_switch_view(self, view: str = None, apikey: str = None) -> Dict[str, Any]:
        """API：切换详情页当前视图。

        :param view: hr=保种任务（默认）/ del=最近删除；其余取值一律回落默认视图
        :param apikey: 由路由的 apikey 鉴权消费，函数体不再重复校验
        :return: 切换结果
        """
        target = (PAGE_VIEW_DEL if str(view or "").strip().lower().startswith("d")
                  else PAGE_VIEW_HR)
        self.save_data(PAGE_VIEW_DATA_KEY, target)
        logger.info(f"彩虹岛HR监控：详情页视图切换为「{target}」")
        return {"success": True, "view": target}

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。

        :return: Vuetify 表单结构与默认配置
        """
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "run_once",
                                            "label": "立即执行一次",
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
                                            "model": "downloader",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "category",
                                            "label": "QB任务分类",
                                            "placeholder": "默认：彩虹岛&HR",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "hr_url",
                                            "label": "站点H&R页面地址",
                                            "placeholder": "https://ptchdbits.co/hnr.php?id=138574",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delete_delay_hours",
                                            "label": "完成后延迟删除（小时）",
                                            "placeholder": "默认4，最小0，可填小数（如 0.5）",
                                            "type": "number",
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
                                            "model": "fallback_cycle_hours",
                                            "label": "兜底保种周期（小时）",
                                            "placeholder": "默认120（5天）",
                                            "type": "number",
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
                                            "model": "interval_minutes",
                                            "label": "检查间隔（分钟）",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件会定时抓取彩虹岛 H&R 页面，与本地 QB 指定分类的任务比对。"
                                                    "站点 H&R 页面已不存在（即已完成保种要求）的任务，"
                                                    "在等待设定时间后自动从 QB 删除，并同时删除本地文件。"
                                                    "H&R 周期 5 天对应 120 小时，3 天对应 72 小时。"
                                                    "安全闸门：站点从下载达标到登记 H&R 记录存在延迟，"
                                                    "因此「站点未列出」不等于「已完成保种」——"
                                                    "插件会先用本地做种时长与该任务的 H&R 周期比对，"
                                                    f"保种时长未达标（周期 + {HR_CYCLE_MARGIN_MIN_HOURS:g} 小时安全余量）"
                                                    "前绝不进入删除流程；"
                                                    "站点从未给出周期时，以「兜底保种周期」为准。",
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
            "downloader": "",
            "category": "彩虹岛&HR",
            "hr_url": "",
            "delete_delay_hours": 4.0,
            "fallback_cycle_hours": 120,
            "interval_minutes": 30,
            "run_once": False,
        }

    def __build_display_items(self) -> List[Dict[str, Any]]:
        """把三份数据源合成一个统一展示清单（一个种子只保留一条）。

        数据源：
          1. ``self._last_compare_items`` —— 本地 QB 任务比对结果（本地视角，带 hash）
          2. ``self._last_hr_tasks``       —— 站点 H&R 页面未完成任务（站点视角）
          3. ``hr_completed_map``          —— 已完成／待复核记录（持久化，reload 后仍在）

        同一个种子在 1 与 2 中都会出现（片名一一对应），这里合并成一条，
        两侧的字段都保留：站点做种时间与本地做种时间分列，站点任务消失后
        站点侧显示「已移除」，本地时长依旧可读。

        :return: 展示项列表，已按「待删除 → 待复核 → 保种中 → 未达阈值 → 本地缺失」排序
        """
        completed_map: Dict[str, Dict[str, Any]] = self.get_data(COMPLETED_DATA_KEY) or {}
        cycle_map: Dict[str, Dict[str, Any]] = self.get_data(HR_CYCLE_DATA_KEY) or {}
        now_ts = datetime.now().timestamp()
        delay_hours = self._delete_delay_hours or 0

        items: List[Dict[str, Any]] = []
        matched_site: set = set()
        used_hashes: set = set()

        # ── 1) 本地视角：每个本地任务一条 ──────────────────────────────
        for cmp_item in self._last_compare_items or []:
            name = str(cmp_item.get("name") or "")
            torrent_hash = str(cmp_item.get("hash") or "")
            if torrent_hash:
                used_hashes.add(torrent_hash)
            status_raw = str(cmp_item.get("status") or "").strip() or "-"
            site_seeding = str(cmp_item.get("seeding_time") or "").strip()
            local_seeding = str(cmp_item.get("local_seeding") or "").strip()
            hr_cycle = str(cmp_item.get("hr_cycle") or "").strip()
            remain_time = str(cmp_item.get("remain_time") or "").strip()
            detail = str(cmp_item.get("detail") or "").strip()

            subtitle, seed_title = self.__get_uhd_titles(name)
            if not subtitle:
                subtitle = self.__find_hr_subtitle(name)

            # 站点任务（存在则说明仍在保种考核中）
            # v2.0.4：优先认判定层那一轮的**互斥分配**结果，保证卡片上显示的站点
            # 记录与实际用来判定的站点记录是同一条；分配结果为 None（刚 reload、
            # 本轮没跑到比对）时才回落到逐条匹配。
            site_task, site_idx = None, -1
            if self._site_assignment is None:
                site_task, site_idx = self.__match_site_task(name)
            else:
                pair = self._site_assignment.get(cmp_item.get("hash"))
                if pair:
                    site_task, site_idx = pair
            if site_idx >= 0:
                matched_site.add(site_idx)
            site_title = str(site_task.get("title") or "") if site_task else ""
            if site_task:
                site_seeding = site_seeding or str(site_task.get("seeding_time") or "").strip()
                hr_cycle = hr_cycle or str(site_task.get("hr_cycle") or "").strip()
            hr_percent = str(site_task.get("hr_percent") or "").strip() if site_task else ""
            site_remain = str(site_task.get("remain_time") or "").strip() if site_task else ""

            record = completed_map.get(torrent_hash) or {}

            if record.get("pending_confirm"):
                # ── 待复核：站点首次未列出，尚未开始删除计时 ──
                items.append({
                    "state": "pending",
                    "title": subtitle or name or "(无标题)",
                    "site_title": site_title,
                    "seed_title": seed_title or name,
                    "site_seeding": site_seeding,
                    "local_seeding": local_seeding,
                    "pct": 100.0,
                    "ring_text": "✓",
                    "color": COLOR_PENDING,
                    "chip": "待复核",
                    "sort_extra": 0.0,
                    "captions": [c for c in (
                        "站点已移除该任务，但需下次检查仍判定为已完成，"
                        "才会进入 %g 小时删除倒计时" % delay_hours,
                    ) if c],
                    "pills": [
                        _pill(f"首次发现 {record.get('first_seen_time') or '-'}", strong=True),
                    ],
                })
                continue

            if record.get("completed_at"):
                # ── 待删除：已连续两次判定完成，进入删除倒计时 ──
                # 统一用 COLOR_DANGER：该状态本身已是「即将删除」的最高级，
                # 再按剩余时长分三色反而削弱了「整卡警示」的一致性
                # （1.1h 显示橙色会让它看起来与「未达标」同类）。
                completed_at = float(record.get("completed_at") or 0)
                elapsed_hours = max(0.0, (now_ts - completed_at) / 3600)
                remain_hours = max(0.0, delay_hours - elapsed_hours)
                color = COLOR_DANGER
                ratio = (elapsed_hours / delay_hours * 100) if delay_hours else 0.0
                items.append({
                    "state": "deleting",
                    "title": subtitle or name or "(无标题)",
                    "site_title": site_title,
                    "seed_title": seed_title or name,
                    "site_seeding": site_seeding,
                    "local_seeding": local_seeding,
                    "pct": min(100.0, ratio),
                    "ring_text": f"{remain_hours:.1f}h",
                    "color": color,
                    "chip": f"{remain_hours:.1f}h 后删除",
                    "sort_extra": remain_hours,
                    "captions": [c for c in (
                        f"站点已连续两次未列出该任务，判定已完成保种；"
                        f"{delay_hours:g} 小时后删除 QB 任务与本地文件",
                    ) if c],
                    "pills": [
                        _pill(f"完成 {record.get('completed_time') or '-'}", strong=True),
                        _pill(f"已等待 {elapsed_hours:.1f}h"),
                        _pill(f"剩余 {remain_hours:.1f}h", color=color),
                    ],
                })
                continue

            # ── 保种中／未达阈值 ──
            cycle_hours = self.__parse_hr_cycle_hours(hr_cycle)
            # 站点仍列出该任务时，完成度按站点口径（站点做种 ÷ 周期）
            seeding_hours = self.__parse_seeding_hours(site_seeding)
            if seeding_hours is None:
                # 站点已移除，退回本地做种时长 ÷ 保种要求（cycle_map 中带余量）
                seeding_hours = _parse_duration_hours(local_seeding)
                if not cycle_hours and torrent_hash:
                    cycle_hours = _parse_duration_hours(
                        str((cycle_map.get(torrent_hash) or {}).get("hr_cycle") or "")
                    )

            if status_raw == "已冻结":
                # 匹配失效被冻结：站点查无不可信，本轮不推进任何删除判定
                items.append({
                    "state": "frozen",
                    "title": subtitle or name or "(无标题)",
                    "site_title": site_title,
                    "seed_title": seed_title or name,
                    "site_seeding": site_seeding,
                    "local_seeding": local_seeding,
                    "pct": 0.0,
                    "ring_text": "!",
                    "color": COLOR_WARN,
                    "chip": "已冻结",
                    "sort_extra": 0.0,
                    "captions": [c for c in (detail, self._last_error) if c],
                    "pills": [_pill("本轮不推进删除", color=COLOR_WARN)],
                })
                continue

            if status_raw == "未见记录":
                # 从未在站点 H&R 页面出现过：查无 ≠ 已完成，永久排除在删除流程外
                items.append({
                    "state": "unseen",
                    "title": subtitle or name or "(无标题)",
                    "site_title": site_title,
                    "seed_title": seed_title or name,
                    "site_seeding": site_seeding,
                    "local_seeding": local_seeding,
                    "pct": 0.0,
                    "ring_text": "?",
                    "color": COLOR_IDLE,
                    "chip": "未判定",
                    "sort_extra": 0.0,
                    "captions": [c for c in (detail,) if c],
                    "pills": [_pill(f"周期 {hr_cycle or '-'}")],
                })
                continue

            if status_raw in ("待删除", "已完成") and not record:
                # 本轮刚删掉的任务：completed_map / cycle_map 记录都已清空，
                # 卡片没有任何可展示的进度口径。正常路径不会走到这里（删除
                # 成功时已从 compare_items 摘掉），留作兜底，避免退化成
                # 「数据缺失」这种看起来像插件报错的文案。
                continue

            if status_raw == "未达阈值":
                items.append({
                    "state": "threshold",
                    "title": subtitle or name or "(无标题)",
                    "site_title": site_title,
                    "seed_title": seed_title or name,
                    "site_seeding": site_seeding,
                    "local_seeding": local_seeding,
                    "pct": 0.0,
                    "ring_text": "–",
                    "color": COLOR_IDLE,
                    "chip": "未达阈值",
                    "sort_extra": 0.0,
                    "captions": [c for c in (detail,) if c],
                    "pills": [],
                })
                continue

            if cycle_hours and cycle_hours > 0 and seeding_hours is not None:
                pct = min(100.0, seeding_hours / cycle_hours * 100)
                remain = max(0.0, cycle_hours - seeding_hours)
                # 颜色按完成度连续映射（蓝 → 青 → 绿），与「还差多少」解耦：
                # 越接近完成越绿，不再用红色表达「快完成了」
                color = _ring_color(pct)
                if seeding_hours >= cycle_hours:
                    chip = "已达标"
                else:
                    chip = f"未达标 · 还差 {remain:.1f}h"
            else:
                pct, chip, color = 0.0, "数据缺失", COLOR_IDLE

            pills = [
                _pill(f"{pct:.1f}%", color=color),
                _pill(f"周期 {hr_cycle or '-'}"),
            ]
            if hr_percent:
                pills.append(_pill(f"H&R {hr_percent}"))
            if site_remain and site_remain != "-":
                pills.append(_pill(f"站点剩余 {site_remain}"))
            if remain_time and remain_time != "-":
                pills.append(_pill(f"还差 {remain_time}"))
            if not site_task and detail:
                pills.append(_pill("站点已移除", color=COLOR_INFO))

            items.append({
                "state": "seeding",
                "title": subtitle or name or "(无标题)",
                "site_title": site_title,
                "seed_title": seed_title or name,
                "site_seeding": site_seeding,
                "local_seeding": local_seeding,
                "pct": pct,
                "ring_text": f"{pct:.0f}%",
                "color": color,
                "chip": chip,
                "sort_extra": 0.0,
                "captions": [c for c in (detail,) if c] if not site_task else [],
                "pills": pills,
            })

        # ── 2) 站点列出、本地没有的任务（不丢站点侧信息） ──────────────
        for idx, task in enumerate(self._last_hr_tasks or []):
            if idx in matched_site:
                continue
            site_title = str(task.get("title") or "")
            subtitle, seed_title = self.__get_uhd_titles(site_title)
            if not subtitle:
                subtitle = str(task.get("subtitle") or "").strip()
            if not seed_title:
                seed_title = self.__find_qb_name(site_title)
            site_seeding = str(task.get("seeding_time") or "").strip()
            hr_cycle = str(task.get("hr_cycle") or "").strip()
            cycle_hours = self.__parse_hr_cycle_hours(hr_cycle)
            seeding_hours = self.__parse_seeding_hours(site_seeding)
            if cycle_hours and cycle_hours > 0 and seeding_hours is not None:
                pct = min(100.0, seeding_hours / cycle_hours * 100)
                remain = max(0.0, cycle_hours - seeding_hours)
                # 与保种中同一口径：颜色只表达完成度
                color = _ring_color(pct)
            else:
                pct, color = 0.0, COLOR_IDLE
            pills = [_pill(f"{pct:.1f}%", color=color)]
            if hr_cycle:
                pills.append(_pill(f"周期 {hr_cycle}"))
            hr_percent = str(task.get("hr_percent") or "").strip()
            if hr_percent:
                pills.append(_pill(f"H&R {hr_percent}"))
            site_remain = str(task.get("remain_time") or "").strip()
            if site_remain and site_remain != "-":
                pills.append(_pill(f"站点剩余 {site_remain}"))
            items.append({
                "state": "siteonly",
                "title": subtitle or site_title or "(无标题)",
                "site_title": site_title,
                "seed_title": seed_title,
                "site_seeding": site_seeding,
                "local_seeding": "",
                "pct": pct,
                "ring_text": f"{pct:.0f}%",
                "color": color,
                "chip": "本地未找到",
                "sort_extra": 0.0,
                "captions": [f"本地 QB 分类「{self._category}」中没有对应任务，无法比对"],
                "pills": pills,
            })

        # ── 3) 持久化记录里、本轮未覆盖的（reload 后内存态为空时仍要可见） ──
        for torrent_hash, record in completed_map.items():
            if torrent_hash in used_hashes:
                continue
            name = str(record.get("name") or "")
            subtitle, seed_title = self.__get_uhd_titles(name)
            display_title = subtitle or name or "(无标题)"
            if record.get("pending_confirm"):
                items.append({
                    "state": "pending",
                    "title": display_title,
                    "site_title": "",
                    "seed_title": seed_title or name,
                    "site_seeding": "",
                    "local_seeding": "",
                    "pct": 100.0,
                    "ring_text": "✓",
                    "color": COLOR_PENDING,
                    "chip": "待复核",
                    "sort_extra": 0.0,
                    "captions": [
                        "站点已移除该任务，但需下次检查仍判定为已完成，"
                        "才会进入 %g 小时删除倒计时" % delay_hours,
                    ],
                    "pills": [
                        _pill(f"首次发现 {record.get('first_seen_time') or '-'}", strong=True),
                    ],
                })
                continue
            # 与「本地视角」分支保持一致：待删除统一用 COLOR_DANGER
            completed_at = float(record.get("completed_at") or 0)
            elapsed_hours = max(0.0, (now_ts - completed_at) / 3600)
            remain_hours = max(0.0, delay_hours - elapsed_hours)
            color = COLOR_DANGER
            ratio = (elapsed_hours / delay_hours * 100) if delay_hours else 0.0
            items.append({
                "state": "deleting",
                "title": display_title,
                "site_title": "",
                "seed_title": seed_title or name,
                "site_seeding": "",
                "local_seeding": "",
                "pct": min(100.0, ratio),
                "ring_text": f"{remain_hours:.1f}h",
                "color": color,
                "chip": f"{remain_hours:.1f}h 后删除",
                "sort_extra": remain_hours,
                "captions": [
                    f"站点已连续两次未列出该任务，判定已完成保种；"
                    f"{delay_hours:g} 小时后删除 QB 任务与本地文件",
                ],
                "pills": [
                    _pill(f"完成 {record.get('completed_time') or '-'}", strong=True),
                    _pill(f"已等待 {elapsed_hours:.1f}h"),
                    _pill(f"剩余 {remain_hours:.1f}h", color=color),
                ],
            })

        # ── 排序：待删除（剩余少的在前）→ 待复核 → 保种中（完成度降序）→ 其它 ──
        order = {"deleting": 0, "pending": 1, "frozen": 2, "seeding": 3,
                 "threshold": 4, "unseen": 5, "siteonly": 6}
        items.sort(key=lambda it: (
            order.get(it.get("state"), 9),
            it.get("sort_extra") or 0.0,
            -(it.get("pct") or 0.0),
        ))
        return items

    def __match_site_task(self, local_title: str) -> Tuple[Optional[Dict[str, Any]], int]:
        """在本地任务标题对应的站点 H&R 任务中查找，同时返回其索引。

        :param local_title: 本地任务标题
        :return: (站点任务, 索引)；未匹配返回 (None, -1)
        """
        if not local_title or not self._last_hr_tasks:
            return None, -1
        task = self.__find_site_task(local_title, self._last_hr_tasks)
        if task is None:
            return None, -1
        for idx, item in enumerate(self._last_hr_tasks):
            if item is task:
                return task, idx
        return task, -1

    def __resolve_view(self) -> str:
        """读取详情页当前视图（存插件数据，缺省／值非法一律回落「保种任务」）。

        不缓存到实例属性：重载/重装后实例会重建，每次都从数据读，
        才能保证页面上显示的永远是用户最后选中的那一页。
        """
        raw = str(self.get_data(PAGE_VIEW_DATA_KEY) or "").strip().lower()
        return PAGE_VIEW_DEL if raw.startswith("d") else PAGE_VIEW_HR

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: Vuetify 详情页结构
        """
        if not self._enabled:
            return None

        page_content: List[dict] = []

        # 配置概览
        page_content.append(
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
                                    "text": f"下载器：{self._downloader or '未配置'}；"
                                            f"QB分类：{self._category}；"
                                            f"延迟删除：{self._delete_delay_hours:g} 小时；"
                                            f"兜底保种周期：{self._fallback_cycle_hours:g} 小时；"
                                            f"检查间隔：{self._interval_minutes} 分钟；"
                                            f"最近检查：{self._last_check_time or '尚未检查'}",
                                },
                            }
                        ],
                    }
                ],
            }
        )

        # 错误提示
        if self._last_error:
            page_content.append(
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
                                        "type": "error",
                                        "variant": "tonal",
                                        "text": f"最近一次检查失败：{self._last_error}",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

        # ── 视图切换条（保种任务 / 最近删除） ──────────────────────
        # 两个视图的计数都要印在分段上，所以两份数据源都在分流之前备好。
        # 切换状态由后端记（get_api 里那条 /switch_view），原因见模块 docstring。
        display_items = self.__build_display_items()
        deleted_log: Dict[str, Dict[str, Any]] = self.get_data(DELETE_LOG_DATA_KEY) or {}
        view = self.__resolve_view()
        page_content.append(
            _page_view_switch(view, len(display_items), len(deleted_log),
                              self.__class__.__name__)
        )

        # ── 视图②：最近删除留档（保险闸门 ⑤：删了什么，事后可追溯） ──────
        # 版式：时间轴（日期列｜轴线｜名称 + 指标），见 _deleted_log_block()。
        # 旧版把记录拼成一段 pre-line 文本，长片名折行后字段彼此对不齐，
        # 5 条会渲染成 11 行；这里改成结构化组件树，每条恒定两行。
        if view == PAGE_VIEW_DEL:
            body_blocks: List[dict] = []
            if deleted_log:
                recent = sorted(
                    deleted_log.items(),
                    key=lambda kv: float(kv[1].get("deleted_at") or 0),
                    reverse=True,
                )[:DELETE_LOG_DISPLAY]
                body_blocks.append(
                    _deleted_log_block([rec for _, rec in recent], len(deleted_log))
                )
            else:
                body_blocks.append(_empty_view_hint("暂无删除留档。"))
            page_content.append(_view_body(body_blocks))
            return page_content

        # ── 视图①：保种任务清单 ────────────────────────────────────
        # 站点视角 / 本地 QB 视角 / 已完成待删除 三份数据源合成一个列表，
        # 同一个种子只保留一条；站点做种时间与本地做种时间分列显示，
        # 站点任务消失后站点侧显示「已移除」，本地时长依旧可读。
        body_blocks: List[dict] = []
        counts: Dict[str, int] = {}
        for item in display_items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1

        if display_items:
            # 插件重载后内存态被清空，此时只有持久化的完成态记录可显示，
            # 需要显式说明，避免被误读成「保种中的任务都消失了」
            if not self._last_check_time:
                body_blocks.append(
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
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "本轮检查尚未执行（插件重载会清空内存态），"
                                                    "以下仅显示已持久化的待删除／待复核记录；"
                                                    "保种中的任务将在下一轮检查后恢复显示。",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                )

            summary = [f"共 {len(display_items)} 个任务"]
            for state, label in (("deleting", "待删除"), ("pending", "待复核"),
                                 ("frozen", "已冻结"), ("seeding", "保种中"),
                                 ("threshold", "未达阈值"), ("unseen", "未判定"),
                                 ("siteonly", "本地未找到")):
                if counts.get(state):
                    summary.append(f"{label} {counts[state]}")
            body_blocks.append(
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
                                        "type": "warning" if counts.get("deleting") else "info",
                                        "variant": "tonal",
                                        "text": " · ".join(summary),
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            card_items = []
            for item in display_items:
                # 三个名字全部保留：主标题（中文名）+ 站点标题（英文原名）+ 种子标题（QB 任务名）
                captions: List[str] = []
                if item.get("site_title") and item["site_title"] != item["title"]:
                    captions.append(f"站点标题：{item['site_title']}")
                if item.get("seed_title") and item["seed_title"] not in (
                        item["title"], item.get("site_title")):
                    captions.append(f"种子标题：{item['seed_title']}")
                captions.extend(item.get("captions") or [])

                # 做种时间两个来源都列出来：站点计数与本地 QB 计数存在偏差，
                # 站点任务消失后站点侧显示「已移除」
                site_text = item.get("site_seeding") or (
                    "已移除" if item["state"] in ("pending", "deleting") else "-")
                local_text = item.get("local_seeding") or "-"
                pills = [
                    _pill(f"站点做种 {site_text}"),
                    _pill(f"本地做种 {local_text}", strong=True),
                ]
                pills.extend(item.get("pills") or [])

                # 「待删除」走醒目样式：整卡红描边 + 淡红底 + 实心红 chip。
                # 该状态是唯一会真正删除本地文件的，必须在长列表里第一眼可见。
                is_danger = item["state"] == "deleting"
                card_items.append(
                    _ring_card(
                        ring=_ring(item.get("pct") or 0.0, item["color"], item["ring_text"]),
                        title=item["title"],
                        chip=_status_chip(item["chip"], item["color"], solid=is_danger),
                        captions=captions,
                        pills=pills,
                        danger=is_danger,
                    )
                )

            body_blocks.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": card_items,
                        }
                    ],
                }
            )
        else:
            # 之前这里什么都不渲染 —— 有了切换条之后整页就只剩它，看着像坏了。
            body_blocks.append(
                _empty_view_hint("当前没有纳入统计的 H&R 任务；"
                                 "站点侧未完成的 H&R 任务会在下一轮检查后出现。")
            )

        page_content.append(_view_body(body_blocks))
        return page_content

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        if self._enabled and self._downloader and self._hr_url:
            return [
                {
                    "id": "ChdbitsHrMonitor",
                    "name": "彩虹岛HR监控",
                    "trigger": "interval",
                    "func": self.check_hr,
                    "kwargs": {"minutes": self._interval_minutes},
                }
            ]
        return []

    def check_hr(self) -> None:
        """检查站点 H&R 任务并与本地 QB 任务比对，处理已完成任务。"""
        if not self._enabled:
            return

        if not self._downloader or not self._hr_url:
            logger.warning("彩虹岛HR监控：未配置下载器或H&R页面地址，跳过检查")
            return

        self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 抓取站点 H&R 页面
        hr_tasks, error = self.__fetch_hr_tasks()
        if error:
            self._last_error = error
            logger.error(f"彩虹岛HR监控：抓取H&R页面失败，{error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【彩虹岛HR监控】",
                    text=f"抓取H&R页面失败：{error}",
                )
            return

        self._last_error = ""
        self._last_hr_tasks = hr_tasks
        logger.info(f"彩虹岛HR监控：站点未完成 H&R 任务 {len(hr_tasks)} 个")

        # 安全校验：站点任务数相比上次骤降时，视为页面异常，跳过本次比对。
        # 站点 H&R 任务通常只会缓慢减少，若一次减少超过一半（且上次有任务），
        # 很可能是页面解析不完整或 cookie 失效，此时比对会误判为「已完成」。
        prev_count = self._last_hr_count
        self._last_hr_count = len(hr_tasks)
        if prev_count > 0 and len(hr_tasks) < prev_count / 2:
            self._last_error = (
                f"站点 H&R 任务数骤降（{prev_count} → {len(hr_tasks)}），"
                f"疑似页面异常，已跳过本次比对"
            )
            logger.warning(f"彩虹岛HR监控：{self._last_error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【彩虹岛HR监控】",
                    text=f"{self._last_error}，为避免误删任务已跳过本次比对。",
                )
            return

        # 2. 获取本地 QB 任务
        downloader_obj = self.__get_downloader()
        if not downloader_obj:
            self._last_error = "获取下载器失败"
            logger.error("彩虹岛HR监控：获取下载器失败")
            return

        torrents, err = downloader_obj.get_torrents()
        if err:
            self._last_error = "获取QB任务失败"
            logger.error("彩虹岛HR监控：获取QB任务失败")
            return

        # 筛选指定分类的任务
        local_torrents = [
            torrent for torrent in torrents
            if (torrent.get("category") or "") == self._category
        ]
        logger.info(f"彩虹岛HR监控：本地分类 {self._category} 任务 {len(local_torrents)} 个")

        # 安全校验：站点解析出 0 个任务但本地有任务时，视为页面异常（如 cookie 失效、
        # 页面改版返回登录页），跳过本次比对，避免误判为已完成而删除任务。
        if not hr_tasks and local_torrents:
            self._last_error = "站点 H&R 页面解析到 0 个任务，疑似页面异常，已跳过本次比对"
            logger.warning(f"彩虹岛HR监控：{self._last_error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【彩虹岛HR监控】",
                    text="站点 H&R 页面解析到 0 个任务，疑似页面异常（cookie 失效或页面改版），"
                         "已跳过本次比对以避免误删任务。",
                )
            return

        # 3. 比对：站点未完成任务（含完整数据，用于关联做种时间/剩余时间）
        site_tasks = hr_tasks

        # 4. 处理已完成任务
        self.__process_completed(downloader_obj, local_torrents, site_tasks)

    def __process_completed(self, downloader_obj: Any, local_torrents: List[Any],
                            site_tasks: List[Dict[str, Any]]) -> None:
        """处理已完成（站点 H&R 页面已不存在）的任务。

        :param downloader_obj: 下载器实例
        :param local_torrents: 本地指定分类的任务列表
        :param site_tasks: 站点未完成 H&R 任务列表
        """
        completed_map: Dict[str, Dict[str, Any]] = self.get_data(COMPLETED_DATA_KEY) or {}
        cycle_map: Dict[str, Dict[str, Any]] = self.get_data(HR_CYCLE_DATA_KEY) or {}
        now_ts = datetime.now().timestamp()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        delay_seconds = self._delete_delay_hours * 3600
        site_titles = {self.__normalize_title(task.get("title") or "") for task in site_tasks}

        # 本地任务哈希集合，用于清理已不存在的记录
        local_hashes = {torrent.get("hash") for torrent in local_torrents if torrent.get("hash")}

        # 清理已不在本地的记录
        for torrent_hash in list(completed_map.keys()):
            if torrent_hash not in local_hashes:
                completed_map.pop(torrent_hash, None)
        for torrent_hash in list(cycle_map.keys()):
            if torrent_hash not in local_hashes:
                cycle_map.pop(torrent_hash, None)

        # ── 站点记录互斥分配（v2.0.4）────────────────────────────────
        # 一条站点 H&R 记录至多归一个本地任务。逐条独立匹配会天然允许「两条本地任务
        # 共用同一条站点记录」（2026-10-05 实测「揭秘日」与「火遮眼」即如此），
        # 结果是两条卡片显示同一份站点数据、其中一条被当成「站点仍在考核」而永远
        # 不进删除流程。分配结果同时供本方法与详情页展示层复用，保证显示与判定同源。
        site_assignment, match_conflicts = self.__assign_site_tasks(
            local_torrents, site_tasks)
        self._site_assignment = site_assignment
        if match_conflicts:
            logger.warning(
                f"彩虹岛HR监控：{len(match_conflicts)} 个本地任务的候选站点记录"
                f"已被更优分配占用（互斥），本轮按「站点查无」处置："
                f"{[n[:40] for n in match_conflicts]}"
            )

        # ── 保险闸门 ②：匹配失效冻结 ────────────────────────────────
        # 站点与本地的标题一旦整体匹配不上，所有任务会同时变成「站点查无」，
        # 而 check_hr 里的「解析 0 条」「条数骤降」两道保护只看站点条数，
        # 拦不住这种情况。这里用「上一轮匹配正常、本轮一个都匹配不上」判定。
        compared_local = 0
        matched_local = 0
        for torrent in local_torrents:
            try:
                progress = float(torrent.get("progress") or 0)
            except (TypeError, ValueError):
                progress = 0
            if progress < HR_PROGRESS_THRESHOLD:
                continue
            compared_local += 1
            if torrent.get("hash") in site_assignment:
                matched_local += 1

        stats = self.get_data(MATCH_STATS_DATA_KEY) or {}
        try:
            prev_matched = int(stats.get("matched") or 0)
        except (TypeError, ValueError):
            prev_matched = 0
        frozen = bool(
            site_tasks
            and compared_local >= MATCH_FAIL_MIN_LOCAL
            and matched_local == 0
            and prev_matched >= MATCH_FAIL_MIN_PREV
        )
        if frozen:
            self._last_error = (
                f"站点与本地标题匹配失效（上轮匹配 {prev_matched} 个，本轮 0 个），"
                f"已冻结本次删除判定"
            )
            logger.warning(f"彩虹岛HR监控：{self._last_error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【彩虹岛HR监控】",
                    text=f"{self._last_error}。\n站点 H&R 页面仍有 {len(site_tasks)} 个任务，"
                         f"但本轮没有一条能与本地任务匹配上，疑似站点改版或标题格式变化；"
                         f"为避免误删，本轮不推进任何删除倒计时。",
                )

        delete_hashes: List[str] = []
        delete_names: List[str] = []
        countdown_items: List[Tuple[str, str, str]] = []
        compare_items: List[Dict[str, Any]] = []

        for torrent in local_torrents:
            torrent_hash = torrent.get("hash")
            if not torrent_hash:
                continue
            title = torrent.get("name") or ""

            # 本地做种时长（秒），保种时长闸门与页面展示共用。
            # 注意：站点任务消失后（已完成）站点做种时间不可得，本地时长仍然可读，
            # 因此这里在判定分支之前统一取值。
            seeding_seconds = self.__get_seeding_seconds(torrent, now_ts)
            local_seeding = _fmt_duration(seeding_seconds)

            # 安全校验：仅处理下载进度达到 HR 统计阈值的任务。
            # 站点规则：HR 种子下载大于等于 50% 时才需要完成规定保种时间，
            # 低于该阈值的任务不会产生 H&R 记录，若站点页面异常导致匹配失败，
            # 会被误判为「已完成」而删除，因此这里直接跳过。
            try:
                progress = float(torrent.get("progress") or 0)
            except (TypeError, ValueError):
                progress = 0
            if progress < HR_PROGRESS_THRESHOLD:
                completed_map.pop(torrent_hash, None)
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "未达阈值",
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": "",
                        "hr_cycle": "",
                        "detail": f"下载进度 {progress * 100:.1f}%，未达 HR 统计阈值（50%），跳过比对",
                    }
                )
                continue

            # 取该任务在**互斥分配**里分到的站点记录（为空即「站点查无」）
            assigned_pair = site_assignment.get(torrent_hash)
            site_task = assigned_pair[0] if assigned_pair else None
            seeding_time = str(site_task.get("seeding_time") or "") if site_task else ""
            hr_cycle = str(site_task.get("hr_cycle") or "") if site_task else ""
            # 剩余时间 = H&R周期 - 做种时间（还差多少做种时长才达标）
            remain_time = self.__calc_remain_time(hr_cycle, seeding_time)

            # 站点 H&R 页面仍存在该任务，说明未完成，跳过
            if site_task:
                completed_map.pop(torrent_hash, None)
                # 记录该任务的 H&R 周期，供「保种时长闸门」使用
                cycle_hours = self.__parse_hr_cycle_hours(hr_cycle)
                if cycle_hours and cycle_hours > 0:
                    prev_req = cycle_map.get(torrent_hash) or {}
                    cycle_map[torrent_hash] = {
                        "name": title,
                        "hr_cycle": hr_cycle,
                        "hr_cycle_hours": cycle_hours,
                        "first_seen_at": prev_req.get("first_seen_at") or now_ts,
                        "first_seen_time": prev_req.get("first_seen_time") or now_str,
                        "updated_at": now_ts,
                        "updated_time": now_str,
                    }
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "未完成",
                        # seeding_time 语义统一为「站点做种时间」；
                        # 本地做种时长单独放在 local_seeding，两者在页面上一并显示
                        "seeding_time": seeding_time,
                        "local_seeding": local_seeding,
                        "remain_time": remain_time,
                        "hr_cycle": hr_cycle,
                        "detail": "站点 H&R 页面仍存在，保种中",
                    }
                )
                continue

            # ── 保种时长闸门（核心安全校验） ──────────────────────────────
            # 站点规则是「HR 种子下载 ≥ 50% 才产生 H&R 记录」，且从下载达标到
            # 页面登记存在延迟。因此「站点未列出该任务」并不等于「已完成保种」：
            # 新下载完成的种子在登记延迟期内会被判定为完成，再等 delete_delay
            # 小时就被连文件一起删除——既丢文件，又直接违反 H&R。
            # 硬闸门：本地做种时长必须已达该任务的 H&R 周期 + 固定余量，才允许
            # 判定完成；周期未知（从未在站点见到）时，以「兜底保种周期」为准。
            req = cycle_map.get(torrent_hash) or {}
            try:
                known_cycle_hours = float(req.get("hr_cycle_hours") or 0)
            except (TypeError, ValueError):
                known_cycle_hours = 0.0
            if known_cycle_hours > 0:
                base_hours = known_cycle_hours
                label = "保种要求"
            else:
                base_hours = float(self._fallback_cycle_hours or HR_DEFAULT_CYCLE_HOURS)
                label = "保种要求（兜底周期）"
            # 闸门在周期之上再加一段固定的安全余量（默认 0.5 小时，见
            # HR_CYCLE_MARGIN_MIN_HOURS；可用 RATIO 按周期比例再放大）
            margin_hours = max(
                HR_CYCLE_MARGIN_MIN_HOURS, base_hours * HR_CYCLE_MARGIN_RATIO
            )
            required_seconds = (base_hours + margin_hours) * 3600
            cycle_text = _fmt_duration(required_seconds)

            if seeding_seconds + 60 < required_seconds:
                completed_map.pop(torrent_hash, None)
                logger.info(
                    f"彩虹岛HR监控：站点未列出该任务，本地做种 "
                    f"{_fmt_duration(seeding_seconds)} 未达{label} {cycle_text}，"
                    f"暂不删除 {title[:60]}"
                )
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "保种中",
                        # 站点已无该任务，站点做种时间不可得，只填本地做种时长
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": _fmt_duration(required_seconds - seeding_seconds),
                        "hr_cycle": cycle_text,
                        "detail": f"站点未列出该任务，本地做种 "
                                  f"{_fmt_duration(seeding_seconds)} 未达{label} "
                                  f"{cycle_text}，不进入删除流程",
                    }
                )
                continue

            # ── 保险闸门 ③：必须有「曾在站点出现过」的证据 ──────────────
            # 站点从未列出过该任务时，「现在查无」根本不构成「已完成」的证据
            # （可能是登记延迟、下载未达阈值、或标题一直匹配不上），此时只靠
            # 兜底周期判断就删除风险过大，这里直接排除在删除流程之外。
            if torrent_hash not in cycle_map:
                completed_map.pop(torrent_hash, None)
                logger.info(
                    f"彩虹岛HR监控：站点从未列出过该任务，不判定完成 {title[:60]}"
                )
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "未见记录",
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": "",
                        "hr_cycle": cycle_text,
                        "detail": "从未在站点 H&R 页面出现过，「站点查无」不能作为完成证据，"
                                  "不进入删除流程",
                    }
                )
                continue

            # ── 保险闸门 ②（生效）：本轮整体冻结 ──────────────────────
            # 冻结期间保持记录原样：不新增待复核、不推进倒计时、不执行删除。
            if frozen:
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "已冻结",
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": "",
                        "hr_cycle": cycle_text,
                        "detail": "站点与本地标题匹配疑似失效，本次不推进删除判定",
                    }
                )
                continue

            # 站点已无该任务，视为已完成。
            # 安全校验：需连续两次检查都判定为「站点无此任务」才开始计时，
            # 避免站点页面偶发解析不完整导致误判。
            record = completed_map.get(torrent_hash)
            if not record:
                # 首次发现，先标记待确认，不立即计时
                completed_map[torrent_hash] = {
                    "name": title,
                    "pending_confirm": True,
                    "first_seen_at": now_ts,
                    "first_seen_time": now_str,
                }
                logger.info(
                    f"彩虹岛HR监控：站点未找到该任务，待下次确认 {title[:60]}"
                )
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "待确认",
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": "",
                        "hr_cycle": "",
                        "detail": "站点未找到该任务，等待下次检查确认",
                    }
                )
                continue

            # 待确认状态：本次仍判定为已完成，正式进入计时
            if record.get("pending_confirm"):
                record.pop("pending_confirm", None)
                record["completed_at"] = now_ts
                record["completed_time"] = now_str
                logger.info(f"彩虹岛HR监控：任务已完成，开始计时 {title[:60]}")
                countdown_items.append((title, local_seeding, cycle_text))
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "已完成",
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": "",
                        "hr_cycle": "",
                        "detail": f"站点已无该任务，开始计时（{self._delete_delay_hours:g} 小时后删除）",
                    }
                )
                continue

            # 已记录，判断是否达到删除延迟
            completed_at = record.get("completed_at") or 0
            elapsed_hours = (now_ts - completed_at) / 3600
            if now_ts - completed_at >= delay_seconds:
                delete_hashes.append(torrent_hash)
                delete_names.append(title)
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "待删除",
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": "",
                        "hr_cycle": "",
                        "detail": f"已完成 {elapsed_hours:.1f} 小时，本次删除",
                    }
                )
            else:
                remain_hours = max(0, self._delete_delay_hours - elapsed_hours)
                compare_items.append(
                    {
                        "name": title,
                        "hash": torrent_hash,
                        "status": "已完成",
                        "seeding_time": "",
                        "local_seeding": local_seeding,
                        "remain_time": "",
                        "hr_cycle": "",
                        "detail": f"已完成 {elapsed_hours:.1f} 小时，剩余 {remain_hours:.1f} 小时删除",
                    }
                )

        self._last_compare_items = compare_items

        # ── 保险闸门 ①：进入删除倒计时立刻通知 ────────────────────────
        # 倒计时期间是唯一的抢救窗口，删完再通知等于没有窗口。
        if countdown_items and self._notify:
            eta_str = datetime.fromtimestamp(now_ts + delay_seconds).strftime("%m-%d %H:%M")
            shown = list(countdown_items[:NOTIFY_NAME_LIMIT])
            while shown:
                hidden = len(countdown_items) - len(shown)
                body = "\n".join(
                    "- %s\n  本地做种 %s ／ 保种要求 %s" % (name, seeding or "-", cycle or "-")
                    for name, seeding, cycle in shown
                )
                if hidden > 0:
                    body += f"\n…等 {hidden} 个"
                if len(body) <= NOTIFY_TEXT_MAX:
                    break
                shown.pop()
            else:
                body = f"（共 {len(countdown_items)} 个任务，名称过长未逐条列出）"
            logger.info(f"彩虹岛HR监控：{len(countdown_items)} 个任务进入删除倒计时")
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【彩虹岛HR监控】",
                text=f"{len(countdown_items)} 个任务已完成保种，进入删除倒计时：\n{body}\n\n"
                     f"将于 {self._delete_delay_hours:g} 小时后（约 {eta_str}）"
                     f"删除 QB 任务并连本地文件一起删除。\n"
                     f"如需保留：把这些种子移出 QB 分类「{self._category}」即可终止。",
            )

        # 执行删除
        if delete_hashes:
            logger.info(f"彩虹岛HR监控：删除 {len(delete_hashes)} 个已完成任务（含文件）")
            seeding_by_hash = {
                str(item.get("hash") or ""): str(item.get("local_seeding") or "")
                for item in compare_items
            }
            deleted_log: Dict[str, Dict[str, Any]] = self.get_data(DELETE_LOG_DATA_KEY) or {}
            if downloader_obj.delete_torrents(delete_file=True, ids=delete_hashes):
                for torrent_hash, name in zip(delete_hashes, delete_names):
                    # ── 保险闸门 ⑤：删除留档，事后可追溯 ──────────────
                    deleted_log[torrent_hash] = {
                        "name": name,
                        "deleted_at": now_ts,
                        "deleted_time": now_str,
                        "local_seeding": seeding_by_hash.get(torrent_hash, ""),
                        "hr_cycle": str((cycle_map.get(torrent_hash) or {}).get("hr_cycle") or ""),
                    }
                    completed_map.pop(torrent_hash, None)
                    cycle_map.pop(torrent_hash, None)
                # 只保留最近若干条，避免无限增长
                if len(deleted_log) > DELETE_LOG_LIMIT:
                    keep = sorted(
                        deleted_log.items(),
                        key=lambda kv: float(kv[1].get("deleted_at") or 0),
                        reverse=True,
                    )[:DELETE_LOG_LIMIT]
                    deleted_log = dict(keep)
                self.save_data(DELETE_LOG_DATA_KEY, deleted_log)
                # ── 保险闸门 ⑥：已删除的任务从本轮展示清单里摘掉 ────────
                # 上面刚把它们的 completed_map / cycle_map 记录清空，
                # 若 compare_items 里还留着这一条，详情页会因为两个 map 都
                # 查不到 H&R 周期，把卡片退化成「数据缺失 · 0.0%」——用户刚
                # 看到「已删除」，转头又看到一张「数据缺失」，会当成插件报错
                # （2026-09-30 实测：删除 Shadow.Force 后卡片仍显示数据缺失）。
                # 删除结果已在「最近删除」区块留档，卡片本身无需再保留。
                deleted_hashes = {str(h) for h in delete_hashes if h}
                compare_items = [
                    item for item in compare_items
                    if str(item.get("hash") or "") not in deleted_hashes
                ]
                self._last_compare_items = compare_items
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【彩虹岛HR监控】",
                        text=f"已删除 {len(delete_hashes)} 个已完成 H&R 任务（含本地文件）：\n"
                             + format_name_list_text(delete_names),
                    )
            else:
                logger.error("彩虹岛HR监控：删除任务失败")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【彩虹岛HR监控】",
                        text="删除已完成任务失败，请检查下载器状态。",
                    )

        # 匹配统计只在未冻结时刷新：冻结期间保持「此前匹配正常」的记忆，
        # 否则下一轮 prev_matched 变成 0，闸门会自行失效。
        if not frozen:
            self.save_data(
                MATCH_STATS_DATA_KEY,
                {
                    "matched": matched_local,
                    "compared": compared_local,
                    "site_count": len(site_tasks),
                    "updated_at": now_ts,
                    "updated_time": now_str,
                },
            )

        self.save_data(COMPLETED_DATA_KEY, completed_map)
        self.save_data(HR_CYCLE_DATA_KEY, cycle_map)

    @staticmethod
    def __get_seeding_seconds(torrent: Dict[str, Any], now_ts: float) -> float:
        """取本地做种时长（秒），用于 H&R 保种时长闸门。

        QB 的 seeding_time 单位为秒；个别下载器不提供该字段时，
        退回用「当前时间 - 完成时间」估算。

        :param torrent: QB 任务字典
        :param now_ts: 当前时间戳
        :return: 做种时长（秒）
        """
        try:
            value = float(torrent.get("seeding_time") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
        try:
            completion_on = float(torrent.get("completion_on") or 0)
        except (TypeError, ValueError):
            completion_on = 0.0
        if completion_on > 0:
            return max(0.0, now_ts - completion_on)
        return 0.0

    def __fetch_hr_tasks(self) -> Tuple[List[Dict[str, Any]], str]:
        """抓取并解析站点 H&R 页面。

        :return: (HR 任务列表, 错误信息)；成功时错误信息为空字符串
        """
        site = self.__get_site_config()
        if not site:
            return [], "未找到彩虹岛站点配置"

        try:
            res = RequestUtils(
                ua=site.get("ua"),
                cookies=site.get("cookie"),
                proxies=settings.PROXY if site.get("proxy") else None,
                timeout=site.get("timeout") or 20,
            ).get_res(url=self._hr_url)
        except Exception as err:
            return [], f"请求异常：{str(err)}"

        if res is None:
            return [], "无法连接站点"
        if res.status_code != 200:
            return [], f"站点返回状态码 {res.status_code}"

        return self.__parse_hr_page(res.text), ""

    @staticmethod
    def __parse_hr_page(html: str) -> List[Dict[str, Any]]:
        """解析 H&R 页面，提取未完成的 HR 任务。

        :param html: 页面 HTML
        :return: HR 任务列表
        """
        tasks: List[Dict[str, Any]] = []
        # 定位 H&R 数据表格（表头含"H&R百分比"）
        table_match = re.search(
            r'<table[^>]*>(?:(?!</table>).)*?H&R百分比(?:(?!</table>).)*?</table>',
            html, re.S
        )
        if not table_match:
            return tasks

        table_html = table_match.group(0)
        # 逐行解析
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if len(cells) < 6:
                continue

            # 标题：优先取 details.php 链接的 title 属性
            title = ""
            title_match = re.search(r'details\.php\?id=\d+[^>]*title="([^"]+)"', cells[1])
            if title_match:
                title = title_match.group(1).strip()
            else:
                title = re.sub(r'<[^>]+>', '', cells[1]).strip()

            # 跳过表头行
            if not title or title == "标题":
                continue

            # 副标题：站点在标题下方以 <br /> 分隔给出中文名与制作说明，
            # 用于 UHD原盘自动下载 插件记录缺失时兜底显示
            subtitle = ""
            subtitle_match = re.search(r'<br\s*/?>(.*)$', cells[1], re.S | re.I)
            if subtitle_match:
                subtitle = re.sub(r'<[^>]+>', '', subtitle_match.group(1)).strip()

            def clean(cell: str) -> str:
                """清理单元格文本。"""
                return re.sub(r'<[^>]+>', '', cell).strip()

            tasks.append(
                {
                    "title": title,
                    "subtitle": subtitle,
                    "hr_percent": clean(cells[2]),
                    "remain_time": clean(cells[3]),
                    "hr_cycle": clean(cells[4]),
                    "seeding_time": clean(cells[5]),
                }
            )
        return tasks

    @staticmethod
    def __parse_hr_cycle_hours(hr_cycle: str) -> Optional[float]:
        """解析 H&R 周期文本为小时数。

        支持 "5天"、"3天"、"120小时"、"72h" 等格式。

        :param hr_cycle: H&R 周期文本
        :return: 小时数；无法解析返回 None
        """
        if not hr_cycle:
            return None
        text = str(hr_cycle).strip()
        # 天
        match = re.search(r'([\d.]+)\s*天', text)
        if match:
            return float(match.group(1)) * 24
        # 小时
        match = re.search(r'([\d.]+)\s*(?:小时|h|H)', text)
        if match:
            return float(match.group(1))
        return None

    @staticmethod
    def __parse_seeding_hours(seeding_time: str) -> Optional[float]:
        """解析做种时间文本为小时数。

        支持 "3天02:48:29"、"14:38:16"、"2天01:15:50" 等格式。

        :param seeding_time: 做种时间文本
        :return: 小时数；无法解析返回 None
        """
        if not seeding_time:
            return None
        text = str(seeding_time).strip()
        total_hours = 0.0
        # 天数部分
        day_match = re.search(r'([\d.]+)\s*天', text)
        if day_match:
            total_hours += float(day_match.group(1)) * 24
            text = text[day_match.end():]
        # 时分秒部分
        time_match = re.search(r'(\d+):(\d+)(?::(\d+))?', text)
        if time_match:
            total_hours += int(time_match.group(1))
            total_hours += int(time_match.group(2)) / 60
            if time_match.group(3):
                total_hours += int(time_match.group(3)) / 3600
        elif not day_match:
            return None
        return total_hours

    def __get_uhd_record(self, name: str) -> Dict[str, Any]:
        """从 UHD原盘自动下载 插件的记录中查询任务对应的条目。

        优先按 qb_name（站点「下载」字段，与 QB 任务名一致）匹配，
        其次按归一化标题匹配，以兼容中文前缀与标点差异。

        :param name: QB 任务名
        :return: 记录字典；未找到返回空字典
        """
        try:
            processed_map = self.get_data(
                UHD_PROCESSED_DATA_KEY, plugin_id=UHD_PLUGIN_ID
            ) or {}
        except Exception as err:
            logger.warning(f"彩虹岛HR监控：读取 UHD 记录失败：{err}")
            return {}

        if not processed_map:
            return {}

        target = self.__normalize_title(name)
        for record in processed_map.values():
            if not isinstance(record, dict):
                continue
            # 优先按 qb_name 精确匹配
            qb_name = str(record.get("qb_name") or "")
            if qb_name and qb_name == name:
                return record
            # 其次按归一化标题匹配
            title = str(record.get("title") or "")
            if title and target and self.__normalize_title(title) == target:
                return record
        return {}

    def __get_uhd_titles(self, name: str) -> Tuple[str, str]:
        """查询任务对应的副标题与种子标题。

        :param name: QB 任务名或站点标题
        :return: (副标题, 种子标题)；未找到返回空字符串
        """
        record = self.__get_uhd_record(name)
        if not record:
            return "", ""
        subtitle = str(record.get("subtitle") or "").strip()
        seed_title = str(record.get("qb_name") or "").strip()
        return subtitle, seed_title

    def __find_qb_name(self, site_title: str) -> str:
        """在 QB 任务列表中反查站点标题对应的实际任务名。

        UHD原盘自动下载 插件记录缺失时（如种子在站点已下载完成被跳过），
        无法从记录中取到 qb_name，此处按归一化标题在 QB 任务中反查。

        :param site_title: 站点种子标题
        :return: 匹配到的 QB 任务名；未找到返回空字符串
        """
        if not site_title:
            return ""

        downloader_obj = self.__get_downloader()
        if not downloader_obj:
            return ""

        try:
            result = downloader_obj.get_torrents()
        except Exception as err:
            logger.warning(f"彩虹岛HR监控：获取 QB 任务失败：{err}")
            return ""

        # get_torrents 返回 (种子列表, 是否异常)
        if isinstance(result, tuple):
            torrents = result[0]
        else:
            torrents = result
        if not torrents:
            return ""

        target = self.__normalize_title(site_title)
        if not target:
            return ""

        # 优先精确匹配，其次包含匹配
        contains_match = ""
        for torrent in torrents:
            if isinstance(torrent, dict):
                name = str(torrent.get("name") or "")
            else:
                name = str(getattr(torrent, "name", "") or "")
            if not name:
                continue
            norm = self.__normalize_title(name)
            if norm == target:
                return name
            if not contains_match and target in norm:
                contains_match = name
        return contains_match

    def __find_hr_subtitle(self, task_name: str) -> str:
        """从站点 H&R 任务中反查任务对应的中文副标题。

        UHD原盘自动下载 插件记录缺失时，副标题无法从记录中取到，
        此处按 QB 任务名在站点 H&R 任务列表中匹配，取其副标题。

        :param task_name: QB 任务名
        :return: 中文副标题；未找到返回空字符串
        """
        if not task_name or not self._last_hr_tasks:
            return ""
        site_task = self.__find_site_task(task_name, self._last_hr_tasks)
        if not site_task:
            return ""
        return str(site_task.get("subtitle") or "").strip()

    @staticmethod
    def __normalize_title(title: str) -> str:
        """规范化标题，便于比对。

        去除中文、扩展名、多余空格，仅保留英文与数字特征，转小写。

        :param title: 原始标题
        :return: 规范化后的标题
        """
        # 去除扩展名
        text = re.sub(r'\.(mkv|mp4|iso|ts|avi)$', '', title.strip(), flags=re.I)
        # 去除中文字符（QB 任务标题常带中文前缀，站点标题为纯英文）
        text = re.sub(r'[\u4e00-\u9fff]+', ' ', text)
        # 统一分隔符为空格
        text = re.sub(r'[._\-]+', ' ', text)
        # 压缩空格
        text = re.sub(r'\s+', ' ', text).strip()
        return text.lower()

    def __calc_remain_time(self, hr_cycle: str, seeding_time: str) -> str:
        """计算剩余时间（H&R周期 - 做种时间）。

        :param hr_cycle: H&R 周期文本，如 "5天"
        :param seeding_time: 做种时间文本，如 "3天02:48:29"
        :return: 剩余时间文本，如 "45.2h"；无法计算返回 "-"
        """
        cycle_hours = self.__parse_hr_cycle_hours(hr_cycle)
        seeding_hours = self.__parse_seeding_hours(seeding_time)
        if cycle_hours is None or seeding_hours is None:
            return "-"
        remain_hours = cycle_hours - seeding_hours
        if remain_hours <= 0:
            return "已达标"
        return f"{remain_hours:.1f}h"

    def __score_site_task(self, normalized: str, local_tokens: List[str],
                          local_name: str, local_sig: set,
                          task: Dict[str, Any]) -> Optional[Tuple[int, float]]:
        """给「一个本地任务 × 一条站点记录」这一对打分。

        从 `__find_site_task` 里拆出来，供「逐条查找」与「互斥分配」共用同一套判据
        （两边各写一份迟早会漂移）。

        :param normalized: 本地标题的规范化结果
        :param local_tokens: 本地标题切分出的词
        :param local_name: 本地标题前 3 个词
        :param local_sig: 本地标题的片名特征词
        :param task: 站点 H&R 任务
        :return: (tier, score)。tier=2 为片名快速路径（完全一致／片名相等／互含），
                 tier=1 为兜底评分（须 ≥ 0.8 才算数）；被片名闸门拒绝时返回 None
        """
        site_title = self.__normalize_title(task.get("title") or "")
        if not site_title:
            return None
        # 完全一致直接命中（唯一不受片名闸门约束的快路径）
        if site_title == normalized:
            return (2, 1.0)
        site_tokens = site_title.split()
        site_name = " ".join(site_tokens[:3])
        # 片名特征必须至少有实质重合，否则只是「同规格、同发布组的另一个种子」
        # （规格词占比过高，仅凭它们重合不足以证明是同一个种子）
        site_sig = self.__name_signature(site_tokens)
        if not (local_sig and site_sig and (local_sig & site_sig)):
            return None
        # 片名完全一致或互相包含，视为同一任务
        if local_name and site_name and (
            local_name == site_name
            or local_name in site_name
            or site_name in local_name
        ):
            return (2, 1.0)
        # 兜底：按 token 交集比例评分
        local_set = set(local_tokens)
        site_set = set(site_tokens)
        if not local_set or not site_set:
            return None
        common = local_set & site_set
        return (1, len(common) / min(len(local_set), len(site_set)))

    def __find_site_task(self, local_title: str,
                         site_tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """根据本地任务标题查找对应的站点 H&R 任务。

        优先按片名（标题开头的英文词）匹配，避免因通用词（分辨率、编码等）
        导致误匹配。

        注意：这里是**逐条独立**查找，只看这一个本地任务，不关心站点记录有没有
        被别的本地任务用掉。需要「一条站点记录只归一个任务」时用
        `__assign_site_tasks`（判定与展示都走那一份分配结果）。

        :param local_title: 本地任务标题
        :param site_tasks: 站点未完成 H&R 任务列表
        :return: 匹配的站点任务；未匹配返回 None
        """
        normalized = self.__normalize_title(local_title)
        if not normalized:
            return None

        # 提取片名特征：取规范化标题的前 3 个词（通常是片名）
        local_tokens = normalized.split()
        local_name = " ".join(local_tokens[:3])
        # 片名特征词（规格锚点之前的词）：兜底评分前的硬闸门，见 SPEC_ANCHOR_TOKENS
        local_sig = self.__name_signature(local_tokens)

        best_task = None
        best_score = 0.0
        for task in site_tasks:
            scored = self.__score_site_task(
                normalized, local_tokens, local_name, local_sig, task)
            if scored is None:
                continue
            tier, score = scored
            if tier == 2:
                # 片名快速路径：命中即返回（与 v2.0.3 行为完全一致）
                return task
            if score > best_score:
                best_score = score
                best_task = task

        # 兜底匹配需达到较高阈值，避免误匹配
        if best_task is not None and best_score >= 0.8:
            return best_task
        return None

    def __assign_site_tasks(
        self, local_torrents: List[Any], site_tasks: List[Dict[str, Any]]
    ) -> Tuple[Dict[Any, Tuple[Dict[str, Any], int]], List[str]]:
        """把站点 H&R 记录**互斥**分配给本地任务（一条记录至多归一个任务）。

        为什么需要：`__find_site_task` 逐条独立调用 ⇒ 天然允许「多条本地任务共用
        同一条站点记录」。同年同规格同发布组的两部片子尤其容易踩（2.0.3 已把年份
        剔出片名特征，但那只是堵一个口子；国家／语言标记等仍可能钻空子）。

        做法：先算出所有合法的「本地 × 站点」候选，再按
        `tier`（片名快速路径优于兜底评分）→ `score` → 「本地序号、站点序号」
        的顺序贪心认领。先算全局再分配（而不是按本地任务顺序先到先得），
        是为了让**最像的那一对先配上**。

        安全性：被互斥挤掉的任务本轮会走「站点查无」分支，但该分支后面还有
        「保种时长闸门」（本地做种 ≥ H&R 周期 + 余量）兜底——没做够时间的种子
        照样不会被删，因此互斥不会造成误删。

        :param local_torrents: 本地任务列表
        :param site_tasks: 站点未完成 H&R 任务列表
        :return: ({本地任务 hash: (站点任务, 站点序号)}, 因互斥落空的本地任务标题列表)
        """
        candidates: List[Tuple[int, float, int, int, Any, str]] = []
        for seq, torrent in enumerate(local_torrents):
            torrent_hash = torrent.get("hash")
            if not torrent_hash:
                continue
            # 只有「下载进度达 HR 阈值」的任务才有资格占用站点记录：
            # 未达阈值的任务本就不产生 H&R 记录（见主循环的安全校验），
            # 让它们参与分配只会白白占掉别人的记录。
            try:
                progress = float(torrent.get("progress") or 0)
            except (TypeError, ValueError):
                progress = 0
            if progress < HR_PROGRESS_THRESHOLD:
                continue
            normalized = self.__normalize_title(torrent.get("name") or "")
            if not normalized:
                continue
            tokens = normalized.split()
            name = " ".join(tokens[:3])
            sig = self.__name_signature(tokens)
            for idx, task in enumerate(site_tasks):
                scored = self.__score_site_task(
                    normalized, tokens, name, sig, task)
                if scored is None:
                    continue
                tier, score = scored
                if tier < 2 and score < 0.8:
                    # 兜底评分未过阈值（与 __find_site_task 同口径）
                    continue
                candidates.append((tier, score, seq, idx, torrent_hash,
                                   str(torrent.get("name") or "")))

        # 全局贪心：tier 高者优先，同 tier 分数高者优先；同分按序号升序，
        # 保证结果稳定可复现（不依赖 dict/set 的遍历顺序）。
        candidates.sort(key=lambda c: (-c[0], -c[1], c[2], c[3]))
        used_local: set = set()
        used_site: set = set()
        assigned: Dict[Any, Tuple[Dict[str, Any], int]] = {}
        for tier, score, seq, idx, torrent_hash, _title in candidates:
            if seq in used_local or idx in used_site:
                continue
            used_local.add(seq)
            used_site.add(idx)
            assigned[torrent_hash] = (site_tasks[idx], idx)

        # 有候选、却因互斥没分到记录的任务：它们本轮会走「站点查无」，
        # 与 v2.0.3「两条都能匹配上」的行为不同，打日志留痕便于排障。
        conflicts: List[str] = []
        noted: set = set()
        for _tier, _score, seq, idx, _hash, title in candidates:
            if seq in used_local or seq in noted:
                continue
            if idx in used_site:
                noted.add(seq)
                conflicts.append(title)
        return assigned, conflicts

    @staticmethod
    def __name_signature(tokens: List[str]) -> set:
        """提取标题的「片名特征词」：第一个规格锚点之前的词，剔除停用词。

        原盘种子名里规格词占绝大多数（分辨率 / 容器 / 编码 / 音轨 / 发布组），
        真正能区分「是不是同一个种子」的只有片名部分。匹配前先用它做闸门，
        避免同规格、同发布组的两部电影互相误配（v2.0.1）。

        :param tokens: 规范化标题切分出的词列表
        :return: 片名特征词集合；标题里没有片名词时返回空集
        """
        signature: set = set()
        for token in tokens:
            if token in SPEC_ANCHOR_TOKENS:
                break
            if token in NAME_STOP_TOKENS:
                continue
            # 年份不构成片名特征（见 YEAR_TOKEN_RE 说明）
            if YEAR_TOKEN_RE.match(token):
                continue
            signature.add(token)
        return signature

    def __match_site_title(self, local_title: str, site_titles: set) -> bool:
        """判断本地任务标题是否匹配站点未完成任务。

        :param local_title: 本地任务标题
        :param site_titles: 站点未完成任务标题集合
        :return: 是否匹配
        """
        normalized = self.__normalize_title(local_title)
        if not normalized:
            return False
        if normalized in site_titles:
            return True
        # 模糊匹配：比较规范化后的英文特征片段
        local_tokens = set(normalized.split())
        for site_title in site_titles:
            if not site_title:
                continue
            site_tokens = set(site_title.split())
            if not site_tokens:
                continue
            # 交集占比达到 60% 视为同一任务
            common = local_tokens & site_tokens
            if len(common) / min(len(local_tokens), len(site_tokens)) >= 0.6:
                return True
        return False

    def __get_downloader(self) -> Optional[Any]:
        """获取下载器实例。

        :return: 下载器实例；获取失败返回 None
        """
        services = DownloaderHelper().get_services(name_filters=[self._downloader])
        if not services:
            return None
        for service_name, service_info in services.items():
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                logger.warning(f"彩虹岛HR监控：下载器 {service_name} 不是 QB 类型")
                continue
            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"彩虹岛HR监控：下载器 {service_name} 未连接")
                continue
            return downloader_obj
        return None

    @staticmethod
    def __get_site_config() -> Optional[dict]:
        """获取彩虹岛站点配置。

        :return: 站点配置字典；未找到返回 None
        """
        from app.db.site_oper import SiteOper
        site = SiteOper().get_by_domain("ptchdbits.co")
        if not site:
            return None
        return {
            "name": site.name,
            "domain": site.domain,
            "url": site.url,
            "cookie": site.cookie,
            "ua": site.ua,
            "proxy": site.proxy,
            "timeout": site.timeout,
        }

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None
