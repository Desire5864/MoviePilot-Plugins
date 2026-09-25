import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from lxml import etree

from app.chain.media import MediaChain
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils

# 已处理种子记录持久化键名
PROCESSED_DATA_KEY = "uhd_processed_map"
# 已处理记录最多保留条数
PROCESSED_LIMIT = 500
# 简介迁移标记（旧版站点简介 → TMDB 简介）
INTRO_MIGRATED_KEY = "intro_migrated_to_tmdb"
# 默认下载标签（站点未单独配置 tag 时使用）
DOWNLOAD_TAG = "UHD自动下载"

# 站点阀门配置键（v2.11.0 起）：单个数组键取代「每站点一个布尔键」。
# 元素沿用 _site_switch_key() 生成的站点键名（如 enable_ptchdbits_co），
# 这样旧配置里的布尔键与新数组元素同名，迁移时无需映射表。
#
# v2.12.0 语义微调：本键管的是「**采集**」（抓不抓），不再等同于「推送」。
# 是否推送由下面三个表格键单独控制。
SITE_VALVE_KEY = "enable_sites"

# 下载分类 / 标签 / 路径 / 推送开关的配置键前缀（v2.12.0 起，v2.13.0 增标签）。
# 每个站点一组，后缀为 _site_conf_alias(domain)（域名里的 '.' 换成 '_'）：
#   dw_cat_<alias>    QB 分类名（空 = 不归类）
#   dw_tag_<alias>    QB 标签（空 = 回退到 DOWNLOAD_TAG 默认标签）
#   dw_path_<alias>   保存路径（空 = 用下载器默认目录）
#   dw_push_<alias>   是否推送该站点抓到的种子（False = 只采集展示，不推送）
# 四者任一缺失时回退到 _site_configs 里写死的默认值，
# 因此老配置（没有这些键）行为与 v2.11.0 完全一致。
DW_CATEGORY_PREFIX = "dw_cat_"
DW_TAG_PREFIX = "dw_tag_"
DW_PATH_PREFIX = "dw_path_"
DW_PUSH_PREFIX = "dw_push_"

# 推送模式（push_mode）可选值
PUSH_MODE_ALL = "all"               # 全部推送：不筛促销（现状）
PUSH_MODE_FREE_ONLY = "free_only"   # 只推免费：抓取照常抓全量，推送时按行级 is_free 跳过收费
PUSH_MODE_FREE_FIRST = "free_first" # 免费优先：先推免费，收费的仍推（延后处理）

# 站点「免费」促销在列表页里的标记形态。三站渲染不一致，需覆盖：
#   彩虹岛  <font class='free'>免费</font>（文本，class 用单引号）
#   我堡     <img class="pro_free" ... alt="Free">（图标）
#   天空     <img class="pro_free" ... alt="Free">（图标）
# 用正则统一识别，命中即视为「免费」。
_FREE_MARK_RE = re.compile(
    r'(?:class=["\'](?:pro_)?free["\']|alt=["\']Free["\'])', re.I
)

# 站点「H&R（Hit & Run）」标记在列表页里的形态（两站各有一套，需同时覆盖）：
#   我堡    <img class="hitandrun" src="pic/trans.gif" alt="H&amp;R" title="H&amp;R" />
#           站点同款外观（styles/sprites.css）：
#             img.hitandrun{width:35px;height:12px;
#               background:url(icons.gif?2) no-repeat -100px -171px}
#           —— 即 35x12 深色底(#060619)白字「H&R」图标（2026-09-24 抠图确证）
#   彩虹岛  <div class="circle"><div class="circle-text" ...>h5</div></div>
#           站点同款外观（include/css/hnr3.css）：
#             .circle{width:14px;height:14px;border-radius:50%;
#               background-color:#1E90FF;border:1px solid #1e90ff}
#             .circle-text{width:14px;height:14px;line-height:14px;
#               text-align:center;font-size:10px;color:white}
#           —— 即 14x14 蓝色圆标 + 白色 10px 字，圆内文字为 h+数字
#           （2026-09-24 实测 h5 / h3；站内 H&R 页即 hnr.php，「hnr」= Hit aNd Run）
# 天空列表页不渲染任何 H&R 标记（2026-09-24 实测 0 处）。
# 命中即视为「该种子带 H&R 考核」——**仅用于详情页展示与推送通知提示**，
# 绝不参与「该不该推送」的判定。
_HR_MARK_RE = re.compile(
    r'(?:class=["\']hitandrun["\']'                     # 我堡：图标 class
    r'|(?:alt|title)=["\']H(?:&amp;|&)?R["\']'          # 通用：alt / title 文本
    r'|class=["\']circle-text["\'][^>]*>\s*[hH]\d)',    # 彩虹岛：圆形 H&R 徽章
    re.I
)
# 彩虹岛圆标内的文字（h+数字），供详情页复刻**站点同款**蓝色圆标时取用
_HR_CIRCLE_RE = re.compile(
    r'class=["\']circle-text["\'][^>]*>\s*([hH]\d+)', re.I
)
# 「站点同款」徽章形态判据：彩虹岛圆标文字形如 h5 / h3，其余（我堡）走图标形态
_HR_CIRCLE_TEXT_RE = re.compile(r'^[hH]\d+$')

# 详情页 H&R 徽章尺寸（v2.9.8 起为「小巧」档，按用户确认的效果图定稿）。
# 🔴 徽章高度与副标题行高**必须分开定义**：副标题是 14px 字、需要 20px 行高，
#    徽章只要 12px。早期把两者绑成同一个常量（HR_BADGE_HEIGHT 既当副标题行高
#    又当徽章高度），一旦把徽章缩小，副标题行高会被一起压扁 —— 现拆成两个常量。
HR_SUBTITLE_LINE_HEIGHT = 20   # 副标题 line-height（固定，不随徽章尺寸变化）
HR_BADGE_HEIGHT = 12           # 徽章高度（彩虹岛与我堡一致）
HR_BADGE_PAD_X = 4             # 徽章左右内边距（宽度随标识文字自适应）
HR_BADGE_RADIUS = 2            # 徽章圆角
HR_BADGE_FONT = 8              # 徽章字号
HR_BADGE_OFFSET_Y = 1          # 垂直微调：行高居中后再下移 1px（视觉居中，用户确认）

# 详情页「站点分类胶囊」的站点配色（键为站点中文名，值为胶囊圆点颜色）。
# 配色沿用 H&R 徽章原则「同站点配色」：彩虹岛蓝 #1E90FF、我堡黑 #060619；
# 天空站列表页无 H&R 徽章可参照，取站点头部主题蓝 #2bb24c（与站点 logo 一致）。
# UBits 取站点标签底色 #e52d15（列表页 team1=1 / tag_id3=1 的标签背景色，实测）。
# 未在映射里的站点（如「未知站点」）回退灰色 #888888。
_SITE_COLORS = {
    "彩虹岛": "#1E90FF",
    "我堡": "#060619",
    "天空": "#2bb24c",
    "UBits": "#e52d15",
}

# 详情页字段缓存最多保留条数（超出后按写入时间淘汰最旧的）
DETAIL_CACHE_LIMIT = 800
# 影片简介缓存最多保留条数
INTRO_CACHE_LIMIT = 500
# 负结果（详情页没有该字段 / TMDB 未识别）的重试间隔（秒）。
# 站点侧确实没有该字段时，每轮重试都是白打一次请求，这里给一个较长的静默期
NEGATIVE_CACHE_TTL = 6 * 3600
# 每轮补齐历史记录时最多发起的**真实网络请求**数
FILL_MAX_ATTEMPTS = 5

# 列表页「存活时间」列的解析规则（按顺序匹配，长单位在前避免 "mo" 被当成 "m"）
_AGE_TOKEN_RE = re.compile(
    r'(\d+)\s*(个月|星期|分钟|小时|年|月|周|天|日|时|分|秒|mo|y|w|d|h|m|s)'
)
_AGE_FACTORS = {
    "年": 365 * 86400, "个月": 30 * 86400, "月": 30 * 86400,
    "周": 7 * 86400, "星期": 7 * 86400,
    "天": 86400, "日": 86400,
    "小时": 3600, "时": 3600,
    "分钟": 60, "分": 60,
    "秒": 1,
    "y": 365 * 86400, "mo": 30 * 86400, "w": 7 * 86400,
    "d": 86400, "h": 3600, "m": 60, "s": 1,
}


def _parse_age_seconds(text: Any) -> Optional[int]:
    """把站点列表页「存活时间」列的文本换算为秒数。

    站点该列格式形如 "1天5时"、"3时20分"、"45分"、"1周2天"（值越小越新）。
    仅用于展示与断言，**不参与排序**：取数一律保留站点自己的展示顺序。

    :param text: 存活时间文本
    :return: 秒数；无法识别时返回 None
    """
    if not text:
        return None
    matches = _AGE_TOKEN_RE.findall(str(text))
    if not matches:
        return None
    total = 0
    for number, unit in matches:
        total += int(number) * _AGE_FACTORS.get(unit, 0)
    return total


# QB 任务状态 → 中文描述（键为 QB API 的 state 字段，小写）
_QB_STATE_TEXT = {
    "downloading": "下载中",
    "forceddl": "下载中",
    "metadl": "获取元数据中",
    "stalleddl": "下载中（无速度）",
    "checkingdl": "校验中",
    "allocating": "分配空间中",
    "queueddl": "排队中",
    "pauseddl": "已暂停",
    "stoppeddl": "已暂停",
    "uploading": "做种中",
    "forcedup": "做种中",
    "stalledup": "做种中（无连接）",
    "queuedup": "排队做种中",
    "checkingup": "校验中",
    "pausedup": "已完成（做种已暂停）",
    "stoppedup": "已完成（做种已暂停）",
    "moving": "移动文件中",
    "error": "出错",
    "missingfiles": "文件丢失",
    "unknown": "状态未知",
}

# QB 任务名匹配时需要忽略的技术词与版本标记
_QB_MATCH_STOP_WORDS = {
    "uhd", "bluray", "blu", "ray", "bd", "remux", "web", "dl", "webdl",
    "hdr", "hdr10", "dovi", "dv", "sdr", "hevc", "avc", "h264", "h265",
    "x264", "x265", "truehd", "atmos", "dts", "dtshd", "ma", "ddp", "dd",
    "ac3", "flac", "aac", "lpcm", "pcm", "5", "1", "7", "2", "0", "51", "71",
    "2160p", "1080p", "720p", "4k", "8bit", "10bit", "v1", "v2", "v3",
    "repack", "proper", "internal", "complete", "usa", "eur", "ger",
    "aus", "ita", "hkg", "jpn", "fra", "cc", "criterion", "collection",
}


def _format_speed(bytes_per_sec: Any) -> str:
    """把 QB 的字节/秒速度格式化为可读文本。

    :param bytes_per_sec: 速度（字节/秒）
    :return: 形如 "4.1 MB/s" 的文本；无速度时返回 "-"
    """
    try:
        speed = float(bytes_per_sec or 0)
    except (TypeError, ValueError):
        return "-"
    if speed <= 0:
        return "-"
    if speed < 1024 * 1024:
        return f"{speed / 1024:.0f} KB/s"
    return f"{speed / 1024 / 1024:.1f} MB/s"


