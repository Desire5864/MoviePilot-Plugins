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
# 已提交记录最多保留条数
SUBMITTED_LIMIT = 500
# 默认标签
DEFAULT_TAG = "UHD自动下载"
# 默认分类（与 UHD原盘自动下载 插件推送 QB 时使用的分类保持一致）
DEFAULT_CATEGORY = "彩虹岛&HR,OurBits原盘"
# UHD原盘自动下载 插件ID（用于读取中文标题映射）
UHD_PLUGIN_ID = "UhdBlurayAutoDownload"
# UHD原盘自动下载 插件的已处理记录键名
UHD_PROCESSED_DATA_KEY = "uhd_processed_map"
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


class BdmvToIso(_PluginBase):
    """BDMV 原盘自动打包 ISO 插件。

    监控指定 QB 下载器中带指定标签的已完成任务，将下载目录名与
    自建「BDMV to ISO」服务的资源目录比对，命中后自动触发打包，
    并轮询打包进度；打包完成后可自动触发 CloudDrive2 备份，
    将 ISO 同步上传到云端，并发送通知。
    """

    # 插件名称
    plugin_name = "BDMV自动打包ISO"
    # 插件描述
    plugin_desc = "监控QB指定标签的已完成原盘，自动打包为ISO，完成后触发CD2备份同步并通知。"
    # 插件图标
    plugin_icon = "UHD.png"
    # 插件版本
    plugin_version = "1.6.0"
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
    # 与服务端保持的登录会话
    _session: Optional[Session] = None
    # CD2 gRPC 客户端
    _cd2_client: Optional[Any] = None

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
        self._session = None
        self._cd2_client = None

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

        self._auto_convert = bool(config.get("auto_convert", True))
        self._notify_on_done = bool(config.get("notify_on_done", True))

        # CD2 备份配置
        self._cd2_enabled = bool(config.get("cd2_enabled"))
        self._cd2_address = str(config.get("cd2_address") or "").strip()
        self._cd2_username = str(config.get("cd2_username") or "").strip()
        self._cd2_password = str(config.get("cd2_password") or "")
        self._cd2_source_path = str(config.get("cd2_source_path") or "").strip().rstrip("/")

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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件会按设定间隔检查所选下载器中同时满足「分类」与「标签」的已完成任务，"
                                                    "将任务目录名与 BDMV to ISO 服务的资源目录比对，"
                                                    "命中后自动触发打包为 ISO，并轮询打包进度；"
                                                    "打包完成后自动触发 CD2 备份扫描，将 ISO 同步到云端，"
                                                    "并发送通知。",
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
                                            f"检查间隔：{self._interval} 秒；"
                                            f"自动打包：{'已启用' if self._auto_convert else '未启用'}",
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
                                        "text": f"已提交打包记录共 {len(submitted_map)} 条",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            submitted_rows = []
            for name, info in list(submitted_map.items())[-50:]:
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

        return page_content

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        services: List[Dict[str, Any]] = []
        if not self._enabled:
            return services

        if not self._server_url:
            logger.warning("BDMV自动打包ISO：未配置服务地址，跳过注册定时服务")
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

        return services

    def check_and_convert(self) -> None:
        """检查已完成任务并触发打包。"""
        if not self._enabled:
            return

        if not self._server_url:
            logger.warning("BDMV自动打包ISO：未配置服务地址，跳过检查")
            return

        # 1. 获取服务端资源目录
        resources = self.__fetch_resources()
        if resources is None:
            logger.warning("BDMV自动打包ISO：获取服务端资源目录失败，跳过检查")
            return

        if not resources:
            logger.info("BDMV自动打包ISO：服务端资源目录为空，跳过检查")
            return

        resource_set = set(resources)

        # 2. 获取服务端当前任务状态
        jobs = self.__fetch_status()
        if jobs is None:
            logger.warning("BDMV自动打包ISO：获取服务端任务状态失败，跳过检查")
            return

        # 3. 收集待打包的目录名
        candidates = self.__collect_completed_names()
        if candidates is None:
            logger.warning("BDMV自动打包ISO：获取下载器任务失败，跳过检查")
            return

        # 4. 处理已完成任务
        submitted_map: Dict[str, Any] = self.get_data(SUBMITTED_DATA_KEY) or {}
        changed = False

        for name in candidates:
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

        # 5. 清理超量记录
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

    def __collect_completed_names(self) -> Optional[List[str]]:
        """收集下载器中带指定标签的已完成任务目录名。

        :return: 目录名列表，获取失败返回 None
        """
        if not self._downloaders:
            logger.warning("BDMV自动打包ISO：未配置下载器，跳过检查")
            return None

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("BDMV自动打包ISO：获取下载器实例失败，请检查配置")
            return None

        names: List[str] = []
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

                # 分类与标签双重匹配（两者都需命中）
                torrent_tags = self.__torrent_field(torrent, "tags", None) or []
                if isinstance(torrent_tags, str):
                    torrent_tags = [tag.strip() for tag in torrent_tags.split(",") if tag.strip()]
                tag_matched = any(tag in torrent_tags for tag in self._tags)

                torrent_category = str(self.__torrent_field(torrent, "category", None) or "").strip()
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

        return names

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

    def __trigger_cd2_backup(self) -> bool:
        """触发 CloudDrive2 备份扫描，将新生成的 ISO 同步到云端。

        :return: 是否触发成功
        """
        client = self.__get_cd2_client()
        if client is None:
            return False

        if not self._cd2_source_path:
            logger.warning("BDMV自动打包ISO：未配置 CD2 备份源路径，跳过备份触发")
            return False

        try:
            from clouddrive2_client.proto import clouddrive_pb2 as pb

            metadata = client._create_authorized_metadata()
            client.stub.BackupRestartWalkingThrough(
                pb.StringValue(value=self._cd2_source_path),
                metadata=metadata,
            )
        except Exception as err:
            logger.error(f"BDMV自动打包ISO：触发 CD2 备份失败：{err}")
            # 连接可能已失效，下次重新建立
            self.__close_cd2_client()
            return False

        logger.info(f"BDMV自动打包ISO：已触发 CD2 备份扫描 {self._cd2_source_path}")
        return True

    def __get_cd2_backup_status(self) -> Optional[Dict[str, Any]]:
        """查询 CD2 备份的当前状态。

        :return: 状态字典，失败返回 None
        """
        client = self.__get_cd2_client()
        if client is None or not self._cd2_source_path:
            return None

        try:
            from clouddrive2_client.proto import clouddrive_pb2 as pb

            metadata = client._create_authorized_metadata()
            status = client.stub.BackupGetStatus(
                pb.StringValue(value=self._cd2_source_path),
                metadata=metadata,
            )
        except Exception as err:
            logger.error(f"BDMV自动打包ISO：查询 CD2 备份状态失败：{err}")
            self.__close_cd2_client()
            return None

        return {
            "status": int(status.status),
            "status_text": CD2_STATUS_TEXT.get(int(status.status), str(status.status)),
            "message": status.statusMessage or "",
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
        以兼容站点列表页与 QB 任务名之间的标点差异。

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
        for record in processed_map.values():
            if not isinstance(record, dict):
                continue
            title = str(record.get("title") or "")
            if not title:
                continue
            # 精确匹配优先
            if title == name:
                return record
            # 归一化模糊匹配
            if target and self.__normalize_title(title) == target:
                return record
        return {}

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
        site_name = self.__get_site_name(name)

        lines = ["🚀 BDMV 原盘开始打包", ""]
        # 首行：状态 + 源盘大小（与 UHD原盘自动下载 通知风格保持一致）
        lines.append(f"▎【打包中】{source_size}" if source_size else "▎【打包中】")
        # 中文标题，缺失时回退到原始目录名
        lines.append(f"▎中文标题：{cn_title or name}")
        if cn_title:
            lines.append(f"▎种子标题：{name}")
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
        site_name = self.__get_site_name(name)

        lines = ["🎬 BDMV 原盘打包完成", ""]
        # 首行：状态 + 源盘大小 → ISO 大小（与 UHD原盘自动下载 通知风格保持一致）
        if source_size and size_text:
            lines.append(f"▎【已完成】{source_size} → {size_text}")
        elif size_text:
            lines.append(f"▎【已完成】{size_text}")
        else:
            lines.append("▎【已完成】")
        # 中文标题，缺失时回退到原始目录名
        lines.append(f"▎中文标题：{cn_title or name}")
        if cn_title:
            lines.append(f"▎种子标题：{name}")
        if site_name:
            lines.append(f"▎站点：{site_name}")

        # 打包完成后触发 CD2 备份同步
        if self._cd2_enabled:
            lines.append("")
            lines.append("☁️ 云端同步")
            if self.__trigger_cd2_backup():
                lines.append("　　✅ 已触发 CD2 备份")
                lines.append(f"　　📂 {self._cd2_source_path}")
            else:
                lines.append("　　❌ 触发失败，请检查 CD2 配置")

        if out_iso:
            # 服务端返回的是其容器内路径，转换为 CD2 侧可读路径
            iso_name = str(out_iso).rstrip("/").split("/")[-1]
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
