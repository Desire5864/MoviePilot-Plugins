import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from requests import Session

from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils

# 已提交打包记录的持久化键名
SUBMITTED_DATA_KEY = "bdmv_submitted_map"
# 已提交记录最多保留条数（超出后自动淘汰最早的记录；保留较多用于去重判定）
SUBMITTED_LIMIT = 20
# 页面表格展示条数（只展示最新的若干条，控制在页面上的视觉长度）
SUBMITTED_DISPLAY = 6
# 默认标签
DEFAULT_TAG = "UHD自动下载"
# 默认分类（与 UHD原盘自动下载 插件推送 QB 时使用的分类保持一致）
DEFAULT_CATEGORY = "彩虹岛&HR,OurBits原盘"
# UHD原盘自动下载 插件ID（用于读取中文标题映射）
UHD_PLUGIN_ID = "UhdBlurayAutoDownload"
# UHD原盘自动下载 插件的已处理记录键名
UHD_PROCESSED_DATA_KEY = "uhd_processed_map"
# CD2 备份待完成记录的持久化键名
CD2_PENDING_DATA_KEY = "cd2_pending_map"
# CD2 待完成记录最多保留条数
CD2_PENDING_LIMIT = 100
# CD2 备份状态枚举
CD2_STATUS_TEXT = {
    0: "空闲",
    1: "扫描中",
    2: "错误",
    3: "已禁用",
    4: "已扫描",
    5: "已完成",
    6: "等待中",
}

# CD2 备份层状态（BackupStatus.Status）中需特殊处理的取值：
# 1 = WalkingThrough（正在扫描源目录）。此时不得评估任何阶段，
# 否则会把「上一轮」扫描的完成时间当成本轮结果，通知早于 CD2 实际扫描完成。
CD2_STATUS_WALKING = 1

# CD2 上传任务状态（UploadFileInfo.Status）取值与文案。
# 注意「预处理中/排队中」并不是在传数据：ISO 刚推进去时任务处于这些阶段，
# 此时不能宣布「开始上传」（这是 2026-09-22 v1.9.0 通知过早的根因）。
CD2_UPLOAD_STATE_TEXT = {
    0: "等待预处理",
    1: "预处理中",
    2: "已取消",
    3: "传输中",
    4: "已暂停",
    5: "已完成",
    6: "已跳过",
    7: "排队中",
    8: "已忽略",
    9: "出错",
    10: "严重错误",
}
# 尚未开始传输的阶段（排队/预处理）
CD2_UPLOAD_PREPARE_STATES = (0, 1, 7)
# 已进入传输的阶段（含暂停）
CD2_UPLOAD_TRANSFER_STATES = (3, 4)
# 传输出错
CD2_UPLOAD_ERROR_STATES = (9, 10)

# ISO 直通（直出 ISO 的站点：跳过打包，直接触发 CD2 备份）记录持久化键名
ISO_SUBMITTED_DATA_KEY = "bdmv_iso_map"
# ISO 直通记录最多保留条数（同时用于去重）
ISO_SUBMITTED_LIMIT = 20
# 页面展示的 ISO 直通记录条数
ISO_SUBMITTED_DISPLAY = 6
# 默认 ISO 直通分类（与 UHD原盘自动下载 插件推送天空站时使用的分类保持一致）
DEFAULT_ISO_CATEGORY = "HDSky原盘"
# 默认 ISO 直通备份源路径（QB 保存目录 /ISO 对应的 CD2 侧路径）
DEFAULT_ISO_SOURCE_PATH = "/downloads/ISO"
# CD2 待完成记录的类型标记
CD2_KIND_BDMV = "bdmv"
CD2_KIND_ISO = "iso"

# 打包状态快通道的轮询间隔（秒）。
# 常规检查间隔默认 300 秒，打包完成的那一刻最多要等 5 分钟才会被感知；
# 快通道只在「确有任务在跑」时启用，用数秒级间隔盯住打包结果，
# 把「打包完成 → 触发 CD2」的延迟压到秒级；空闲时门闸关闭，零请求。
WATCH_INTERVAL = 10
# 快通道关心的记录状态：已提交待打包、打包中
PACKING_ACTIVE_STATES = ("submitted", "running")
# 快通道门闸的过期阈值（秒）：记录挂在这些状态超过该时长仍未收敛时，
# 不再由快通道跟踪（例如服务端任务被清理掉，状态永远等不到），
# 交回常规检查间隔处理，避免门闸长期敞开、高频请求白白跑着。
WATCH_STALE_SECONDS = 6 * 3600
# 快通道连续请求失败的退避阈值：达到该次数即暂停快通道（避免服务端不可用时
# 每轮都刷日志），等下一次常规检查重新评估后恢复。
WATCH_FAIL_LIMIT = 5


