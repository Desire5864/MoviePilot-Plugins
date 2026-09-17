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


class BdmvToIso(_PluginBase):
    """BDMV 原盘自动打包 ISO 插件。

    监控指定 QB 下载器中带指定标签的已完成任务，将下载目录名与
    自建「BDMV to ISO」服务的资源目录比对，命中后自动触发打包，
    并轮询打包进度，完成后发送通知。
    """

    # 插件名称
    plugin_name = "BDMV自动打包ISO"
    # 插件描述
    plugin_desc = "监控QB指定标签的已完成原盘，自动调用BDMV to ISO服务打包为ISO并通知。"
    # 插件图标
    plugin_icon = "UHD.png"
    # 插件版本
    plugin_version = "1.0.0"
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
    # 检查间隔（秒）
    _interval: int = 300
    # 是否自动触发打包
    _auto_convert: bool = True
    # 是否在打包完成后删除源 BDMV 目录（由服务端处理，此处仅记录意愿）
    _notify_on_done: bool = True
    # 与服务端保持的登录会话
    _session: Optional[Session] = None

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
        self._interval = 300
        self._auto_convert = True
        self._notify_on_done = True
        self._session = None

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

        try:
            self._interval = max(30, int(config.get("interval") or 300))
        except (TypeError, ValueError):
            self._interval = 300

        self._auto_convert = bool(config.get("auto_convert", True))
        self._notify_on_done = bool(config.get("notify_on_done", True))

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
                                "props": {"cols": 12},
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件会按设定间隔检查所选下载器中带指定标签的已完成任务，"
                                                    "将任务目录名与 BDMV to ISO 服务的资源目录比对，"
                                                    "命中后自动触发打包为 ISO，并轮询打包进度，"
                                                    "完成后发送通知。",
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
            "auto_convert": True,
            "notify_on_done": True,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: Vuetify 详情页结构
        """
        if not self._enabled:
            return None

        downloaders_text = "、".join(self._downloaders) if self._downloaders else "未配置"
        tags_text = "、".join(self._tags) if self._tags else "未配置"

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
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【BDMV自动打包ISO】",
                        text=f"已提交打包任务：\n{name}",
                    )

        # 5. 清理超量记录
        if changed:
            if len(submitted_map) > SUBMITTED_LIMIT:
                keys = list(submitted_map.keys())
                for key in keys[: len(submitted_map) - SUBMITTED_LIMIT]:
                    submitted_map.pop(key, None)
            self.save_data(SUBMITTED_DATA_KEY, submitted_map)

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
                torrents = downloader_obj.get_torrents() or []
            except Exception as err:
                logger.error(f"BDMV自动打包ISO：获取 {service_name} 任务失败：{err}")
                continue

            for torrent in torrents:
                # 仅处理已完成任务
                progress = float(getattr(torrent, "progress", 0) or 0)
                if progress < 1:
                    continue

                # 标签匹配
                torrent_tags = getattr(torrent, "tags", None) or []
                if isinstance(torrent_tags, str):
                    torrent_tags = [tag.strip() for tag in torrent_tags.split(",") if tag.strip()]
                if not any(tag in torrent_tags for tag in self._tags):
                    continue

                # 取内容路径的目录名
                content_path = getattr(torrent, "content_path", None) or ""
                if not content_path:
                    continue
                name = content_path.rstrip("/").split("/")[-1]
                if name:
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

    def __notify_done(self, name: str, job: Dict[str, Any]) -> None:
        """发送打包完成通知。

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

        lines = [f"资源目录：{name}"]
        if detail:
            lines.append(f"源大小：{detail}")
        if size_text:
            lines.append(f"ISO 大小：{size_text}")
        if out_iso:
            lines.append(f"输出路径：{out_iso}")

        self.post_message(
            mtype=NotificationType.Plugin,
            title="【BDMV自动打包ISO】打包完成",
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
        """停止插件服务并释放会话资源。"""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
