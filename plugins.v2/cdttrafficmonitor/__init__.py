"""阿里云 CDT 流量监控（MoviePilot V2 插件）。

移植自 https://github.com/wang4386/CDT-Monitor （Go 版 CDT Monitor）的核心能力：

  · 多阿里云账号 / 地域 / ECS 实例集中管理
  · CDT 公网流量（中国大陆 / 非中国大陆分别聚合）与采样历史
  · 流量阈值告警：仅通知 / 停机并通知，带重复告警抑制与恢复通知
  · 每日定时开关机（窗口内补偿触发 + 按周期去重，支持跨零点时段）
  · 实例保活（意外停止且在允许运行时段内则拉起，阈值熔断优先）
  · 定时日报（统计「时段内实际消耗」而非累计值）
  · 可选账单：账户可用余额 + 实例当月费用
  · 详情页看板：逐账号卡片（金额 / 进度条 / 用量 / 剩余 / 增量）+ 手动刷新 / 开机 / 停机
  · 配置表单：主实例逐字段填写（AK / Secret / 实例 / 地域 / 额度 / 站点类型 / 备注 / 定时），
    Secret 不回显（留空即保持不变），多账号写在下方的「附加账号」里

与原版 Go 项目的差异（刻意的）：

  · 通知渠道直接复用 MoviePilot（Telegram / 微信 / 邮件 / Webhook 等），
    不再自带 SMTP、Telegram Bot 与 Webhook 发送实现。
  · AccessKey Secret 以插件配置形式存库（不做单机 master.key 加密）。
    面板本身即是可信边界，切勿在多人共用的面板上存放高权限 AK。
  · 所有「写操作」（停机 / 开机 / 保活）默认关闭，需要显式打开开关才生效。

阿里云接口（RPC 风格，HMAC-SHA1 签名）：

  · CDT  ListCdtInternetTraffic   cdt.aliyuncs.com              2021-08-13
  · ECS  DescribeInstanceStatus   ecs.{region}.aliyuncs.com     2014-05-26
  · ECS  StartInstance / StopInstance（StopInstance 带 StoppedMode）
  · BSS  QueryAccountBalance / DescribeInstanceBill            2017-12-14
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import requests
import urllib3

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# 关闭 urllib3 的 InsecureRequestWarning，避免刷屏
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 插件 ID（= 类名；目录名取其小写）。分身友好：不要写死在路由里
_PLUGIN_ID = "CdtTrafficMonitor"

# ---- 阿里云各产品的接入点与版本 ----
CDT_HOST = "cdt.aliyuncs.com"
CDT_VERSION = "2021-08-13"
# CDT 的公网流量查询不区分地域（一次返回该账号全部地域明细），
# 固定用 cn-hongkong 作为请求的 RegionId（与原版 Go 项目一致）。
CDT_REGION = "cn-hongkong"

ECS_VERSION = "2014-05-26"
BSS_VERSION = "2017-12-14"

# CVD 免费额度：每月 220 GB（中国大陆 20 GB + 非中国大陆 200 GB），
# 账号级、按自然月重置（每月 1 日 00:00 刷新，不结转）。
CDT_FREE_QUOTA_GB = 220.0
CDT_FREE_QUOTA_CN_GB = 20.0
CDT_FREE_QUOTA_INTL_GB = 200.0

# 单字节 → GiB 的换算系数（接口返回的 Traffic 单位是 Byte）
_BYTES_PER_GIB = 1024.0 * 1024.0 * 1024.0

# 持久化键名
STATE_KEY = "account_state"          # 逐账号运行时状态 + 采样 + 告警去重
# 主实例的 AccessKey Secret 不进配置表单：只在这里留一份，表单永远显示「留空保持不变」
SECRET_KEY = "access_key_secret"

# 站点类型：只决定 BSS 账单接口走中国站还是国际站（与流量分类无关）
SITE_TYPE_CN = "china"
SITE_TYPE_INTL = "international"
_SITE_TYPE_ITEMS = [
    {"title": "中国站", "value": SITE_TYPE_CN},
    {"title": "国际站", "value": SITE_TYPE_INTL},
]

# 币种符号（CDC 账单返回的 Currency，未收录的原样带出）
_CURRENCY_SYMBOLS = {
    "CNY": "¥", "RMB": "¥", "USD": "$", "HKD": "HK$", "JPY": "JP¥", "EUR": "€",
}
# 每个账号保留的采样条数（详情页算「较上次 / 24h 增量」用）
ACCOUNT_SAMPLE_LIMIT = 24
# 打开详情页时缓存的最大容忍年龄（秒），超过则后台静默刷新一次
PAGE_CACHE_TTL = 120
# 实例处于中间态（正在启动/停止/未知）时的查询间隔（秒）
TRANSIENT_INTERVAL_SECONDS = 60
# 手动「刷新全部 / 立即执行」最多等上一轮跑完多少秒（宁可等一下也不能静默跳过）
MANUAL_LOCK_WAIT_SECONDS = 20

# 数值边界
MIN_TICK_MINUTES = 1
MAX_TICK_MINUTES = 1440
DEFAULT_TICK_MINUTES = 5          # 调度心跳：定时开关机靠它保证不误点
DEFAULT_INTERVAL_MINUTES = 10     # 实际查询阿里云的最小间隔
MIN_INTERVAL_MINUTES = 1
DEFAULT_THRESHOLD = 90            # 告警阈值（百分比）
SCHEDULE_WINDOW_MINUTES = 10      # 定时任务补偿窗口（进程重启 / 任务延迟）
THRESHOLD_ACTION_NOTIFY = "notify_only"
THRESHOLD_ACTION_STOP = "stop_and_notify"
DEFAULT_SHUTDOWN_MODE = "KeepCharging"

# ---- 详情页配色（按主题变量取色，深/浅色主题都自适应）----
COLOR_LABEL = "rgba(var(--v-theme-on-surface), 0.62)"   # 字段标签
COLOR_NAME = "rgba(var(--v-theme-on-surface), 0.80)"    # 次要信息（AK / 地域）
COLOR_VALUE = "rgba(var(--v-theme-on-surface), 0.94)"   # 真正要看清的取值
BAR_COLOR_WARN = "#E08A17"                              # 接近阈值
BAR_COLOR_OVER = "#E52D15"                              # 越过阈值
COLOR_OK = "#2E7D32"                                    # 正常 / 恢复
COLOR_ERR = "#C0392B"                                   # 失败
# 右下角悬浮齿轮（56px，距对话框右下各 12px）会扫过整页，右端有内容的块都要让位
_FAB_CHANNEL = 68

# ---- 图标 ----
# 🔴 只能用面板前端**确实打包进来**的名字：前端按需打包 @mdi/js，
#    用没被打包的图标名（例如 mdi-server-outline / mdi-power）会渲染成空白。
ICON_SERVER = "mdi-server-network-outline"
ICON_CLOCK = "mdi-clock-outline"
ICON_REFRESH = "mdi-refresh"
ICON_PLAY = "mdi-play-circle-outline"
ICON_STOP = "mdi-stop-circle-outline"

# 实例状态
STATUS_RUNNING = "Running"
STATUS_STOPPED = "Stopped"
STATUS_STARTING = "Starting"
STATUS_STOPPING = "Stopping"
STATUS_UNKNOWN = "Unknown"
_TRANSIENT_STATUS = {STATUS_STARTING, STATUS_STOPPING, "Pending", STATUS_UNKNOWN, ""}

# 地域名（与原版 engine.RegionName 保持一致）
_REGION_NAMES = {
    "cn-hongkong": "中国香港",
    "ap-southeast-1": "新加坡",
    "us-west-1": "美国（硅谷）",
    "us-east-1": "美国（弗吉尼亚）",
    "cn-hangzhou": "华东 1（杭州）",
    "cn-shanghai": "华东 2（上海）",
    "cn-qingdao": "华北 1（青岛）",
    "cn-beijing": "华北 2（北京）",
    "cn-zhangjiakou": "华北 3（张家口）",
    "cn-huhehaote": "华北 5（呼和浩特）",
    "cn-wulanchabu": "华北 6（乌兰察布）",
    "cn-shenzhen": "华南 1（深圳）",
    "cn-heyuan": "华南 2（河源）",
    "cn-guangzhou": "华南 3（广州）",
    "cn-chengdu": "西南 1（成都）",
    "ap-northeast-1": "日本（东京）",
    "ap-northeast-2": "韩国（首尔）",
}


def _region_name(region_id: str) -> str:
    """地域 ID → 中文名；未知地域原样返回。"""
    region_id = (region_id or "").strip()
    if not region_id:
        return "未指定地域"
    return _REGION_NAMES.get(region_id, region_id)


def _traffic_class(region_id: str) -> str:
    """按地域判断属于哪一类 CDT 流量。

    与原版一致：``cn-`` 开头且不是 ``cn-hongkong`` 的算中国大陆，
    其余（含港澳台以外的所有海外地域）算非中国大陆。
    """
    region_id = (region_id or "").strip()
    if region_id.startswith("cn-") and region_id != "cn-hongkong":
        return "china"
    return "international"


def _class_label(traffic_class: str) -> str:
    return "中国大陆" if traffic_class == "china" else "非中国大陆"


def _mask_ak(access_key_id: str) -> str:
    """AccessKeyId 掩码（前 7 位 + ***），与面板整体习惯一致。"""
    value = access_key_id or ""
    if len(value) <= 7:
        return value + "***"
    return value[:7] + "***"


def _to_bool(value: Any, default: bool = False) -> bool:
    """宽容的布尔解析：兼容 True/False、"1"/"0"、"是"/"否"、中文开关词。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on", "是", "开", "启用", "开启"):
        return True
    if text in ("0", "false", "no", "n", "off", "否", "关", "禁用", "关闭"):
        return False
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalize_clock(value: str, default: str = "") -> str:
    """把 ``8:5`` / ``08:05`` / 全角冒号 之类归一成 ``HH:MM``；非法值返回 default。"""
    text = str(value or "").strip().replace("：", ":")
    if not text:
        return default
    match = re.match(r"^(\d{1,2}):(\d{1,2})$", text)
    if not match:
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return f"{hour:02d}:{minute:02d}"


def _clock_minutes(value: str) -> Optional[int]:
    """``HH:MM`` → 当天的分钟数；非法返回 None。"""
    parsed = _normalize_clock(value)
    if not parsed:
        return None
    hour, minute = parsed.split(":")
    return int(hour) * 60 + int(minute)