class BdmvToIso(_PluginBase):
    """BDMV 原盘自动打包 ISO 插件。

    监控指定 QB 下载器中带指定标签的已完成任务，将下载目录名与
    自建「BDMV to ISO」服务的资源目录比对，命中后自动触发打包，
    并轮询打包进度；打包完成后可自动触发 CloudDrive2 备份，
    将 ISO 同步上传到云端，并发送通知。

    另支持「ISO 直通」：某些站点（如天空）直接下发 ISO 文件，
    无需打包，命中直通分类的已完成任务会跳过打包环节，
    直接触发对应源目录的 CD2 备份。
    """

    # 插件名称
    plugin_name = "BDMV自动打包ISO"
    # 插件描述
    plugin_desc = "监控QB指定分类/标签的已完成原盘，自动打包ISO，打包或直出后触发CD2备份同步并通知。"
    # 插件图标
    plugin_icon = "UHD.png"
    # 插件版本
    plugin_version = "1.9.5"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "bdmvtoiso_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    # 服务地址
    _server_url: str = ""
    # 服务账号
    _username: str = ""
    # 服务密码
    _password: str = ""
    # 下载器
    _downloaders: List[str] = []
    # 监控标签
    _tags: List[str] = []
    # 监控分类
    _categories: List[str] = []
    # 检查间隔（秒）
    _interval: int = 300
    # 是否自动触发打包
    _auto_convert: bool = True
    # 是否在打包完成后发送通知
    _notify_on_done: bool = True
    # 是否在打包完成后触发 CD2 备份
    _cd2_enabled: bool = False
    # CD2 服务地址
    _cd2_address: str = ""
    # CD2 账号
    _cd2_username: str = ""
    # CD2 密码
    _cd2_password: str = ""
    # CD2 备份源路径（对应 ISO 输出目录）
    _cd2_source_path: str = ""
    # CD2 备份目标路径（云端目录，用于校验上传是否完成）
    _cd2_destination_path: str = ""
    # 是否启用 ISO 直通（直出 ISO 的站点跳过打包，直接触发 CD2 备份）
    _iso_passthrough: bool = True
    # ISO 直通分类
    _iso_categories: List[str] = []
    # ISO 直通备份源路径（CD2 侧，对应 QB 的 ISO 保存目录）
    _cd2_iso_source_path: str = ""
    # 与服务端保持的登录会话
    _session: Optional[Session] = None
    # CD2 gRPC 客户端
    _cd2_client: Optional[Any] = None
    # 打包状态快通道间隔（秒）
    _watch_interval: int = WATCH_INTERVAL
    # 快通道门闸：None=未知（下次运行时查一次存储判断），
    # True=有活跃任务在跑，False=空闲（直接返回，不读存储、不发请求）
    _watch_active: Optional[bool] = None
    # 快通道连续失败计数（用于服务端不可用时退避）
    _watch_fail: int = 0

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        # 停止现有任务
        self.stop_service()

        # 重置状态
        self._enabled = False
        self._notify = False
        self._server_url = ""
        self._username = ""
        self._password = ""
        self._downloaders = []
        self._tags = []
        self._categories = []
        self._interval = 300
        self._auto_convert = True
        self._notify_on_done = True
        self._cd2_enabled = False
        self._cd2_address = ""
        self._cd2_username = ""
        self._cd2_password = ""
        self._cd2_source_path = ""
        self._cd2_destination_path = ""
        self._iso_passthrough = True
        self._iso_categories = []
        self._cd2_iso_source_path = ""
        self._session = None
        self._cd2_client = None
        self._watch_interval = WATCH_INTERVAL
        # 门闸未知：首次运行时查一次存储，判断是否有遗留的活跃任务
        self._watch_active = None
        self._watch_fail = 0

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._server_url = str(config.get("server_url") or "").strip().rstrip("/")
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._downloaders = config.get("downloaders") or []

        # 标签支持逗号或换行分隔
        tags_raw = config.get("tags") or DEFAULT_TAG
        self._tags = [
            tag.strip()
            for tag in str(tags_raw).replace("\n", ",").split(",")
            if tag.strip()
        ]

        # 分类支持逗号或换行分隔
        categories_raw = config.get("categories") or DEFAULT_CATEGORY
        self._categories = [
            category.strip()
            for category in str(categories_raw).replace("\n", ",").split(",")
            if category.strip()
        ]

        try:
            self._interval = max(30, int(config.get("interval") or 300))
        except (TypeError, ValueError):
            self._interval = 300

        # 快通道间隔：5~60 秒，默认 10 秒（打包完成后的感知延迟上限）
        try:
            self._watch_interval = min(60, max(5, int(config.get("watch_interval") or WATCH_INTERVAL)))
        except (TypeError, ValueError):
            self._watch_interval = WATCH_INTERVAL

        self._auto_convert = bool(config.get("auto_convert", True))
        self._notify_on_done = bool(config.get("notify_on_done", True))

        # CD2 备份配置
        self._cd2_enabled = bool(config.get("cd2_enabled"))
        self._cd2_address = str(config.get("cd2_address") or "").strip()
        self._cd2_username = str(config.get("cd2_username") or "").strip()
        self._cd2_password = str(config.get("cd2_password") or "")
        self._cd2_source_path = str(config.get("cd2_source_path") or "").strip().rstrip("/")
        self._cd2_destination_path = str(config.get("cd2_destination_path") or "").strip().rstrip("/")

        # ISO 直通配置（直出 ISO 的站点：跳过打包，直接触发 CD2 备份）
        self._iso_passthrough = bool(config.get("iso_passthrough", True))
        iso_categories_raw = config.get("iso_categories")
        if iso_categories_raw is None:
            iso_categories_raw = DEFAULT_ISO_CATEGORY
        self._iso_categories = [
            category.strip()
            for category in str(iso_categories_raw).replace("\n", ",").split(",")
            if category.strip()
        ]
        self._cd2_iso_source_path = str(
            config.get("cd2_iso_source_path") or DEFAULT_ISO_SOURCE_PATH
        ).strip().rstrip("/")

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
                                            "placeholder": "默认300，最小30",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "watch_interval",
                                            "label": "完成感知间隔（秒）",
                                            "placeholder": "默认10，最小5；仅在有任务时生效",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "server_url",
                                            "label": "BDMV to ISO 服务地址",
                                            "placeholder": "例如：http://192.168.3.36:5173",
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
                                            "model": "username",
                                            "label": "服务账号",
                                            "placeholder": "登录 BDMV to ISO 的账号",
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
                                            "model": "password",
                                            "label": "服务密码",
                                            "type": "password",
                                            "placeholder": "登录 BDMV to ISO 的密码",
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
                                            "model": "tags",
                                            "label": "监控标签",
                                            "placeholder": f"用,分隔多个标签，默认：{DEFAULT_TAG}",
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
                                            "model": "categories",
                                            "label": "监控分类",
                                            "placeholder": f"用,分隔多个分类，默认：{DEFAULT_CATEGORY}",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_convert",
                                            "label": "自动触发打包",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify_on_done",
                                            "label": "打包完成后通知",
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
                                        "component": "VDivider",
                                        "props": {"class": "my-2"},
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "cd2_enabled",
                                            "label": "打包完成后触发 CD2 备份同步",
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
                                            "model": "cd2_address",
                                            "label": "CD2 gRPC 地址",
                                            "placeholder": "例如：192.168.3.36:19798",
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
                                            "model": "cd2_source_path",
                                            "label": "CD2 备份源路径",
                                            "placeholder": "例如：/Storage/ISO",
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
                                            "model": "cd2_destination_path",
                                            "label": "CD2 备份目标路径（云端目录）",
                                            "placeholder": "例如：/115open/云下载/蓝光原盘，用于校验上传是否完成",
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
                                            "model": "cd2_username",
                                            "label": "CD2 账号",
                                            "placeholder": "CloudDrive2 登录账号",
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
                                            "model": "cd2_password",
                                            "label": "CD2 密码",
                                            "type": "password",
                                            "placeholder": "CloudDrive2 登录密码",
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
                                        "component": "VDivider",
                                        "props": {"class": "my-2"},
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "iso_passthrough",
                                            "label": "启用 ISO 直通（直出 ISO 的站点跳过打包，直接触发 CD2 备份）",
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
                                            "model": "iso_categories",
                                            "label": "ISO 直通分类",
                                            "placeholder": f"用,分隔多个分类，默认：{DEFAULT_ISO_CATEGORY}",
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
                                            "model": "cd2_iso_source_path",
                                            "label": "ISO 直通备份源路径",
                                            "placeholder": f"默认：{DEFAULT_ISO_SOURCE_PATH}",
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
                                            "text": "插件会按设定间隔检查所选下载器中同时满足「分类」与「标签」的已完成任务，"
                                                    "将任务目录名与 BDMV to ISO 服务的资源目录比对，"
                                                    "命中后自动触发打包为 ISO，并轮询打包进度；"
                                                    "打包完成后自动触发 CD2 备份扫描，将 ISO 同步到云端，"
                                                    "并发送通知。"
                                                    "「ISO 直通」分类中的已完成任务会跳过打包与资源目录比对，"
                                                    "直接触发对应源路径的 CD2 备份。",
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
            "interval": 300,
            "watch_interval": WATCH_INTERVAL,
            "server_url": "",
            "username": "",
            "password": "",
            "downloaders": [],
            "tags": DEFAULT_TAG,
            "categories": DEFAULT_CATEGORY,
            "auto_convert": True,
            "notify_on_done": True,
            "cd2_enabled": False,
            "cd2_address": "",
            "cd2_username": "",
            "cd2_password": "",
            "cd2_source_path": "",
            "cd2_destination_path": "",
            "iso_passthrough": True,
            "iso_categories": DEFAULT_ISO_CATEGORY,
            "cd2_iso_source_path": DEFAULT_ISO_SOURCE_PATH,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: Vuetify 详情页结构
        """
        if not self._enabled:
            return None

        downloaders_text = "、".join(self._downloaders) if self._downloaders else "未配置"
        tags_text = "、".join(self._tags) if self._tags else "未配置"
        categories_text = "、".join(self._categories) if self._categories else "未配置"
        iso_categories_text = "、".join(self._iso_categories) if self._iso_categories else "未配置"
        iso_text = (
            f"已启用（分类：{iso_categories_text}；源：{self._cd2_iso_source_path}）"
            if self._iso_passthrough
            else "未启用"
        )

        page_content: List[dict] = [
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
                                    "text": f"服务地址：{self._server_url or '未配置'}；"
                                            f"监控下载器：{downloaders_text}；"
                                            f"监控分类：{categories_text}；"
                                            f"监控标签：{tags_text}；"
                                            f"检查间隔：{self._interval} 秒"
                                            f"（有任务时快通道 {self._watch_interval} 秒）；"
                                            f"自动打包：{'已启用' if self._auto_convert else '未启用'}；"
                                            f"ISO 直通：{iso_text}",
                                },
                            }
                        ],
                    }
                ],
            }
        ]

        # 服务连通性检查
        jobs = self.__fetch_status()
        if jobs is None:
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
                                        "text": "无法连接 BDMV to ISO 服务，请检查服务地址、账号密码与服务状态。",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
            return page_content

        # CD2 备份状态
        if self._cd2_enabled:
            cd2_status = self.__get_cd2_backup_status()
            if cd2_status:
                cd2_text = (
                    f"CD2 备份源 {self._cd2_source_path}："
                    f"{cd2_status.get('status_text')}"
                )
                if cd2_status.get("message"):
                    cd2_text += f"（{cd2_status['message']}）"
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
                                            "type": "success",
                                            "variant": "tonal",
                                            "text": cd2_text,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                )
            else:
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
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "未能获取 CD2 备份状态，请检查 CD2 地址、账号密码与备份源路径。",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                )

        # 服务端资源目录
        resources = self.__fetch_resources()
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
                                    "type": "success" if resources else "warning",
                                    "variant": "tonal",
                                    "text": f"服务端资源目录共 {len(resources)} 个"
                                            if resources is not None
                                            else "未能获取服务端资源目录列表。",
                                },
                            }
                        ],
                    }
                ],
            }
        )

        # 打包任务状态
        if jobs:
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
                                        "text": f"当前打包任务共 {len(jobs)} 个",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            rows = []
            for name, job in jobs.items():
                progress = float(job.get("progress") or 0) * 100
                status = job.get("status") or "-"
                detail = job.get("detail") or ""
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": self.__status_text(status)},
                            {"component": "td", "text": f"{progress:.1f}%"},
                            {"component": "td", "text": detail},
                            {"component": "td", "text": name},
                        ],
                    }
                )

            page_content.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VTable",
                                    "props": {"density": "compact"},
                                    "content": [
                                        {
                                            "component": "thead",
                                            "content": [
                                                {
                                                    "component": "tr",
                                                    "content": [
                                                        {"component": "th", "text": "状态"},
                                                        {"component": "th", "text": "进度"},
                                                        {"component": "th", "text": "详情"},
                                                        {"component": "th", "text": "资源目录"},
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "component": "tbody",
                                            "content": rows,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )

        # 已提交记录
        submitted_map: Dict[str, Any] = self.get_data(SUBMITTED_DATA_KEY) or {}
        if submitted_map:
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
                                        "text": f"已提交打包记录共 {len(submitted_map)} 条"
                                                f"（最多保留 {SUBMITTED_LIMIT} 条，"
                                                f"显示最近 {SUBMITTED_DISPLAY} 条）",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            submitted_rows = []
            # 只展示最新 SUBMITTED_DISPLAY 条（存储上限 SUBMITTED_LIMIT 更大，用于去重）
            for name, info in list(submitted_map.items())[-SUBMITTED_DISPLAY:]:
                submitted_rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": str(info.get("time") or "-")},
                            {"component": "td", "text": str(info.get("status") or "-")},
                            {"component": "td", "text": name},
                        ],
                    }
                )

            page_content.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VTable",
                                    "props": {"density": "compact"},
                                    "content": [
                                        {
                                            "component": "thead",
                                            "content": [
                                                {
                                                    "component": "tr",
                                                    "content": [
                                                        {"component": "th", "text": "提交时间"},
                                                        {"component": "th", "text": "状态"},
                                                        {"component": "th", "text": "资源目录"},
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "component": "tbody",
                                            "content": submitted_rows,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )

        # ISO 直通：备份源状态与已触发记录
        if self._iso_passthrough and self._iso_categories:
            if self._cd2_enabled and self._cd2_iso_source_path:
                iso_status = self.__get_cd2_backup_status(self._cd2_iso_source_path)
                if iso_status:
                    iso_status_text = (
                        f"ISO 直通备份源 {self._cd2_iso_source_path}："
                        f"{iso_status.get('status_text')}"
                    )
                    if iso_status.get("message"):
                        iso_status_text += f"（{iso_status['message']}）"
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
                                                "type": "success",
                                                "variant": "tonal",
                                                "text": iso_status_text,
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    )
                else:
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
                                                "type": "warning",
                                                "variant": "tonal",
                                                "text": f"未能获取 ISO 直通备份源 {self._cd2_iso_source_path} "
                                                        "的状态，请检查该源路径是否已在 CD2 中配置备份任务。",
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    )

            iso_map: Dict[str, Any] = self.get_data(ISO_SUBMITTED_DATA_KEY) or {}
            if iso_map:
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
                                            "text": f"ISO 直通记录共 {len(iso_map)} 条"
                                                    f"（最多保留 {ISO_SUBMITTED_LIMIT} 条，"
                                                    f"显示最近 {ISO_SUBMITTED_DISPLAY} 条）",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                )

                iso_rows = []
                for name, info in list(iso_map.items())[-ISO_SUBMITTED_DISPLAY:]:
                    iso_rows.append(
                        {
                            "component": "tr",
                            "content": [
                                {"component": "td", "text": str(info.get("time") or "-")},
                                {"component": "td", "text": str(info.get("status") or "-")},
                                {"component": "td", "text": name},
                            ],
                        }
                    )

                page_content.append(
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTable",
                                        "props": {"density": "compact"},
                                        "content": [
                                            {
                                                "component": "thead",
                                                "content": [
                                                    {
                                                        "component": "tr",
                                                        "content": [
                                                            {"component": "th", "text": "触发时间"},
                                                            {"component": "th", "text": "状态"},
                                                            {"component": "th", "text": "ISO 文件"},
                                                        ],
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "tbody",
                                                "content": iso_rows,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                )

        return page_content

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        services: List[Dict[str, Any]] = []
        if not self._enabled:
            return services

        # ISO 直通不依赖 BDMV to ISO 服务，服务地址缺失时仍可运行
        iso_ready = bool(self._iso_passthrough and self._cd2_enabled)

        if not self._server_url and not iso_ready:
            logger.warning("BDMV自动打包ISO：未配置服务地址且未启用 ISO 直通，跳过注册定时服务")
            return services

        services.append(
            {
                "id": "BdmvToIsoCheck",
                "name": "BDMV原盘自动打包检查",
                "trigger": "interval",
                "func": self.check_and_convert,
                "kwargs": {"seconds": self._interval},
            }
        )

        # 打包状态快通道：常规检查间隔默认 300 秒，打包完成的那一刻最多要等
        # 5 分钟才会被感知到。这条服务只在「确有任务在跑」时工作（内部有门闸，
        # 空闲时直接返回，不读存储、不发请求），用秒级间隔盯住打包结果，
        # 把「打包完成 → 触发 CD2 备份」的延迟压到数秒。
        services.append(
            {
                "id": "BdmvToIsoWatch",
                "name": "BDMV打包完成快通道",
                "trigger": "interval",
                "func": self.__watch_packing,
                "kwargs": {"seconds": self._watch_interval},
            }
        )

        return services

    def check_and_convert(self) -> None:
        """检查已完成任务：ISO 直通任务直接触发 CD2 备份，其余走 BDMV 打包流程。"""
        if not self._enabled:
            return

        # 1. 收集已完成任务（打包候选与 ISO 直通候选）
        collected = self.__collect_completed_names()
        if collected is None:
            logger.warning("BDMV自动打包ISO：获取下载器任务失败，跳过检查")
            return
        candidates, iso_names, iso_hashes = collected

        # 2. ISO 直通：直出 ISO 的站点跳过打包，直接触发 CD2 备份（不依赖 BDMV 服务）
        self.__process_iso_passthrough(iso_names, iso_hashes)

        # 3. BDMV 打包
        self.__process_bdmv_packaging(candidates)

        # 4. 检查已触发的 CD2 备份是否完成
        self.__check_cd2_finished()

        # 5. 重算快通道门闸。常规检查同时充当快通道的兜底心跳：
        #    快通道在服务端连续无响应时会自行退避关闭，靠这里的重新评估恢复。
        self.__refresh_watch_gate()

    def __collect_active_packing(self) -> Tuple[List[str], Dict[str, Any]]:
        """收集仍在打包中的记录，供快通道比对服务端状态。

        过期记录（挂在打包中状态超过 `WATCH_STALE_SECONDS` 仍未收敛，
        例如服务端任务被清理、状态永远等不到结果）不再由快通道跟踪，
        交回常规检查间隔处理，避免门闸长期敞开白跑请求。

        :return: (待比对的资源目录名列表, 已提交打包记录)
        """
        submitted_map = self.get_data(SUBMITTED_DATA_KEY) or {}
        if not isinstance(submitted_map, dict):
            submitted_map = {}

        if not self._server_url:
            return [], submitted_map

        names = [
            name
            for name, record in submitted_map.items()
            if isinstance(record, dict)
            and str(record.get("status") or "") in PACKING_ACTIVE_STATES
            and not self.__is_packing_stale(record)
        ]
        return names, submitted_map

    def __is_packing_stale(self, record: Dict[str, Any]) -> bool:
        """判断一条打包记录是否已超过快通道的跟踪时限。

        :param record: 打包记录
        :return: 是否过期
        """
        time_text = str((record or {}).get("time") or "")
        if not time_text:
            return False
        try:
            start = datetime.strptime(time_text, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return False
        return (datetime.now() - start).total_seconds() > WATCH_STALE_SECONDS

    def __refresh_watch_gate(self) -> None:
        """重算快通道门闸：当前是否有需要高频跟踪的打包任务。"""
        active_names, _ = self.__collect_active_packing()
        self._watch_active = bool(active_names)

    def __watch_packing(self) -> None:
        """打包状态快通道：高频感知打包完成，尽快触发 CD2 备份。

        它只负责「打包完成 → 立即通知并触发 CD2」这一段 —— 这一段没有任何
        现成的事件源可用（ISO 服务是加密的、无出站回调能力，插件又看不到
        输出目录），只能靠缩短轮询间隔来抢时间，而它后面的采集环节本身就要
        几分钟到几十分钟，提前一点毫无意义。所以 CD2 的扫描/上传跟踪仍然
        由常规检查按 `_interval`（默认 300 秒）进行，既不给 CD2 增加查询压力，
        也不会把日志刷密。

        它不是一套新逻辑，只是「常规检查的加速版」——打包完成的判定与处理
        完全复用 `__advance_packing`（最终走 `__notify_done`：发通知 +
        触发 CD2 备份 + 写待完成记录）。

        门闸（`_watch_active`）是它能以秒级间隔注册的前提：
        - `False`：上一轮已确认没有打包任务 → 直接返回，**零存储读、零网络请求**
        - `None`：插件重载后的首跑 → 正常走一遍，自然算出真实状态
        - `True`：有打包任务 → 推进；完成后重算门闸并自动落下

        常规检查按 `_interval` 照旧运行，所以即使门闸判断有偏差，
        最坏也只是回退到常规间隔，不会漏掉任务。
        """
        if not self._enabled:
            return

        # 已确认空闲：直接返回（这一句是快通道能高频注册的关键）
        if self._watch_active is False:
            return

        active_names, submitted_map = self.__collect_active_packing()
        if not active_names:
            self._watch_active = False
            return

        self._watch_active = True

        if self._server_url:
            try:
                ok = self.__advance_packing(active_names, submitted_map)
            except Exception as err:
                # 异常同样计入失败（快通道是高频服务，不能让它把调度日志刷爆，
                # 也不能让它绕过退避机制无限重试）
                ok = False
                logger.error(f"BDMV自动打包ISO：快通道推进打包状态异常：{err}")

            if ok:
                self._watch_fail = 0
            else:
                # 服务端不可用：连续失败到阈值就退避，避免每轮都刷日志。
                # 下次常规检查成功后会重新打开门闸。
                self._watch_fail += 1
                if self._watch_fail >= WATCH_FAIL_LIMIT:
                    logger.warning(
                        "BDMV自动打包ISO：服务端连续无响应，快通道退避至下次常规检查"
                    )
                    self._watch_active = False
                    return

        # 收尾重算：本轮可能已经打包完成，门闸应随之落下
        self.__refresh_watch_gate()

    def __advance_packing(
        self, names: List[str], submitted_map: Dict[str, Any]
    ) -> bool:
        """按服务端状态推进指定打包记录（快通道用）。

        与常规检查里的处理保持一致：仅在状态发生变化时落盘，
        打包完成后走 `__notify_done`（其中含 CD2 备份触发）。

        :param names: 待比对的资源目录名列表
        :param submitted_map: 已提交打包记录（就地更新）
        :return: 服务端状态是否取到（False 表示请求失败，调用方据此退避）
        """
        jobs = self.__fetch_status()
        if jobs is None:
            return False

        changed = False
        for name in names:
            job = jobs.get(name)
            if not isinstance(job, dict):
                continue

            job_status = job.get("status")

            if job_status == "done":
                if submitted_map.get(name, {}).get("status") != "done":
                    submitted_map[name] = {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "done",
                    }
                    changed = True
                    logger.info(f"BDMV自动打包ISO：打包完成（快通道）{name}")
                    if self._notify and self._notify_on_done:
                        self.__notify_done(name, job)
                continue

            if job_status in ("running", "queued"):
                if submitted_map.get(name, {}).get("status") != "running":
                    submitted_map[name] = {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "running",
                    }
                    changed = True

        if changed:
            self.save_data(SUBMITTED_DATA_KEY, submitted_map)
        return True

    def __process_iso_passthrough(
        self, iso_names: List[str], iso_hashes: Optional[Dict[str, str]] = None
    ) -> None:
        """处理 ISO 直通任务：命中直通分类的已完成任务直接触发 CD2 备份。

        这些站点的种子本身就是 ISO 文件，无需经过 BDMV to ISO 服务打包，
        只需触发对应源目录的 CD2 备份扫描即可同步到云端。

        :param iso_names: 命中 ISO 直通分类的已完成任务名列表
        :param iso_hashes: {任务名: 种子 hash}，用于去重（REPACK 重制版文件名相同、
            hash 不同，必须用 hash 区分，否则会被误判为「已提交」而漏触发）
        """
        if not iso_names:
            return

        if not self._iso_passthrough:
            logger.info(
                f"BDMV自动打包ISO：检测到 {len(iso_names)} 个 ISO 直通任务，但未启用 ISO 直通"
            )
            return

        if not self._cd2_enabled:
            logger.warning("BDMV自动打包ISO：检测到 ISO 直通任务，但未启用 CD2 备份，跳过")
            return

        if not self._cd2_iso_source_path:
            logger.warning("BDMV自动打包ISO：检测到 ISO 直通任务，但未配置 ISO 直通备份源路径，跳过")
            return

        iso_map: Dict[str, Any] = self.get_data(ISO_SUBMITTED_DATA_KEY) or {}
        if not isinstance(iso_map, dict):
            iso_map = {}

        iso_hashes = iso_hashes or {}

        # 过滤出尚未触发过的任务。
        #
        # 🔴 去重 key 用「种子 hash」而非「文件名」：
        # 站点的 REPACK 重制版会保留与原始版完全相同的 ISO 文件名（REPACK 只标在
        # 文件夹名上），若按文件名判重，重制版会被误判为「已提交」而漏触发 CD2。
        # hash 是种子的唯一指纹，REPACK = 新种子 = 新 hash，绝不会撞。
        # 取不到 hash 时回退文件名（兼容存量老记录 + 兜底）。
        pending_names = []
        for name in iso_names:
            hash_val = (iso_hashes.get(name) or "").strip()
            # 优先用 hash 判重：新记录 key = hash，老记录 key = 文件名，两者都查
            if hash_val:
                if (iso_map.get(hash_val) or {}).get("status") in ("submitted", "done"):
                    continue
                # 兼容：老记录可能仍以文件名做 key，且未存 hash，此时也视为已提交
                legacy = iso_map.get(name)
                if isinstance(legacy, dict) and not legacy.get("torrent_hash") \
                        and legacy.get("status") in ("submitted", "done"):
                    continue
            else:
                if (iso_map.get(name) or {}).get("status") in ("submitted", "done"):
                    continue
            pending_names.append(name)
        if not pending_names:
            return

        # 记录 ISO 文件名与大小（一次扫描即可同步整个源目录，故只触发一次）
        targets = self.__collect_iso_targets(pending_names)

        if not self.__trigger_cd2_backup(self._cd2_iso_source_path):
            logger.error(
                f"BDMV自动打包ISO：触发 ISO 直通 CD2 备份失败，本轮不记录："
                f"{'、'.join(pending_names)}"
            )
            return

        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for name in pending_names:
            iso_name, iso_size = targets.get(name, (name, 0))
            hash_val = (iso_hashes.get(name) or "").strip()
            # 新记录统一用 hash 做 key（无 hash 回退文件名）；value 里冗余存 name，
            # 供 CD2 上传校验与详情页展示继续按文件名工作。
            record_key = hash_val or name
            iso_map[record_key] = {
                "time": now_text,
                "status": "submitted",
                "iso_name": iso_name,
                "iso_size": iso_size,
                "torrent_hash": hash_val,
                "name": name,
            }
            # 记录触发时间与 ISO 信息，供后续校验上传是否完成
            self.__record_cd2_trigger(
                name,
                iso_name=iso_name,
                iso_size=iso_size,
                source_path=self._cd2_iso_source_path,
                kind=CD2_KIND_ISO,
            )
            logger.info(f"BDMV自动打包ISO：已触发 ISO 直通 CD2 备份 {name} -> {self._cd2_iso_source_path}")

        # 限制记录条数
        if len(iso_map) > ISO_SUBMITTED_LIMIT:
            keys = list(iso_map.keys())
            for key in keys[: len(iso_map) - ISO_SUBMITTED_LIMIT]:
                iso_map.pop(key, None)
        self.save_data(ISO_SUBMITTED_DATA_KEY, iso_map)

        if self._notify:
            self.__notify_iso_submitted(pending_names, targets)

    def __collect_iso_targets(self, names: List[str]) -> Dict[str, Tuple[str, int]]:
        """收集 ISO 直通任务的 ISO 文件名与字节数，用于后续校验云端文件。

        :param names: 需要收集的任务名列表
        :return: {任务名: (ISO 文件名, 字节数)}
        """
        targets: Dict[str, Tuple[str, int]] = {}
        if not names or not self._downloaders:
            return targets

        wanted = set(names)
        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        for _service_name, service_info in (services or {}).items():
            if not DownloaderHelper().is_downloader(
                service_type="qbittorrent", service=service_info
            ):
                continue
            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                continue
            try:
                result = downloader_obj.get_torrents()
            except Exception:
                continue
            torrents = result[0] if isinstance(result, tuple) else result
            for torrent in torrents or []:
                content_path = str(self.__torrent_field(torrent, "content_path", "") or "")
                if not content_path:
                    continue
                name = content_path.rstrip("/").split("/")[-1]
                if name not in wanted or name in targets:
                    continue
                try:
                    size = int(float(self.__torrent_field(torrent, "size", 0) or 0))
                except (TypeError, ValueError):
                    size = 0
                targets[name] = (name, size)

        # 未采集到的任务回退为任务名本身
        for name in names:
            if name not in targets:
                targets[name] = (name, 0)
        return targets

    def __process_bdmv_packaging(self, names: List[str]) -> None:
        """将命中的已完成原盘任务提交到 BDMV to ISO 服务打包。

        :param names: 待打包的资源目录名列表
        """
        if not names:
            return

        if not self._server_url:
            logger.warning("BDMV自动打包ISO：未配置服务地址，跳过打包检查")
            return

        # 1. 获取服务端资源目录
        resources = self.__fetch_resources()
        if resources is None:
            logger.warning("BDMV自动打包ISO：获取服务端资源目录失败，跳过打包检查")
            return

        if not resources:
            logger.info("BDMV自动打包ISO：服务端资源目录为空，跳过打包检查")
            return

        resource_set = set(resources)

        # 2. 获取服务端当前任务状态
        jobs = self.__fetch_status()
        if jobs is None:
            logger.warning("BDMV自动打包ISO：获取服务端任务状态失败，跳过打包检查")
            return

        # 3. 处理已完成任务
        submitted_map: Dict[str, Any] = self.get_data(SUBMITTED_DATA_KEY) or {}
        changed = False

        for name in names:
            # 未命中服务端资源目录
            if name not in resource_set:
                continue

            job = jobs.get(name)
            job_status = (job or {}).get("status")

            # 已在打包中或已完成，仅同步状态
            if job_status in ("running", "queued"):
                if submitted_map.get(name, {}).get("status") != "running":
                    submitted_map[name] = {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "running",
                    }
                    changed = True
                continue

            if job_status == "done":
                if submitted_map.get(name, {}).get("status") != "done":
                    submitted_map[name] = {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "done",
                    }
                    changed = True
                    if self._notify and self._notify_on_done:
                        self.__notify_done(name, job)
                continue

            # 已提交过且非失败状态，避免重复触发
            record = submitted_map.get(name)
            if record and record.get("status") in ("submitted", "running", "done"):
                continue

            # 未提交过，触发打包
            if not self._auto_convert:
                continue

            if self.__trigger_convert(name):
                submitted_map[name] = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "submitted",
                }
                changed = True
                logger.info(f"BDMV自动打包ISO：已提交打包任务 {name}")
                if self._notify:
                    self.__notify_submitted(name)

        # 4. 清理超量记录
        if changed:
            if len(submitted_map) > SUBMITTED_LIMIT:
                keys = list(submitted_map.keys())
                for key in keys[: len(submitted_map) - SUBMITTED_LIMIT]:
                    submitted_map.pop(key, None)
            self.save_data(SUBMITTED_DATA_KEY, submitted_map)

    @staticmethod
    def __torrent_field(torrent: Any, key: str, default: Any = None) -> Any:
        """兼容字典与对象两种形式读取种子字段。

        :param torrent: 种子对象或字典
        :param key: 字段名
        :param default: 缺省值
        :return: 字段值
        """
        if isinstance(torrent, dict):
            return torrent.get(key, default)
        return getattr(torrent, key, default)

    def __collect_completed_names(self) -> Optional[Tuple[List[str], List[str], Dict[str, str]]]:
        """收集下载器中的已完成任务，分为打包候选与 ISO 直通候选。

        命中 ISO 直通分类的任务只进入 ISO 直通候选，不再参与打包匹配。

        :return: (待打包目录名列表, ISO 直通任务名列表, {ISO 任务名: 种子 hash})，
            获取失败返回 None
        """
        if not self._downloaders:
            logger.warning("BDMV自动打包ISO：未配置下载器，跳过检查")
            return None

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("BDMV自动打包ISO：获取下载器实例失败，请检查配置")
            return None

        names: List[str] = []
        iso_names: List[str] = []
        iso_hashes: Dict[str, str] = {}
        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                logger.warning(f"BDMV自动打包ISO：下载器 {service_name} 不是 QB 类型，跳过")
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"BDMV自动打包ISO：下载器 {service_name} 未连接，跳过")
                continue

            try:
                # get_torrents 返回 (种子列表, 是否异常) 元组
                result = downloader_obj.get_torrents()
            except Exception as err:
                logger.error(f"BDMV自动打包ISO：获取 {service_name} 任务失败：{err}")
                continue

            if isinstance(result, tuple):
                torrents, error = result
                if error:
                    logger.error(f"BDMV自动打包ISO：获取 {service_name} 任务返回异常")
                    continue
            else:
                torrents = result

            for torrent in torrents or []:
                # 仅处理已完成任务
                try:
                    progress = float(self.__torrent_field(torrent, "progress", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if progress < 1:
                    continue

                torrent_category = str(self.__torrent_field(torrent, "category", None) or "").strip()

                # ISO 直通优先：命中直通分类则不参与打包匹配
                if (
                    self._iso_passthrough
                    and self._iso_categories
                    and torrent_category
                    and torrent_category in self._iso_categories
                ):
                    content_path = self.__torrent_field(torrent, "content_path", None) or ""
                    if not content_path:
                        continue
                    iso_name = str(content_path).rstrip("/").split("/")[-1]
                    if iso_name and iso_name not in iso_names:
                        iso_names.append(iso_name)
                        # 记录种子 hash，供去重（REPACK 重制版文件名相同、hash 不同）
                        iso_hashes[iso_name] = str(
                            self.__torrent_field(torrent, "hash", "") or ""
                        )
                    continue

                # 分类与标签双重匹配（两者都需命中）
                torrent_tags = self.__torrent_field(torrent, "tags", None) or []
                if isinstance(torrent_tags, str):
                    torrent_tags = [tag.strip() for tag in torrent_tags.split(",") if tag.strip()]
                tag_matched = any(tag in torrent_tags for tag in self._tags)

                category_matched = bool(torrent_category) and torrent_category in self._categories

                if not (tag_matched and category_matched):
                    continue

                # 取内容路径的目录名
                content_path = self.__torrent_field(torrent, "content_path", None) or ""
                if not content_path:
                    continue
                name = str(content_path).rstrip("/").split("/")[-1]
                if name and name not in names:
                    names.append(name)

        return names, iso_names, iso_hashes

    def __login(self) -> bool:
        """登录 BDMV to ISO 服务并保持会话。

        :return: 是否登录成功
        """
        if not self._server_url:
            return False

        # 每次登录使用全新的会话，避免残留失效 Cookie
        self._session = Session()
        try:
            res = RequestUtils(
                session=self._session,
                headers={
                    "User-Agent": "Mozilla/5.0 (MoviePilot-BdmvToIso)",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            ).post_res(
                url=f"{self._server_url}/login",
                data={"username": self._username, "password": self._password},
            )
        except Exception as err:
            logger.error(f"BDMV自动打包ISO：登录服务失败：{err}")
            self._session = None
            return False

        if not res:
            logger.error("BDMV自动打包ISO：登录服务无响应")
            self._session = None
            return False

        # 登录成功时服务端返回 302 并下发 session Cookie
        if not self._session.cookies.get("session"):
            logger.error("BDMV自动打包ISO：登录服务未返回 session cookie，请检查账号密码")
            self._session = None
            return False

        return True

    def __ensure_session(self, force_refresh: bool = False) -> bool:
        """确保存在可用的登录会话，必要时重新登录。

        :param force_refresh: 是否强制重新登录
        :return: 会话是否可用
        """
        if force_refresh or self._session is None:
            return self.__login()
        return True

    def __request(self, method: str, path: str, **kwargs) -> Optional[Any]:
        """使用当前会话请求服务端接口，会话失效时自动重登一次。

        :param method: HTTP 方法，支持 get 或 post
        :param path: 相对服务地址的路径
        :param kwargs: 传递给 RequestUtils 的额外参数
        :return: 响应对象，失败返回 None
        """
        for attempt in range(2):
            if not self.__ensure_session(force_refresh=attempt > 0):
                return None

            try:
                utils = RequestUtils(session=self._session)
                if method == "post":
                    res = utils.post_res(url=f"{self._server_url}{path}", **kwargs)
                else:
                    res = utils.get_res(url=f"{self._server_url}{path}", **kwargs)
            except Exception as err:
                logger.error(f"BDMV自动打包ISO：请求 {path} 失败：{err}")
                return None

            if not res:
                continue

            # 会话失效，丢弃后重试一次
            if res.status_code in (401, 403):
                self._session = None
                continue

            return res

        return None

    def __fetch_status(self) -> Optional[Dict[str, Any]]:
        """获取服务端所有打包任务状态。

        :return: 任务字典，失败返回 None
        """
        res = self.__request("get", "/api/status")
        if not res:
            logger.warning("BDMV自动打包ISO：获取服务端任务状态失败")
            return None

        try:
            data = res.json()
        except Exception as err:
            logger.error(f"BDMV自动打包ISO：解析任务状态失败：{err}")
            return None

        return data.get("jobs") or {}

    def __fetch_resources(self) -> Optional[List[str]]:
        """获取服务端资源目录列表。

        :return: 目录名列表，失败返回 None
        """
        res = self.__request("get", "/")
        if not res:
            logger.warning("BDMV自动打包ISO：获取服务端资源目录失败")
            return None

        return self.__parse_resources(res.text or "")

    @staticmethod
    def __parse_resources(html: str) -> List[str]:
        """从首页 HTML 中解析资源目录名。

        :param html: 首页 HTML 文本
        :return: 目录名列表
        """
        import re
        from html import unescape

        names: List[str] = []
        # 优先使用 data-bdmv-ok-summary 属性
        for match in re.finditer(r'data-bdmv-ok-summary="([^"]*)"', html):
            name = unescape(match.group(1)).strip()
            if name and name not in names:
                names.append(name)

        if names:
            return names

        # 回退：使用 data-job-surface 属性
        for match in re.finditer(r'data-job-surface="([^"]*)"', html):
            name = unescape(match.group(1)).strip()
            if name and name not in names:
                names.append(name)

        return names

    def __trigger_convert(self, name: str) -> bool:
        """触发指定资源目录的打包。

        :param name: 资源目录名
        :return: 是否提交成功
        """
        res = self.__request(
            "post",
            "/convert",
            data={"name": name},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not res:
            logger.error(f"BDMV自动打包ISO：提交打包任务失败 {name}")
            return False

        # 302 重定向表示提交成功
        if res.status_code in (200, 302):
            return True

        logger.error(f"BDMV自动打包ISO：提交打包任务返回异常状态码 {res.status_code}")
        return False

    @staticmethod
    def __is_cd2_auth_error(err: Exception) -> bool:
        """判断 CD2 调用异常是否为认证失效。

        CD2 的 token 有有效期，插件进程长期运行时缓存的客户端可能已失效，
        此时 gRPC 返回 UNAUTHENTICATED，需要重新认证后重试。

        :param err: 捕获到的异常
        :return: 是否为认证失效错误
        """
        try:
            code = err.code()
        except Exception:
            code = None
        if code is not None and "UNAUTHENTICATED" in str(code):
            return True
        text = str(err)
        return "UNAUTHENTICATED" in text or "Invalid auth token" in text

    def __get_cd2_client(self) -> Optional[Any]:
        """获取已认证的 CloudDrive2 gRPC 客户端。

        :return: 客户端实例，失败返回 None
        """
        if not self._cd2_enabled:
            return None

        if not self._cd2_address:
            logger.warning("BDMV自动打包ISO：未配置 CD2 服务地址，跳过备份触发")
            return None

        # 复用已认证的客户端
        if self._cd2_client is not None:
            return self._cd2_client

        return self.__create_cd2_client()

    def __create_cd2_client(self) -> Optional[Any]:
        """新建并认证一个 CD2 客户端。

        :return: 客户端实例，失败返回 None
        """
        try:
            from clouddrive2_client import CloudDriveClient
        except ImportError:
            logger.error("BDMV自动打包ISO：未安装 clouddrive2-client，无法触发 CD2 备份")
            return None

        try:
            client = CloudDriveClient(address=self._cd2_address)
            if not client.authenticate(self._cd2_username, self._cd2_password):
                logger.error("BDMV自动打包ISO：CD2 认证失败，请检查账号密码")
                try:
                    client.close()
                except Exception:
                    pass
                return None
        except Exception as err:
            logger.error(f"BDMV自动打包ISO：连接 CD2 服务失败：{err}")
            return None

        self._cd2_client = client
        return client

    def __renew_cd2_client(self) -> Optional[Any]:
        """关闭旧客户端并重新认证，用于 token 失效后重试。

        :return: 新的客户端实例，失败返回 None
        """
        self.__close_cd2_client()
        logger.info("BDMV自动打包ISO：CD2 认证已失效，正在重新认证")
        return self.__create_cd2_client()

    def __trigger_cd2_backup(self, source_path: str = "") -> bool:
        """触发 CloudDrive2 备份扫描，将新生成的 ISO 同步到云端。

        认证失效时自动重新认证并重试一次。

        :param source_path: 备份源路径，缺省使用打包输出目录
        :return: 是否触发成功
        """
        path = str(source_path or self._cd2_source_path or "").strip().rstrip("/")
        if not path:
            logger.warning("BDMV自动打包ISO：未配置 CD2 备份源路径，跳过备份触发")
            return False

        for attempt in range(2):
            client = self.__get_cd2_client()
            if client is None:
                return False

            try:
                from clouddrive2_client.proto import clouddrive_pb2 as pb

                metadata = client._create_authorized_metadata()
                client.stub.BackupRestartWalkingThrough(
                    pb.StringValue(value=path),
                    metadata=metadata,
                )
            except Exception as err:
                # 认证失效：重新认证后重试一次
                if attempt == 0 and self.__is_cd2_auth_error(err):
                    logger.warning(f"BDMV自动打包ISO：触发 CD2 备份认证失效，准备重试：{err}")
                    if self.__renew_cd2_client() is None:
                        return False
                    continue
                logger.error(f"BDMV自动打包ISO：触发 CD2 备份失败：{err}")
                # 连接可能已失效，下次重新建立
                self.__close_cd2_client()
                return False

            logger.info(f"BDMV自动打包ISO：已触发 CD2 备份扫描 {path}")
            return True

        return False

    def __record_cd2_trigger(
        self,
        name: str,
        iso_name: str = "",
        iso_size: int = 0,
        source_path: str = "",
        kind: str = CD2_KIND_BDMV,
    ) -> None:
        """记录 CD2 备份触发信息，用于后续检测同步是否完成。

        :param name: 资源目录名或 ISO 文件名
        :param iso_name: 输出 ISO 文件名（用于校验云端文件是否已存在）
        :param iso_size: 输出 ISO 字节数（用于校验云端文件大小是否一致）
        :param source_path: 本次触发的 CD2 备份源路径（缺省为打包输出目录）
        :param kind: 记录类型，打包（bdmv）或 ISO 直通（iso）
        """
        pending = self.get_data(CD2_PENDING_DATA_KEY) or {}
        if not isinstance(pending, dict):
            pending = {}
        pending[name] = {
            "trigger_ts": int(datetime.now().timestamp()),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "iso_name": iso_name,
            "iso_size": int(iso_size or 0),
            "source_path": str(source_path or self._cd2_source_path or "").strip().rstrip("/"),
            "kind": kind,
        }
        # 限制记录条数
        if len(pending) > CD2_PENDING_LIMIT:
            keys = list(pending.keys())
            for key in keys[: len(pending) - CD2_PENDING_LIMIT]:
                pending.pop(key, None)
        self.save_data(CD2_PENDING_DATA_KEY, pending)

    def __get_cd2_upload_progress(self, iso_name: str) -> Optional[Dict[str, Any]]:
        """查询指定文件在 CD2 中的上传任务状态。

        CD2 的 BackupStatus 不提供上传进度，但可通过 GetUploadFileList 查询
        上传任务列表，其中包含目标路径、总大小、已传输字节数与状态。

        :param iso_name: ISO 文件名
        :return: 上传任务信息字典；无匹配任务返回 None
        """
        if not iso_name:
            return None

        client = self.__get_cd2_client()
        if client is None:
            return None

        try:
            from clouddrive2_client.proto import clouddrive_pb2 as pb

            metadata = client._create_authorized_metadata()
            # 用文件名作为过滤关键词，减少返回条数
            request = pb.GetUploadFileListRequest(
                getAll=False,
                itemsPerPage=50,
                pageNumber=0,
                filter=iso_name,
            )
            result = client.stub.GetUploadFileList(request, metadata=metadata)
        except Exception as err:
            if self.__is_cd2_auth_error(err):
                logger.warning(f"BDMV自动打包ISO：查询上传任务认证失效：{err}")
                self.__close_cd2_client()
            else:
                logger.warning(f"BDMV自动打包ISO：查询上传任务失败：{err}")
            return None

        for item in result.uploadFiles:
            # 目标路径需以该 ISO 文件名结尾，避免同名前缀误匹配
            if not str(item.destPath or "").endswith(iso_name):
                continue
            return {
                "size": int(item.size or 0),
                "transfered": int(item.transferedBytes or 0),
                "status": str(item.status or ""),
                "status_enum": int(item.statusEnum or 0),
                "operator_type": int(item.operatorType or 0),
            }

        return None

    def __get_cd2_cloud_file_size(self, iso_name: str, expect_size: int = 0) -> Optional[int]:
        """查询云端目标目录中指定文件的大小。

        用于校验 ISO 是否已完整上传到云端（CD2 的 BackupStatus 不提供上传进度）。
        按文件名匹配不到时，若提供了 expect_size，则退化为查找大小一致的 `.iso`
        文件（兼容云端文件名与本地不完全一致的情况）。

        :param iso_name: ISO 文件名
        :param expect_size: 期望字节数，用于文件名匹配失败时的兜底匹配
        :return: 文件字节数；文件不存在返回 None
        """
        if not iso_name or not self._cd2_destination_path:
            return None

        client = self.__get_cd2_client()
        if client is None:
            return None

        fallback: Optional[int] = None
        try:
            from clouddrive2_client.proto import clouddrive_pb2 as pb

            metadata = client._create_authorized_metadata()
            stream = client.stub.GetSubFiles(
                pb.ListSubFileRequest(path=self._cd2_destination_path),
                metadata=metadata,
            )
            for reply in stream:
                for item in reply.subFiles:
                    if item.isDirectory:
                        continue
                    if item.name == iso_name:
                        return int(item.size)
                    # 兜底：大小一致且扩展名为 .iso 的文件
                    if (
                        expect_size
                        and fallback is None
                        and str(item.name).lower().endswith(".iso")
                        and int(item.size) == int(expect_size)
                    ):
                        fallback = int(item.size)
        except Exception as err:
            if self.__is_cd2_auth_error(err):
                logger.warning(f"BDMV自动打包ISO：查询云端目录认证失效：{err}")
                self.__close_cd2_client()
            else:
                logger.warning(f"BDMV自动打包ISO：查询云端目录失败：{err}")
            return None

        return fallback

    def __check_cd2_finished(self) -> None:
        """检查已触发的 CD2 备份进度，并在关键阶段发送通知。

        CD2 的 BackupStatus 只提供 lastFinishTime，其语义为「扫描完成时间」，
        并非「上传完成时间」，扫描结束后文件仍可能长时间上传中。

        两个层次的状态必须分开看（2026-09-22 修正，v1.9.0 通知过早的根因）：

        **备份层**（`BackupStatus.status`）：
        - `WalkingThrough(1)`：源目录正在扫描 → 本轮不评估任何阶段。
          注意「源目录 walker」可能只花几百毫秒就结束（毕竟只读元数据），
          但它**不代表 CD2 已经开始传数据**。
        - `Scanned(4)` / `Finished(5)`：源目录扫描完成。

        **传输层**（`UploadFileInfo.statusEnum`，经 GetUploadFileList 查询）：
        - `等待预处理(0)` / `预处理中(1)` / `排队中(7)`：CD2 还没开始传数据，
          任务在自己的准备阶段 → **不发任何通知**，继续等待。
          （v1.9.0 之前把这些阶段和「传输中」混为一谈，于是源目录 walker
          一结束就立刻宣称「源目录已扫描、正在上传」，而 CD2 侧任务仍显示准备中。）
        - `传输中(3)` / `已暂停(4)`：已进入传输。**且已传输字节 > 0** 时，
          才发送「扫描完成」通知（带上真实进度），每个任务只发一次。
        - `出错(9)` / `严重错误(10)`：只告警，保留记录等待重试或人工处理。
        - 其余（已完成/已跳过/已取消/已忽略）或查询不到任务：交给云端校验。

        判断顺序：
        1. 通过 GetUploadFileList 查询该 ISO 的传输任务，处于传输中且已动数据时
           发送「扫描完成」通知；
        2. 传输任务已结束（或查询不到）时，校验云端目标目录中文件是否已存在
           且大小与本地一致，确认后才发送「同步完成」通知。

        待完成记录按「CD2 备份源路径」分组，逐源查询扫描状态，
        以同时支持打包输出目录与 ISO 直通源目录。
        """
        if not self._cd2_enabled:
            return

        pending = self.get_data(CD2_PENDING_DATA_KEY) or {}
        if not isinstance(pending, dict) or not pending:
            return

        # 按备份源路径分组（旧记录无 source_path 时回退为打包输出目录）
        groups: Dict[str, List[str]] = {}
        changed = False
        for name, record in list(pending.items()):
            if not isinstance(record, dict):
                pending.pop(name, None)
                changed = True
                continue
            source = str(
                record.get("source_path") or self._cd2_source_path or ""
            ).strip().rstrip("/")
            if not source:
                continue
            groups.setdefault(source, []).append(name)

        finished: List[Tuple[str, Dict[str, Any], str]] = []
        for source, names in groups.items():
            status = self.__get_cd2_backup_status(source)
            if not status:
                continue

            # 备份层：源目录正在扫描时本轮不做任何评估。
            # 「源目录 walker」可能只跑几百毫秒，但它不等于 CD2 已开始传数据，
            # 更不能用上一轮的 lastFinishTime 当作本轮结果。
            if int(status.get("status") or 0) == CD2_STATUS_WALKING:
                logger.info(
                    f"BDMV自动打包ISO：CD2 正在扫描源目录 {source}，本轮不评估"
                )
                continue

            last_finish_ts = int(status.get("last_finish_ts") or 0)
            if not last_finish_ts:
                continue

            destination = status.get("destination") or self._cd2_destination_path

            for name in names:
                record = pending.get(name)
                if not isinstance(record, dict):
                    continue

                trigger_ts = int(record.get("trigger_ts") or 0)
                # 扫描完成时间需晚于触发时间，说明本次扫描已结束
                if not trigger_ts or last_finish_ts < trigger_ts:
                    continue

                iso_name = str(record.get("iso_name") or "")
                iso_size = int(record.get("iso_size") or 0)
                if not iso_name:
                    # 旧记录缺少文件名，无法校验，直接清理避免长期滞留
                    pending.pop(name, None)
                    changed = True
                    logger.info(f"BDMV自动打包ISO：CD2 记录缺少 ISO 文件名，跳过校验 {name}")
                    continue

                # 先查传输任务：源目录扫描完成 ≠ CD2 已开始传输。
                # 任务处于「预处理中/排队中」时依旧不能发「扫描完成」通知，
                # 否则 CD2 侧还显示准备中，通知却已经宣称开始上传了。
                progress = self.__get_cd2_upload_progress(iso_name)
                if progress is not None:
                    status_enum = int(progress.get("status_enum") or 0)
                    transfered = int(progress.get("transfered") or 0)
                    total = int(progress.get("size") or 0)
                    state_text = CD2_UPLOAD_STATE_TEXT.get(status_enum, "未知状态")

                    if status_enum in CD2_UPLOAD_PREPARE_STATES:
                        logger.info(
                            f"BDMV自动打包ISO：CD2 尚未开始传输 {iso_name}"
                            f"（{state_text}），继续等待"
                        )
                        continue

                    if status_enum in CD2_UPLOAD_ERROR_STATES:
                        logger.warning(
                            f"BDMV自动打包ISO：云端上传出错 {iso_name} - {progress.get('status')}"
                        )
                        continue

                    if status_enum in CD2_UPLOAD_TRANSFER_STATES:
                        # CD2 的 transferedBytes 偶发异常（如任务重建后计数重置），
                        # 用云端实际文件大小交叉校验，取两者较小值作为可信进度
                        cloud_size = self.__get_cd2_cloud_file_size(iso_name, iso_size) or 0
                        if cloud_size > 0 and cloud_size < transfered:
                            logger.info(
                                f"BDMV自动打包ISO：上传进度异常，改用云端文件大小 "
                                f"({cloud_size} < {transfered})"
                            )
                            transfered = cloud_size
                        percent = (transfered / total * 100) if total else 0
                        logger.info(
                            f"BDMV自动打包ISO：云端上传进行中 {iso_name} "
                            f"({state_text}, {transfered}/{total} 字节, {percent:.1f}%)"
                        )
                        # 已传输字节仍是 0：任务只是建好了，CD2 还没真的动数据
                        if transfered <= 0:
                            continue
                        # 确认 CD2 真的在传，此时宣布「正在上传」才成立
                        if not record.get("scan_notified"):
                            record["scan_notified"] = True
                            changed = True
                            if self._notify:
                                self.__notify_cd2_scan_done(
                                    name, destination, last_finish_ts, record, progress
                                )
                        continue

                # 上传任务已结束（或查询不到），再校验云端文件是否已完整上传
                cloud_size = self.__get_cd2_cloud_file_size(iso_name, iso_size)
                if cloud_size is None:
                    # 文件尚未出现在云端，继续等待
                    continue
                if iso_size and cloud_size != iso_size:
                    # 文件存在但大小不一致，说明仍在上传中
                    logger.info(
                        f"BDMV自动打包ISO：云端文件仍在上传 {iso_name} "
                        f"({cloud_size}/{iso_size} 字节)"
                    )
                    continue

                finished.append((name, record, destination))
                pending.pop(name, None)
                changed = True

        # 保存通知标记与清理结果
        if changed:
            self.save_data(CD2_PENDING_DATA_KEY, pending)

        if not finished:
            return

        for name, record, destination in finished:
            finish_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"BDMV自动打包ISO：CD2 备份已完成 {name} - {finish_time}")
            if not self._notify:
                continue

            kind = str(record.get("kind") or CD2_KIND_BDMV)
            cn_title = self.__get_cn_title(name)
            en_title = self.__get_en_title(name)
            site_name = self.__get_site_name(name)
            seed_title = self.__get_seed_title(name)

            if kind == CD2_KIND_ISO:
                lines = ["☁️ UHD 原盘 ISO 云端同步完成", ""]
            else:
                lines = ["☁️ BDMV 原盘云端同步完成", ""]
            lines.append("▎✅ 已同步到云端")
            lines.append(f"▎中文标题：{cn_title or name}")
            if en_title:
                lines.append(f"▎英文标题：{en_title}")
            if cn_title:
                lines.append(f"▎种子标题：{seed_title}")
            if site_name:
                lines.append(f"▎站点：{site_name}")
            lines.append("")
            lines.append("📂 云端目录")
            lines.append(f"　　{destination}")
            lines.append(f"🕐 完成时间：{finish_time}")

            self.post_message(
                mtype=NotificationType.Plugin,
                title="【BDMV自动打包ISO】",
                text="\n".join(lines),
            )

    def __get_cd2_backup_status(self, source_path: str = "") -> Optional[Dict[str, Any]]:
        """查询 CD2 备份的当前状态。

        认证失效时自动重新认证并重试一次。

        :param source_path: 备份源路径，缺省使用打包输出目录
        :return: 状态字典，失败返回 None
        """
        path = str(source_path or self._cd2_source_path or "").strip().rstrip("/")
        if not path:
            return None

        status = None
        for attempt in range(2):
            client = self.__get_cd2_client()
            if client is None:
                return None

            try:
                from clouddrive2_client.proto import clouddrive_pb2 as pb

                metadata = client._create_authorized_metadata()
                status = client.stub.BackupGetStatus(
                    pb.StringValue(value=path),
                    metadata=metadata,
                )
            except Exception as err:
                # 认证失效：重新认证后重试一次
                if attempt == 0 and self.__is_cd2_auth_error(err):
                    logger.warning(f"BDMV自动打包ISO：查询 CD2 状态认证失效，准备重试：{err}")
                    if self.__renew_cd2_client() is None:
                        return None
                    continue
                logger.error(f"BDMV自动打包ISO：查询 CD2 备份状态失败：{err}")
                self.__close_cd2_client()
                return None
            break

        if status is None:
            return None

        # 取目标端最后完成时间与目标路径
        last_finish_ts = 0
        destination = ""
        for dest in status.backup.destinations:
            if not dest.isEnabled:
                continue
            if not destination:
                destination = dest.destinationPath or ""
            if dest.HasField("lastFinishTime"):
                ts = int(dest.lastFinishTime.seconds)
                if ts > last_finish_ts:
                    last_finish_ts = ts

        return {
            "status": int(status.status),
            "status_text": CD2_STATUS_TEXT.get(int(status.status), str(status.status)),
            "message": status.statusMessage or "",
            "last_finish_ts": last_finish_ts,
            "destination": destination,
        }

    def __close_cd2_client(self) -> None:
        """关闭并释放 CD2 客户端连接。"""
        if self._cd2_client is not None:
            try:
                self._cd2_client.close()
            except Exception:
                pass
        self._cd2_client = None

    @staticmethod
    def __normalize_title(text: str) -> str:
        """归一化标题用于模糊匹配。

        站点列表页可能把标点替换为空格（如「5.1」显示为「5 1」），
        因此比较时统一去除所有非字母数字字符并转小写。

        :param text: 原始标题
        :return: 归一化后的标题
        """
        return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', str(text or '').lower())

    def __get_uhd_record(self, name: str) -> Dict[str, Any]:
        """从 UHD原盘自动下载 插件的记录中查询资源目录对应的条目。

        优先精确匹配，失败时按归一化标题模糊匹配，
        以兼容站点列表页与 QB 任务名之间的标点差异；
        再退化为包含匹配，以兼容 QB 任务名带中文前缀
        （如「93航班.United.93.2006...」）而记录标题仅含英文的情形。

        :param name: 资源目录名（QB 任务名）
        :return: 记录字典；未找到返回空字典
        """
        try:
            processed_map = self.get_data(
                UHD_PROCESSED_DATA_KEY, plugin_id=UHD_PLUGIN_ID
            ) or {}
        except Exception as err:
            logger.warning(f"BDMV自动打包ISO：读取中文标题失败：{err}")
            return {}

        target = self.__normalize_title(name)
        if not target:
            return {}

        # 第零轮：优先按 UHD 插件记录的 QB 任务名匹配（最准确）
        for record in processed_map.values():
            if not isinstance(record, dict):
                continue
            qb_name = str(record.get("qb_name") or "")
            if not qb_name:
                continue
            if qb_name == name:
                return record
            if self.__normalize_title(qb_name) == target:
                return record

        # 第一轮：精确匹配与归一化全等匹配
        for record in processed_map.values():
            if not isinstance(record, dict):
                continue
            title = str(record.get("title") or "")
            if not title:
                continue
            if title == name:
                return record
            if self.__normalize_title(title) == target:
                return record

        # 第二轮：包含匹配（记录标题为资源目录名的子串）
        # 取最长匹配，避免短标题误命中
        best_record: Dict[str, Any] = {}
        best_len = 0
        for record in processed_map.values():
            if not isinstance(record, dict):
                continue
            title = str(record.get("title") or "")
            if not title:
                continue
            norm_title = self.__normalize_title(title)
            if len(norm_title) < 8:
                # 过短的标题容易误匹配，跳过
                continue
            if norm_title in target and len(norm_title) > best_len:
                best_record = record
                best_len = len(norm_title)
        if best_record:
            return best_record

        # 第三轮：分词匹配（兼容版本标记等差异）
        # 例如 QB 任务名「惊声尖笑6.Scary.Movie.2026.2160p...」
        # 与记录标题「Scary Movie 2026 V1 2160p...」仅差 V1 标记，
        # 此时按英文关键词重合度匹配。
        target_tokens = self.__title_tokens(name)
        if not target_tokens:
            return {}

        best_record = {}
        best_score = 0.0
        for record in processed_map.values():
            if not isinstance(record, dict):
                continue
            title = str(record.get("title") or "")
            if not title:
                continue
            rec_tokens = self.__title_tokens(title)
            if not rec_tokens:
                continue
            # 以记录标题为基准计算覆盖率，避免长任务名稀释
            common = target_tokens & rec_tokens
            score = len(common) / len(rec_tokens)
            # 覆盖率需足够高，且共同关键词数量足够，避免误匹配
            if score >= 0.8 and len(common) >= 4 and score > best_score:
                best_record = record
                best_score = score
        return best_record

    @staticmethod
    def __title_tokens(text: str) -> set:
        """将标题拆分为用于匹配的英文关键词集合。

        过滤掉分辨率、编码、音轨等通用技术词与版本标记，
        仅保留片名、年份、发布组等有区分度的关键词。

        :param text: 原始标题
        :return: 关键词集合
        """
        # 通用技术词与版本标记，不参与匹配
        stop_words = {
            "uhd", "bluray", "blu", "ray", "bd", "remux", "web", "dl", "webdl",
            "hdr", "hdr10", "dovi", "dv", "sdr", "hevc", "avc", "h264", "h265",
            "x264", "x265", "truehd", "atmos", "dts", "dtshd", "ma", "ddp", "dd",
            "ac3", "flac", "aac", "lpcm", "pcm", "5", "1", "7", "2", "0", "51", "71",
            "2160p", "1080p", "720p", "4k", "8bit", "10bit", "v1", "v2", "v3",
            "repack", "proper", "internal", "complete", "usa", "eur", "ger",
            "aus", "ita", "hkg", "jpn", "fra", "cc", "criterion", "collection",
        }
        tokens = re.findall(r'[a-z0-9]+', str(text or '').lower())
        return {t for t in tokens if t not in stop_words and len(t) >= 2}

    def __get_cn_title(self, name: str) -> str:
        """查询资源目录对应的中文标题。

        优先返回完整副标题（含制作说明），缺失时回退到提取的中文名。

        :param name: 资源目录名（QB 任务名）
        :return: 中文标题；未找到返回空字符串
        """
        record = self.__get_uhd_record(name)
        if not record:
            return ""
        # 优先使用完整副标题
        subtitle = str(record.get("subtitle") or "").strip()
        if subtitle:
            return subtitle
        return str(record.get("cn_title") or "").strip()

    def __get_site_name(self, name: str) -> str:
        """查询资源目录对应的来源站点名称。

        :param name: 资源目录名（QB 任务名）
        :return: 站点名称；未找到返回空字符串
        """
        record = self.__get_uhd_record(name)
        if not record:
            return ""
        return str(record.get("site") or "").strip()

    def __get_en_title(self, name: str) -> str:
        """查询资源目录对应的英文标题。

        取自 UHD原盘自动下载 插件记录的站点资源主标题（title）。

        :param name: 资源目录名（QB 任务名）
        :return: 英文标题；未找到返回空字符串
        """
        record = self.__get_uhd_record(name)
        if not record:
            return ""
        return str(record.get("title") or "").strip()

    def __get_seed_title(self, name: str) -> str:
        """查询资源目录对应的种子标题。

        优先使用 UHD原盘自动下载 插件记录的种子标题（qb_name，
        来自站点详情页「下载」字段，与 QB 任务名一致），
        其次使用站点标题，最后回退到 QB 任务名。

        :param name: 资源目录名（QB 任务名）
        :return: 种子标题
        """
        record = self.__get_uhd_record(name)
        if record:
            # 优先：站点详情页「下载」字段（即 QB 任务名）
            qb_name = str(record.get("qb_name") or "").strip()
            if qb_name:
                return qb_name
            # 其次：站点列表页标题
            title = str(record.get("title") or "").strip()
            if title:
                return title
        return name

    def __notify_submitted(self, name: str) -> None:
        """发送打包任务已提交通知。

        :param name: 资源目录名
        """
        # 提交后立即查询一次状态，尽量取到源盘大小
        source_size = ""
        try:
            jobs = self.__fetch_status() or {}
            detail = (jobs.get(name) or {}).get("detail") or ""
            source_size = detail.replace("总大小：", "").strip()
        except Exception as err:
            logger.warning(f"BDMV自动打包ISO：获取源盘大小失败：{err}")

        cn_title = self.__get_cn_title(name)
        en_title = self.__get_en_title(name)
        site_name = self.__get_site_name(name)
        seed_title = self.__get_seed_title(name)

        lines = ["🚀 BDMV 原盘开始打包", ""]
        # 首行：状态图标 + 文字 + 源盘大小
        lines.append(f"▎🔄 打包中　{source_size}" if source_size else "▎🔄 打包中")
        # 中文标题，缺失时回退到原始目录名
        lines.append(f"▎中文标题：{cn_title or name}")
        # 英文标题（站点资源主标题）
        if en_title:
            lines.append(f"▎英文标题：{en_title}")
        # 种子标题（站点「下载」字段，即 QB 任务名）
        if cn_title:
            lines.append(f"▎种子标题：{seed_title}")
        if site_name:
            lines.append(f"▎站点：{site_name}")

        if self._cd2_enabled and self._cd2_source_path:
            lines.append("")
            lines.append("☁️ 云端同步")
            lines.append("　　⏸ 打包完成后自动触发")

        self.post_message(
            mtype=NotificationType.Plugin,
            title="【BDMV自动打包ISO】",
            text="\n".join(lines),
        )

    def __notify_iso_submitted(
        self, names: List[str], targets: Dict[str, Tuple[str, int]]
    ) -> None:
        """发送 ISO 直通已触发 CD2 备份的通知。

        :param names: 本轮触发 CD2 备份的任务名列表
        :param targets: {任务名: (ISO 文件名, 字节数)}
        """
        iso_files = [str((targets.get(n) or (n, 0))[0]) for n in names]
        total_size = 0
        for name in names:
            try:
                total_size += int((targets.get(name) or (name, 0))[1] or 0)
            except (TypeError, ValueError):
                continue

        lines = ["☁️ UHD 原盘 ISO 直通（跳过打包）", ""]
        lines.append("▎🚀 已触发 CD2 备份")
        if len(iso_files) == 1:
            name = names[0]
            lines.append(f"▎中文标题：{self.__get_cn_title(name) or name}")
            en_title = self.__get_en_title(name)
            if en_title:
                lines.append(f"▎英文标题：{en_title}")
            site_name = self.__get_site_name(name)
            if site_name:
                lines.append(f"▎站点：{site_name}")
            if total_size:
                lines.append(f"▎体积：{total_size / 1024 / 1024 / 1024:.2f} GB")
        else:
            lines.append(f"▎数量：{len(iso_files)} 个")
            shown = "、".join(iso_files[:3])
            if len(iso_files) > 3:
                shown += f" 等共 {len(iso_files)} 个"
            lines.append(f"▎ISO 文件：{shown}")

        lines.append("")
        lines.append("☁️ 云端同步")
        lines.append(f"　　📂 {self._cd2_iso_source_path}")
        lines.append(f"　　➡ {self._cd2_destination_path or '未配置'}")

        self.post_message(
            mtype=NotificationType.Plugin,
            title="【BDMV自动打包ISO】",
            text="\n".join(lines),
        )

    def __notify_done(self, name: str, job: Dict[str, Any]) -> None:
        """发送打包完成通知，并按需触发 CD2 备份。

        :param name: 资源目录名
        :param job: 任务状态字典
        """
        detail = job.get("detail") or ""
        out_iso = job.get("out_iso") or ""
        size_text = ""
        if job.get("out_iso_bytes_disk"):
            try:
                size_text = f"{float(job['out_iso_bytes_disk']) / 1024 / 1024 / 1024:.2f} GB"
            except (TypeError, ValueError):
                size_text = ""

        # 源大小去掉服务端前缀，仅保留数值部分
        source_size = detail.replace("总大小：", "").strip() if detail else ""

        cn_title = self.__get_cn_title(name)
        en_title = self.__get_en_title(name)
        site_name = self.__get_site_name(name)
        seed_title = self.__get_seed_title(name)

        lines = ["🎬 BDMV 原盘打包完成", ""]
        # 首行：绿色勾图标 + 源盘大小 → ISO 大小
        if source_size and size_text:
            lines.append(f"▎✅ {source_size} → {size_text}")
        elif size_text:
            lines.append(f"▎✅ {size_text}")
        else:
            lines.append("▎✅")
        # 中文标题，缺失时回退到原始目录名
        lines.append(f"▎中文标题：{cn_title or name}")
        # 英文标题（站点资源主标题）
        if en_title:
            lines.append(f"▎英文标题：{en_title}")
        # 种子标题（站点「下载」字段，即 QB 任务名）
        if cn_title:
            lines.append(f"▎种子标题：{seed_title}")
        if site_name:
            lines.append(f"▎站点：{site_name}")

        # 输出 ISO 文件名与大小（用于 CD2 上传完成校验）
        iso_name = str(out_iso).rstrip("/").split("/")[-1] if out_iso else ""
        iso_size = 0
        if job.get("out_iso_bytes_disk"):
            try:
                iso_size = int(float(job["out_iso_bytes_disk"]))
            except (TypeError, ValueError):
                iso_size = 0

        # 打包完成后触发 CD2 备份同步
        if self._cd2_enabled:
            lines.append("")
            lines.append("☁️ 云端同步")
            if self.__trigger_cd2_backup():
                # 记录触发时间与 ISO 信息，供后续校验上传是否完成
                self.__record_cd2_trigger(name, iso_name=iso_name, iso_size=iso_size)
                lines.append("　　✅ 已触发 CD2 备份")
                lines.append(f"　　📂 {self._cd2_source_path}")
            else:
                lines.append("　　❌ 触发失败，请检查 CD2 配置")

        if out_iso:
            # 服务端返回的是其容器内路径，转换为 CD2 侧可读路径
            if self._cd2_enabled and self._cd2_source_path:
                display_path = f"{self._cd2_source_path}/{iso_name}"
            else:
                display_path = out_iso
            lines.append("")
            lines.append("📄 输出文件")
            lines.append(f"　　{display_path}")

        self.post_message(
            mtype=NotificationType.Plugin,
            title="【BDMV自动打包ISO】",
            text="\n".join(lines),
        )

    def __notify_cd2_scan_done(
        self,
        name: str,
        destination: str,
        scan_ts: int,
        record: Optional[Dict[str, Any]] = None,
        progress: Optional[Dict[str, Any]] = None,
    ) -> None:
        """发送 CD2 扫描完成通知。

        只在 CD2 的传输任务确实已进入传输阶段（且已传输字节 > 0）时调用，
        因此通知里的「开始上传」与进度都是 CD2 的真实状态，而不是推测。

        :param name: 资源目录名或 ISO 文件名
        :param destination: 云端目标目录
        :param scan_ts: CD2 扫描完成时间戳（BackupDestination.lastFinishTime）
        :param record: CD2 待完成记录（用于区分打包与 ISO 直通）
        :param progress: CD2 传输任务信息（含 size / transfered / status_enum）
        """
        kind = str((record or {}).get("kind") or CD2_KIND_BDMV)
        cn_title = self.__get_cn_title(name)
        en_title = self.__get_en_title(name)
        site_name = self.__get_site_name(name)
        seed_title = self.__get_seed_title(name)
        scan_time = datetime.fromtimestamp(scan_ts).strftime("%Y-%m-%d %H:%M:%S")

        if kind == CD2_KIND_ISO:
            lines = ["☁️ UHD 原盘 ISO 云端扫描完成", ""]
        else:
            lines = ["☁️ BDMV 原盘云端扫描完成", ""]
        lines.append("▎🔍 源目录已扫描，CD2 正在上传")
        total = int((progress or {}).get("size") or 0)
        transfered = int((progress or {}).get("transfered") or 0)
        if total > 0 and transfered >= 0:
            lines.append(
                "▎上传进度：%.1f%%（%.2f GB / %.2f GB）" % (
                    transfered / total * 100,
                    transfered / 1024 / 1024 / 1024,
                    total / 1024 / 1024 / 1024,
                )
            )
        lines.append(f"▎中文标题：{cn_title or name}")
        if en_title:
            lines.append(f"▎英文标题：{en_title}")
        if cn_title:
            lines.append(f"▎种子标题：{seed_title}")
        if site_name:
            lines.append(f"▎站点：{site_name}")
        lines.append("")
        lines.append("📂 云端目录")
        lines.append(f"　　{destination}")
        lines.append(f"🕐 扫描时间：{scan_time}")

        self.post_message(
            mtype=NotificationType.Plugin,
            title="【BDMV自动打包ISO】",
            text="\n".join(lines),
        )

    @staticmethod
    def __status_text(status: str) -> str:
        """将服务端状态码转换为中文描述。

        :param status: 服务端状态码
        :return: 中文描述
        """
        mapping = {
            "running": "打包中",
            "done": "已完成",
            "error": "失败",
            "queued": "排队中",
        }
        return mapping.get(status, status or "-")

    def stop_service(self) -> None:
        """停止插件服务并释放会话与连接资源。"""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self.__close_cd2_client()