class UhdBlurayAutoDownload(_PluginBase):
    """4K UHD BluRay 原盘自动下载插件。

    定时抓取彩虹岛/我堡/天空/UBits 站点的 UHD BluRay 原盘列表，取**站点顺序最
    前面**的 latest_count 条（与站点网页逐条对应），把其中尚未下载、也
    未被本插件推送过的种子自动推送到 QB 下载器，并按站点指定分类与路径。
    """

    # 插件名称
    plugin_name = "UHD原盘自动下载"
    plugin_desc = "监控多站点4K UHD原盘并推送下载器。采集站点可勾选，分类/标签/路径逐站可配，支持只推免费。"
    # 插件图标
    plugin_icon = "UHD.png"
    # 插件版本
    plugin_version = "2.19.0"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = "https://github.com/Desire5864"
    # 插件配置项ID前缀
    plugin_config_prefix = "uhdblurayautodownload_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 站点配置：域名 -> 列表页地址、QB分类、保存路径、标签、筛选模式
    #
    # list_url        列表页地址，可为字符串或字符串列表。**优先写字符串**：
    #                 直接填在浏览器里打开的那个地址，插件取到的就是网页上
    #                 看到的顺序（含站点置顶段），逐条对得上。
    #                 需要多值筛选时用**站点自己的写法**（如天空的
    #                 medium13=1&medium14=1），而不是拆成多个地址——拆开后
    #                 必须自己合成一条序列，那一步无论怎么排都无法与任何
    #                 单个网页对应（v2.8.x 就是这么踩坑的）。
    #                 确实要配多个地址时，按配置顺序拼接、按种子 ID 去重，
    #                 **不做重排**，以保住站点自己的展示优先级。
    #                 **地址里不要带 sort 参数**：站点对未文档化的 sort 取值
    #                 的解释不受我们控制，带上它就可能拿到与浏览器打开的
    #                 默认视图不同的排列，进而与站点网页对不上。
    # category        QB 分类名
    # save_path       QB 保存路径
    # tag             QB 标签（不配置时回退到 DOWNLOAD_TAG）
    # filter_mode     "uhd_title"  标题需匹配 UHD BluRay
    #                 "uhd_2160p"  标题需**同时**含 2160p 与 UHD Blu-ray（v2.17.0 起，UBits 用）
    #                 "bluray_only" 站点已按媒介筛选，仅排除 WEB-DL/HDTV/Encode 等
    #                 "none"        不做标题筛选（列表页地址已带站点侧筛选条件）
    #                 "hdhome_diy"  家园专用（见 __parse_hdhome_page）
    # diy_team        可选，标题需含该制作组（大小写不敏感）。与家园同名键语义一致，
    #                 区别是家园走独立解析函数、这里直接在 __parse_list_page 里过滤，
    #                 **不重排**，保住站点自己的展示顺序。
    # no_download_marks  站点「进度」列中代表「尚无下载记录」的取值。
    #                 各站渲染不一致：彩虹岛/我堡为 "-"/"--"，天空则用 "0%"，
    #                 若按同一口径判断会导致天空站永不推送。
    # has_progress_column  该站列表页是否有「进度」列（默认 True）。
    #                 ⚠️ 设为 False 的站点**完全跳过进度判定**，见 UBits 条目说明。
    # list_subtitle_index  可选，列表页副标题取标题单元格 text 分段的第几段
    #                 （0 起算）。缺省沿用旧口径「取最后一段」。
    # default_enabled    未保存过配置时该站点开关的默认状态
    _site_configs = {
        "ptchdbits.co": {
            "name": "彩虹岛",
            "list_url": "https://ptchdbits.co/torrents.php?medium=19",
            "category": "彩虹岛&HR",
            "save_path": "/原盘",
            "tag": "UHD自动下载",
            # 站点 medium=19 已按 UHD Blu-ray 媒介筛选，标题不一定含 UHD，
            # 因此仅排除 WEB-DL/HDTV/Encode 等非原盘
            "filter_mode": "bluray_only",
            "no_download_marks": ("-", "--"),
            "default_enabled": True,
        },
        "ourbits.club": {
            "name": "我堡",
            "list_url": "https://ourbits.club/torrents.php?standard=5",
            "category": "OurBits原盘",
            "save_path": "/原盘",
            "tag": "UHD自动下载",
            # 站点 standard=5 仅按 2160p 分辨率筛选，需按标题匹配 UHD BluRay
            "filter_mode": "uhd_title",
            "no_download_marks": ("-", "--"),
            "default_enabled": True,
        },
        "hdsky.me": {
            "name": "天空",
            # 站点自己的多值筛选写法是 medium13=1&medium14=1，一个地址就能把
            # UHD Blu-ray（medium=13）与 UHD Blu-ray/DIY（medium=14）全取回来，
            # 顺序与浏览器打开的页面完全一致（站点置顶段在最前）。
            # 实测：medium=13 只筛到其中一个子类；medium=13&medium=14 只取
            # 最后一个；而拆成两个地址再自行合并会把置顶段整个冲掉
            # （v2.8.x 的回归，v2.8.4 修回单地址）。
            "list_url": (
                "https://hdsky.me/torrents.php?medium13=1&medium14=1"
                "&incldead=0&spstate=0&inclbookmarked=0"
                "&search=&search_area=0&search_mode=0"
            ),
            "category": "HDSky原盘",
            "save_path": "/ISO",
            "tag": "UHD原盘下载",
            # 列表页地址已按 UHD Blu-ray / DIY 媒介筛选，无需再做标题匹配
            "filter_mode": "none",
            # 天空的「进度」列用 "0%" 表示尚无下载记录
            "no_download_marks": ("-", "--", "0%", "0"),
            # 新站点默认关闭，由用户手动开启后再开始推送
            "default_enabled": False,
        },
        "hdhome.org": {
            "name": "家园",
            "list_url": "https://hdhome.org/torrents.php",
            # 分类/路径/标签为推送时的占位配置，当前 push_enabled=False 暂不使用
            "category": "HDHome原盘",
            "save_path": "/原盘",
            "tag": "UHD自动下载",
            # 家园列表页混着置顶行与其它分类，且排序非严格按发布时间，
            # 不能沿用「取列表最前 N 条」。用专用解析 filter_mode="hdhome_diy"：
            # 整页抓取后按三条规则过滤 ——
            #   ① 标题含 2160p 与 UHD Blu-ray（大小写不敏感，兼容 UHD BluRay 写法）
            #   ② 标题含制作组 DiY@HDHome（大小写不敏感）
            #   ③ 发种时间 < max_age_hours（时间列带精确时间戳 span title）
            # 过滤后按发种时间倒序取最新 N 条。
            "filter_mode": "hdhome_diy",
            "diy_team": "diy@hdhome",
            "max_age_hours": 120,
            "no_download_marks": ("-", "--", "0%", "0"),
            # 先只上架抓取展示，暂不推送（推送逻辑照常解析但不落 QB）
            "push_enabled": False,
            "default_enabled": True,
        },
        "ubits.club": {
            "name": "UBits",
            # 列表页地址就是浏览器里打开的地址，站点侧已带两道筛选：
            # medium10=1（UHD Blu-ray）+ standard5=1（2160p）。
            # **不要追加 sort 参数**（见上方 list_url 说明）。
            "list_url": "https://ubits.club/torrents.php?medium10=1&standard5=1",
            "category": "UBits原盘",
            "save_path": "/原盘",
            "tag": "UHD自动下载",
            # 站点侧已按媒介 + 分辨率筛过，这里再按标题复核一道
            # （实测该地址下 100 条：2160p 命中 100、UHD Blu-ray 命中 99）。
            # 真正起作用的筛子是下面的 diy_team —— 100 条里只有 17 条命中，
            # 也就是说这个列表页混着别家的 DIY/官转，靠制作组才能收敛到 UBits 自制。
            "filter_mode": "uhd_2160p",
            # 制作组取「去掉分隔符的组名」而非 "-DIY@UBits"：
            # 标题里的形态是 "…5.1-DIY@UBits"，前导 "-" 只是分隔符；
            # 实测 100 条里 17 条命中，且不存在不带 "-" 的写法，
            # 去掉 "-" 只会在站点将来改分隔符时更容忍，不会误伤。
            "diy_team": "DIY@UBits",
            # 站点新上架，勾选与推送都由用户手动开
            "default_enabled": False,
            "push_enabled": True,
            # 🔴 UBits 列表页**没有「进度」列**，这里是本次适配最实质的一处改动。
            #    实测该页列序：td[3]存活 td[4]大小 td[5]评论 td[6]…… td[8]是
            #    「盒子」(class="seed-box-policy-column"，取值 100% 或空)，
            #    td[9]是发布人（"匿名"）。
            #    若沿用其它站的进度判定，td[8] 的 "100%" 会被认成「站点侧已下载
            #    完成」——实测前 100 条里有 65 条是 100%，即 65 条会被静默跳过。
            #    整页也没有任何用户级下载状态可用（"已下载" 出现 0 次）。
            #    → 该站关闭进度列读取，详情页「站点进度」显示 —，
            #      去重改由「插件历史记录 + QB 任务名核对」两道兜底
            #      （见 __process_site 里的 qb 兜底段）。
            "has_progress_column": False,
            # 该站标题单元格是「英文标题 <br> 中文副标题 <br> 标签… <br> 评分」：
            #   [0] 英文标题  [1] 中文副标题  [2..] 标签/评分（末段是豆瓣分，如 "7.4"）
            # 旧的「取最后一段」口径在本站会取到评分或末位标签，故显式指定第 2 段。
            "list_subtitle_index": 1,
        },
    }

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _downloader: str = ""
    # 各站点开关状态：域名 -> 是否启用（采集）
    _site_enabled: Dict[str, bool] = {}
    # 下载分类 / 路径 / 推送开关的表单覆盖值（v2.12.0 起）：域名 -> {category/save_path/push_enabled}
    # 只存「表单里显式配过」的项，其余由 __site_effective_conf() 回退到 _site_configs 默认值
    _dw_override: Dict[str, Dict[str, Any]] = {}
    _interval_minutes: int = 15
    # 每个站点只采集最新 N 条
    _latest_count: int = 5
    # 推送模式：all / free_only / free_first（见 PUSH_MODE_* 常量）
    _push_mode: str = PUSH_MODE_ALL
    _run_once: bool = False
    # 最近一次检查时间
    _last_check_time: Optional[str] = None
    # 最近一次检查错误
    _last_error: str = ""
    # 最近一次发现的种子明细
    _last_items: List[Dict[str, Any]] = []
    # 详情页字段缓存：f"{domain}:{torrent_id}" -> {"subtitle", "seed_name", "ts"}
    # 键带域名：各站点的种子 ID 都是站点内自增数字，只用 ID 会跨站互相覆盖
    _detail_cache: Dict[str, Dict[str, Any]] = {}
    # 影片简介缓存：f"{title}|{subtitle}" -> {"text", "ts"}
    _intro_cache: Dict[str, Dict[str, Any]] = {}
    # 站点配置缓存：domain -> 站点配置（同一轮内复用，避免反复查库）
    _site_conf_cache: Dict[str, Optional[dict]] = {}
    # 本轮已发出的真实网络请求数（详情页 + TMDB），用于给补齐流程计配额
    _fetch_count: int = 0
    # 已处理记录本轮是否被改动（决定收尾时是否需要落库）
    _map_dirty: bool = False

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
        self._site_enabled = {}
        self._interval_minutes = 15
        self._latest_count = 5
        self._push_mode = PUSH_MODE_ALL
        self._run_once = False
        self._last_check_time = None
        self._last_error = ""
        self._last_items = []
        self._detail_cache = {}
        self._intro_cache = {}
        self._site_conf_cache = {}
        self._fetch_count = 0
        self._map_dirty = False

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._downloader = str(config.get("downloader") or "").strip()

        # 站点阀门（v2.11.0 起）：
        #   新版 = 单个数组键 enable_sites，元素为 _site_switch_key(domain)
        #   旧版 = 每个站点一个布尔键 enable_<domain>
        # 判定优先级：enable_sites 存在则以它为准（最后一次保存的表单是权威），
        # 否则回退读旧布尔键；两者都缺失时按站点 default_enabled 取值。
        raw_sites = config.get(SITE_VALVE_KEY)
        if isinstance(raw_sites, (list, tuple, set)):
            selected = {str(x) for x in raw_sites}
            for domain in self._site_configs:
                # 数组里没有就是「没勾」，不看旧布尔键——避免用户取消勾选后被旧值顶回来
                self._site_enabled[domain] = self._site_switch_key(domain) in selected
        else:
            for domain, site_conf in self._site_configs.items():
                key = self._site_switch_key(domain)
                if key in config:
                    self._site_enabled[domain] = bool(config.get(key))
                else:
                    self._site_enabled[domain] = bool(site_conf.get("default_enabled", True))

        # 下载分类 / 标签 / 路径 / 推送开关（v2.12.0 起，v2.13.0 增标签）：
        #   表单表格里逐站点可编辑，缺键时回退到 _site_configs 的写死默认值，
        #   因此老配置（没有这些前缀的键）行为与 v2.11.0 保持一致。
        # 注意：只把「配置里有显式键」的值写进覆盖表，
        #       不预先铺满全部站点 —— __site_conf() 里按需回退，避免脏值扩散。
        self._dw_override = {}
        for domain, site_conf in self._site_configs.items():
            alias = self._site_conf_alias(domain)
            override: Dict[str, Any] = {}
            cat_key = DW_CATEGORY_PREFIX + alias
            tag_key = DW_TAG_PREFIX + alias
            path_key = DW_PATH_PREFIX + alias
            push_key = DW_PUSH_PREFIX + alias
            if cat_key in config:
                override["category"] = str(config.get(cat_key) or "").strip()
            if tag_key in config:
                override["tag"] = str(config.get(tag_key) or "").strip()
            if path_key in config:
                override["save_path"] = str(config.get(path_key) or "").strip()
            if push_key in config:
                override["push_enabled"] = bool(config.get(push_key))
            if override:
                self._dw_override[domain] = override

        try:
            self._interval_minutes = max(5, int(config.get("interval_minutes") or 15))
        except (TypeError, ValueError):
            self._interval_minutes = 15
        try:
            self._latest_count = max(1, int(config.get("latest_count") or 5))
        except (TypeError, ValueError):
            self._latest_count = 5
        # 推送模式：未知取值一律回退到 all（现状），避免配置脏数据导致异常
        push_mode = str(config.get("push_mode") or PUSH_MODE_ALL)
        self._push_mode = push_mode if push_mode in (
            PUSH_MODE_ALL, PUSH_MODE_FREE_ONLY, PUSH_MODE_FREE_FIRST
        ) else PUSH_MODE_ALL
        self._run_once = bool(config.get("run_once"))

        # 立即执行一次：执行后自动关闭开关
        if self._run_once:
            logger.info("UHD原盘自动下载：立即执行一次检查")
            self.check_uhd()
            # update_config 是整键覆盖写（systemconfig.set）：
            # 必须以当前完整 config 为基底回写、仅翻转 run_once，
            # 逐字段手工枚举会在新增配置项时漏掉（v2.9.0/v2.9.1 曾因此把 push_mode 覆盖丢失）
            new_config: Dict[str, Any] = dict(config)
            new_config["run_once"] = False
            self.update_config(new_config)

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
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。

        :return: Vuetify 表单结构与默认配置
        """
        # 默认配置：站点阀门用单个数组键，默认勾上 default_enabled 为真的站点，
        # 新增站点会自动出现在选项里。
        # 注意：这里**不再**生成旧版的每站点布尔键（enable_<domain>）——
        # 新装用户用不到它们；老用户配置里的旧键由 init_plugin 兼容读取。
        default_config: Dict[str, Any] = {
            "enabled": False,
            "notify": False,
            "downloader": "",
            "interval_minutes": 15,
            "latest_count": 5,
            "push_mode": PUSH_MODE_ALL,
            "run_once": False,
            SITE_VALVE_KEY: self.__default_enabled_keys(),
        }
        # 下载分类 / 路径 / 推送开关（v2.12.0 起）：逐站点生成默认值，
        # 取值 = _site_configs 里写死的 category / save_path / push_enabled。
        for domain, site_conf in self._site_configs.items():
            alias = self._site_conf_alias(domain)
            default_config[DW_CATEGORY_PREFIX + alias] = str(site_conf.get("category") or "")
            default_config[DW_TAG_PREFIX + alias] = str(site_conf.get("tag") or "")
            default_config[DW_PATH_PREFIX + alias] = str(site_conf.get("save_path") or "")
            default_config[DW_PUSH_PREFIX + alias] = bool(site_conf.get("push_enabled", True))

        return [
            {
                "component": "VForm",
                "content": [
                    # ===== 分区一：全局设置 =====
                    {
                        "component": "div",
                        "props": {"class": "text-caption text-medium-emphasis mb-2"},
                        "content": [
                            {"component": "VChip", "props": {"size": "small", "variant": "text"}, "text": "全局设置"}
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"dense": True, "align": "center", "class": "mb-2"},
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
                                            "color": "primary",
                                            "hideDetails": True,
                                            "density": "compact",
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
                                            "color": "primary",
                                            "hideDetails": True,
                                            "density": "compact",
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
                                            "color": "primary",
                                            "hideDetails": True,
                                            "density": "compact",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 站点阀门（v2.11.0 引入，v2.12.0 起语义收窄为「采集」）：
                    # 一个多选下拉取代原先每站点一个开关；是否推送另由下方卡片组的开关控制。
                    {
                        "component": "VRow",
                        "props": {"align": "center", "class": "mb-1"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": SITE_VALVE_KEY,
                                            "label": "采集站点",
                                            "multiple": True,
                                            "chips": True,
                                            "closableChips": True,
                                            "items": [
                                                {
                                                    "title": f"{site_conf.get('name') or domain}"
                                                             f"（{domain}）",
                                                    "value": self._site_switch_key(domain),
                                                }
                                                for domain, site_conf
                                                in self._site_configs.items()
                                            ],
                                            "placeholder": "点击展开勾选要采集的站点",
                                            "persistentHint": True,
                                            "hint": "只有勾选的站点才会被抓取",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {"component": "div", "props": {"class": "py-3"}},
                    # 下载器 / 间隔 / 条数 / 推送模式：2×2 两列对齐
                    {
                        "component": "VRow",
                        "props": {"class": "mb-2"},
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
                                            "density": "compact",
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
                                            "model": "interval_minutes",
                                            "label": "检查间隔（分钟）",
                                            "placeholder": "默认15，最小5",
                                            "type": "number",
                                            "density": "compact",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"class": "mb-2"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "latest_count",
                                            "label": "每站采集最新条数",
                                            "placeholder": "默认5，只处理列表页最新N条",
                                            "type": "number",
                                            "density": "compact",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "push_mode",
                                            "label": "推送模式",
                                            "density": "compact",
                                            "items": [
                                                {"title": "全部推送（不筛选促销）", "value": PUSH_MODE_ALL},
                                                {"title": "免费优先（先推免费，收费排后）", "value": PUSH_MODE_FREE_FIRST},
                                                {"title": "只推免费（收费直接跳过）", "value": PUSH_MODE_FREE_ONLY},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "text-caption mt-2 mb-3",
                            "text": "定时抓取已勾选站点的 UHD BluRay 原盘列表，每站只处理最新 N 条，"
                                    "筛掉已有下载记录的种子；「推送」开关打开的站点才自动推到下载器。"
                                    "未保存过配置时：彩虹岛、我堡、家园默认勾选，天空与 UBits 需手动勾选。"
                                    "家园另按 2160p UHD Blu-ray + DiY@HDHome + 发种<120H 三规则过滤；"
                                    "UBits 按 2160p UHD Blu-ray + DIY@UBits 两规则过滤，"
                                    "该站列表页无进度列，站点进度以 — 展示，去重改由插件记录 + QB 任务名核对承担。",
                        },
                    },
                    {"component": "VDivider", "props": {"class": "my-3"}},
                    # ===== 分区二：站点下载配置 =====
                    {
                        "component": "div",
                        "props": {"class": "text-caption text-medium-emphasis mb-2"},
                        "content": [
                            {"component": "VChip", "props": {"size": "small", "variant": "text"}, "text": "站点下载配置"}
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "text-caption mb-2",
                            "text": "一行一个站点，直接编辑分类、标签与保存路径；"
                                    "「推送」关掉后照常采集但不推送。新增站点会自动往下排。",
                        },
                    },
                    {
                        "component": "VSheet",
                        "props": {
                            "class": "px-3 py-1 mb-2 rounded",
                            "style": "border:1px solid #e3e6ea; border-left:4px solid #e8590c; background:#fbfcfd;",
                        },
                        "content": [
                            {
                                "component": "VRow",
                                "props": {"dense": True, "align": "center", "class": "py-0"},
                                "content": [
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"size": "x-small", "variant": "tonal", "color": "deep-orange"},
                                "text": "彩虹岛",
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_CATEGORY_PREFIX}ptchdbits_co",
                                    "label": "分类",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_TAG_PREFIX}ptchdbits_co",
                                    "label": "标签",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_PATH_PREFIX}ptchdbits_co",
                                    "label": "路径",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": f"{DW_PUSH_PREFIX}ptchdbits_co",
                                    "label": "推送",
                                    "color": "primary",
                                    "density": "compact",
                                    "hideDetails": True,
                                    "class": "ml-1 text-no-wrap",
                                },
                            },
                        ],
                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VSheet",
                        "props": {
                            "class": "px-3 py-1 mb-2 rounded",
                            "style": "border:1px solid #e3e6ea; border-left:4px solid #0c8599; background:#fbfcfd;",
                        },
                        "content": [
                            {
                                "component": "VRow",
                                "props": {"dense": True, "align": "center", "class": "py-0"},
                                "content": [
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"size": "x-small", "variant": "tonal", "color": "teal"},
                                "text": "我堡",
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_CATEGORY_PREFIX}ourbits_club",
                                    "label": "分类",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_TAG_PREFIX}ourbits_club",
                                    "label": "标签",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_PATH_PREFIX}ourbits_club",
                                    "label": "路径",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": f"{DW_PUSH_PREFIX}ourbits_club",
                                    "label": "推送",
                                    "color": "primary",
                                    "density": "compact",
                                    "hideDetails": True,
                                    "class": "ml-1 text-no-wrap",
                                },
                            },
                        ],
                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VSheet",
                        "props": {
                            "class": "px-3 py-1 mb-2 rounded",
                            "style": "border:1px solid #e3e6ea; border-left:4px solid #3b5bdb; background:#fbfcfd;",
                        },
                        "content": [
                            {
                                "component": "VRow",
                                "props": {"dense": True, "align": "center", "class": "py-0"},
                                "content": [
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"size": "x-small", "variant": "tonal", "color": "indigo"},
                                "text": "天空",
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_CATEGORY_PREFIX}hdsky_me",
                                    "label": "分类",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_TAG_PREFIX}hdsky_me",
                                    "label": "标签",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_PATH_PREFIX}hdsky_me",
                                    "label": "路径",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": f"{DW_PUSH_PREFIX}hdsky_me",
                                    "label": "推送",
                                    "color": "primary",
                                    "density": "compact",
                                    "hideDetails": True,
                                    "class": "ml-1 text-no-wrap",
                                },
                            },
                        ],
                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VSheet",
                        "props": {
                            "class": "px-3 py-1 mb-2 rounded",
                            "style": "border:1px solid #e3e6ea; border-left:4px solid #7048e8; background:#fbfcfd;",
                        },
                        "content": [
                            {
                                "component": "VRow",
                                "props": {"dense": True, "align": "center", "class": "py-0"},
                                "content": [
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"size": "x-small", "variant": "tonal", "color": "purple"},
                                "text": "家园",
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_CATEGORY_PREFIX}hdhome_org",
                                    "label": "分类",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_TAG_PREFIX}hdhome_org",
                                    "label": "标签",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": f"{DW_PATH_PREFIX}hdhome_org",
                                    "label": "路径",
                                    "density": "compact",
                                    "variant": "solo",
                                    "flat": True,
                                    "hideDetails": True,
                                },
                            },
                        ],
                    },
{
                        "component": "VCol",
                        "props": {"cols": 2},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": f"{DW_PUSH_PREFIX}hdhome_org",
                                    "label": "推送",
                                    "color": "primary",
                                    "density": "compact",
                                    "hideDetails": True,
                                    "class": "ml-1 text-no-wrap",
                                },
                            },
                        ],
                    },
                                ],
                            },
                        ],
                    },
                    # UBits（v2.17.0 新增）。以下是**照抄**上面四个站点块的写法，
                    # 只换域名后缀与配色（域名 ubits.club -> ubits_club）。
                    # 四个旧站块刻意保持原样不动：这块逐站点手写虽然啰嗦，
                    # 但改它的收益只是「少写几行」，风险却是「四个在跑的站点
                    # 表单一起来变」。要重构成循环就单独做一次、单独验证。
                    {
                        "component": "VSheet",
                        "props": {
                            "class": "px-3 py-1 mb-2 rounded",
                            "style": "border:1px solid #e3e6ea; border-left:4px solid #e52d15; background:#fbfcfd;",
                        },
                        "content": [
                            {
                                "component": "VRow",
                                "props": {"dense": True, "align": "center", "class": "py-0"},
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 2},
                                        "content": [
                                            {
                                                "component": "VChip",
                                                "props": {"size": "x-small", "variant": "tonal",
                                                          "color": "red-darken-2"},
                                                "text": "UBits",
                                            },
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 3},
                                        "content": [
                                            {
                                                "component": "VTextField",
                                                "props": {
                                                    "model": f"{DW_CATEGORY_PREFIX}ubits_club",
                                                    "label": "分类",
                                                    "density": "compact",
                                                    "variant": "solo",
                                                    "flat": True,
                                                    "hideDetails": True,
                                                },
                                            },
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 3},
                                        "content": [
                                            {
                                                "component": "VTextField",
                                                "props": {
                                                    "model": f"{DW_TAG_PREFIX}ubits_club",
                                                    "label": "标签",
                                                    "density": "compact",
                                                    "variant": "solo",
                                                    "flat": True,
                                                    "hideDetails": True,
                                                },
                                            },
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 2},
                                        "content": [
                                            {
                                                "component": "VTextField",
                                                "props": {
                                                    "model": f"{DW_PATH_PREFIX}ubits_club",
                                                    "label": "路径",
                                                    "density": "compact",
                                                    "variant": "solo",
                                                    "flat": True,
                                                    "hideDetails": True,
                                                },
                                            },
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 2},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": f"{DW_PUSH_PREFIX}ubits_club",
                                                    "label": "推送",
                                                    "color": "primary",
                                                    "density": "compact",
                                                    "hideDetails": True,
                                                    "class": "ml-1 text-no-wrap",
                                                },
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ], default_config

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
                                            f"检查间隔：{self._interval_minutes} 分钟；"
                                            f"采集站点：{self.__enabled_site_text()}；"
                                            f"推送站点：{self.__push_site_text()}；"
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

        # 最近发现的种子明细（顶部站点胶囊 + 分类内容，CSS :has() 联动切换）
        if self._last_items:
            # 实时读取 QB 任务列表，用于展示本地真实进度
            # （与「站点进度」区分：站点进度是站点侧统计值，本地进度才是下载器实际进度）
            # 读取失败返回 None，此时不展示误导性的进度
            qb_torrents = self.__list_qb_torrents()
            # 按站点分组（顺序 = _last_items 中站点出现顺序，即站点配置顺序）
            site_groups: Dict[str, List[Dict[str, Any]]] = {}
            for item in self._last_items:
                site_name = str(item.get("site") or "未知站点")
                site_groups.setdefault(site_name, []).append(item)

            sites = list(site_groups.keys())

            # 顶部站点胶囊：VChipGroup 的选中态由组件内部自维护（PageRender 不解析
            # model，但 group 内部状态可用）；内容区切换靠 CSS :has() 联动 data-*
            # 属性，完全绕开 model 绑定（机理见 merge 技能「详情页点击切换第二条路」）。
            chips: List[Dict[str, Any]] = []
            for i, site_name in enumerate(sites):
                key = f"site{i}"
                color = _SITE_COLORS.get(site_name, "#888888")
                chips.append(
                    {
                        "component": "VChip",
                        "props": {
                            "value": key,
                            "data-site": key,
                            "variant": "outlined",
                            "link": True,
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "style": f"width:10px; height:10px; "
                                             f"border-radius:50%; "
                                             f"background:{color}; "
                                             f"margin-right:6px;",
                                },
                            },
                            {
                                "component": "span",
                                "text": f"{site_name}（{len(site_groups[site_name])}）",
                            },
                        ],
                    }
                )

            # 每个站点的内容 pane + CSS 联动规则
            panes: List[Dict[str, Any]] = []
            css_rules: List[str] = [".uhd-site-pane { display: none; }"]
            for i, site_name in enumerate(sites):
                key = f"site{i}"
                group_items = site_groups[site_name]
                # 「本次推送」只统计**本轮新推**（action 恰好为「已推送」）；
                # 历史轮次推过的条目，action 是「已推送，下载中/已完成」。
                # 两者共用「推送」二字，只报前者时会与卡片上的「已推送…」看起来
                # 自相矛盾 —— 窗口是站点最前 N 条、多轮之间高度重叠，已推送的条目
                # 会在窗口里停留好几轮，于是顶栏长期显示 0/1，卡片却满屏「已推送」，
                # 看着像漏报（用户据此反馈过一次）。此处把两个数都给出，
                # 明确区分「刚推的」与「此前推过的」。
                pushed_count = len([i for i in group_items if i.get("action") == "已推送"])
                prior_pushed = len([i for i in group_items if i.get("pushed")]) - pushed_count
                summary_text = (
                    f"【{site_name}】共 {len(group_items)} 个 UHD BluRay 原盘，"
                    f"本次新推送 {pushed_count} 个"
                )
                if prior_pushed > 0:
                    summary_text += f"，其中 {prior_pushed} 条此前已推送"

                # 该站点 pane 内容 = 汇总 Alert（置顶）+ 卡片列表
                pane_content: List[Dict[str, Any]] = [
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
                                            "type": "success" if pushed_count else "info",
                                            "variant": "tonal",
                                            "text": summary_text,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]

                # 使用卡片式布局，避免 VTable 单元格强制 nowrap 导致标题截断
                card_items = []
                for item in group_items:
                    # 本地状态行：实时读取 QB 任务状态，回答「这个原盘现在到底在不在下载器里」。
                    #
                    # 展示条件是**两选一**：
                    #   ① 已推送（含历史轮次推过的）—— 刚推下去时站点侧往往还是 0%，
                    #      正是最需要看本地进度的时刻（v2.7.5 起的老口径）；
                    #   ② 该站点**没有进度列**（has_progress_column=False，当前只有 UBits）。
                    #      这类站点的「站点进度」恒为 —，卡片上再没有第二个信号能说明
                    #      「本地有没有」，而本插件的去重恰恰依赖 QB 任务名核对 ——
                    #      把这个结果直接摆出来，用户才看得出「没在下载器里 = 真候选」。
                    # 有进度列的站点（彩虹岛/我堡/天空/家园）保持原样：只在已推送时展示，
                    # 未推送的条目由「站点进度 / 处理结果」两列说明，不再多一行噪音。
                    local_text = ""
                    if item.get("pushed") or item.get("no_progress_col"):
                        if qb_torrents is None:
                            local_text = f"本地：无法读取 QB 状态（{self._downloader or '未配置下载器'}）"
                        else:
                            matched_qb = self.__pick_qb_torrent(
                                qb_torrents,
                                str(item.get("qb_name") or ""),
                                str(item.get("title") or ""),
                            )
                            if matched_qb is not None or item.get("pushed"):
                                # 已推送过却匹配不到 → __describe_qb(None) 会说明
                                # 「QB 中未找到该任务（可能已被删除或改名）」
                                local_text = "本地：" + self.__describe_qb(matched_qb)
                            else:
                                # 从未推送过：说清「下载器里没有」，别让「未找到」被读成任务丢了
                                local_text = "本地：下载器中无此任务"

                    # 副标题行：副标题文本 + 可选 H&R 徽章。徽章紧跟在副标题之后
                    # （flex 布局：空间够就并排在同一行，副标题过长时自动换行）。
                    # 徽章沿用**站点同款配色**（彩虹岛蓝底 #1E90FF、我堡黑底 #060619，
                    # 均白字），尺寸见 HR_BADGE_* 常量（现为小巧档：12px 高 / 8px 字 /
                    # 内边距 4px / 圆角 2px），宽度随标识文字自适应；副标题行高固定
                    # HR_SUBTITLE_LINE_HEIGHT(20px)，徽章行高居中后再下移
                    # HR_BADGE_OFFSET_Y(1px) 做视觉微调（该位置经效果图与用户确认）。
                    # 彩虹岛徽章文字取站点圆标原值（h5 / h3），我堡固定「H&R」；
                    # 字体跟随站点行内 SimHei（Arial 的小写 h 过于纤细，不像站点标识）。
                    # 早期记录只存了 is_hr 布尔值、没有标识文本 → 回退我堡形态。
                    subtitle_row: List[Dict[str, Any]] = [
                        {
                            "component": "div",
                            "props": {
                                "style": "min-width: 0; white-space: normal; "
                                         "word-break: break-all; font-size: 14px; "
                                         "font-weight: 600; "
                                         f"line-height: {HR_SUBTITLE_LINE_HEIGHT}px;",
                            },
                            "text": str(item.get("subtitle") or ""),
                        },
                    ]
                    hr_mark = str(item.get("hr_mark") or
                                  ("H&R" if item.get("is_hr") else ""))
                    if hr_mark:
                        if _HR_CIRCLE_TEXT_RE.match(hr_mark):
                            # 彩虹岛：蓝底白字，文字为站点圆标原值（h5 / h3）
                            hr_bg = "#1E90FF"
                            hr_text = hr_mark
                        else:
                            # 我堡：黑底白字「H&R」
                            hr_bg = "#060619"
                            hr_text = "H&R"
                        subtitle_row.append(
                            {
                                "component": "div",
                                "props": {
                                    "style": (
                                        "flex: none; align-self: center; "
                                        f"position: relative; "
                                        f"top: {HR_BADGE_OFFSET_Y}px; "
                                        "margin-left: 6px; box-sizing: border-box; "
                                        f"height: {HR_BADGE_HEIGHT}px; "
                                        f"line-height: {HR_BADGE_HEIGHT}px; "
                                        f"padding: 0 {HR_BADGE_PAD_X}px; "
                                        f"border-radius: {HR_BADGE_RADIUS}px; "
                                        f"background: {hr_bg}; color: #ffffff; "
                                        f"font-size: {HR_BADGE_FONT}px; "
                                        "font-weight: 700; letter-spacing: 0.5px; "
                                        "font-family: SimHei, 'Microsoft YaHei', "
                                        "Arial, sans-serif;"
                                    ),
                                },
                                "text": hr_text,
                            }
                        )

                    # 「站点进度」：无进度列的站点（UBits）显示长破折号 —，
                    # 与那几站用短横「-」表示「尚无下载记录」区分开，
                    # 避免被读成「这站显示无下载记录，却没被推送」。
                    progress_text = str(item.get("progress") or "")
                    if not progress_text:
                        progress_text = "—" if item.get("no_progress_col") else "-"
                    card_lines: List[Dict[str, Any]] = [
                        {
                            "component": "div",
                            "props": {
                                "style": "display: flex; align-items: center; "
                                         "flex-wrap: wrap;",
                            },
                            "content": subtitle_row,
                        },
                        {
                            "component": "div",
                            "props": {
                                "style": "white-space: normal; word-break: break-all; "
                                         "font-size: 12px; opacity: 0.75; line-height: 1.5; margin-top: 2px;",
                            },
                            "text": str(item.get("title") or ""),
                        },
                        {
                            "component": "div",
                            "props": {
                                "style": "font-size: 12px; opacity: 0.85; margin-top: 4px;",
                            },
                            "text": f"大小：{item.get('size') or '-'}　|　"
                                    f"站点进度：{progress_text}　|　"
                                    f"促销：{'免费' if item.get('is_free') else '收费'}　|　"
                                    f"H&R：{'是' if item.get('is_hr') else '否'}　|　"
                                    f"处理结果：{item.get('action') or '-'}",
                        },
                    ]
                    if local_text:
                        card_lines.append(
                            {
                                "component": "div",
                                "props": {
                                    "style": "white-space: normal; word-break: break-all; "
                                             "font-size: 12px; font-weight: 500; "
                                             "line-height: 1.5; margin-top: 2px;",
                                },
                                "text": local_text,
                            }
                        )
                    card_lines.append(
                        {
                            "component": "div",
                            "props": {
                                "style": "white-space: normal; word-break: break-all; "
                                         "font-size: 12px; opacity: 0.75; line-height: 1.5; margin-top: 2px;",
                            },
                            "text": f"种子标题：{item.get('qb_name') or '（未获取到）'}",
                        }
                    )

                    card_items.append(
                        {
                            "component": "div",
                            "props": {
                                "style": "padding: 10px 12px; margin-bottom: 8px; "
                                         "border-radius: 10px; "
                                         "background: rgba(var(--v-theme-surface-variant), 0.18); "
                                         "backdrop-filter: blur(10px) saturate(150%); "
                                         "-webkit-backdrop-filter: blur(10px) saturate(150%); "
                                         "border: 1px solid rgba(var(--v-theme-on-surface), 0.12); "
                                         "box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);",
                            },
                            "content": card_lines,
                        }
                    )

                pane_content.append(
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
                panes.append(
                    {
                        "component": "div",
                        "props": {"class": "uhd-site-pane", "data-pane": key},
                        "content": pane_content,
                    }
                )
                css_rules.append(
                    f'.uhd-site-root:has(.v-chip--selected[data-site="{key}"]) '
                    f'.uhd-site-pane[data-pane="{key}"] {{ display: block; }}'
                )

            # 兜底：不支持 :has() 的浏览器退回「全展开」，保证内容不丢
            css_rules.append(
                "@supports not (selector(:has(*))) { "
                ".uhd-site-pane { display: block; } }"
            )

            # 组装：注入 <style> + 顶部胶囊组 + 各站点 pane
            page_content.append(
                {
                    "component": "div",
                    "props": {"class": "uhd-site-root"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"style": "display:none"},
                            "html": "<style>" + "\n".join(css_rules) + "</style>",
                        },
                        {
                            "component": "VChipGroup",
                            "props": {
                                "mandatory": True,
                                "modelValue": "site0",
                                "density": "comfortable",
                            },
                            "content": chips,
                        },
                        *panes,
                    ],
                }
            )

        return page_content

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        if self._enabled and self._downloader:
            return [
                {
                    "id": "UhdBlurayAutoDownload",
                    "name": "UHD原盘自动下载",
                    "trigger": "interval",
                    "func": self.check_uhd,
                    "kwargs": {"minutes": self._interval_minutes},
                }
            ]
        return []

    def check_uhd(self) -> None:
        """检查两个站点的 UHD BluRay 原盘并推送未下载的种子。"""
        if not self._enabled:
            return

        if not self._downloader:
            logger.warning("UHD原盘自动下载：未配置下载器，跳过检查")
            return

        self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 获取下载器实例
        downloader_obj = self.__get_downloader()
        if not downloader_obj:
            self._last_error = "获取下载器失败"
            logger.error("UHD原盘自动下载：获取下载器失败")
            return

        self._last_error = ""
        # 每轮重置：站点配置缓存、真实请求计数、落库脏标记
        self._site_conf_cache = {}
        self._fetch_count = 0
        self._map_dirty = False
        processed_map: Dict[str, Dict[str, Any]] = self.get_data(PROCESSED_DATA_KEY) or {}
        all_items: List[Dict[str, Any]] = []
        downloaded_items: List[Dict[str, Any]] = []

        for domain, site_conf in self._site_configs.items():
            if not self._site_enabled.get(domain, True):
                logger.info(f"UHD原盘自动下载：站点 {site_conf.get('name')} 未勾选采集，跳过")
                continue
            # 合并表单里的「下载分类 / 路径 / 推送开关」覆盖值（v2.12.0 起）
            eff_conf = self.__site_effective_conf(domain, site_conf)
            try:
                items = self.__process_site(domain, eff_conf, downloader_obj, processed_map)
                all_items.extend(items)
                downloaded_items.extend([item for item in items if item.get("action") == "已推送"])
            except Exception as err:
                logger.error(f"UHD原盘自动下载：处理站点 {site_conf.get('name')} 失败，{err}")

        # 一次性迁移：清空旧版站点简介，改用 TMDB 简介重新抓取
        self.__migrate_intro_to_tmdb(processed_map)

        # 补齐历史记录中缺失的副标题与简介（兼容旧版本记录）
        self.__fill_missing_subtitles(processed_map)

        self._last_items = all_items

        # 限制记录长度
        if len(processed_map) > PROCESSED_LIMIT:
            keys = list(processed_map.keys())
            for key in keys[:len(processed_map) - PROCESSED_LIMIT]:
                processed_map.pop(key, None)
            self._map_dirty = True
        # 仅在本轮记录确有变化时落库。旧实现不管有没有变化，
        # 每轮都无条件把整份记录重写一次（窗口里全是已推送的条目时
        # 这次写入是纯浪费：内容与库里完全一致）
        if self._map_dirty:
            self.save_data(PROCESSED_DATA_KEY, processed_map)

        # 发送通知
        if self._notify and downloaded_items:
            hr_count = len([i for i in downloaded_items if i.get("is_hr")])
            head = f"🎬 已推送 {len(downloaded_items)} 个 UHD 原盘到 QB"
            if hr_count:
                head += f"（其中 {hr_count} 个带 H&R 考核）"
            lines = [head, ""]
            for item in downloaded_items[:20]:
                subtitle = item.get("subtitle") or ""
                title = item.get("title") or ""
                # 下载器里的实际任务名（站点详情页「下载」字段给出的种子文件名）。
                # 「种子标题」要的正是这个名字：用户是拿它去 QB 里对照任务的。
                # 站点列表页标题（title）是另一种写法——分隔符不同（空格 vs 点），
                # 且缺少站点加的中文前缀，拿去 QB 里根本搜不到。
                # 🔴 旧实现这里取的是 title，于是通知里的种子标题与详情页卡片
                # （卡片一直用的是 qb_name）对不上。
                qb_name = item.get("qb_name") or ""
                intro = item.get("intro") or ""
                size = item.get("size") or ""
                site = item.get("site") or ""
                # 中文标题（副标题，缺失时回退到种子标题）
                cn_line = subtitle or title
                lines.append(f"▎中文标题：{cn_line}")
                # 种子标题取 QB 任务名；与上一行内容相同时不重复输出
                seed_line = qb_name or title
                if seed_line and seed_line != cn_line:
                    lines.append(f"▎种子标题：{seed_line}")
                # 站点
                if site:
                    lines.append(f"▎站点：{site}")
                # 体积
                if size:
                    lines.append(f"▎体积：{size}")
                # H&R 考核提示：带考核的种子单独提一行，避免下载后忘保种被站点处罚
                if item.get("is_hr"):
                    lines.append("▎H&R：⚠️ 该种子带 Hit & Run 考核，请勿删种，注意保种达标")
                # 影片简介（TMDB）
                if intro:
                    lines.append(f"▎简介：{intro}")
                lines.append("")
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【UHD原盘自动下载】",
                text="\n".join(lines).rstrip(),
            )

    def __migrate_intro_to_tmdb(self, processed_map: Dict[str, Dict[str, Any]]) -> None:
        """一次性迁移：清空旧版站点简介，改用 TMDB 简介重新抓取。

        旧版本记录的 intro 来自站点详情页的"简介"字段（多为制作说明），
        此处清空这些记录，交由 __fill_missing_subtitles 用 TMDB 重新补齐。
        通过迁移标记确保只执行一次。

        :param processed_map: 已处理记录字典
        """
        if self.get_data(INTRO_MIGRATED_KEY):
            return

        cleared = 0
        for record in processed_map.values():
            if not isinstance(record, dict):
                continue
            if record.get("intro"):
                record["intro"] = ""
                cleared += 1

        self.save_data(INTRO_MIGRATED_KEY, True)
        if cleared:
            self._map_dirty = True
            logger.info(f"UHD原盘自动下载：已清空 {cleared} 条旧版站点简介，将改用 TMDB 简介")

    def __fill_missing_subtitles(self, processed_map: Dict[str, Dict[str, Any]]) -> None:
        """补齐历史记录中缺失的副标题、种子标题与简介。

        旧版本记录只保存了 title 与 cn_title，缺少完整副标题与简介。
        此处按记录 key 中的域名与种子 ID 从站点详情页补全。

        配额按**真实网络请求数**计（详情页 + TMDB 合计最多
        `FILL_MAX_ATTEMPTS` 次）：旧实现按「补齐成功的条数」计数，
        抓不到的记录不计数，于是每轮都会从第一条记录往下把整份历史
        依次重抓一遍——记录攒到 500 条时一轮就是几百次请求，下一轮再来一次。
        取不到的负结果另有 TTL 缓存兜住，不会每轮重试。

        :param processed_map: 已处理记录字典
        """
        filled = 0
        spent = 0

        for record_key, record in processed_map.items():
            if spent >= FILL_MAX_ATTEMPTS:
                break
            if not isinstance(record, dict):
                continue
            if ":" not in record_key:
                continue

            domain, torrent_id = record_key.split(":", 1)
            site = self.__get_site_config(domain)
            if not site:
                continue

            # 补齐站点名称
            if not record.get("site"):
                record["site"] = site.get("name") or ""
                self._map_dirty = True

            # 站点副标题与种子标题同处一张详情页：只要缺其中任意一项就抓一次，
            # 一次把两个字段都取回来（旧实现缺 qb_name 抓一次、缺 subtitle
            # 再抓一次，同一个 details.php 打了两遍）
            need_name = not record.get("qb_name")
            need_subtitle = not record.get("subtitle")
            if need_name or need_subtitle:
                before = self._fetch_count
                subtitle, seed_name = self.__fetch_detail_fields(site, domain, torrent_id)
                spent += self._fetch_count - before
                if need_name and seed_name:
                    record["qb_name"] = seed_name
                    filled += 1
                    self._map_dirty = True
                    logger.info(
                        f"UHD原盘自动下载：已补齐种子标题 {domain}:{torrent_id} - {seed_name[:50]}"
                    )
                if need_subtitle and subtitle:
                    record["subtitle"] = subtitle
                    if not record.get("cn_title"):
                        record["cn_title"] = self.__extract_cn_title(subtitle)
                    filled += 1
                    self._map_dirty = True
                    logger.info(
                        f"UHD原盘自动下载：已补齐副标题 {domain}:{torrent_id} - {subtitle[:50]}"
                    )

            if record.get("intro") or spent >= FILL_MAX_ATTEMPTS:
                continue

            before = self._fetch_count
            intro = self.__fetch_tmdb_intro(
                str(record.get("title") or ""),
                str(record.get("subtitle") or ""),
            )
            spent += self._fetch_count - before
            if intro:
                record["intro"] = intro
                filled += 1
                self._map_dirty = True
                logger.info(
                    f"UHD原盘自动下载：已补齐简介 {domain}:{torrent_id} - {intro[:50]}"
                )

        if filled:
            logger.info(f"UHD原盘自动下载：本次共补齐 {filled} 条副标题/简介")

    def __fetch_detail_fields(self, site: Dict[str, Any], domain: str,
                              torrent_id: str) -> Tuple[str, str]:
        """一次请求详情页，同时取回「完整副标题」与「种子文件名」。

        列表页的副标题可能被站点截断（如彩虹岛显示为 "保留Dolb.."），
        详情页的「副标题」字段才是完整内容；「下载」字段的链接文本则是
        种子文件名，即 QB 任务名（供 BDMV自动打包ISO 插件精确匹配）。

        🔴 这两个字段同处一张详情页（同一个 `details.php?id=X`）。
        旧实现由两个方法各自请求一次，等于把同一张页面抓两遍——
        窗口里每一条都多打一次请求，纯属重复抓取。此处合并为一次。

        缓存键带上域名：各站点的种子 ID 都是站点内自增数字，只用 ID 作键
        会让两个站点的同名 ID 互相覆盖，取到别的站点的数据。

        :param site: 站点配置
        :param domain: 站点域名（作缓存键前缀，避免跨站 ID 碰撞）
        :param torrent_id: 种子 ID
        :return: (完整副标题, 种子文件名)；未取到为空字符串
        """
        if not torrent_id:
            return "", ""

        cache_key = f"{domain}:{torrent_id}"
        cached = self._detail_cache.get(cache_key)
        if isinstance(cached, dict):
            subtitle = str(cached.get("subtitle") or "")
            seed_name = str(cached.get("seed_name") or "")
            # 两个字段都取到过：长期有效
            if subtitle and seed_name:
                return subtitle, seed_name
            # 曾取不到（页面里没有该字段）：静默期内不再重试，避免每轮空打
            if self.__cache_fresh(cached.get("ts"), NEGATIVE_CACHE_TTL):
                return subtitle, seed_name

        base_url = (site.get("url") or "").rstrip("/")
        detail_url = f"{base_url}/details.php?id={torrent_id}"

        for attempt in range(2):
            self._fetch_count += 1
            try:
                res = RequestUtils(
                    ua=site.get("ua"),
                    cookies=site.get("cookie"),
                    proxies=settings.PROXY if site.get("proxy") else None,
                    timeout=site.get("timeout") or 20,
                ).get_res(url=detail_url)
            except Exception as err:
                logger.error(f"UHD原盘自动下载：获取详情页失败 {detail_url}，{err}")
                continue

            if res is None or res.status_code != 200:
                logger.warning(
                    f"UHD原盘自动下载：详情页返回异常 {detail_url}，"
                    f"状态码 {res.status_code if res else 'None'}"
                )
                continue

            page = etree.HTML(res.text)
            if page is None:
                continue

            # 页面拿到了：两个字段一起解析、一起入缓存（含"确实没有该字段"）
            subtitle = self.__parse_detail_subtitle(page)
            seed_name = self.__parse_detail_seed_name(page)
            self.__remember_detail(cache_key, subtitle, seed_name)
            return subtitle, seed_name

        # 网络层失败（超时 / 非 200）：不写负缓存，下一轮仍然重试
        logger.warning(f"UHD原盘自动下载：详情页获取失败，已重试 {detail_url}")
        return "", ""

    @staticmethod
    def __parse_detail_subtitle(page: Any) -> str:
        """从详情页解析「副标题」字段。

        :param page: lxml 解析后的页面对象
        :return: 完整副标题；页面中没有该字段时返回空字符串
        """
        # 定位"副标题"字段所在行的下一个单元格
        nodes = page.xpath(
            '//td[text()="副标题" or text()="副標題"]/following-sibling::td[1]'
        )
        if not nodes:
            return ""
        return nodes[0].xpath('string(.)').strip()

    @staticmethod
    def __parse_detail_seed_name(page: Any) -> str:
        """从详情页「下载」字段解析种子文件名（即 QB 任务名）。

        :param page: lxml 解析后的页面对象
        :return: 去掉站点前缀与 .torrent 后缀的文件名；无该字段时返回空字符串
        """
        # 定位「下载」字段所在行的下一个单元格，取其内链接文本
        nodes = page.xpath('//td[text()="下载"]/following-sibling::td[1]')
        if not nodes:
            return ""
        text = ""
        # 形式一：<a href="download.php...">[CHDBits].xxx.torrent</a>
        links = nodes[0].xpath('.//a[contains(@href,"download.php")]')
        if links:
            text = links[0].xpath('string(.)').strip()
        if not text:
            # 形式二（天空等）：<input type="submit" value="[HDSky].xxx.torrent">
            values = nodes[0].xpath(
                './/input[@value and contains(@value,".torrent")]/@value'
            )
            if values:
                text = str(values[0]).strip()
        if not text:
            return ""
        # 去掉 .torrent 后缀与站点前缀（如 [CHDBits].）
        name = re.sub(r'\.torrent$', '', text, flags=re.I).strip()
        name = re.sub(r'^\[[^\]]+\]\.?', '', name).strip()
        return name

    def __remember_detail(self, cache_key: str, subtitle: str, seed_name: str) -> None:
        """写入详情页字段缓存，并按上限淘汰最旧的记录。

        :param cache_key: 缓存键（f"{domain}:{torrent_id}"）
        :param subtitle: 完整副标题
        :param seed_name: 种子文件名
        """
        self._detail_cache[cache_key] = {
            "subtitle": subtitle,
            "seed_name": seed_name,
            "ts": time.time(),
        }
        self.__trim_cache(self._detail_cache, DETAIL_CACHE_LIMIT)

    @staticmethod
    def __cache_fresh(ts: Any, ttl: int) -> bool:
        """判断缓存时间戳是否仍在有效期内。

        :param ts: 写入时间戳（秒）
        :param ttl: 有效期（秒）
        :return: 未过期返回 True
        """
        try:
            return (time.time() - float(ts)) < ttl
        except (TypeError, ValueError):
            return False

    @staticmethod
    def __trim_cache(cache: Dict[str, Any], limit: int) -> None:
        """缓存超出上限时，按写入时间淘汰最旧的若干条。

        :param cache: 缓存字典（值需含 "ts" 字段）
        :param limit: 保留条数上限
        """
        if len(cache) <= limit:
            return
        oldest = sorted(cache.items(), key=lambda kv: (kv[1] or {}).get("ts") or 0)
        for key, _ in oldest[:len(cache) - limit]:
            cache.pop(key, None)

    def __fetch_tmdb_intro(self, title: str, subtitle: str = "") -> str:
        """通过 TMDB 识别媒体并获取影片简介。

        使用种子标题与副标题识别媒体，取 TMDB 的 overview 作为简介。
        识别失败或 TMDB 无简介时返回空字符串，并写入负缓存——
        旧实现不缓存失败结果，同一条识别不了的种子每轮都会重新识别一遍。

        :param title: 种子标题
        :param subtitle: 站点副标题（含中文名，有助于识别）
        :return: TMDB 影片简介；获取失败返回空字符串
        """
        if not title and not subtitle:
            return ""
        cache_key = f"{title}|{subtitle}"
        cached = self._intro_cache.get(cache_key)
        if isinstance(cached, dict):
            text = str(cached.get("text") or "")
            if text:
                return text
            # 曾识别失败 / TMDB 无简介：静默期内不再重试
            if self.__cache_fresh(cached.get("ts"), NEGATIVE_CACHE_TTL):
                return ""

        self._fetch_count += 1
        try:
            meta = MetaInfo(title=title, subtitle=subtitle)
            mediainfo = MediaChain().recognize_media(meta=meta)
        except Exception as err:
            logger.error(f"UHD原盘自动下载：TMDB 识别失败 {title[:60]}，{err}")
            return ""

        if not mediainfo:
            logger.warning(f"UHD原盘自动下载：TMDB 未识别到媒体 {title[:60]}")
            self.__remember_intro(cache_key, "")
            return ""

        intro = self.__clean_intro(mediainfo.overview or "")
        if intro:
            self.__remember_intro(cache_key, intro)
            logger.info(
                f"UHD原盘自动下载：TMDB 简介获取成功 {mediainfo.title} - {intro[:40]}"
            )
        else:
            # 识别到了媒体但 TMDB 没有简介：同样记负缓存，避免每轮重来
            self.__remember_intro(cache_key, "")
        return intro

    def __remember_intro(self, cache_key: str, text: str) -> None:
        """写入简介缓存，并按上限淘汰最旧的记录。

        :param cache_key: 缓存键（f"{title}|{subtitle}"）
        :param text: 简介文本；空字符串表示负结果
        """
        self._intro_cache[cache_key] = {"text": text, "ts": time.time()}
        self.__trim_cache(self._intro_cache, INTRO_CACHE_LIMIT)

    @staticmethod
    def __clean_intro(text: str, max_length: int = 500) -> str:
        """清洗简介文本：去除 BBCode、HTML 残留与多余空白，并按长度截断。

        :param text: 原始简介文本
        :param max_length: 最大保留字符数
        :return: 清洗后的简介
        """
        if not text:
            return ""
        # 去除 BBCode 标记（如 [quote]、[color=Red]、[/b] 等）
        cleaned = re.sub(r'\[/?[a-zA-Z][^\]]*\]', '', text)
        # 去除 HTML 标签残留
        cleaned = re.sub(r'<[^>]+>', '', cleaned)
        # 统一不可见字符为空格
        cleaned = cleaned.replace('\xa0', ' ').replace('\u3000', ' ')
        # 合并连续空白（保留换行结构）
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        cleaned = cleaned.strip()
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip() + "…"
        return cleaned

    def __process_site(self, domain: str, site_conf: Dict[str, Any], downloader_obj: Any,
                       processed_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """处理单个站点的 UHD BluRay 列表。

        :param domain: 站点域名
        :param site_conf: 站点配置
        :param downloader_obj: 下载器实例
        :param processed_map: 已处理种子记录
        :return: 处理明细列表
        """
        site_name = site_conf.get("name") or domain
        items: List[Dict[str, Any]] = []

        # 获取站点配置
        site = self.__get_site_config(domain)
        if not site:
            logger.warning(f"UHD原盘自动下载：未找到站点 {site_name} 配置")
            return items

        # 抓取该站点的列表页，严格保持站点顺序（含站点置顶段），取最前面的
        # latest_count 条。取数阶段不跳过任何行，也不按发布时间重排——
        # 是否已下载、是否已推送，由下面的逐条判定负责，这样插件看到的种子
        # 与站点网页逐条对应。
        torrents = self.__collect_site_torrents(site, site_conf, site_name)
        if not torrents:
            return items

        # 站点对「尚无下载记录」的取值口径（各站渲染不一致：
        # 彩虹岛/我堡为 "-"/"--"，天空为 "0%"）
        no_download_marks = tuple(site_conf.get("no_download_marks") or ("-", "--"))
        # 该站列表页是否有「进度」列（UBits 没有，见 _site_configs 说明）。
        # 为 False 时整段进度判定跳过，改用下面 QB 兜底去重。
        has_progress_column = bool(site_conf.get("has_progress_column", True))
        # QB 任务表兜底缓存（惰性读取：本轮真正要判重时才读一次）
        qb_guard: Dict[str, Any] = {"loaded": False, "torrents": None}

        # 「免费优先」模式下暂缓处理的收费种子（先推完免费的再回头处理）。
        # 只影响推送动作的先后，不改变取数顺序（站点顺序铁律不受影响）。
        deferred: List[Dict[str, Any]] = []

        for torrent in torrents:
            title = torrent.get("title") or ""
            progress = torrent.get("progress") or ""
            torrent_id = torrent.get("id") or ""
            record_key = f"{domain}:{torrent_id}"

            item = {
                "site": site_name,
                "title": title,
                "subtitle": torrent.get("subtitle") or "",
                "intro": "",
                "size": torrent.get("size") or "",
                "progress": progress,
                # 该站没有进度列时置位，详情页把「站点进度」渲染成长破折号 —
                # （而不是短横「-」，后者在那几站是「尚无下载记录」的取值，容易误读）
                "no_progress_col": not has_progress_column,
                "is_free": bool(torrent.get("is_free")),
                "is_hr": bool(torrent.get("is_hr")),
                "hr_mark": str(torrent.get("hr_mark") or ""),
                "action": "",
            }

            # 详情页与 TMDB 一律**按需抓取**：本插件推送过的条目，记录里已经
            # 存着当初抓到的完整副标题与种子标题，直接复用即可。旧实现不分
            # 青红皂白，每轮都为窗口内每一条重抓一遍详情页、重跑一次 TMDB——
            # 已推送的条目等于把一模一样的数据反复抓回来再丢掉。
            record = processed_map.get(record_key)
            if not isinstance(record, dict):
                record = None

            need_detail = True
            if record:
                # H&R 标识以「记录里有就保留」为准：站点改版导致本轮解析不到
                # hitandrun 图标时，历史记录的标识不至于凭空消失。
                # 标识文本同样补全：本轮没解析到就沿用记录里的；早期记录只存了
                # is_hr 布尔值，此时回退成「H&R」以便详情页仍有徽章可渲染。
                if not item.get("hr_mark"):
                    item["hr_mark"] = str(record.get("hr_mark") or "")
                if record.get("is_hr"):
                    item["is_hr"] = True
                    if not item.get("hr_mark"):
                        item["hr_mark"] = "H&R"
                cached_subtitle = str(record.get("subtitle") or "")
                cached_name = str(record.get("qb_name") or "")
                # 两项都在记录里才可跳过抓取（缺任一项仍需回源补齐）
                if cached_subtitle and cached_name:
                    item["subtitle"] = cached_subtitle
                    item["qb_name"] = cached_name
                    item["intro"] = str(record.get("intro") or "")
                    need_detail = False

            if need_detail:
                # 一次请求同时取回完整副标题与种子标题（同处一个详情页）
                detail_subtitle, seed_name = self.__fetch_detail_fields(
                    site, domain, torrent_id
                )
                if detail_subtitle:
                    item["subtitle"] = detail_subtitle
                    torrent["subtitle"] = detail_subtitle
                if seed_name:
                    item["qb_name"] = seed_name

                # 从 TMDB 获取影片简介
                tmdb_intro = self.__fetch_tmdb_intro(title, item.get("subtitle") or "")
                if tmdb_intro:
                    item["intro"] = tmdb_intro

            # 站点进度判断：
            # 进度列取值为「尚无下载记录」标记（彩虹岛/我堡为 "-"/"--"，
            # 天空为 "0%"）时表示站点侧未产生下载记录，需要推送；
            # 其余（"1%"~"100%"）表示站点侧已有下载记录。
            #
            # 特别注意：站点侧的进度是站点自己统计的值，并不等于本地下载进度
            # （本插件推送后，站点侧会从「无记录」变成 "0%" 再逐步上涨）。
            # 因此对本插件推送过的种子单独归类为「已推送」，与「别家在下」区分；
            # 本地真实进度在详情页单独展示（由 QB 实时读取）。
            if has_progress_column and progress not in no_download_marks:
                if record_key in processed_map:
                    item["pushed"] = True
                    item["action"] = "已推送，已完成" if progress == "100%" else "已推送，下载中"
                elif progress == "100%":
                    item["action"] = "站点侧已下载完成，跳过"
                elif re.match(r'^\d+(\.\d+)?%$', progress):
                    item["action"] = f"站点侧下载中（{progress}），跳过"
                else:
                    item["action"] = "站点侧已下载，跳过"
                items.append(item)
                continue

            # 已处理过则跳过，但补齐缺失的副标题与站点（兼容旧版本记录）
            if record_key in processed_map:
                if record is not None:
                    if not record.get("site"):
                        record["site"] = site_name
                        self._map_dirty = True
                    if not record.get("subtitle"):
                        current_subtitle = item.get("subtitle") or ""
                        if current_subtitle:
                            record["subtitle"] = current_subtitle
                            if not record.get("cn_title"):
                                record["cn_title"] = self.__extract_cn_title(current_subtitle)
                            self._map_dirty = True
                            logger.info(
                                f"UHD原盘自动下载：已补齐副标题 {site_name} - {title[:60]}"
                            )
                item["pushed"] = True
                item["action"] = "已推送，下载中"
                items.append(item)
                continue

            # —— 无进度列站点的去重兜底（v2.17.0 起，当前只有 UBits）——
            # 站点没有「进度」列时，上面那道「站点侧是否已有下载记录」的判断被跳过，
            # 只剩「插件历史记录」一道。这会漏掉两种情况：
            #   ① 用户在站点侧手动下过（或别的客户端在跑），本插件没有记录；
            #   ② 插件数据被重置过（记录清空），种子其实还在 QB 里。
            # 两者都会导致重复推送。这里在真正推送前拿 QB 现有任务名核对一次。
            # 匹配口径**故意收紧成「归一化全等」**，不沿用 __pick_qb_torrent 的模糊
            # 重合（那是为「展示本地进度」服务的，宁可多匹配）；
            # 判重场景下宁可漏判（重复推送最多是多一个任务）也不能错判（会漏推原盘）。
            if not has_progress_column:
                if not qb_guard["loaded"]:
                    qb_guard["loaded"] = True
                    qb_guard["torrents"] = self.__list_qb_torrents()
                if self.__qb_has_torrent(
                    qb_guard.get("torrents"), item.get("qb_name") or "", title
                ):
                    item["pushed"] = True
                    item["action"] = "已在下载器中，跳过"
                    items.append(item)
                    continue

            # 站点未开启推送（表单表格里「推送」开关关掉，或站点常量 push_enabled=False）：
            # 走到这里说明该种子「该推了」（站点侧无下载记录、本插件也未推送过），
            # 但按站点配置不执行推送 —— 仅采集展示、不下载种子、不写已处理记录。
            if not site_conf.get("push_enabled", True):
                item["action"] = "未推送（该站点的推送开关已关闭）"
                items.append(item)
                continue

            # —— 免费促销闸门（推送优先功能）——
            # 走到这里说明：站点侧无下载记录、且本插件尚未推送过，属于「该推」的种子。
            # 在此按 push_mode 决定是否推送：
            #   free_only  → 非免费直接跳过，不推送
            #   free_first → 非免费暂缓，等免费的处理完再回头推（不影响取数顺序）
            #   all        → 不筛（现状）
            is_free = bool(torrent.get("is_free"))
            if self._push_mode == PUSH_MODE_FREE_ONLY and not is_free:
                item["action"] = "跳过（非免费）"
                items.append(item)
                continue
            if self._push_mode == PUSH_MODE_FREE_FIRST and not is_free:
                # 先记下，循环末尾统一处理
                deferred.append((torrent, item))
                continue

            # 下载种子文件并推送到 QB
            success = self.__download_and_push(
                site=site,
                site_conf=site_conf,
                torrent=torrent,
                downloader_obj=downloader_obj,
            )
            if success:
                # 本轮新推送：标记 pushed，让卡片同样展示本地真实进度
                # （刚推送时站点侧往往还是 0%，正是最需要看本地进度的时刻）
                item["pushed"] = True
                item["action"] = "已推送"
                # 种子标题已在前面从详情页获取，缺失时回退到 QB 反查
                seed_name = item.get("qb_name") or self.__find_qb_torrent_name(
                    downloader_obj, title
                )
                item["qb_name"] = seed_name
                processed_map[record_key] = {
                    "title": title,
                    "qb_name": seed_name,
                    "subtitle": item.get("subtitle") or "",
                    "intro": item.get("intro") or "",
                    "cn_title": self.__extract_cn_title(item.get("subtitle") or ""),
                    "site": site_name,
                    # H&R 标识一并落库：历史轮次也能展示，站点改版时标识不丢
                    "is_hr": bool(item.get("is_hr")),
                    "hr_mark": str(item.get("hr_mark") or ""),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self._map_dirty = True
                logger.info(f"UHD原盘自动下载：已推送 {site_name} - {title[:60]}")
            else:
                item["action"] = "推送失败"
                logger.error(f"UHD原盘自动下载：推送失败 {site_name} - {title[:60]}")
            items.append(item)

        # 「免费优先」模式的收尾：免费的已在上面推完，这里回头处理暂缓的收费种子。
        # 收费种子照常推送（只是排后），保证不错过优质收费原盘。
        for torrent, item in deferred:
            title = torrent.get("title") or ""
            record_key = f"{domain}:{torrent.get('id') or ''}"
            success = self.__download_and_push(
                site=site,
                site_conf=site_conf,
                torrent=torrent,
                downloader_obj=downloader_obj,
            )
            if success:
                item["pushed"] = True
                item["action"] = "已推送（收费·延后）"
                seed_name = item.get("qb_name") or self.__find_qb_torrent_name(
                    downloader_obj, title
                )
                item["qb_name"] = seed_name
                processed_map[record_key] = {
                    "title": title,
                    "qb_name": seed_name,
                    "subtitle": item.get("subtitle") or "",
                    "intro": item.get("intro") or "",
                    "cn_title": self.__extract_cn_title(item.get("subtitle") or ""),
                    "site": site_name,
                    # H&R 标识一并落库：历史轮次也能展示，站点改版时标识不丢
                    "is_hr": bool(item.get("is_hr")),
                    "hr_mark": str(item.get("hr_mark") or ""),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self._map_dirty = True
                logger.info(f"UHD原盘自动下载：已推送 {site_name} - {title[:60]}")
            else:
                item["action"] = "推送失败"
                logger.error(f"UHD原盘自动下载：推送失败 {site_name} - {title[:60]}")
            items.append(item)

        return items

    def __collect_site_torrents(self, site: Dict[str, Any], site_conf: Dict[str, Any],
                                site_name: str) -> List[Dict[str, Any]]:
        """挑出该站点本轮要看的种子（站点顺序最前面 latest_count 条）。

        取数规则（与 v2.7.5 一致，顺序敏感，三条）：

        1. **直接取站点列表页最前面的 N 条**，取数阶段不做任何「跳过」。
           站点网页长什么样，插件取到的就是什么，逐条对得上——包括
           NexusPHP 排在最前面的置顶段（sticky）以及「我已下载过」那一段。
           站点把它们排在最前面就是它的展示优先级，插件不应替站点重排或剔除。
        2. 该不该下载、是否已经推送过，全部交给 __process_site 逐条判定：
           站点侧已有下载记录的展示为「跳过」，本插件推送过的展示为「已推送」。
           若在取数阶段就按这两条过滤，窗口会被整体下推到第 10 行以后，
           抓到的种子与站点网页完全对不上（v2.8.0~v2.8.2 的回归）。
        3. 一个站点配了**多个**列表页时（尽量别这么配，见 `_site_configs`
           里 list_url 的说明），按配置顺序拼接后按种子 ID 去重，**不做重排**。
           v2.8.x 曾用列表页的「存活时间」重排，结果把站点置顶段整个冲掉：
           置顶项（sticky）往往比普通段更旧，一排序就沉底，抓到的种子自然
           与站点网页对不上。站点自己的多值筛选写法（如天空的
           medium13=1&medium14=1）能一个地址取全，就不该拆成两页再自行合成。

        :param site: 站点配置
        :param site_conf: 站点插件配置
        :param site_name: 站点名称（日志用）
        :return: 待处理种子信息列表
        """
        raw_urls = site_conf.get("list_url")
        if isinstance(raw_urls, str):
            urls = [raw_urls]
        else:
            urls = [str(url) for url in (raw_urls or []) if url]

        collected: List[Dict[str, Any]] = []
        seen_ids = set()
        for url in urls:
            # 抓取阶段**不做任何免费/促销过滤**：直接按列表页原地址抓全量，
            # 严格保持站点顺序与「取前 N 条、不重排」的铁律。
            # 「只推免费 / 免费优先」的判定一律放到 __process_site 的推送闸门，
            # 依据行级 is_free（__parse_list_page 里提取）逐条决定是否推送——
            # 抓取与推送彻底解耦，避免在取数阶段就把收费种子筛掉。
            request_url = url
            # 抓取重试：部分站点（如家园）偶发限流，RequestUtils 返回 None 或
            # 非 200，短暂等待后重试几次，避免偶发抖动导致整轮抓空。
            res = None
            for attempt in range(3):
                try:
                    res = RequestUtils(
                        ua=site.get("ua"),
                        cookies=site.get("cookie"),
                        proxies=settings.PROXY if site.get("proxy") else None,
                        timeout=site.get("timeout") or 20,
                    ).get_res(url=request_url)
                except Exception as err:
                    logger.error(f"UHD原盘自动下载：抓取 {site_name} 列表失败，{err}")
                    res = None
                if res is not None and res.status_code == 200:
                    break
                time.sleep(2)

            if res is None or res.status_code != 200:
                logger.error(
                    f"UHD原盘自动下载：抓取 {site_name} 列表失败，"
                    f"状态码 {res.status_code if res else 'None'}（已重试）"
                )
                continue

            filter_mode = site_conf.get("filter_mode", "uhd_title")
            if filter_mode == "hdhome_diy":
                # 家园专用：整页抓取 + 三规则过滤，返回发种时间倒序的命中列表
                parsed = self.__parse_hdhome_page(res.text, site_conf)
            else:
                parsed = self.__parse_list_page(res.text, filter_mode, site_conf)
            logger.info(
                f"UHD原盘自动下载：{site_name} 列表页解析出 {len(parsed)} 个原盘"
            )
            for torrent in parsed:
                torrent_id = str(torrent.get("id") or "")
                if torrent_id and torrent_id in seen_ids:
                    continue
                if torrent_id:
                    seen_ids.add(torrent_id)
                collected.append(torrent)

        # 顺序 = 站点顺序（多地址时 = 各地址按配置顺序拼接后按 ID 去重）。
        # 刻意**不按发布时间重排**：站点把置顶/推荐段排在列表最前，这一段往往
        # 比普通段更旧，一律按时间排序会把置顶段整体冲掉，抓到的种子与站点
        # 网页就对不上了（v2.8.x 曾按存活时间重排，天空的置顶段因此全军覆没）。
        # （例外：家园的 filter_mode="hdhome_diy" 已在 __parse_hdhome_page 内
        #   按发种时间倒序排好，符合「找最新」语义，不适用上面这条铁律。）

        # 取最前面的 N 条。这里刻意**不做**任何跳过：
        # 已下载/已推送的种子该不该推送由 __process_site 逐条判定并展示，
        # 但它们在窗口里的位置必须保留，否则抓到的种子与站点网页对不上。
        selected = collected[: self._latest_count]
        logger.info(
            f"UHD原盘自动下载：{site_name} 取站点顺序前 {len(selected)} 个 UHD BluRay 原盘"
        )
        return selected

    def __list_qb_torrents(self) -> Optional[List[Any]]:
        """读取 QB 全部任务列表。

        :return: 任务对象列表；下载器未配置或读取失败时返回 None
        """
        if not self._downloader:
            return None
        downloader_obj = self.__get_downloader()
        if not downloader_obj:
            return None
        try:
            result = downloader_obj.get_torrents()
        except Exception as err:
            logger.warning(f"UHD原盘自动下载：读取 QB 任务列表失败：{err}")
            return None
        if isinstance(result, tuple):
            torrents, error = result
            if error:
                return None
        else:
            torrents = result
        return list(torrents or [])

    @staticmethod
    def __qb_has_torrent(torrents: Optional[List[Any]], qb_name: str = "",
                         title: str = "") -> bool:
        """判断种子是否已经存在于 QB 任务列表中（无进度列站点的去重兜底）。

        与 __pick_qb_torrent 的区别：**只做归一化全等匹配**，不做关键词重合。
        __pick_qb_torrent 服务于「展示本地进度」，宁可多匹配；
        本方法服务于「该不该再推一次」，错判会导致漏推原盘，因此必须收紧。

        归一化：去掉所有非字母数字与中文字符、统一小写
        （QB 任务名可能带中文前缀或调整过标点，与站点给的种名不完全一致）。

        :param torrents: QB 任务对象列表；None 表示读取失败（此时返回 False，按未命中处理）
        :param qb_name: 站点详情页给出的种子标题（预期 QB 任务名）
        :param title: 站点列表页的种子标题（备用匹配依据）
        :return: 已存在返回 True
        """
        if not torrents:
            return False

        def norm(text: str) -> str:
            return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', str(text or '').lower())

        def name_of(torrent: Any) -> str:
            try:
                return str(torrent.get("name") or "")
            except AttributeError:
                return str(getattr(torrent, "name", "") or "")

        targets = {norm(k) for k in (qb_name, title) if norm(k)}
        if not targets:
            return False
        for torrent in torrents:
            if norm(name_of(torrent)) in targets:
                return True
        return False

    @staticmethod
    def __pick_qb_torrent(torrents: List[Any], qb_name: str = "",
                          title: str = "") -> Optional[Any]:
        """从 QB 任务列表中匹配出目标种子。

        依次尝试：任务名精确匹配 → 归一化后互相包含 → 有区分度的关键词重合。
        归一化会去掉标点与大小写差异（QB 任务名可能带中文前缀或调整过标点）。

        :param torrents: QB 任务对象列表
        :param qb_name: 站点详情页给出的种子标题（即预期的 QB 任务名）
        :param title: 站点列表页的种子标题（备用匹配依据）
        :return: 匹配到的 QB 任务对象；未找到返回 None
        """
        if not torrents:
            return None

        def norm(text: str) -> str:
            return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', str(text or '').lower())

        def tokens(text: str) -> set:
            """提取有区分度的英文关键词，过滤技术词与版本标记。"""
            return {t for t in re.findall(r'[a-z0-9]+', str(text or '').lower())
                    if t not in _QB_MATCH_STOP_WORDS and len(t) >= 2}

        def name_of(torrent: Any) -> str:
            try:
                return str(torrent.get("name") or "")
            except AttributeError:
                return str(getattr(torrent, "name", "") or "")

        # 1) 任务名精确匹配
        if qb_name:
            for torrent in torrents:
                if name_of(torrent) == qb_name:
                    return torrent

        # 2) 归一化后全等或互相包含
        for key in (qb_name, title):
            target = norm(key)
            if not target:
                continue
            for torrent in torrents:
                current = norm(name_of(torrent))
                if current and (current == target or target in current or current in target):
                    return torrent

        # 3) 关键词重合（兼容发布组、版本标记等差异）
        target_tokens = tokens(title)
        if target_tokens:
            best: Optional[Any] = None
            best_score = 0.0
            for torrent in torrents:
                record_tokens = tokens(name_of(torrent))
                if not record_tokens:
                    continue
                common = target_tokens & record_tokens
                score = len(common) / len(target_tokens)
                if score >= 0.8 and len(common) >= 4 and score > best_score:
                    best = torrent
                    best_score = score
            if best is not None:
                return best
        return None

    @staticmethod
    def __describe_qb(torrent: Optional[Any]) -> str:
        """把 QB 任务状态描述为一句中文。

        仅描述「本地下载器」侧的真实状态，与站点侧进度无关。

        :param torrent: QB 任务对象；None 表示 QB 中不存在该任务
        :return: 形如 "下载中 23.5%（4.1 MB/s）" 的文本
        """
        if not torrent:
            return "QB 中未找到该任务（可能已被删除或改名）"

        def field(key: str, default: Any = None) -> Any:
            try:
                value = torrent.get(key, default)
            except AttributeError:
                value = getattr(torrent, key, default)
            return default if value is None else value

        try:
            progress = float(field("progress", 0) or 0)
        except (TypeError, ValueError):
            progress = 0.0
        state = str(field("state", "") or "").lower()

        # 已完成：区分「还在做种」与「做种已暂停」
        if progress >= 1.0:
            if state in ("pausedup", "stoppedup"):
                return "已完成（做种已暂停）"
            return "已完成（做种中）"

        label = _QB_STATE_TEXT.get(state, state or "状态未知")
        speed_text = _format_speed(field("dlspeed", 0))
        if speed_text != "-":
            return f"{label} {progress * 100:.1f}%（{speed_text}）"
        return f"{label} {progress * 100:.1f}%"

    @staticmethod
    def __find_qb_torrent_name(downloader_obj: Any, title: str) -> str:
        """从 QB 中查询刚推送任务的真实任务名。

        QB 会按站点种子名创建任务，但可能带中文前缀或调整标点/版本标记，
        因此复用 __pick_qb_torrent 的匹配策略。

        :param downloader_obj: 下载器实例
        :param title: 站点种子标题
        :return: QB 任务名；未找到返回空字符串
        """
        if not downloader_obj or not title:
            return ""

        try:
            result = downloader_obj.get_torrents()
        except Exception as err:
            logger.warning(f"UHD原盘自动下载：查询 QB 任务名失败：{err}")
            return ""

        if isinstance(result, tuple):
            torrents, error = result
            if error:
                return ""
        else:
            torrents = result

        torrent = UhdBlurayAutoDownload.__pick_qb_torrent(
            list(torrents or []), "", title
        )
        if not torrent:
            return ""
        try:
            return str(torrent.get("name") or "")
        except AttributeError:
            return str(getattr(torrent, "name", "") or "")

    @staticmethod
    def __extract_cn_title(subtitle: str) -> str:
        """从站点副标题中提取中文标题。

        副标题格式形如「双子杀手 [DIY UHD原盘 ...]」或
        「查理·威尔森的战争 / 盖世奇才(台) / 韦氏风云 / [DIY ...]」，
        先取第一个方括号之前的部分，再按别名分隔符（/ 或 |）取第一个别名。

        :param subtitle: 站点副标题
        :return: 中文标题；无法提取时返回空字符串
        """
        if not subtitle:
            return ""
        # 去掉方括号内的制作说明
        head = re.split(r'[\[【]', subtitle, maxsplit=1)[0].strip()
        # 多别名时只保留第一个
        first = re.split(r'\s*[/|]\s*', head, maxsplit=1)[0].strip()
        return first.rstrip("/").strip()

    def __download_and_push(self, site: Dict[str, Any], site_conf: Dict[str, Any],
                            torrent: Dict[str, Any], downloader_obj: Any) -> bool:
        """下载种子文件并推送到 QB。

        :param site: 站点配置
        :param site_conf: 站点插件配置
        :param torrent: 种子信息
        :param downloader_obj: 下载器实例
        :return: 是否成功
        """
        download_url = torrent.get("download_url") or ""
        if not download_url:
            return False

        # 拼接完整下载地址（列表页返回的是相对路径）
        if not download_url.startswith("http"):
            base_url = (site.get("url") or "").rstrip("/")
            download_url = f"{base_url}/{download_url.lstrip('/')}"

        # 下载种子文件：部分站点（如天空）用 POST 表单触发下载，
        # 需按列表页解析出的请求方法提交
        method = str(torrent.get("download_method") or "get").lower()
        request = RequestUtils(
            ua=site.get("ua"),
            cookies=site.get("cookie"),
            proxies=settings.PROXY if site.get("proxy") else None,
            timeout=site.get("timeout") or 30,
        )
        try:
            if method == "post":
                res = request.post_res(url=download_url)
            else:
                res = request.get_res(url=download_url)
        except Exception as err:
            logger.error(f"UHD原盘自动下载：下载种子文件失败，{err}")
            return False

        if res is None or res.status_code != 200 or not res.content:
            logger.error(f"UHD原盘自动下载：下载种子文件失败，状态码 {res.status_code if res else 'None'}")
            return False

        # 校验拿到的是真正的种子文件（bencode 以 "d" 开头），
        # 避免把登录页/提示页当成种子推送给 QB
        if res.content[:1] != b"d":
            logger.error(
                f"UHD原盘自动下载：下载内容不是种子文件，已跳过（{method.upper()} {download_url}）"
            )
            return False

        # 推送到 QB（标签按站点配置，未配置时回退到默认标签）
        tag = str(site_conf.get("tag") or DOWNLOAD_TAG)
        success, _ = downloader_obj.add_torrent(
            content=res.content,
            download_dir=site_conf.get("save_path"),
            category=site_conf.get("category"),
            tag=tag,
            is_paused=False,
        )
        return success

    @staticmethod
    def __parse_list_page(html: str, filter_mode: str,
                          site_conf: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """解析站点种子列表页，提取 UHD BluRay 原盘。

        :param html: 页面 HTML
        :param filter_mode: 筛选模式
                            "uhd_title"   标题需匹配 UHD BluRay
                            "uhd_2160p"   标题需同时含 2160p 与 UHD BluRay
                            "bluray_only" 仅排除非原盘（站点已按媒介筛选）
                            "none"        不做标题筛选（列表页地址已带站点侧筛选条件）
        :param site_conf: 站点配置，可选。读取三个站点级开关：
                          diy_team             标题需含的制作组（空 = 不过滤）
                          has_progress_column  该页是否有「进度」列（False = 不读进度）
                          list_subtitle_index  副标题取 text 分段的第几段
        :return: 种子信息列表
        """
        torrents: List[Dict[str, Any]] = []
        page = etree.HTML(html)
        if page is None:
            return torrents

        conf = site_conf or {}
        # 站点级制作组过滤（与家园同名键语义一致，但不重排、保持站点顺序）
        require_team = str(conf.get("diy_team") or "")
        team_re = re.compile(re.escape(require_team), re.I) if require_team else None
        # 该站列表页是否有「进度」列。没有时不读进度，交由调用方换用别的去重手段
        has_progress_column = bool(conf.get("has_progress_column", True))
        # 副标题在标题单元格 text 分段里的下标；None = 沿用旧口径「取最后一段」
        subtitle_index = conf.get("list_subtitle_index")

        rows = page.xpath('//table[contains(@class,"torrents")]//tr[position()>1]')
        for row in rows:
            tds = row.xpath('./td')
            if len(tds) < 10:
                continue

            # 详情链接与标题
            detail_links = row.xpath('.//a[contains(@href,"details.php")]')
            if not detail_links:
                continue
            detail_href = detail_links[0].get('href') or ""
            id_match = re.search(r'id=(\d+)', detail_href)
            torrent_id = id_match.group(1) if id_match else ""

            # 主标题：优先取 a 的 title 属性
            title = detail_links[0].get('title') or ""
            if not title:
                title = detail_links[0].xpath('string(.)').strip()

            # 按筛选模式过滤
            if filter_mode == "uhd_title":
                # 需标题含 UHD BluRay
                if not re.search(r'UHD\s*Blu-?ray', title, re.I):
                    continue
            elif filter_mode == "uhd_2160p":
                # UBits：分辨率与媒介两个条件都要满足（站点侧已筛，这里复核）
                if not re.search(r'2160p', title, re.I):
                    continue
                if not re.search(r'UHD\s*Blu-?ray', title, re.I):
                    continue
            elif filter_mode == "bluray_only":
                # 站点已按媒介筛选，仅排除非原盘（WEB-DL/HDTV/Encode 等）
                if re.search(r'WEB-?DL|HDTV|WEBRip|Encode|Remux', title, re.I):
                    continue
                # 需含 BluRay/Blu-ray
                if not re.search(r'Blu-?ray', title, re.I):
                    continue
            # filter_mode == "none"：列表页地址已带站点侧筛选条件，此处不再过滤标题

            # 制作组过滤（UBits 的真正筛子）：标题不含指定组名直接跳过。
            # 放在标题筛选之后、解析字段之前，命中的行才继续走后面的请求。
            if team_re and not team_re.search(title):
                continue

            # 副标题：优先从 font.subtitle（彩虹岛）提取，其次取 td.embedded 文本（我堡/天空）
            subtitle = ""
            subtitle_nodes = tds[1].xpath('.//font[contains(@class,"subtitle")]')
            if subtitle_nodes:
                # 彩虹岛：副标题在 font.subtitle 内，需排除标签 div 与 H&R 标识 div
                texts = []
                for node in subtitle_nodes[0].xpath('.//text()'):
                    text = node.strip()
                    if not text:
                        continue
                    # 跳过纯 H&R 标识（如 h3、h5）
                    if re.fullmatch(r'h[35]', text, re.I):
                        continue
                    texts.append(text)
                if texts:
                    subtitle = texts[-1]
            if not subtitle:
                embedded = tds[1].xpath('.//td[@class="embedded"]')
                if embedded:
                    texts = [t.strip() for t in embedded[0].xpath('.//text()') if t.strip()]
                    if texts:
                        # 站点指定了下标就按下标取（UBits 的中文副标题固定在第 2 段，
                        # 末段是豆瓣评分，如 "7.4"）；否则沿用旧口径取最后一段
                        if isinstance(subtitle_index, int) and 0 <= subtitle_index < len(texts):
                            subtitle = texts[subtitle_index]
                        else:
                            subtitle = texts[-1]
            # 列表页副标题只是兜底（部分站点会截断或取到无意义的片段），
            # 形如 "]" 的无效值直接丢弃，交由详情页补齐
            if subtitle and (len(subtitle) < 4
                             or not re.search(r'[0-9A-Za-z\u4e00-\u9fff]', subtitle)):
                subtitle = ""

            # 大小
            size = tds[4].xpath('string(.)').strip() if len(tds) > 4 else ""

            # 存活时间（td[3]）；仅用于展示，不参与排序
            age_text = tds[3].xpath('string(.)').strip() if len(tds) > 3 else ""
            age_seconds = _parse_age_seconds(age_text) if len(age_text) <= 20 else None

            # 进度列：我堡为 td[8]，彩虹岛为 td[9]，天空为 td[8]
            # 未下载时值为 "-"（我堡）或 "--"（彩虹岛）或 "0%"（天空），已下载为 "100%"
            #
            # ⚠️ 站点没有该列时必须整段跳过（has_progress_column=False）：
            #    UBits 的 td[8] 是「盒子」(class="seed-box-policy-column")，取值
            #    100% 或空。照搬这段会读出 "100%" 并被当成「站点侧已下载完成」，
            #    实测前 100 条里 65 条会因此被静默跳过。
            progress = ""
            if has_progress_column:
                for idx in (8, 9):
                    if len(tds) > idx:
                        text = tds[idx].xpath('string(.)').strip()
                        # 部分站点用全角百分号渲染进度（如彩虹岛的 "29.29％"），
                        # 归一化为半角后再匹配，否则会被当成「无法识别」而误判
                        normalized = text.replace("％", "%")
                        if normalized in ("-", "--", "100%") or re.match(
                            r'^\d+(\.\d+)?%$', normalized
                        ):
                            progress = normalized
                            break

            # 下载链接：优先 <a href="download.php...">（彩虹岛/我堡），
            # 其次 <form action="download.php...">（天空用 POST 表单触发下载）
            download_url = ""
            download_method = "get"
            dl_links = row.xpath('.//a[contains(@href,"download.php")]/@href')
            if dl_links:
                download_url = dl_links[0]
            else:
                dl_forms = row.xpath('.//form[contains(@action,"download.php")]')
                # 站点同时提供「单种」与「ZIP 打包」两个表单，取单种那个
                picked = None
                for form in dl_forms:
                    if "type=" not in (form.get('action') or ""):
                        picked = form
                        break
                if picked is None and dl_forms:
                    picked = dl_forms[0]
                if picked is not None:
                    download_url = picked.get('action') or ""
                    download_method = (picked.get('method') or "post").lower()

            # 行级标记：对该行 HTML 做正则识别，一次序列化供多个标记复用。
            #   - 免费促销（class="pro_free" / class="free" / alt="Free"）：
            #     仅用于「只推免费 / 免费优先」的行级复核，不改变取数顺序。
            #   - H&R 考核（我堡 class="hitandrun" / 彩虹岛 class="circle-text"）：
            #     仅用于详情页展示与推送通知提示，不参与推送判定
            #     （是否带 H&R 由站点自行考核，插件不做取舍）。
            row_html = ""
            try:
                row_html = etree.tostring(row, encoding="unicode")
            except Exception:
                row_html = ""
            is_free = bool(_FREE_MARK_RE.search(row_html)) if row_html else False
            # 站点同款 H&R 标识文本：彩虹岛取圆标里的 h+数字（如 h5），
            # 我堡取图标语义「H&R」。详情页据此复刻各自站点的同款徽章。
            hr_mark = ""
            if row_html:
                circle_match = _HR_CIRCLE_RE.search(row_html)
                if circle_match:
                    hr_mark = circle_match.group(1)
                elif _HR_MARK_RE.search(row_html):
                    hr_mark = "H&R"
            is_hr = bool(hr_mark)

            torrents.append(
                {
                    "id": torrent_id,
                    "title": title,
                    "subtitle": subtitle,
                    "size": size,
                    "progress": progress,
                    "age_seconds": age_seconds,
                    "download_url": download_url,
                    "download_method": download_method,
                    "is_free": is_free,
                    "is_hr": is_hr,
                    "hr_mark": hr_mark,
                }
            )
        return torrents

    @staticmethod
    def __parse_hdhome_page(html: str, site_conf: Dict[str, Any]) -> List[Dict[str, Any]]:
        """解析家园（HDHome）列表页，按三条规则过滤后返回命中列表（发种时间倒序）。

        家园列表页是标准 NexusPHP，但**混着置顶行与其它分类**，且列表页排序并非
        严格按发布时间（置顶段会插在普通段之前），因此不能沿用「取列表最前 N 条」。
        改为整页抓取后按规则过滤：

          ① 标题需含 2160p 与 UHD Blu-ray（大小写不敏感，兼容 UHD BluRay 写法）
          ② 标题需含指定制作组（默认 DiY@HDHome，大小写不敏感）
          ③ 发种时间 < max_age_hours（时间列带精确时间戳 span title，无歧义）

        过滤后按发种时间倒序（最新在前），由调用方取前 N 条。

        列表页的副标题不做解析（家园副标题较长、列表页可能截断），
        留空后交由 __fetch_detail_fields 从详情页补齐（家园详情页字段与
        现有 __parse_detail_subtitle / __parse_detail_seed_name 兼容）。

        :param html: 页面 HTML
        :param site_conf: 站点插件配置（含 diy_team / max_age_hours）
        :return: 命中种子信息列表（字段与 __parse_list_page 返回一致）
        """
        torrents: List[Dict[str, Any]] = []
        page = etree.HTML(html)
        if page is None:
            return torrents

        diy_team = str(site_conf.get("diy_team") or "diy@hdhome")
        try:
            max_age_hours = float(site_conf.get("max_age_hours") or 120)
        except (TypeError, ValueError):
            max_age_hours = 120.0

        team_re = re.compile(re.escape(diy_team), re.I)
        res_re = re.compile(r'2160p', re.I)
        uhd_re = re.compile(r'uhd\s*blu-?ray', re.I)
        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz)

        rows = page.xpath('//table[contains(@class,"torrents")]//tr[position()>1]')
        for row in rows:
            tds = row.xpath('./td')
            if len(tds) < 10:
                continue

            detail_links = row.xpath('.//a[contains(@href,"details.php")]')
            if not detail_links:
                continue
            detail_href = detail_links[0].get('href') or ""
            id_match = re.search(r'id=(\d+)', detail_href)
            torrent_id = id_match.group(1) if id_match else ""

            title = detail_links[0].get('title') or ""
            if not title:
                title = detail_links[0].xpath('string(.)').strip()

            # 规则 ① + ②：标题含 2160p 与 UHD Blu-ray，且制作组匹配
            if not (res_re.search(title) and uhd_re.search(title)):
                continue
            if not team_re.search(title):
                continue

            # 规则 ③：发种时间 < max_age_hours（精确时间戳 span title）
            ts_vals = tds[3].xpath('.//span/@title') if len(tds) > 3 else []
            age_seconds: Optional[int] = None
            if ts_vals:
                try:
                    t = datetime.strptime(ts_vals[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
                    age_seconds = int((now - t).total_seconds())
                except (ValueError, TypeError):
                    age_seconds = None
            if age_seconds is None or age_seconds >= max_age_hours * 3600:
                continue

            # 大小（家园为 td[4]）
            size = tds[4].xpath('string(.)').strip() if len(tds) > 4 else ""

            # 进度列（家园为 td[8]，未下载 "-"）
            progress = ""
            for idx in (8, 9):
                if len(tds) > idx:
                    text = tds[idx].xpath('string(.)').strip().replace("％", "%")
                    if text in ("-", "--", "0%", "0", "100%") or re.match(
                        r'^\d+(\.\d+)?%$', text
                    ):
                        progress = text
                        break

            # 下载链接（家园为 <a href="download.php?id=...">）
            download_url = ""
            download_method = "get"
            dl_links = row.xpath('.//a[contains(@href,"download.php")]/@href')
            if dl_links:
                download_url = dl_links[0]
            else:
                dl_forms = row.xpath('.//form[contains(@action,"download.php")]')
                picked = None
                for form in dl_forms:
                    if "type=" not in (form.get('action') or ""):
                        picked = form
                        break
                if picked is None and dl_forms:
                    picked = dl_forms[0]
                if picked is not None:
                    download_url = picked.get('action') or ""
                    download_method = (picked.get('method') or "post").lower()

            # 免费促销（家园用 pro_free 图标，与其它站一致，复用 _FREE_MARK_RE）
            row_html = ""
            try:
                row_html = etree.tostring(row, encoding="unicode")
            except Exception:
                row_html = ""
            is_free = bool(_FREE_MARK_RE.search(row_html)) if row_html else False

            torrents.append(
                {
                    "id": torrent_id,
                    "title": title,
                    "subtitle": "",  # 列表页不解析，交详情页补齐
                    "size": size,
                    "progress": progress,
                    "age_seconds": age_seconds,
                    "download_url": download_url,
                    "download_method": download_method,
                    "is_free": is_free,
                    "is_hr": False,
                    "hr_mark": "",
                }
            )

        # 按发种时间倒序（age_seconds 升序 = 最新在前）
        torrents.sort(key=lambda x: x.get("age_seconds") or 0)
        return torrents

    @staticmethod
    def _site_switch_key(domain: str) -> str:
        """把站点域名转换为配置表单里的站点键名。

        :param domain: 站点域名（如 hdsky.me）
        :return: 站点键名（如 enable_hdsky_me）
        """
        return "enable_" + re.sub(r'[^0-9a-z]+', '_', str(domain or '').lower()).strip('_')

    @staticmethod
    def _site_conf_alias(domain: str) -> str:
        """把站点域名转换为「下载配置」键的后缀（v2.12.0 起）。

        与 _site_switch_key 的区别：不带 enable_ 前缀，
        用于拼 dw_cat_ / dw_tag_ / dw_path_ / dw_push_ 四个表格键。

        :param domain: 站点域名（如 hdsky.me）
        :return: 键后缀（如 hdsky_me）
        """
        return re.sub(r'[^0-9a-z]+', '_', str(domain or '').lower()).strip('_')

    @classmethod
    def __default_enabled_keys(cls) -> List[str]:
        """返回默认勾上的站点键名列表，用于表单默认值。

        :return: default_enabled 为真的站点的键名列表
        """
        return [
            cls._site_switch_key(domain)
            for domain, site_conf in cls._site_configs.items()
            if site_conf.get("default_enabled", True)
        ]

    def __enabled_site_text(self) -> str:
        """返回「已勾选采集」的站点名称文本，用于详情页概览。

        :return: 形如 "彩虹岛、天空"；全部未勾选时返回 "无（全部未勾选）"
        """
        names = [
            str(site_conf.get("name") or domain)
            for domain, site_conf in self._site_configs.items()
            if self._site_enabled.get(domain, True)
        ]
        return "、".join(names) if names else "无（全部未勾选）"

    def __push_site_text(self) -> str:
        """返回「采集且推送开关为开」的站点名称文本，用于详情页概览（v2.12.0 起）。

        与 __enabled_site_text 的区别：这里额外要求 push_enabled 为真，
        所以「采集了但不推送」的站点不会出现在这段里 —— 详情页一眼能看出
        哪些站点是「只看不推」的状态。

        :return: 形如 "彩虹岛、天空"；无此类站点时返回 "无（均仅采集）"
        """
        names = []
        for domain, site_conf in self._site_configs.items():
            if not self._site_enabled.get(domain, True):
                continue
            eff = self.__site_effective_conf(domain, site_conf)
            if eff.get("push_enabled", True):
                names.append(str(site_conf.get("name") or domain))
        return "、".join(names) if names else "无（均仅采集）"

    def __get_downloader(self) -> Optional[Any]:
        """获取下载器实例。

        :return: 下载器实例；获取失败返回 None
        """
        services = DownloaderHelper().get_services(name_filters=[self._downloader])
        if not services:
            return None
        for service_name, service_info in services.items():
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                logger.warning(f"UHD原盘自动下载：下载器 {service_name} 不是 QB 类型")
                continue
            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"UHD原盘自动下载：下载器 {service_name} 未连接")
                continue
            return downloader_obj
        return None

    def __site_effective_conf(self, domain: str, site_conf: Dict[str, Any]) -> Dict[str, Any]:
        """把表单里的「下载分类 / 路径 / 推送开关」覆盖到站点配置上（v2.12.0 起）。

        改造前：category / save_path / push_enabled 写死在 _site_configs 常量里，
                配置页上只以说明文字出现，用户改不了。
        改造后：三个键均可在表单表格里逐站点编辑；
                配置里没有对应键时（老配置 / 未保存过）**原样返回**，
                行为与 v2.11.0 完全一致。

        只做浅拷贝 + 局部覆盖，不修改 _site_configs 本身（类级共享，改了会污染其它实例）。

        :param domain: 站点域名
        :param site_conf: _site_configs 里的站点配置
        :return: 覆盖后的站点配置（新字典）
        """
        override = self._dw_override.get(domain)
        if not override:
            return site_conf
        merged = dict(site_conf)
        for key, value in override.items():
            # 分类 / 路径允许「显式留空 = 不归类 / 用下载器默认目录」，
            # 故空字符串也要覆盖，不能用 `if value:` 判断。
            merged[key] = value
        return merged

    def __get_site_config(self, domain: str) -> Optional[dict]:
        """获取站点配置（同一轮内复用，避免同一条记录反复查库）。

        旧实现每次都重新 `SiteOper().get_by_domain()` 查一遍库：补齐流程
        逐条遍历历史记录时会反复查同一个域名，一轮实测查到 24 次。
        缓存按轮清空（见 check_uhd 开头）。

        :param domain: 站点域名
        :return: 站点配置字典；未找到返回 None
        """
        if domain in self._site_conf_cache:
            return self._site_conf_cache[domain]
        site = SiteOper().get_by_domain(domain)
        config: Optional[dict] = None
        if site:
            config = {
                "name": site.name,
                "domain": site.domain,
                "url": site.url,
                "cookie": site.cookie,
                "ua": site.ua,
                "proxy": site.proxy,
                "timeout": site.timeout,
            }
        self._site_conf_cache[domain] = config
        return config

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None