def _parse_accounts(raw: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析账号配置文本，返回 ``(账号列表, 错误列表)``。

    支持两种写法，自动识别：

    1. **JSON 数组**（文本以 ``[`` 开头）::

           [{"name": "香港中转", "ak": "LTAI...", "sk": "...",
             "region": "cn-hongkong", "instance": "i-j6...", "quota": 500}]

    2. **行式**（每行一个账号，``键=值`` 用 ``,`` / ``;`` 分隔，支持中文键名）::

           名称=香港中转, AK=LTAI..., SK=..., 地域=cn-hongkong, 实例=i-j6..., 额度=500

    ``#`` 开头的行视为注释。错误只记录不抛异常 —— 一行写错不该让整份配置失效。
    """
    text = str(raw or "").strip()
    accounts: List[Dict[str, Any]] = []
    errors: List[str] = []
    if not text:
        return accounts, errors

    # ---- 形态 1：JSON 数组 ----
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as err:
            return accounts, [f"JSON 解析失败：{err}"]
        if not isinstance(data, list):
            return accounts, ["JSON 配置必须是数组"]
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append(f"第 {index + 1} 项不是对象，已跳过")
                continue
            account = _normalize_account(item, index + 1, errors)
            if account:                      # 校验失败的条目返回空字典，不并入列表
                accounts.append(account)
        return accounts, errors

    # ---- 形态 2：行式 ----
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields: Dict[str, str] = {}
        for token in re.split(r"[,;，；]", stripped):
            token = token.strip()
            if not token:
                continue
            if "=" not in token:
                errors.append(f"第 {line_no} 行片段「{token}」缺少 = 号，已忽略")
                continue
            key, _, value = token.partition("=")
            fields[key.strip()] = value.strip()
        if not fields:
            continue
        account = _normalize_account(fields, line_no, errors)
        if account:                          # 校验失败的条目返回空字典，不并入列表
            accounts.append(account)
    return accounts, errors


# 行式 / JSON 两套键名都收敛到统一的英文键
_KEY_ALIASES = {
    "name": ("name", "remark", "alias", "名称", "备注", "别名"),
    "ak": ("ak", "access_key_id", "accesskeyid", "accesskey", "keyid", "密钥id", "accesskey_id"),
    "sk": ("sk", "access_key_secret", "accesskeysecret", "secret", "密钥", "secretkey"),
    "region": ("region", "region_id", "regionid", "地域", "区域", "地区"),
    "instance": ("instance", "instance_id", "instanceid", "实例", "实例id", "ecs"),
    "quota": ("quota", "max_traffic", "maxtraffic", "traffic", "额度", "流量", "额度gb", "流量gb"),
    "site_type": ("site_type", "sitetype", "site", "站点", "站点类型"),
    "schedule": ("schedule", "schedule_enabled", "定时", "定时开关机"),
    "start_time": ("start_time", "start", "starttime", "开机", "开机时间", "启动时间"),
    "stop_time": ("stop_time", "stop", "stoptime", "关机", "关机时间", "停止时间"),
    "keep_alive": ("keep_alive", "keepalive", "保活", "实例保活"),
    "shutdown_mode": ("shutdown_mode", "shutdownmode", "停机模式", "停止模式"),
    "daily_report": ("daily_report", "dailyreport", "日报", "日报推送"),
    "daily_report_time": ("daily_report_time", "report_time", "日报时间", "推送时间"),
}


def _normalize_account(raw: Dict[str, Any], line_no: int, errors: List[str]) -> Dict[str, Any]:
    """把一份原始配置（行式或 JSON）规范成统一结构。"""
    # 先把所有键小写化 + 去空格，便于匹配
    lowered: Dict[str, Any] = {}
    for key, value in raw.items():
        if key is None:
            continue
        lowered[str(key).strip().lower().replace(" ", "")] = value

    def pick(field: str, default: Any = None) -> Any:
        for alias in _KEY_ALIASES[field]:
            if alias in lowered and lowered[alias] not in (None, ""):
                return lowered[alias]
        return default

    access_key_id = str(pick("ak", "") or "").strip()
    secret = str(pick("sk", "") or "").strip()
    region = str(pick("region", "") or "").strip() or "cn-hongkong"
    instance_id = str(pick("instance", "") or "").strip()
    name = str(pick("name", "") or "").strip()

    if not access_key_id or not secret:
        errors.append(f"第 {line_no} 条缺少 AK 或 SK，已跳过")
        return {}

    site_type = str(pick("site_type", "") or "").strip().lower()
    if site_type not in ("china", "international"):
        site_type = "china" if region.startswith("cn-") and region != "cn-hongkong" else "international"

    # 布尔项：显式写了才覆盖全局（None = 跟随全局）
    def tri_bool(field: str) -> Optional[bool]:
        for alias in _KEY_ALIASES[field]:
            if alias not in lowered:
                continue
            value = lowered[alias]
            # 键存在但值为空（表单没填 / JSON 里写了 null）→ 仍视为「跟随全局」
            if value is None or str(value).strip() == "":
                return None
            return _to_bool(value)
        return None

    quota = _to_float(pick("quota", None), -1.0)
    account = {
        "name": name or _mask_ak(access_key_id),
        "ak": access_key_id,
        "sk": secret,
        "region": region,
        "instance": instance_id,
        "quota": quota if quota > 0 else 0.0,   # 0 = 用全局默认额度
        "site_type": site_type,
        "schedule": tri_bool("schedule"),
        "start_time": _normalize_clock(str(pick("start_time", "") or "")),
        "stop_time": _normalize_clock(str(pick("stop_time", "") or "")),
        "keep_alive": tri_bool("keep_alive"),
        "shutdown_mode": str(pick("shutdown_mode", "") or "").strip(),
        "daily_report": tri_bool("daily_report"),
        "daily_report_time": _normalize_clock(str(pick("daily_report_time", "") or "")),
    }
    return account


def _account_key(account: Dict[str, Any]) -> str:
    """账号的稳定标识（持久化状态的键）。

    用 ``AK:实例ID或地域``：同一个 AK 下若监控多台实例，各自独立计数。
    """
    return f"{account.get('ak', '')}:{account.get('instance') or account.get('region')}"


def _usage_percent(traffic_gb: float, quota_gb: float) -> float:
    """用量百分比，四舍五入到两位小数（与原版 usagePercent 同口径）。"""
    if quota_gb <= 0:
        return 0.0
    return round(traffic_gb / quota_gb * 100, 2)


def _bar_color(percent: float, threshold: float) -> str:
    """进度条配色：越过 100% 红 / 达到阈值橙 / 其余主题主色。"""
    if percent >= 100.0:
        return BAR_COLOR_OVER
    if threshold > 0 and percent >= threshold:
        return BAR_COLOR_WARN
    if threshold <= 0 and percent >= 90.0:
        return BAR_COLOR_WARN
    return "rgb(var(--v-theme-primary))"


def _status_tone(status: str) -> str:
    """实例状态对应的圆点颜色。"""
    if status == STATUS_RUNNING:
        return COLOR_OK
    if status in (STATUS_STARTING, STATUS_STOPPING):
        return BAR_COLOR_WARN
    if status == STATUS_STOPPED:
        return COLOR_LABEL
    return COLOR_LABEL


def _status_text(status: str) -> str:
    return {
        STATUS_RUNNING: "运行中",
        STATUS_STOPPED: "已停止",
        STATUS_STARTING: "启动中",
        STATUS_STOPPING: "停止中",
        "Pending": "创建中",
        STATUS_UNKNOWN: "未知",
    }.get(status or "", status or "未知")


def _format_gb(value: float) -> str:
    """流量文案：小于 1 GB 时多给一位小数，避免显示成 0.00。"""
    value = max(0.0, _to_float(value))
    if 0 < value < 1.0:
        return f"{value:.3f} GB"
    return f"{value:.2f} GB"


def _format_signed_gb(value: float) -> str:
    value = _to_float(value)
    sign = "+" if value >= 0 else "-"
    return f"{sign}{_format_gb(abs(value))}"


def _currency_symbol(currency: str) -> str:
    """币种代码 → 符号（BSS 返回的 Currency，未知币种原样带出代码）。"""
    code = str(currency or "").strip().upper()
    if not code:
        return "¥"
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def _format_money(value: Any, currency: str = "CNY") -> str:
    """金额文案；拿不到值时给占位符（而不是显示成 ¥0.00）。"""
    if value in (None, ""):
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{_currency_symbol(currency)}{amount:,.2f}"


def _region_items() -> List[dict]:
    """地域下拉候选（与原版前端列表一致，顺序按 _REGION_NAMES）。"""
    return [{"title": name, "value": region} for region, name in _REGION_NAMES.items()]


def _percent_encode(value: Any) -> str:
    """阿里云 RPC 签名的 percentEncode。

    规则：``A-Z a-z 0-9`` 与 ``- _ . ~`` 不编码，空格 → ``%20``，其余 → ``%XY``。
    Python 的 ``quote(safe="")`` 恰好就是这个口径（不会把 ``~`` 编成 ``%7E``、
    也不会把空格编成 ``+``）。
    """
    return quote(str(value), safe="")


def _sign(params: Dict[str, str], secret: str) -> str:
    """阿里云 RPC 签名：``HMAC-SHA1(secret + "&", "POST&%2F&" + percentEncode(canonical))``。"""
    canonical = "&".join(
        f"{_percent_encode(key)}={_percent_encode(params[key])}"
        for key in sorted(params)
        if key != "Signature"
    )
    string_to_sign = "POST&%2F&" + _percent_encode(canonical)
    digest = hmac.new(
        (secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# 阿里云客户端
# ---------------------------------------------------------------------------

class _AliyunError(Exception):
    """阿里云接口错误（带是否可重试的标记）。"""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _is_retryable_timestamp_error(code: str, message: str) -> bool:
    """时间戳过期 / 缺失属于可重试的瞬时错误（原版也是这么处理的）。"""
    text = (f"{code} {message}").lower().replace(" ", "")
    return "timestamp" in text and any(
        token in text for token in ("expired", "notsupplied", "missing")
    )


class _AliyunClient:
    """阿里云 OpenAPI 客户端（只实现本插件用到的几个接口）。"""

    def __init__(self, proxy: Optional[str] = None, timeout: int = 20):
        self._session = requests.Session()
        # 阿里云是境内站点，默认不继承容器里的 http_proxy 环境变量（国内直连）
        self._session.trust_env = False
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
        self._session.headers.update({
            "User-Agent": "MoviePilot-CdtTrafficMonitor/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        self._timeout = timeout
        self._traffic_cache: Dict[str, float] = {}   # 一次检查内复用（按 AK+class）

    # ---- 底层调用 ----

    def _call(
        self,
        access_key_id: str,
        secret: str,
        region: str,
        host: str,
        version: str,
        action: str,
        extras: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self._call_once(
                    access_key_id, secret, region, host, version, action, extras or {}
                )
            except _AliyunError as err:
                last_error = err
                if not err.retryable:
                    break
            except Exception as err:  # noqa: BLE001  网络类异常都可重试
                last_error = err
            if attempt < 2:
                # 退避：0.3s / 0.7s（原版是 2^n×300ms + n×100ms）
                import time as _time

                _time.sleep(0.3 * (2 ** attempt) + 0.1 * attempt)
        raise _AliyunError(f"调用 {action} 失败：{last_error}")

    def _call_once(
        self,
        access_key_id: str,
        secret: str,
        region: str,
        host: str,
        version: str,
        action: str,
        extras: Dict[str, str],
    ) -> Dict[str, Any]:
        params: Dict[str, str] = {
            "AccessKeyId": access_key_id,
            "Action": action,
            "Format": "JSON",
            "RegionId": region,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": version,
        }
        for key, value in extras.items():
            if value is not None:
                params[key] = str(value)
        params["Signature"] = _sign(params, secret)

        url = f"https://{host}/"
        try:
            resp = self._session.post(
                url, data=params, timeout=self._timeout, verify=True
            )
        except Exception as err:  # noqa: BLE001
            raise _AliyunError(f"请求 {host} 失败：{err}", retryable=True)

        body = resp.text or ""
        try:
            result = json.loads(body)
        except ValueError:
            raise _AliyunError(
                f"{action} 返回非 JSON（HTTP {resp.status_code}）：{body[:200]}",
                retryable=resp.status_code >= 500,
            )
        if not isinstance(result, dict):
            raise _AliyunError(f"{action} 返回结构异常：{str(result)[:200]}")

        if resp.status_code >= 400:
            code = str(result.get("Code") or "")
            message = _compact_message(result, body)
            raise _AliyunError(
                f"{action} HTTP {resp.status_code}：{message}",
                retryable=(
                    resp.status_code >= 500
                    or resp.status_code == 429
                    # 阿里云把限流也走 HTTP 400 返回（Code=Throttling.*），
                    # 这种是典型的「等一下就好」，必须重试
                    or "throttl" in code.lower()
                    or _is_retryable_timestamp_error(code, message)
                ),
            )

        code = str(result.get("Code") or "")
        if code and code.lower() not in ("ok", "200", "success"):
            message = str(result.get("Message") or "")
            raise _AliyunError(
                f"{action} {code}：{message}",
                retryable=(
                    "throttl" in code.lower()
                    or _is_retryable_timestamp_error(code, message)
                ),
            )

        # CDT 的空明细多为瞬时抖动，重试一次大概率能拿到（原版同样处理）
        if action == "ListCdtInternetTraffic" and not _has_traffic_details(result):
            raise _AliyunError(f"{action} 未返回 TrafficDetails", retryable=True)
        return result

    # ---- 业务接口 ----

    def get_traffic(self, access_key_id: str, secret: str, traffic_class: str) -> float:
        """查询该账号在指定分类下的 CDT 公网流量（GiB）。"""
        cache_key = f"{access_key_id}:{traffic_class}"
        if cache_key in self._traffic_cache:
            return self._traffic_cache[cache_key]
        result = self._call(
            access_key_id, secret, CDT_REGION, CDT_HOST, CDT_VERSION,
            "ListCdtInternetTraffic", None,
        )
        value = _traffic_from_response(result, traffic_class)
        self._traffic_cache[cache_key] = value
        return value

    def get_traffic_both(self, access_key_id: str, secret: str) -> Dict[str, float]:
        """一次请求同时拿到「中国大陆 / 非中国大陆」两条累计值（GiB）。"""
        result = self._call(
            access_key_id, secret, CDT_REGION, CDT_HOST, CDT_VERSION,
            "ListCdtInternetTraffic", None,
        )
        both = {
            "china": _traffic_from_response(result, "china"),
            "international": _traffic_from_response(result, "international"),
        }
        for key, value in both.items():
            self._traffic_cache[f"{access_key_id}:{key}"] = value
        return both

    def get_instance_status(self, access_key_id: str, secret: str, region: str,
                            instance_id: str) -> str:
        """查询 ECS 实例状态；未配置实例 ID 或查不到时返回 Unknown。"""
        extras: Dict[str, str] = {"RegionId": region}
        if instance_id:
            extras["InstanceId"] = instance_id
        result = self._call(
            access_key_id, secret, region, f"ecs.{region}.aliyuncs.com",
            ECS_VERSION, "DescribeInstanceStatus", extras,
        )
        for item in _nested_slice(result, "InstanceStatuses", "InstanceStatus"):
            if isinstance(item, dict):
                status = str(item.get("Status") or "").strip()
                if status:
                    return status
        return STATUS_UNKNOWN

    def control_instance(self, access_key_id: str, secret: str, region: str,
                         instance_id: str, action: str, shutdown_mode: str) -> None:
        """开机 / 停机。action 取 ``start`` / ``stop``。"""
        if not instance_id:
            raise _AliyunError("未配置实例 ID，无法执行开关机")
        params: Dict[str, str] = {"RegionId": region, "InstanceId": instance_id}
        action = (action or "").lower()
        if action == "stop":
            api_action = "StopInstance"
            mode = shutdown_mode if shutdown_mode == "StopCharging" else "KeepCharging"
            params["StoppedMode"] = mode
        else:
            api_action = "StartInstance"
        self._call(
            access_key_id, secret, region, f"ecs.{region}.aliyuncs.com",
            ECS_VERSION, api_action, params,
        )

    def get_balance(self, access_key_id: str, secret: str, site_type: str) -> Dict[str, Any]:
        """账户可用余额（中国站 / 国际站走不同接入点）。"""
        host, region = _bss_endpoint(site_type)
        result = self._call(
            access_key_id, secret, region, host, BSS_VERSION, "QueryAccountBalance", None
        )
        data = result.get("Data") if isinstance(result.get("Data"), dict) else {}
        currency = str(data.get("Currency") or "CNY")
        return {"amount": _number(data.get("AvailableAmount")), "currency": currency}

    def get_instance_bill(self, access_key_id: str, secret: str, site_type: str,
                          instance_id: str, cycle: str) -> float:
        """实例当月费用合计。"""
        host, region = _bss_endpoint(site_type)
        result = self._call(
            access_key_id, secret, region, host, BSS_VERSION, "DescribeInstanceBill",
            {"BillingCycle": cycle, "InstanceID": instance_id or "", "Granularity": "MONTHLY"},
        )
        data = result.get("Data") if isinstance(result.get("Data"), dict) else {}
        items = _as_list(data.get("Items"))
        nested = _nested_slice(data, "Items", "Item")
        if nested:
            items = nested
        total = sum(
            _number(item.get("PretaxAmount"))
            for item in items
            if isinstance(item, dict)
        )
        return round(total, 2)


def _bss_endpoint(site_type: str) -> Tuple[str, str]:
    """账单接口的接入点（host, region）。"""
    if site_type == "international":
        return "business.ap-southeast-1.aliyuncs.com", "ap-southeast-1"
    return "business.aliyuncs.com", "cn-hangzhou"


def _compact_message(result: Dict[str, Any], raw: str) -> str:
    """错误摘要：Message 更好读，但 Code 才是排查依据，两者都要带上。"""
    code = str(result.get("Code") or "").strip()
    message = str(result.get("Message") or "").strip()
    if code and message:
        return f"{code}: {message}"
    return code or message or raw[:200]


def _number(value: Any) -> float:
    """宽容取数：接口偶尔把数字回成字符串。"""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        inner = value.get("Item")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            return [inner]
    return []


def _nested_slice(root: Dict[str, Any], *keys: str) -> List[Any]:
    current: Any = root
    for key in keys:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return _as_list(current)


def _traffic_details(result: Dict[str, Any]) -> List[Any]:
    """兼容 ``TrafficDetails`` 顶层与包在 ``Data`` 里两种形态。"""
    details = _as_list(result.get("TrafficDetails"))
    if details:
        return details
    data = result.get("Data")
    if isinstance(data, dict):
        return _as_list(data.get("TrafficDetails"))
    return []


def _has_traffic_details(result: Dict[str, Any]) -> bool:
    return bool(_traffic_details(result))


def _traffic_from_response(result: Dict[str, Any], traffic_class: str) -> float:
    """把明细里属于该分类的流量求和，Byte → GiB。"""
    details = _traffic_details(result)
    if not details:
        return 0.0
    total_bytes = 0.0
    for item in details:
        if not isinstance(item, dict):
            continue
        # 字段名做兼容（不同版本 / 大小写）
        region = item.get("BusinessRegionId")
        if region is None:
            region = item.get("business_region_id") or item.get("RegionId") or ""
        traffic = item.get("Traffic")
        if traffic is None:
            traffic = item.get("traffic")
        if _traffic_class(str(region)) != traffic_class:
            continue
        total_bytes += _number(traffic)
    return total_bytes / _BYTES_PER_GIB


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------

class CdtTrafficMonitor(_PluginBase):
    """阿里云 CDT 公网流量监控插件。

    定时拉取每个账号的 CDT 累计流量与 ECS 实例状态，达到阈值时通知
    （可选顺带停机）；同时提供每日定时开关机、保活与定时日报。
    """

    # 插件名称
    plugin_name = "阿里云CDT流量监控"
    # 插件描述（≤48 字，卡片 3 行内可完整显示）
    plugin_desc = "监控阿里云账号CDT流量与ECS实例，超阈值通知，可定时开关机、日报与账单。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/Desire5864/MoviePilot-Plugins/main/icons/cdttrafficmonitor.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "cdttrafficmonitor_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    # ---- 运行态（init_plugin 里全部重建）----
    _enabled: bool = False
    _notify: bool = False
    _notify_recover: bool = True
    _interval: int = DEFAULT_INTERVAL_MINUTES      # 实际查询阿里云的最小间隔（分钟）
    _tick: int = DEFAULT_TICK_MINUTES              # 调度心跳（分钟）
    _threshold: float = float(DEFAULT_THRESHOLD)
    _threshold_action: str = THRESHOLD_ACTION_NOTIFY
    _shutdown_mode: str = DEFAULT_SHUTDOWN_MODE
    _keep_alive: bool = False
    _schedule_enabled: bool = False
    _start_time: str = ""
    _stop_time: str = ""
    _daily_report: bool = False
    _daily_report_time: str = "22:00"
    _enable_billing: bool = False
    _proxy: bool = False
    _accounts_raw: str = ""

    _accounts: List[Dict[str, Any]] = []
    _config_errors: List[str] = []
    _client: Optional[_AliyunClient] = None

    # 状态与样本（持久化在 STATE_KEY 下，dict：account_key → {...}）
    _state: Dict[str, Dict[str, Any]] = {}
    _last_check_time: str = ""
    _last_error: str = ""

    # 并发保护：锁必须是**类属性**，在 init_plugin 里新建会让正在跑的那一轮握旧锁
    _check_lock = threading.Lock()
    # 后台刷新标记（避免线程堆积）
    _refreshing = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: dict = None) -> None:
        """按配置重建运行态。"""
        self.stop_service()

        self._enabled = False
        self._notify = False
        self._notify_recover = True
        self._interval = DEFAULT_INTERVAL_MINUTES
        self._tick = DEFAULT_TICK_MINUTES
        self._threshold = float(DEFAULT_THRESHOLD)
        self._threshold_action = THRESHOLD_ACTION_NOTIFY
        self._shutdown_mode = DEFAULT_SHUTDOWN_MODE
        self._keep_alive = False
        self._schedule_enabled = False
        self._start_time = ""
        self._stop_time = ""
        self._daily_report = False
        self._daily_report_time = "22:00"
        self._enable_billing = False
        self._proxy = False
        self._accounts_raw = ""
        self._accounts = []
        self._config_errors = []
        self._last_check_time = ""
        self._last_error = ""

        # 恢复持久化状态（重启后详情页不空白、告警不重复推）
        self._state = self.get_data(STATE_KEY) or {}
        if not isinstance(self._state, dict):
            self._state = {}

        if not config:
            self._client = None
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._notify_recover = bool(config.get("notify_recover", True))
        self._proxy = bool(config.get("proxy"))
        self._keep_alive = bool(config.get("keep_alive"))
        self._schedule_enabled = bool(config.get("schedule_enabled"))
        self._daily_report = bool(config.get("daily_report"))
        self._enable_billing = bool(config.get("enable_billing"))

        self._interval = self.__clamp_int(
            config.get("interval"), DEFAULT_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, MAX_TICK_MINUTES
        )
        self._tick = self.__clamp_int(
            config.get("tick"), DEFAULT_TICK_MINUTES, MIN_TICK_MINUTES, MAX_TICK_MINUTES
        )
        # 心跳不该比查询间隔还慢，否则定时开关机容易错过窗口
        if self._tick > self._interval:
            self._tick = self._interval

        raw_threshold = config.get("threshold")
        if raw_threshold in (None, ""):
            self._threshold = float(DEFAULT_THRESHOLD)
        else:
            self._threshold = min(100.0, max(1.0, _to_float(raw_threshold, DEFAULT_THRESHOLD)))

        action = str(config.get("threshold_action") or "").strip()
        self._threshold_action = (
            THRESHOLD_ACTION_STOP if action == THRESHOLD_ACTION_STOP else THRESHOLD_ACTION_NOTIFY
        )
        self._shutdown_mode = (
            "StopCharging"
            if str(config.get("shutdown_mode") or "").strip() == "StopCharging"
            else "KeepCharging"
        )

        self._start_time = _normalize_clock(str(config.get("start_time") or ""))
        self._stop_time = _normalize_clock(str(config.get("stop_time") or ""))
        self._daily_report_time = _normalize_clock(
            str(config.get("daily_report_time") or ""), "22:00"
        )

        # 主实例的 Secret：表单留空时沿用上次保存的（只在换了 AccessKey ID 时才失效）
        form_ak = str(config.get("account_ak") or "").strip()
        form_sk = str(config.get("account_sk") or "").strip()
        if form_ak and form_sk:
            self.__remember_secret(form_ak, form_sk)

        self._accounts_raw = str(config.get("accounts") or "")
        self._accounts, self._config_errors = self.__resolve_accounts(config)
        # 兜底：任何形态的脏数据都不许让半个账号流进后续流程
        self._accounts = [item for item in self._accounts if item and item.get("ak")]

        # 代理：取自面板设置（PROXY_HOST），与 GitHub 那套共用同一份
        proxy_url = None
        if self._proxy:
            proxy_url = getattr(settings, "PROXY_HOST", None) or None
        self._client = _AliyunClient(proxy=proxy_url) if self._accounts else None

        if self._config_errors:
            logger.warning(f"{self.plugin_name}：账号配置有 {len(self._config_errors)} 处问题"
                           f"（{'；'.join(self._config_errors[:3])}）")

        # 立即执行一次（同步跑完再落库，保证刷新页面即见本轮结果）
        if config.get("run_once"):
            logger.info(f"{self.plugin_name}：立即执行一次检查")
            new_config = dict(config)
            new_config["run_once"] = False
            try:
                self.check_traffic(force=True)
            except Exception as err:  # noqa: BLE001
                logger.error(f"{self.plugin_name}：立即执行失败 {err}")
            finally:
                # 开关落库必须包在 finally：一旦上面抛异常，run_once 会永远挂在 True，
                # 此后每次「保存配置」都会被动触发一整轮检查
                self.update_config(new_config)

    @staticmethod
    def __clamp_int(value: Any, default: int, low: int, high: int) -> int:
        parsed = _to_int(value, default)
        if parsed <= 0:
            parsed = default
        return max(low, min(high, parsed))

    # ------------------------------------------------------------------
    # 账号配置：结构化「实例表单」+ 可选的多账号文本框
    # ------------------------------------------------------------------

    def __stored_secret(self, access_key_id: str) -> str:
        """取已保存的主实例 Secret；只跟同一个 AccessKey ID 配对，换 AK 即失效。"""
        access_key_id = str(access_key_id or "").strip()
        if not access_key_id:
            return ""
        stored = self.get_data(SECRET_KEY)
        if not isinstance(stored, dict):
            return ""
        if str(stored.get("ak") or "").strip() != access_key_id:
            return ""
        return str(stored.get("sk") or "").strip()

    def __remember_secret(self, access_key_id: str, secret: str) -> None:
        """把表单里新填的 Secret 存起来（表单本身只留空占位，不回显明文）。"""
        access_key_id = str(access_key_id or "").strip()
        secret = str(secret or "").strip()
        if not access_key_id or not secret:
            return
        current = self.get_data(SECRET_KEY)
        if (isinstance(current, dict)
                and str(current.get("ak") or "").strip() == access_key_id
                and str(current.get("sk") or "").strip() == secret):
            return
        try:
            self.save_data(SECRET_KEY, {"ak": access_key_id, "sk": secret})
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{self.plugin_name}：保存 AccessKey Secret 失败 {err}")

    def __resolve_accounts(self, config: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
        """把「实例表单」与「附加账号文本」合并成账号列表。

        结构化字段（``account_*``）描述主实例；``accounts`` 文本框留给附加账号，
        两种都空就是没配账号。只填了 ``accounts`` 的老配置照常可用。
        """
        errors: List[str] = []
        accounts: List[Dict[str, Any]] = []

        access_key_id = str(config.get("account_ak") or "").strip()
        if access_key_id:
            # 表单留空 = 沿用已保存的 Secret（「留空保持不变」）
            secret = str(config.get("account_sk") or "").strip() or self.__stored_secret(access_key_id)
            if secret:
                primary = _normalize_account({
                    "name": config.get("account_name"),
                    "ak": access_key_id,
                    "sk": secret,
                    "region": config.get("account_region"),
                    "instance": config.get("account_instance"),
                    "quota": config.get("account_quota"),
                    "site_type": config.get("account_site_type"),
                    "schedule": config.get("account_schedule"),
                    "start_time": config.get("account_start_time"),
                    "stop_time": config.get("account_stop_time"),
                }, 1, errors)
                if primary:
                    accounts.append(primary)
            else:
                errors.append("主实例缺少 AccessKey Secret：请在插件设置里填一次"
                              "（之后留空即表示保持不变）")
        elif any(str(config.get(key) or "").strip()
                 for key in ("account_sk", "account_instance", "account_name", "account_region")):
            errors.append("主实例缺少 AccessKey ID，已跳过")

        extra, extra_errors = _parse_accounts(str(config.get("accounts") or ""))
        errors.extend(extra_errors)
        accounts.extend(extra)

        # 去重：同一个「AK + 实例」只保留一条（结构化字段与文本框重复填时不会双算）
        seen = set()
        unique: List[Dict[str, Any]] = []
        for account in accounts:
            if not account or not account.get("ak"):
                continue
            key = _account_key(account)
            if key in seen:
                errors.append(f"账号 {account.get('name')} 与前面的配置重复，已忽略")
                continue
            seen.add(key)
            unique.append(account)
        return unique, errors

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/refresh",
                "endpoint": self.api_refresh,
                "methods": ["GET"],
                "summary": "刷新流量与实例状态",
                "description": "按 idx 刷新指定账号，idx 缺省时刷新全部账号。",
            },
            {
                "path": "/start",
                "endpoint": self.api_start,
                "methods": ["GET"],
                "summary": "启动实例",
                "description": "对指定账号的 ECS 实例发送开机指令。",
            },
            {
                "path": "/stop",
                "endpoint": self.api_stop,
                "methods": ["GET"],
                "summary": "停止实例",
                "description": "对指定账号的 ECS 实例发送停机指令（按配置的停机模式）。",
            },
        ]

    def stop_service(self) -> None:
        """清理该插件注册的定时服务。

        调度器按 id 覆盖注册，这里不需要额外动作；保留方法体是为了
        未来若引入自建线程时有一个明确的收口点。
        """
        return None

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """配置表单（Vuetify 组件树）。

        账号部分用**结构化字段**描述主实例（对齐原版 CDT-Monitor 的「实例」表单），
        另留一个文本框给附加账号；AccessKey Secret 不接受回显 —— 表单里永远是空的，
        留空即沿用已保存的那一份。
        """

        def col(md: int, component: str, props: Dict[str, Any]) -> dict:
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [{"component": component, "props": props}],
            }

        def text_col(model: str, label: str, hint: str = "", md: int = 4,
                     placeholder: str = "", field_type: str = "text", suffix: str = "") -> dict:
            props: Dict[str, Any] = {
                "model": model, "label": label, "type": field_type, "density": "comfortable",
            }
            if placeholder:
                props["placeholder"] = placeholder
            if suffix:
                props["suffix"] = suffix
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return col(md, "VTextField", props)

        def switch_col(model: str, label: str, hint: str = "", md: int = 4) -> dict:
            props: Dict[str, Any] = {"model": model, "label": label, "density": "comfortable"}
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return col(md, "VSwitch", props)

        def select_col(model: str, label: str, items: List[dict], hint: str = "",
                       md: int = 4) -> dict:
            props: Dict[str, Any] = {
                "model": model, "label": label, "items": items, "density": "comfortable",
            }
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return col(md, "VSelect", props)

        def section(title: str) -> dict:
            return {
                "component": "VRow",
                "content": [col(12, "div", {
                    "style": ("font-size:13px;font-weight:700;letter-spacing:0;"
                              "padding:10px 0 2px;"),
                    "text": title,
                })],
            }

        # 表单永远不回显 Secret：这里只判断「是否已经存过一份」，用来提示「已配置」
        saved: Dict[str, Any] = {}
        try:
            loaded = self.get_config()
            if isinstance(loaded, dict):
                saved = loaded
        except Exception:  # noqa: BLE001
            saved = {}
        saved_ak = str(saved.get("account_ak") or "").strip()
        secret_saved = bool(saved_ak and self.__stored_secret(saved_ak))

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            switch_col("enabled", "启用插件"),
                            switch_col("notify", "发送通知"),
                            switch_col("notify_recover", "阈值恢复时也通知",
                                       "流量回落到阈值以下时补一条恢复提示"),
                        ],
                    },
                    section("监控实例"),
                    {
                        "component": "VRow",
                        "content": [
                            text_col("account_ak", "AccessKey ID",
                                     "建议使用只授予只读权限的 RAM 子账号",
                                     md=6, placeholder="LTAI5t..."),
                            col(6, "VTextField", {
                                "model": "account_sk",
                                "label": "AccessKey Secret" + (" · 已配置" if secret_saved else ""),
                                "type": "password",
                                "density": "comfortable",
                                "placeholder": "留空保持不变",
                                "hint": "留空即沿用已保存的 Secret；换了 AccessKey ID 才需要重新填",
                                "persistent-hint": True,
                            }),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            text_col("account_instance", "实例 ID",
                                     "留空表示只监控流量、不控制实例",
                                     md=6, placeholder="i-j6c2qpj3j9q562vvkckc"),
                            select_col("account_region", "地域", _region_items(),
                                       "决定流量归属：cn- 开头且非 cn-hongkong 的算中国大陆流量",
                                       md=6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            text_col("account_quota", "流量额度",
                                     f"单位 GB；留空按阿里云免费额度 {CDT_FREE_QUOTA_GB:.0f} GB 计算",
                                     md=6, field_type="number", suffix="GB"),
                            select_col("account_site_type", "站点类型", _SITE_TYPE_ITEMS,
                                       "只影响账单接口（余额 / 当月费用）走中国站还是国际站",
                                       md=6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            text_col("account_name", "备注",
                                     "显示在详情页卡片标题，留空显示 AccessKey 掩码",
                                     md=6, placeholder="香港主节点"),
                            switch_col("account_schedule", "每日定时开关机",
                                       "账号级开关，按下面的开机 / 关机时间执行", md=6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            text_col("account_start_time", "每天开机时间",
                                     "24 小时制，如 08:00；支持跨零点时段", md=6),
                            text_col("account_stop_time", "每天关机时间",
                                     "24 小时制，如 23:30；留空表示不自动关机", md=6),
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "accounts",
                                            "label": "附加账号（可选）",
                                            "rows": 4,
                                            "auto-grow": True,
                                            "placeholder": (
                                                "名称=香港中转, AK=LTAI5t..., SK=..., "
                                                "地域=cn-hongkong, 实例=i-j6..., 额度=500"
                                            ),
                                            "hint": "每行一个附加账号；也支持直接粘贴 JSON 数组。"
                                                    "附加账号自带 AK/SK，不读上面的字段",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": self.__form_help_text(),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    section("告警与调度"),
                    {
                        "component": "VRow",
                        "content": [
                            text_col("interval", "查询间隔（分钟）",
                                     f"实际请求阿里云的最小间隔，默认 {DEFAULT_INTERVAL_MINUTES}",
                                     field_type="number"),
                            text_col("tick", "调度心跳（分钟）",
                                     f"定时开关机靠它保证不误点，默认 {DEFAULT_TICK_MINUTES}；"
                                     f"大于查询间隔时自动收敛到查询间隔",
                                     field_type="number"),
                            text_col("threshold", "告警阈值（%）",
                                     f"累计流量占额度的百分比，默认 {DEFAULT_THRESHOLD}",
                                     field_type="number"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            select_col(
                                "threshold_action", "超阈值动作",
                                [{"title": "仅通知（不动实例）", "value": THRESHOLD_ACTION_NOTIFY},
                                 {"title": "停机并通知", "value": THRESHOLD_ACTION_STOP}],
                                "默认仅通知；选「停机并通知」后插件会真的调用 ECS 停机",
                            ),
                            select_col(
                                "shutdown_mode", "停机模式",
                                [{"title": "普通停机（KeepCharging，保留公网 IP、可能继续计费）",
                                  "value": "KeepCharging"},
                                 {"title": "节省停机（StopCharging，释放计算资源、停止计费）",
                                  "value": "StopCharging"}],
                                "仅在需要停机时生效（超阈值动作 / 定时关机）",
                            ),
                            switch_col("keep_alive", "实例保活",
                                       "允许运行时段内发现实例意外停止即拉起；超阈值时熔断不拉起"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            switch_col("daily_report", "启用日报",
                                       "每天在下面时间推送「时段内实际消耗」日报"),
                            text_col("daily_report_time", "日报时间",
                                     "未配置定时开关机时使用，默认 22:00"),
                            switch_col("enable_billing", "查询账单",
                                       "账户可用余额 + 实例当月费用，需要 bss 只读权限"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            switch_col("schedule_enabled", "定时开关机（附加账号默认）",
                                       "只对没单独设置定时的附加账号生效；主实例用上面的开关"),
                            text_col("start_time", "默认开机时间",
                                     "24 小时制；主实例请用上面的「每天开机时间」"),
                            text_col("stop_time", "默认关机时间",
                                     "24 小时制；主实例请用上面的「每天关机时间」"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            switch_col("proxy", "使用代理",
                                       "仅当阿里云接口必须经代理访问时打开；默认直连"),
                            switch_col("run_once", "立即执行一次",
                                       "保存后同步跑一轮检查（期间页面会等待），完成后自动关闭"),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "notify_recover": True,
            # ---- 主实例（结构化字段）----
            "account_name": "",
            "account_ak": "",
            "account_sk": "",
            "account_region": "cn-hongkong",
            "account_instance": "",
            "account_quota": "",
            "account_site_type": SITE_TYPE_CN,
            "account_schedule": False,
            "account_start_time": "",
            "account_stop_time": "",
            "accounts": "",
            # ---- 告警与调度 ----
            "interval": DEFAULT_INTERVAL_MINUTES,
            "tick": DEFAULT_TICK_MINUTES,
            "threshold": DEFAULT_THRESHOLD,
            "threshold_action": THRESHOLD_ACTION_NOTIFY,
            "shutdown_mode": DEFAULT_SHUTDOWN_MODE,
            "keep_alive": False,
            "schedule_enabled": False,
            "start_time": "",
            "stop_time": "",
            "daily_report": False,
            "daily_report_time": "22:00",
            "enable_billing": False,
            "proxy": False,
            "run_once": False,
        }

    def __form_help_text(self) -> str:
        """表单底部的填写说明（跟着常量走，避免改了规则忘了改文案）。"""
        return (
            f"上方字段描述主实例；「附加账号」每行一条、字段用逗号分隔、键名支持中英文："
            f"名称 / AK / SK / 地域 / 实例 / 额度 / 站点 / 定时 / 开机 / 关机 / "
            f"保活 / 停机模式 / 日报 / 日报时间。"
            f"「额度」单位 GB，按账号所在「地域」归属的那一类流量比对 —— "
            f"地域是 cn- 开头且非 cn-hongkong 的算中国大陆流量，其余算非中国大陆流量。"
            f"阿里云 CDT 免费额度为每月 {CDT_FREE_QUOTA_GB:.0f} GB"
            f"（中国大陆 {CDT_FREE_QUOTA_CN_GB:.0f} GB + 非中国大陆 "
            f"{CDT_FREE_QUOTA_INTL_GB:.0f} GB，账号级、每月 1 日 00:00 重置），"
            f"额度留空即按 {CDT_FREE_QUOTA_GB:.0f} GB 计算。"
            f"AccessKey Secret 以明文存在面板数据库中（表单不回显、留空即保持不变），"
            f"请务必使用只授予下列权限的 RAM 子账号，且不要给这个面板开放公网访问。"
            f"需要的最小权限：cdt 只读（ListCdtInternetTraffic）、"
            f"ecs 只读（DescribeInstanceStatus）、"
            f"ecs 启停（StartInstance / StopInstance，仅在你打开停机或保活时才需要）、"
            f"bss 只读（QueryAccountBalance / DescribeInstanceBill，仅查询账单时需要）。"
        )
    def __form_help_text(self) -> str:
        """表单底部的填写说明（跟着常量走，避免改了规则忘了改文案）。"""
        return (
            f"账号每行一条，字段用逗号分隔、顺序随意（键名支持中英文）："
            f"名称 / AK / SK / 地域 / 实例 / 额度 / 站点 / 定时 / 开机 / 关机 / "
            f"保活 / 停机模式 / 日报 / 日报时间。"
            f"「额度」单位 GB，按账号所在「地域」归属的那一类流量比对 —— "
            f"地域是 cn- 开头且非 cn-hongkong 的算中国大陆流量，其余算非中国大陆流量。"
            f"阿里云 CDT 免费额度为每月 {CDT_FREE_QUOTA_GB:.0f} GB"
            f"（中国大陆 {CDT_FREE_QUOTA_CN_GB:.0f} GB + 非中国大陆 "
            f"{CDT_FREE_QUOTA_INTL_GB:.0f} GB，账号级、每月 1 日 00:00 重置），"
            f"额度留空即按 {CDT_FREE_QUOTA_GB:.0f} GB 计算。"
            f"需要的最小权限：cdt 只读（ListCdtInternetTraffic）、"
            f"ecs 只读（DescribeInstanceStatus）、"
            f"ecs 启停（StartInstance / StopInstance，仅在你打开停机或保活时才需要）、"
            f"bss 只读（QueryAccountBalance / DescribeInstanceBill，仅查询账单时需要）。"
            f"提醒：AccessKey Secret 以明文存在面板数据库中，请务必使用只授予上述权限的 "
            f"RAM 子账号，且不要给这个面板开放公网访问。"
        )

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def get_page(self) -> Optional[List[dict]]:
        """详情页：配置概览 + 汇总行 + 逐账号卡片。

        打开页面**不做任何网络请求**（阿里云接口慢且要计费调用次数）：
        有缓存就直接渲染，缓存超过 ``PAGE_CACHE_TTL`` 时丢到后台线程静默刷新。
        要立刻看最新值，点卡片上的「刷新」按钮（会同步拉一轮再重载页面）。
        """
        if not self._enabled:
            return None

        if self._accounts and self.__cache_age_seconds() > PAGE_CACHE_TTL:
            self.__refresh_in_background()

        page: List[dict] = []

        # ---- 顶部：配置概览 ----
        threshold_text = f"{self._threshold:g}%"
        action_text = "停机并通知" if self._threshold_action == THRESHOLD_ACTION_STOP else "仅通知"
        # 定时开关机是账号级的，这里报「有几个账号开了」而不是一个全局开关
        scheduled = [item for item in
                     (self.__schedule_of(account) for account in self._accounts) if item[0]]
        if scheduled:
            schedule_text = (f"{len(scheduled)} 个账号"
                             f"（{scheduled[0][1] or '—'} 开 / {scheduled[0][2] or '—'} 关）")
        else:
            schedule_text = "未启用"
        if self._enable_billing:
            billing_text = "账单已开启"
        else:
            billing_text = "账单未开启"
        page.append(self.__alert(
            f"账号 {len(self._accounts)} 个；查询间隔 {self._interval} 分钟；"
            f"阈值 {threshold_text}（{action_text}）；定时开关机 {schedule_text}；"
            f"{billing_text}；最近检查 {self._last_check_time or '尚未检查'}",
            alert_type="info",
        ))

        if self._config_errors:
            page.append(self.__alert(
                "账号配置有 %d 处问题：%s" % (
                    len(self._config_errors), "；".join(self._config_errors[:5])
                ),
                alert_type="warning",
            ))
        if self._last_error:
            page.append(self.__alert(f"最近一次检查出错：{self._last_error}", alert_type="error"))
        if not self._accounts:
            page.append(self.__alert(
                "还没有可用的账号配置。请在插件设置的「监控实例」里填好 AccessKey ID、"
                "AccessKey Secret 与地域（要控制实例再补实例 ID）后保存；"
                "多账号可以写在下方的「附加账号」里。",
                alert_type="info",
            ))
            return page

        # ---- 汇总：总量 / 总额度 / 最高占比（+ 账单金额）----
        total_used = 0.0
        total_quota = 0.0
        peak_percent = 0.0
        alarmed = 0
        total_cost = 0.0
        total_balance = 0.0
        currencies: set = set()
        money_seen = False
        for account in self._accounts:
            state = self.__account_state(account)
            total_used += _to_float(state.get("traffic"))
            total_quota += self.__quota_of(account)
            peak_percent = max(peak_percent, _to_float(state.get("percent")))
            if state.get("alerting"):
                alarmed += 1
            billing = state.get("billing") if isinstance(state.get("billing"), dict) else {}
            if billing:
                currencies.add(str(billing.get("currency") or "CNY"))
                if billing.get("monthly_cost") is not None:
                    total_cost += _to_float(billing.get("monthly_cost"))
                    money_seen = True
                if billing.get("balance") is not None:
                    total_balance += _to_float(billing.get("balance"))
                    money_seen = True
        # 多币种不做合计（各账号站点不同时会出现），宁可不显示也不给一个错的数字
        money: List[Tuple[str, str, str]] = []
        if self._enable_billing and money_seen and len(currencies) <= 1:
            code = currencies.pop() if currencies else "CNY"
            money = [
                ("本月费用", _format_money(total_cost, code), COLOR_VALUE),
                ("账户余额", _format_money(total_balance, code), COLOR_VALUE),
            ]
        page.append(self.__summary_row(
            len(self._accounts), total_used, total_quota, peak_percent, alarmed, money
        ))

        # ---- 逐账号卡片 ----
        cards = [
            self.__account_card(index, account)
            for index, account in enumerate(self._accounts)
        ]
        page.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "div",
                    "props": {
                        "style": f"display:flex;flex-wrap:wrap;gap:10px;"
                                 f"padding-right:{_FAB_CHANNEL}px;"
                    },
                    "content": cards,
                }],
            }],
        })

        # 底部操作行（刷新全部）
        page.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "div",
                    "props": {"style": f"display:flex;gap:8px;padding-right:{_FAB_CHANNEL}px;"},
                    "content": [
                        self.__action_button("刷新全部", "/refresh", {}, primary=True),
                    ],
                }],
            }],
        })
        return page

    @staticmethod
    def __alert(text: str, alert_type: str = "info") -> dict:
        return {
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VAlert",
                    "props": {"type": alert_type, "variant": "tonal", "text": text},
                }],
            }],
        }

    @staticmethod
    def __summary_row(count: int, used: float, quota: float, peak: float, alarmed: int,
                      money: Optional[List[Tuple[str, str, str]]] = None) -> dict:
        """汇总条：账号数 / 总用量 / 总额度 / 最高占比 / 告警数（+ 金额）。"""
        percent = _usage_percent(used, quota)
        tone = COLOR_ERR if alarmed else (BAR_COLOR_WARN if percent >= 80 else COLOR_OK)

        def cell(label: str, value: str, value_color: str = COLOR_VALUE) -> dict:
            return {
                "component": "div",
                "props": {"style": "display:flex;flex-direction:column;min-width:0;"
                                   "flex:1 1 110px;"},
                "content": [
                    {"component": "span",
                     "props": {"style": f"font-size:11px;color:{COLOR_LABEL};"},
                     "text": label},
                    {"component": "span",
                     "props": {"style": f"font-size:16px;font-weight:700;color:{value_color};"
                                       "font-variant-numeric:tabular-nums;white-space:nowrap;"},
                     "text": value},
                ],
            }

        cells = [
            cell("账号", f"{count} 个"),
            cell("合计用量", _format_gb(used)),
            cell("合计额度", _format_gb(quota)),
            cell("最高占比", f"{peak:.2f}%", tone),
            cell("告警中", f"{alarmed} 个", COLOR_ERR if alarmed else COLOR_LABEL),
        ]
        for label, value, color in (money or []):
            cells.append(cell(label, value, color))

        return {
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "div",
                    "props": {
                        "style": "display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;"
                                 "padding:12px 84px 12px 16px;box-sizing:border-box;"
                                 "border-radius:10px;"
                                 "background:rgba(var(--v-theme-on-surface),0.04);"
                                 "border:1px solid rgba(var(--v-theme-on-surface),0.08);"
                    },
                    "content": cells,
                }],
            }],
        }

    def __billing_block(self, state: Dict[str, Any]) -> List[dict]:
        """卡片右上角的金额区：本月费用 / 账户余额 / 同步状态。

        账单没开启、或这一轮还没取到时返回空列表 —— 宁可什么都不显示，
        也不要凭空多出一块 ``¥0.00`` 让人以为免费。
        """
        if not self._enable_billing:
            return []
        billing = state.get("billing") if isinstance(state.get("billing"), dict) else {}
        if not billing:
            return [{
                "component": "div",
                "props": {"style": f"font-size:10.5px;color:{COLOR_LABEL};margin-top:4px;"},
                "text": "账单待同步",
            }]

        currency = str(billing.get("currency") or "CNY")
        has_cost = billing.get("monthly_cost") is not None
        has_balance = billing.get("balance") is not None
        error = str(billing.get("error") or "")
        if not (has_cost or has_balance):
            return [{
                "component": "div",
                "props": {"style": f"font-size:10.5px;color:{COLOR_ERR};margin-top:4px;",
                          "title": error or "账单未取到"},
                "text": "账单同步失败",
            }]

        def money_cell(label: str, value: Any) -> dict:
            return {
                "component": "div",
                "props": {"style": "display:flex;flex-direction:column;align-items:flex-end;"},
                "content": [
                    {"component": "span",
                     "props": {"style": f"font-size:10.5px;color:{COLOR_LABEL};"},
                     "text": label},
                    {"component": "span",
                     "props": {"style": f"font-size:13px;font-weight:700;color:{COLOR_VALUE};"
                                       "font-variant-numeric:tabular-nums;white-space:nowrap;"},
                     "text": _format_money(value, currency)},
                ],
            }

        stamp = str(billing.get("at") or "")
        if len(stamp) >= 16:
            sync_text = f"已同步 {stamp[11:16]}"
        else:
            sync_text = "已同步" if stamp else "尚未同步"
        return [
            {
                "component": "div",
                "props": {"style": "display:flex;gap:14px;justify-content:flex-end;"
                                   "margin-top:4px;"},
                "content": [
                    money_cell("本月费用", billing.get("monthly_cost")),
                    money_cell("账户余额", billing.get("balance")),
                ],
            },
            {
                "component": "div",
                "props": {"style": f"font-size:10.5px;margin-top:1px;"
                                   f"color:{COLOR_ERR if error else COLOR_LABEL};",
                          "title": error},
                "text": sync_text,
            },
        ]

    def __account_card(self, index: int, account: Dict[str, Any]) -> dict:
        """单个账号的卡片：顶部信息 + 金额 / 流量 / 进度 + 动作按钮。"""
        state = self.__account_state(account)
        traffic = _to_float(state.get("traffic"))
        quota = self.__quota_of(account)
        percent = _usage_percent(traffic, quota)
        status = str(state.get("status") or STATUS_UNKNOWN)
        alerting = bool(state.get("alerting"))
        traffic_class = _traffic_class(account["region"])
        bar_color = BAR_COLOR_OVER if alerting else _bar_color(percent, self._threshold)
        bar_width = max(0.0, min(100.0, percent))
        name = account.get("name") or _mask_ak(account["ak"])
        tone = _status_tone(status)
        remaining = quota - traffic
        china_gb = _to_float(state.get("traffic_china"))
        intl_gb = _to_float(state.get("traffic_intl"))

        sub_bits: List[str] = []
        # 标题已经用了 AK 掩码时就不再重复；写了备注才把 AK 掩码放到副标题里
        if name != _mask_ak(account["ak"]):
            sub_bits.append(_mask_ak(account["ak"]))
        sub_bits.append(_region_name(account["region"]))
        if account.get("instance"):
            sub_bits.append(f"实例 {account['instance']}")
        sub_bits.append(_class_label(traffic_class))

        rows: List[dict] = []

        # ① 顶部：左（图标 + 名称 / 地域），右（状态徽章 + 金额）
        rows.append({
            "component": "div",
            "props": {"style": "display:flex;align-items:flex-start;gap:10px;"},
            "content": [
                {
                    "component": "div",
                    "props": {"style": "display:flex;align-items:flex-start;gap:8px;"
                                       "flex:1 1 auto;min-width:0;"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"style": "flex:0 0 auto;width:32px;height:32px;"
                                               "border-radius:9px;display:flex;"
                                               "align-items:center;justify-content:center;"
                                               "background:rgba(var(--v-theme-on-surface),0.06);"},
                            "content": [
                                {"component": "VIcon",
                                 "props": {"size": "small", "style": f"color:{tone};"},
                                 "text": ICON_SERVER},
                            ],
                        },
                        {
                            "component": "div",
                            "props": {"style": "min-width:0;padding-top:1px;"},
                            "content": [
                                {"component": "div",
                                 "props": {"style": f"font-size:13.5px;font-weight:700;"
                                                   f"color:{COLOR_VALUE};overflow:hidden;"
                                                   "text-overflow:ellipsis;white-space:nowrap;",
                                           "title": name},
                                 "text": name},
                                {"component": "div",
                                 "props": {"style": f"font-size:11.5px;color:{COLOR_NAME};"
                                                   "overflow:hidden;text-overflow:ellipsis;"
                                                   "white-space:nowrap;",
                                           "title": " · ".join(sub_bits)},
                                 "text": " · ".join(sub_bits)},
                            ],
                        },
                    ],
                },
                {
                    "component": "div",
                    "props": {"style": "flex:0 0 auto;text-align:right;"},
                    "content": [
                        {"component": "span",
                         "props": {"style": f"display:inline-block;font-size:11px;font-weight:700;"
                                           "padding:2px 9px;border-radius:9px;white-space:nowrap;"
                                           f"color:{tone};"
                                           "background:rgba(var(--v-theme-on-surface),0.06);"},
                         "text": _status_text(status)},
                    ] + self.__billing_block(state),
                },
            ],
        })

        # ② 本月流量：大号数字 + 刷新按钮
        rows.append({
            "component": "div",
            "props": {"style": "display:flex;align-items:flex-end;justify-content:space-between;"
                               "gap:8px;margin-top:10px;"},
            "content": [
                {
                    "component": "div",
                    "props": {"style": "min-width:0;"},
                    "content": [
                        {"component": "div",
                         "props": {"style": f"font-size:11.5px;color:{COLOR_LABEL};"},
                         "text": "本月 CDT 流量"},
                        {"component": "div",
                         "props": {"style": "display:flex;align-items:baseline;gap:4px;"
                                           "margin-top:2px;flex-wrap:wrap;"},
                         "content": [
                             {"component": "span",
                              "props": {"style": f"font-size:22px;font-weight:800;line-height:1.1;"
                                                f"color:{COLOR_VALUE};"
                                                "font-variant-numeric:tabular-nums;"},
                              "text": _format_gb(traffic)},
                             {"component": "span",
                              "props": {"style": f"font-size:12.5px;color:{COLOR_LABEL};"},
                              "text": f"/ {_format_gb(quota)}"},
                         ]},
                    ],
                },
                self.__action_button("刷新", "/refresh", {"idx": str(index)},
                                     icon=ICON_REFRESH),
            ],
        })

        # ③ 进度条（容器 + 单个色块：定宽定高的色块比 VProgressLinear 更好控）
        rows.append({
            "component": "div",
            "props": {"style": "margin-top:8px;height:6px;border-radius:3px;overflow:hidden;"
                               "background:rgba(var(--v-theme-on-surface),0.10);"},
            "content": [{
                "component": "div",
                "props": {"style": f"height:100%;width:{bar_width:.3f}%;border-radius:3px;"
                                   f"background:{bar_color};"},
            }],
        })

        # ④ 左「已使用」/ 右「阈值」
        rows.append({
            "component": "div",
            "props": {"style": "display:flex;justify-content:space-between;gap:8px;"
                               "margin-top:6px;font-size:11.5px;"},
            "content": [
                {"component": "span",
                 "props": {"style": f"color:{bar_color};font-weight:700;"
                                   "font-variant-numeric:tabular-nums;"},
                 "text": f"{percent:.2f}% 已使用"},
                {"component": "span",
                 "props": {"style": f"color:{COLOR_LABEL};"},
                 "text": f"阈值 {self._threshold:g}%"},
            ],
        })

        # ⑤ 两条分类的实测值 + 剩余
        rows.append({
            "component": "div",
            "props": {"style": f"display:block;font-size:11.5px;color:{COLOR_LABEL};"
                               "margin-top:6px;"},
            "content": [
                {"component": "span",
                 "text": f"中国大陆 {_format_gb(china_gb)} · "
                         f"非中国大陆 {_format_gb(intl_gb)}"},
                {"component": "span",
                 "props": {"style": f"color:{COLOR_OK if remaining >= 0 else COLOR_ERR};"
                                   "font-weight:700;font-variant-numeric:tabular-nums;"},
                 "text": f" · 剩余 {_format_gb(max(0.0, remaining))}"},
            ],
        })

        # ⑥ 最近检查时间 + 增量
        rows.append({
            "component": "div",
            "props": {"style": "display:flex;align-items:center;gap:5px;margin-top:6px;"},
            "content": [
                {"component": "VIcon",
                 "props": {"size": "x-small", "style": f"color:{COLOR_LABEL};"},
                 "text": ICON_CLOCK},
                {"component": "span",
                 "props": {"style": f"font-size:11.5px;color:{COLOR_LABEL};"},
                 "text": f"最近检查 {self._last_check_time or '尚未检查'}"
                         f" · {self.__trend_text(state)}"},
            ],
        })

        error = str(state.get("error") or "")
        if error:
            rows.append({
                "component": "div",
                "props": {"style": f"display:block;font-size:11px;color:{COLOR_ERR};"
                                   "margin-top:4px;overflow:hidden;text-overflow:ellipsis;"
                                   "white-space:nowrap;",
                           "title": error},
                "content": [{"component": "span", "text": error}],
            })

        # ⑦ 动作按钮（只有 VBtn 带得动面板的事件回调，div / span 无效）
        has_instance = bool(account.get("instance"))
        rows.append({
            "component": "div",
            "props": {"style": "display:flex;justify-content:flex-end;gap:4px;margin-top:6px;"},
            "content": [
                self.__action_button("开机", "/start", {"idx": str(index)},
                                     icon=ICON_PLAY, disabled=not has_instance),
                self.__action_button("停机", "/stop", {"idx": str(index)},
                                     icon=ICON_STOP, disabled=not has_instance),
            ],
        })

        return {
            "component": "div",
            "props": {
                "style": "flex:1 1 calc(50% - 5px);min-width:280px;box-sizing:border-box;"
                         "padding:12px;border-radius:12px;"
                         "background:rgba(var(--v-theme-on-surface),0.03);"
                         f"border:1px solid "
                         f"{BAR_COLOR_OVER if alerting else 'rgba(var(--v-theme-on-surface),0.10)'};",
            },
            "content": rows,
        }

    def __action_button(self, label: str, path: str, params: Dict[str, str],
                        primary: bool = False, disabled: bool = False,
                        icon: str = "") -> dict:
        """详情页动作按钮。

        🔴 详情页没有 v-model，``events`` 是唯一的「交互 → 后端」出口；
        ``params`` 是**静态字典**，所以每个按钮必须自己写死自己的取值；
        ``apikey`` 不带会 401（插件 API 默认走 verify_apikey）。
        """
        query = dict(params or {})
        query["apikey"] = getattr(settings, "API_TOKEN", "") or ""
        props: Dict[str, Any] = {
            "variant": "text",
            "size": "small",
            "ripple": False,
            "disabled": bool(disabled),
            "style": "letter-spacing:0;text-transform:none;box-shadow:none;"
                     "min-width:0;padding:0 10px;height:30px;",
        }
        if primary:
            props["color"] = "primary"
        if icon:
            props["prepend-icon"] = icon
        return {
            "component": "VBtn",
            "props": props,
            "text": label,
            "events": {
                "click": {
                    "api": f"plugin/{_PLUGIN_ID}/{path.lstrip('/')}",
                    "method": "get",
                    "params": query,
                }
            },
        }

    def __trend_text(self, state: Dict[str, Any]) -> str:
        """「较上次 / 24h」增量文案；样本不足时给出可读的占位。"""
        samples = state.get("samples") or []
        if len(samples) < 2:
            return "尚无增量样本"
        current = _to_float(samples[-1].get("v"))
        previous = _to_float(samples[-2].get("v"))
        delta_last = current - previous
        # 24h 基线：24 小时前最近的那个样本；不够 24 小时就用最早的一条
        baseline_value = _to_float(samples[0].get("v"))
        try:
            cutoff = datetime.now() - timedelta(hours=24)
            for item in samples:
                stamp = datetime.strptime(str(item.get("t")), "%Y-%m-%d %H:%M:%S")
                if stamp >= cutoff:
                    baseline_value = _to_float(item.get("v"))
                    break
        except (TypeError, ValueError):
            pass
        delta_day = current - baseline_value
        return f"较上次 {_format_signed_gb(delta_last)} · 24h {_format_signed_gb(delta_day)}"

    # ------------------------------------------------------------------
    # 定时服务
    # ------------------------------------------------------------------

    def get_service(self) -> List[Dict[str, Any]]:
        """注册调度心跳。

        用 ``interval`` 触发器而不是 cron：定时开关机需要「每几分钟看一眼」，
        cron 只在固定时刻触发一次，一旦那一刻面板在重启就会漏掉整天的开关机。
        """
        if self._enabled and self._accounts:
            return [{
                "id": _PLUGIN_ID,
                "name": self.plugin_name,
                "trigger": "interval",
                "func": self.check_traffic,
                "kwargs": {"minutes": max(MIN_TICK_MINUTES, self._tick)},
            }]
        return []

    # ------------------------------------------------------------------
    # 核心检查
    # ------------------------------------------------------------------

    def check_traffic(self, force: bool = False) -> None:
        """一轮检查：定时开关机 → 拉流量/状态 → 阈值判定 → 保活 → 日报。

        :param force: 忽略查询间隔，强制向阿里云重新取值（手动刷新 / 立即执行用）
        """
        if not self._enabled:
            return
        if not self._accounts:
            return
        # 抢不到锁说明上一轮还没跑完。定时心跳直接跳过（避免任务堆积），
        # 但手动「刷新全部 / 立即执行」必须等 —— 否则用户点了按钮却静默无反应。
        if force:
            acquired = self._check_lock.acquire(timeout=MANUAL_LOCK_WAIT_SECONDS)
        else:
            acquired = self._check_lock.acquire(blocking=False)
        if not acquired:
            logger.info(f"{self.plugin_name}：上一轮检查尚未结束，本次跳过")
            return
        try:
            if self._client is None:
                return
            self.__run_round(force)
        except Exception as err:  # noqa: BLE001
            logger.error(f"{self.plugin_name}：检查异常 {err}")
            self._last_error = str(err)
        finally:
            self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.__save_state()
            self._check_lock.release()

    def __run_round(self, force: bool) -> None:
        now = datetime.now()
        errors: List[str] = []

        # 需要联网的账号先算出来，交给线程池并发跑（一台慢不该拖住全部）
        jobs: List[Tuple[int, Dict[str, Any], bool]] = []
        for index, account in enumerate(self._accounts):
            state = self.__account_state(account)
            due = self.__is_due(account, state, now, force)
            jobs.append((index, account, due))

        # ① 先做「到点动作」——它依赖缓存状态，不需要等待联网
        scheduled_indices: set = set()
        for index, account, _due in jobs:
            state = self.__account_state(account)
            if self.__run_schedules(account, state, now):
                scheduled_indices.add(index)

        # ② 并发取数
        fetch = [(index, account) for index, account, due in jobs if due]
        if fetch:
            workers = max(1, min(4, len(fetch)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self.__fetch_account, account): index
                    for index, account in fetch
                }
                for future in futures:
                    index = futures[future]
                    account = self._accounts[index]
                    try:
                        future.result()
                    except Exception as err:  # noqa: BLE001
                        # 单账号失败不影响其它账号（原版也是「降级而不是中断」）
                        state = self.__account_state(account)
                        state["error"] = str(err)
                        errors.append(f"{account['name']}: {err}")
                        logger.warning(f"{self.plugin_name}：{account['name']} 取数失败 {err}")
                    else:
                        # __fetch_account 自己吞了异常（降级），这里要把它记下的错误
                        # 提到 `_last_error` 上，否则详情页顶部的错误条永远不显示
                        problem = str(self.__account_state(account).get("error") or "")
                        if problem:
                            errors.append(f"{account['name']}: {problem}")

        # ③ 阈值判定 / 停机 / 保活 / 采样
        for index, account, due in jobs:
            state = self.__account_state(account)
            if index in scheduled_indices:
                state["status"] = state.get("pending_status") or state.get("status")
                state.pop("pending_status", None)
            try:
                self.__evaluate(account, state, now)
            except Exception as err:  # noqa: BLE001
                state["error"] = str(err)
                errors.append(f"{account['name']}: {err}")
                logger.warning(f"{self.plugin_name}：{account['name']} 判定失败 {err}")

        # ④ 实例保活：允许运行时段内发现实例意外停止就拉起。
        #    阈值熔断优先 —— 只要这一轮判过超阈值（alerting 为真），保活直接不参与。
        for index, account, _due in jobs:
            state = self.__account_state(account)
            if index in scheduled_indices:
                continue
            try:
                self.__run_keep_alive(account, state, now)
            except Exception as err:  # noqa: BLE001
                state["error"] = f"保活失败：{err}"
                errors.append(f"{account['name']}: 保活 {err}")
                logger.warning(f"{self.plugin_name}：{account['name']} 保活失败 {err}")

        # ⑤ 日报
        try:
            self.__run_daily_report(now)
        except Exception as err:  # noqa: BLE001
            errors.append(f"日报: {err}")
            logger.warning(f"{self.plugin_name}：日报失败 {err}")

        self._last_error = "；".join(errors[:3])

    def __is_due(self, account: Dict[str, Any], state: Dict[str, Any],
                 now: datetime, force: bool) -> bool:
        """判断该账号本轮是否需要真的去请求阿里云。"""
        if force:
            return True
        updated = str(state.get("updated_at") or "")
        if not updated:
            return True
        try:
            age = (now - datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except (TypeError, ValueError):
            return True
        status = str(state.get("status") or "")
        interval = TRANSIENT_INTERVAL_SECONDS if status in _TRANSIENT_STATUS else self._interval * 60
        # 整点强制刷一次（原版行为：now.Minute()==0），保证跨天/跨月后基线及时更新
        return age >= interval or now.minute == 0

    def __fetch_account(self, account: Dict[str, Any]) -> None:
        """拉取单个账号的流量与实例状态，写入状态。"""
        client = self._client
        state = self.__account_state(account)
        now = datetime.now()

        traffic_error = None
        try:
            both = client.get_traffic_both(account["ak"], account["sk"])
            traffic_class = _traffic_class(account["region"])
            state["traffic_china"] = round(both.get("china", 0.0), 4)
            state["traffic_intl"] = round(both.get("international", 0.0), 4)
            state["traffic"] = round(both.get(traffic_class, 0.0), 4)
            state["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.__append_sample(state, now)
            state.pop("error", None)
        except Exception as err:  # noqa: BLE001
            traffic_error = str(err)

        status_error = None
        try:
            status = client.get_instance_status(
                account["ak"], account["sk"], account["region"], account.get("instance") or ""
            )
            if status:
                state["status"] = status
        except Exception as err:  # noqa: BLE001
            status_error = str(err)

        # 账单（可选，缓存 6 小时，避免每次检查都打 bss 接口）
        if self._enable_billing:
            self.__refresh_billing(account, state, now)

        problems = [item for item in (traffic_error, status_error) if item]
        if problems:
            state["error"] = "；".join(problems)
        elif traffic_error is None:
            state.pop("error", None)

    def __refresh_billing(self, account: Dict[str, Any], state: Dict[str, Any],
                          now: datetime) -> None:
        """按 6 小时缓存刷新余额与当月费用。"""
        billing = state.get("billing") if isinstance(state.get("billing"), dict) else {}
        stamp = str(billing.get("at") or "")
        fresh = False
        if stamp:
            try:
                fresh = (now - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")) < timedelta(hours=6)
            except (TypeError, ValueError):
                fresh = False
        if fresh:
            return
        result: Dict[str, Any] = {"at": now.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            balance = self._client.get_balance(account["ak"], account["sk"], account["site_type"])
            result["balance"] = balance.get("amount")
            result["currency"] = balance.get("currency") or "CNY"
        except Exception as err:  # noqa: BLE001
            result["error"] = str(err)
        if account.get("instance"):
            try:
                result["monthly_cost"] = self._client.get_instance_bill(
                    account["ak"], account["sk"], account["site_type"],
                    account["instance"], now.strftime("%Y-%m"),
                )
            except Exception as err:  # noqa: BLE001
                result["error"] = str(err)
        state["billing"] = result

    def __evaluate(self, account: Dict[str, Any], state: Dict[str, Any],
                   now: datetime) -> None:
        """阈值判定 → 告警/恢复通知 → 停机。"""
        quota = self.__quota_of(account)
        traffic = _to_float(state.get("traffic"))
        percent = _usage_percent(traffic, quota)
        state["percent"] = percent
        status = str(state.get("status") or STATUS_UNKNOWN)

        over = self._threshold > 0 and percent >= self._threshold

        if not over:
            if state.get("alerting"):
                state["alerting"] = False
                state["alert_at"] = ""
                if self._notify and self._notify_recover:
                    self.__notify(
                        f"【{self.plugin_name}】流量已回落",
                        f"账号 {account['name']}（{_mask_ak(account['ak'])}）\n"
                        f"当前用量 {_format_gb(traffic)} / {_format_gb(quota)}"
                        f"（{percent:.2f}%），已低于阈值 {self._threshold:g}%。\n"
                        f"实例状态：{_status_text(status)}\n"
                        f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                    )
            return

        # ---- 越过阈值：只在「从未告警 → 告警」这一跳通知，避免每轮刷屏 ----
        first_time = not state.get("alerting")
        if not first_time:
            return
        state["alerting"] = True
        state["alert_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

        stopped_by_threshold = False
        if (self._threshold_action == THRESHOLD_ACTION_STOP
                and account.get("instance")
                and status not in (STATUS_STOPPED, STATUS_STOPPING)):
            try:
                self._client.control_instance(
                    account["ak"], account["sk"], account["region"],
                    account["instance"], "stop", self.__shutdown_mode_of(account),
                )
                state["status"] = STATUS_STOPPING
                stopped_by_threshold = True
                logger.info(f"{self.plugin_name}：{account['name']} 超阈值已停机")
            except Exception as err:  # noqa: BLE001
                # 停机失败不该让告警本身丢掉：记错误但继续发通知
                state["error"] = f"超阈值停机失败：{err}"
                logger.warning(f"{self.plugin_name}：{account['name']} 停机失败 {err}")

        self.__notify(
            f"【{self.plugin_name}】流量阈值告警",
            f"账号 {account['name']}（{_mask_ak(account['ak'])}）的 CDT 流量使用率"
            f"达到 {percent:.2f}%。\n"
            f"当前用量：{_format_gb(traffic)} / {_format_gb(quota)}\n"
            f"分类：{_class_label(_traffic_class(account['region']))}"
            f"（中国大陆 {_format_gb(_to_float(state.get('traffic_china')))} · "
            f"非中国大陆 {_format_gb(_to_float(state.get('traffic_intl')))}）\n"
            f"设定阈值：{self._threshold:g}%\n"
            f"实例状态：{_status_text(status)}"
            + ("（已发送停机指令）" if stopped_by_threshold else "") + "\n"
            f"地域：{_region_name(account['region'])}\n"
            f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        )

    def __run_schedules(self, account: Dict[str, Any], state: Dict[str, Any],
                        now: datetime) -> bool:
        """每日定时开关机。返回本轮是否执行过动作（执行过则强制刷新状态）。"""
        enabled, start_time, stop_time = self.__schedule_of(account)
        if not enabled or not account.get("instance"):
            return False
        changed = False
        for action, clock in (("start", start_time), ("stop", stop_time)):
            if not clock or not _within_window(now, clock, SCHEDULE_WINDOW_MINUTES):
                continue
            cycle_date = _cycle_date(now, start_time, stop_time)
            # 当日去重：同一周期同动作只执行一次（进程重启也不会重复开机）
            field = f"last_schedule_{action}"
            if str(state.get(field) or "") == cycle_date:
                continue
            try:
                self._client.control_instance(
                    account["ak"], account["sk"], account["region"],
                    account["instance"], action, self.__shutdown_mode_of(account),
                )
            except Exception as err:  # noqa: BLE001
                # 失败**不写**去重标记，下一轮窗口内会重试
                state["error"] = f"定时{'开机' if action == 'start' else '关机'}失败：{err}"
                logger.warning(f"{self.plugin_name}：{account['name']} 定时{action} 失败 {err}")
                continue
            state[field] = cycle_date
            state["status"] = STATUS_STARTING if action == "start" else STATUS_STOPPING
            state["pending_status"] = state["status"]
            changed = True
            logger.info(f"{self.plugin_name}：{account['name']} 执行定时"
                        f"{'开机' if action == 'start' else '关机'}")
            # 定时动作按配置决定是否通知
            if self._notify:
                self.__notify(
                    f"【{self.plugin_name}】定时任务已执行",
                    f"账号 {account['name']}（{_mask_ak(account['ak'])}）的实例"
                    f"{account['instance']} 定时"
                    f"{'开机' if action == 'start' else '关机'}指令已发送。\n"
                    f"计划时间：{clock}\n"
                    f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                )
        return changed

    def __run_daily_report(self, now: datetime) -> None:
        """每日日报：统计各账号「时段内实际消耗」并合并成一条通知。

        时段消耗 = 当前累计 − 时段起点累计；时段起点取采样序列里最接近
        「上次日报时刻」的那条，找不到就退化成 24 小时前。

        日报开关与时间都是**账号级优先**（未写则跟随全局），每个账号按自己的
        时间到点后当天只推一次；同一次触发里到点的账号合并成一条通知，避免刷屏。
        """
        if not self._accounts:
            return
        cycle_date = now.strftime("%Y-%m-%d")
        due: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
        for account in self._accounts:
            enabled, clock = self.__daily_report_of(account)
            state = self.__account_state(account)
            if str(state.get("last_report_date") or "") == cycle_date:
                continue
            if not enabled or not clock:
                continue
            if not _within_window(now, clock, SCHEDULE_WINDOW_MINUTES):
                continue
            due.append((account, state, clock))
        if not due:
            return

        lines: List[str] = []
        total_used = 0.0
        total_quota = 0.0
        total_cost: Dict[str, float] = {}
        for account, state, _clock in due:
            traffic = _to_float(state.get("traffic"))
            quota = self.__quota_of(account)
            consumed = self.__consumed_of(state, traffic, now)
            total_used += traffic
            total_quota += quota
            lines.append(
                f"· {account['name']}（{_mask_ak(account['ak'])} / "
                f"{_region_name(account['region'])}）\n"
                f"    时段消耗 {_format_gb(consumed)}"
                f" | 累计 {_format_gb(traffic)} / {_format_gb(quota)}"
                f"（{_usage_percent(traffic, quota):.2f}%）"
            )
            billing = state.get("billing") if isinstance(state.get("billing"), dict) else {}
            if billing.get("monthly_cost") is not None:
                symbol = "$" if billing.get("currency") == "USD" else "¥"
                cost = _to_float(billing.get("monthly_cost"))
                lines.append(f"    当月费用 {symbol}{cost:.2f}")
                total_cost[symbol] = total_cost.get(symbol, 0.0) + cost
            # 标记当天已推 + 记下本次推送时刻（供下一轮算时段消耗的基线）
            state["last_report_date"] = cycle_date
            state["last_report_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

        header = (f"统计日期：{cycle_date}\n"
                  f"合计用量：{_format_gb(total_used)} / {_format_gb(total_quota)}"
                  f"（{_usage_percent(total_used, total_quota):.2f}%）")
        if total_cost:
            header += "\n当月费用合计：" + " + ".join(
                f"{symbol}{value:.2f}" for symbol, value in sorted(total_cost.items())
            )
        self.__notify(
            f"【{self.plugin_name}】流量与账单日报",
            header + f"\n\n各账号详情（{len(due)} 个）：\n" + "\n".join(lines),
        )
        logger.info(f"{self.plugin_name}：日报已生成（{len(due)} 个账号）")

    def __consumed_of(self, state: Dict[str, Any], traffic: float, now: datetime) -> float:
        """时段内消耗：当前值 − 时段起点值。"""
        samples = state.get("samples") or []
        if not samples:
            return 0.0
        # 优先用「上次日报时间」，否则退化成 24 小时前
        anchor = None
        stamp = str(state.get("last_report_at") or "")
        if stamp:
            try:
                anchor = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                anchor = None
        if anchor is None:
            anchor = now - timedelta(hours=24)
        # 取「时间上最接近基线时刻」的那条采样当基线（采样是离散的，
        # 取最近比取「不晚于基线」更贴近基线时刻的真实累计值）。
        baseline = None
        best_gap = None
        for item in samples:
            try:
                item_time = datetime.strptime(str(item.get("t")), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            gap = abs((item_time - anchor).total_seconds())
            if best_gap is None or gap < best_gap:
                best_gap = gap
                baseline = _to_float(item.get("v"))
        if baseline is None:
            baseline = _to_float(samples[0].get("v"))
        consumed = traffic - baseline
        # 跨月清零会让差值变负：月初两天按「当月累计」解释，其余按 0
        if consumed < 0:
            consumed = traffic if now.day <= 2 else 0.0
        return round(max(0.0, consumed), 3)

    def __run_keep_alive(self, account: Dict[str, Any], state: Dict[str, Any],
                         now: datetime) -> bool:
        """实例保活：允许运行时段内发现实例停止就拉起。

        四个必要条件（缺一不可）：

        1. 该账号开启了保活；
        2. 实例确实处于 ``Stopped``（中间态不碰，免得和正在进行的启停打架）；
        3. 当前**没有**处于超阈值告警中 —— 阈值熔断优先于保活；
        4. 当前在允许运行的时段内（由该账号的定时开关机时间界定）。

        去重粒度到分钟（``keepalive:YYYYMMDDHHMM``）：同一分钟内只发一次指令，
        但失败不留标记，下一分钟会继续重试。
        """
        if not self.__keep_alive_of(account):
            return False
        if not account.get("instance"):
            return False
        if str(state.get("status") or "") != STATUS_STOPPED:
            return False
        if state.get("alerting"):
            return False

        enabled, start_time, stop_time = self.__schedule_of(account)
        current = now.strftime("%H:%M")
        if enabled and not _in_time_range(current, start_time, stop_time):
            return False

        marker = now.strftime("%Y%m%d%H%M")
        if str(state.get("last_keepalive") or "") == marker:
            return False
        try:
            self._client.control_instance(
                account["ak"], account["sk"], account["region"],
                account["instance"], "start", self.__shutdown_mode_of(account),
            )
        except Exception as err:  # noqa: BLE001
            state["error"] = f"保活启动失败：{err}"
            logger.warning(f"{self.plugin_name}：{account['name']} 保活失败 {err}")
            return False
        state["last_keepalive"] = marker
        state["status"] = STATUS_STARTING
        state["pending_status"] = STATUS_STARTING
        logger.info(f"{self.plugin_name}：{account['name']} 检测到实例停止，已发保活启动指令")
        # 保活是「无人值守地替你动了实例」，必须留痕（与原版 keepalive 事件一致）
        if self._notify:
            self.__notify(
                f"【{self.plugin_name}】实例保活启动",
                f"检测到实例在允许运行时段意外停止，已发送启动指令。\n"
                f"账号：{account['name']}（{_mask_ak(account['ak'])}）\n"
                f"实例：{account['instance']}\n"
                f"地域：{_region_name(account['region'])}\n"
                f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
            )
        return True
        logger.info(f"{self.plugin_name}：{account['name']} 实例意外停止，已保活拉起")
        self.__notify(
            f"【{self.plugin_name}】实例保活启动",
            f"账号 {account['name']}（{_mask_ak(account['ak'])}）的实例"
            f"{account['instance']} 在允许运行时段内意外停止，已发送开机指令。\n"
            f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        )
        return True

    # ------------------------------------------------------------------
    # 手动动作（详情页按钮 / API）
    # ------------------------------------------------------------------

    def api_refresh(self, idx: str = None, apikey: str = None) -> Dict[str, Any]:
        """刷新指定账号（idx 缺省 = 全部）。"""
        _ = apikey
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if idx not in (None, ""):
            account = self.__account_by_index(idx)
            if not account:
                return {"success": False, "message": f"账号下标 {idx} 不存在"}
            self.__refresh_one(account)
            return {"success": True, "message": f"已刷新 {account['name']}"}
        self.check_traffic(force=True)
        return {"success": True, "message": "已刷新全部账号"}

    def api_start(self, idx: str = None, apikey: str = None) -> Dict[str, Any]:
        """启动实例。"""
        _ = apikey
        return self.__control(idx, "start")

    def api_stop(self, idx: str = None, apikey: str = None) -> Dict[str, Any]:
        """停止实例。"""
        _ = apikey
        return self.__control(idx, "stop")

    def __control(self, idx: Optional[str], action: str) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        account = self.__account_by_index(idx)
        if not account:
            return {"success": False, "message": f"账号下标 {idx} 不存在"}
        if not account.get("instance"):
            return {"success": False, "message": f"{account['name']} 未配置实例 ID"}
        state = self.__account_state(account)
        status = str(state.get("status") or "")
        if status in (STATUS_STARTING, STATUS_STOPPING):
            return {"success": False, "message": f"实例当前为 {_status_text(status)}，请稍后再试"}
        if action == "stop" and self.__keep_alive_of(account):
            # 与原版一致：开启保活时禁止手动停机（否则下一轮又会被拉起）
            return {"success": False, "message": "该账号已开启保活，手动停机无效"}
        try:
            self._client.control_instance(
                account["ak"], account["sk"], account["region"],
                account["instance"], action, self.__shutdown_mode_of(account),
            )
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": f"执行失败：{err}"}
        state["status"] = STATUS_STARTING if action == "start" else STATUS_STOPPING
        state["pending_status"] = state["status"]
        self.__save_state()
        logger.info(f"{self.plugin_name}：面板手动{'开机' if action == 'start' else '停机'}"
                    f" {account['name']}")
        return {
            "success": True,
            "message": f"已向 {account['name']} 的实例发送"
                       f"{'开机' if action == 'start' else '停机'}指令",
        }

    def __refresh_one(self, account: Dict[str, Any]) -> None:
        """同步刷新单个账号（详情页按钮用）。"""
        if self._client is None:
            return
        self.__fetch_account(account)
        state = self.__account_state(account)
        self.__evaluate(account, state, datetime.now())
        self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.__save_state()

    # ------------------------------------------------------------------
    # 状态存取
    # ------------------------------------------------------------------

    def __account_by_index(self, idx: Optional[str]) -> Optional[Dict[str, Any]]:
        """按下标取账号（按钮只带静态参数，所以用下标定位）。"""
        try:
            index = int(str(idx))
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(self._accounts):
            return self._accounts[index]
        return None

    def __account_state(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """取（必要时创建）某账号的状态字典。"""
        key = _account_key(account)
        value = self._state.get(key)
        if not isinstance(value, dict):
            value = {}
            self._state[key] = value
        value.setdefault("name", account.get("name"))
        value.setdefault("samples", [])
        value.setdefault("status", STATUS_UNKNOWN)
        value.setdefault("traffic", 0.0)
        value.setdefault("alerting", False)
        return value

    @staticmethod
    def __append_sample(state: Dict[str, Any], now: datetime) -> None:
        """追加一条采样（同一个分钟内不重复记）。"""
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        samples = state.setdefault("samples", [])
        if samples and str(samples[-1].get("t")) == stamp:
            samples[-1]["v"] = state.get("traffic")
            return
        samples.append({"t": stamp, "v": state.get("traffic")})
        if len(samples) > ACCOUNT_SAMPLE_LIMIT:
            del samples[: len(samples) - ACCOUNT_SAMPLE_LIMIT]

    def __save_state(self) -> None:
        try:
            self.save_data(STATE_KEY, self._state)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{self.plugin_name}：写入状态失败 {err}")

    def __cache_age_seconds(self) -> float:
        """当前状态距上次检查的秒数；没跑过返回一个很大的值。"""
        if not self._last_check_time:
            # 用状态里最近一次 updated_at 兜底（面板重启后 _last_check_time 是空的）
            stamps = [
                str(item.get("updated_at"))
                for item in self._state.values()
                if isinstance(item, dict) and item.get("updated_at")
            ]
            if not stamps:
                return float("inf")
            self._last_check_time = max(stamps)
        try:
            last = datetime.strptime(self._last_check_time, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return float("inf")
        return (datetime.now() - last).total_seconds()

    def __refresh_in_background(self) -> None:
        """后台静默刷新（不阻塞详情页渲染，也不与前台检查抢锁）。"""
        if type(self)._refreshing:
            return
        type(self)._refreshing = True

        def _worker() -> None:
            try:
                self.check_traffic(force=False)
            except Exception as err:  # noqa: BLE001  线程里的异常没人接，必须自己吞
                logger.warning(f"{self.plugin_name}：后台刷新失败 {err}")
            finally:
                type(self)._refreshing = False

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # 配置解析辅助（账号级值优先，缺省回落全局）
    # ------------------------------------------------------------------

    def __quota_of(self, account: Dict[str, Any]) -> float:
        quota = _to_float(account.get("quota"))
        return quota if quota > 0 else CDT_FREE_QUOTA_GB

    def __shutdown_mode_of(self, account: Dict[str, Any]) -> str:
        mode = str(account.get("shutdown_mode") or "").strip()
        if mode in ("KeepCharging", "StopCharging"):
            return mode
        return self._shutdown_mode

    def __keep_alive_of(self, account: Dict[str, Any]) -> bool:
        value = account.get("keep_alive")
        return self._keep_alive if value is None else bool(value)

    def __schedule_of(self, account: Dict[str, Any]) -> Tuple[bool, str, str]:
        """解析该账号的定时开关机配置（账号级覆盖全局）。"""
        value = account.get("schedule")
        enabled = self._schedule_enabled if value is None else bool(value)
        start_time = account.get("start_time") or self._start_time
        stop_time = account.get("stop_time") or self._stop_time
        if not (start_time or stop_time):
            enabled = False
        return enabled, start_time, stop_time

    def __daily_report_of(self, account: Dict[str, Any]) -> Tuple[bool, str]:
        value = account.get("daily_report")
        enabled = self._daily_report if value is None else bool(value)
        clock = account.get("daily_report_time") or self._daily_report_time
        return enabled, clock

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    def __notify(self, title: str, text: str) -> None:
        if not self._notify:
            return
        try:
            # 🔴 post_message 的第一个位置参数是 channel，必须用关键字调用
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{self.plugin_name}：发送通知失败 {err}")


def _within_window(now: datetime, clock: str, window_minutes: int) -> bool:
    """判断 now 是否落在「clock 之后 window 分钟」这个补偿窗口内。

    用「相差多少分钟」而不是直接比大小，跨零点（23:50 → 00:05）时也成立。
    """
    target = _clock_minutes(clock)
    if target is None:
        return False
    current = now.hour * 60 + now.minute
    delta = (current - target) % (24 * 60)
    return 0 <= delta <= window_minutes


def _in_time_range(current: str, start: str, end: str) -> bool:
    """当前时刻是否落在 ``[start, end)`` 这个允许运行的时段内。

    支持跨零点（``23:00 ~ 07:00``）；只配了一端时按那一端界定半开区间；
    两端都没配视为「全天允许」。
    """
    start_minutes = _clock_minutes(start)
    end_minutes = _clock_minutes(end)
    current_minutes = _clock_minutes(current)
    if current_minutes is None:
        return False
    if start_minutes is None and end_minutes is None:
        return True
    if start_minutes is None:
        return current_minutes < end_minutes
    if end_minutes is None:
        return current_minutes >= start_minutes
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    # 跨零点：落在起点之后或终点之前都算
    return current_minutes >= start_minutes or current_minutes < end_minutes


def _overnight_schedule(start: str, stop: str) -> bool:
    """时段是否跨零点（如 23:00 → 07:00）。"""
    start_minutes = _clock_minutes(start)
    stop_minutes = _clock_minutes(stop)
    if start_minutes is None or stop_minutes is None:
        return False
    return start_minutes >= stop_minutes


def _cycle_date(now: datetime, start: str, stop: str) -> str:
    """定时任务的「周期日期」，用于当日去重（跨零点时段归属前一天）。"""
    if _overnight_schedule(start, stop):
        start_minutes = _clock_minutes(start)
        if start_minutes is not None and now.hour * 60 + now.minute < start_minutes:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")
