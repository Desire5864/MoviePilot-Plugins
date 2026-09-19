import re
from datetime import datetime
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
# 自动下载任务标签
DOWNLOAD_TAG = "UHD自动下载"


class UhdBlurayAutoDownload(_PluginBase):
    """4K UHD BluRay 原盘自动下载插件。

    定时抓取彩虹岛与我堡站点的 UHD BluRay 原盘列表，筛选出未下载
    （站点进度列为 "-"）的种子，自动推送到 QB 下载器，并按站点
    指定分类与保存路径。
    """

    # 插件名称
    plugin_name = "UHD原盘自动下载"
    plugin_desc = "监控彩虹岛/我堡最新4K UHD BluRay原盘，未下载的自动推送QB。"
    # 插件图标
    plugin_icon = "UHD.png"
    # 插件版本
    plugin_version = "2.6.0"
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

    # 站点配置：域名 -> 列表页地址、QB分类、保存路径、筛选模式
    # filter_mode: "uhd_title" 按标题匹配 UHD BluRay；"bluray_only" 仅排除非原盘（站点已按媒介筛选）
    _site_configs = {
        "ptchdbits.co": {
            "name": "彩虹岛",
            "list_url": "https://ptchdbits.co/torrents.php?medium=19&sort=4&type=desc",
            "category": "彩虹岛&HR",
            "save_path": "/原盘",
            # 站点 medium=19 已按 UHD Blu-ray 媒介筛选，标题不一定含 UHD，
            # 因此仅排除 WEB-DL/HDTV/Encode 等非原盘
            "filter_mode": "bluray_only",
        },
        "ourbits.club": {
            "name": "我堡",
            "list_url": "https://ourbits.club/torrents.php?standard=5&sort=4&type=desc",
            "category": "OurBits原盘",
            "save_path": "/原盘",
            # 站点 standard=5 仅按 2160p 分辨率筛选，需按标题匹配 UHD BluRay
            "filter_mode": "uhd_title",
        },
    }

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _downloader: str = ""
    _interval_minutes: int = 15
    # 每个站点只采集最新 N 条
    _latest_count: int = 5
    _run_once: bool = False
    # 最近一次检查时间
    _last_check_time: Optional[str] = None
    # 最近一次检查错误
    _last_error: str = ""
    # 最近一次发现的种子明细
    _last_items: List[Dict[str, Any]] = []
    # 详情页副标题缓存：种子ID -> 完整副标题
    _subtitle_cache: Dict[str, str] = {}
    # 详情页简介缓存：种子ID -> 清洗后的简介
    _intro_cache: Dict[str, str] = {}

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
        self._interval_minutes = 15
        self._latest_count = 5
        self._run_once = False
        self._last_check_time = None
        self._last_error = ""
        self._last_items = []
        self._subtitle_cache = {}
        self._intro_cache = {}

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._downloader = str(config.get("downloader") or "").strip()
        try:
            self._interval_minutes = max(5, int(config.get("interval_minutes") or 15))
        except (TypeError, ValueError):
            self._interval_minutes = 15
        try:
            self._latest_count = max(1, int(config.get("latest_count") or 5))
        except (TypeError, ValueError):
            self._latest_count = 5
        self._run_once = bool(config.get("run_once"))

        # 立即执行一次：执行后自动关闭开关
        if self._run_once:
            logger.info("UHD原盘自动下载：立即执行一次检查")
            self.check_uhd()
            self.update_config(
                {
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "downloader": self._downloader,
                    "interval_minutes": self._interval_minutes,
                    "latest_count": self._latest_count,
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
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval_minutes",
                                            "label": "检查间隔（分钟）",
                                            "placeholder": "默认15，最小5",
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
                                            "model": "latest_count",
                                            "label": "每站采集最新条数",
                                            "placeholder": "默认5，只处理列表页最新N条",
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
                                            "text": "插件会定时抓取彩虹岛与我堡的 UHD BluRay 原盘列表，"
                                                    "只处理列表页最新的 N 条（默认 5 条），"
                                                    "筛选出站点进度列为「-」或「--」（未下载）的种子，"
                                                    "自动推送到 QB 下载器，并添加标签「UHD自动下载」。"
                                                    "彩虹岛使用分类「彩虹岛&HR」，我堡使用分类「OurBits原盘」，"
                                                    "保存路径均为「/原盘」。",
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
            "interval_minutes": 15,
            "latest_count": 5,
            "run_once": False,
        }

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

        # 最近发现的种子明细（按站点分组显示）
        if self._last_items:
            # 按站点分组
            site_groups: Dict[str, List[Dict[str, Any]]] = {}
            for item in self._last_items:
                site_name = str(item.get("site") or "未知站点")
                site_groups.setdefault(site_name, []).append(item)

            for site_name, group_items in site_groups.items():
                pushed_count = len([i for i in group_items if i.get("action") == "已推送"])
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
                                            "type": "success" if pushed_count else "info",
                                            "variant": "tonal",
                                            "text": f"【{site_name}】共 {len(group_items)} 个 UHD BluRay 原盘，"
                                                    f"本次推送 {pushed_count} 个",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                )

                # 使用卡片式布局，避免 VTable 单元格强制 nowrap 导致标题截断
                card_items = []
                for item in group_items:
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
                            "content": [
                                {
                                    "component": "div",
                                    "props": {
                                        "style": "white-space: normal; word-break: break-all; "
                                                 "font-size: 14px; font-weight: 600; line-height: 1.5;",
                                    },
                                    "text": str(item.get("subtitle") or ""),
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
                                            f"站点进度：{item.get('progress') or '-'}　|　"
                                            f"处理结果：{item.get('action') or '-'}",
                                },
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
                                "content": card_items,
                            }
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
        processed_map: Dict[str, Dict[str, Any]] = self.get_data(PROCESSED_DATA_KEY) or {}
        all_items: List[Dict[str, Any]] = []
        downloaded_items: List[Dict[str, Any]] = []

        for domain, site_conf in self._site_configs.items():
            try:
                items = self.__process_site(domain, site_conf, downloader_obj, processed_map)
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
        self.save_data(PROCESSED_DATA_KEY, processed_map)

        # 发送通知
        if self._notify and downloaded_items:
            lines = [f"🎬 已推送 {len(downloaded_items)} 个 UHD 原盘到 QB", ""]
            for item in downloaded_items[:20]:
                subtitle = item.get("subtitle") or ""
                title = item.get("title") or ""
                intro = item.get("intro") or ""
                size = item.get("size") or ""
                site = item.get("site") or ""
                # 中文标题（副标题，缺失时回退到种子标题）
                lines.append(f"▎中文标题：{subtitle or title}")
                # 种子标题
                if subtitle:
                    lines.append(f"▎种子标题：{title}")
                # 站点
                if site:
                    lines.append(f"▎站点：{site}")
                # 体积
                if size:
                    lines.append(f"▎体积：{size}")
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
            logger.info(f"UHD原盘自动下载：已清空 {cleared} 条旧版站点简介，将改用 TMDB 简介")

    def __fill_missing_subtitles(self, processed_map: Dict[str, Dict[str, Any]]) -> None:
        """补齐历史记录中缺失的副标题与简介。

        旧版本记录只保存了 title 与 cn_title，缺少完整副标题与简介。
        此处按记录 key 中的域名与种子 ID 从站点详情页补全，
        每次最多补全若干条，避免请求过多。

        :param processed_map: 已处理记录字典
        """
        # 每次最多补全的条数，避免一次性请求过多
        max_fill = 5
        filled = 0

        for record_key, record in processed_map.items():
            if filled >= max_fill:
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

            # 副标题与简介都齐全时跳过
            if record.get("subtitle") and record.get("intro"):
                continue

            if not record.get("subtitle"):
                subtitle = self.__fetch_detail_subtitle(site, torrent_id)
                if subtitle:
                    record["subtitle"] = subtitle
                    if not record.get("cn_title"):
                        record["cn_title"] = self.__extract_cn_title(subtitle)
                    filled += 1
                    logger.info(
                        f"UHD原盘自动下载：已补齐副标题 {domain}:{torrent_id} - {subtitle[:50]}"
                    )

            if not record.get("intro"):
                intro = self.__fetch_tmdb_intro(
                    str(record.get("title") or ""),
                    str(record.get("subtitle") or ""),
                )
                if intro:
                    record["intro"] = intro
                    filled += 1
                    logger.info(
                        f"UHD原盘自动下载：已补齐简介 {domain}:{torrent_id} - {intro[:50]}"
                    )

        if filled:
            logger.info(f"UHD原盘自动下载：本次共补齐 {filled} 条副标题/简介")

    def __fetch_detail_subtitle(self, site: Dict[str, Any], torrent_id: str) -> str:
        """从种子详情页获取完整副标题。

        列表页的副标题可能被站点截断（如彩虹岛显示为 "保留Dolb.."），
        详情页的"副标题"字段为完整内容。请求失败时重试一次，
        避免偶发超时导致回退到截断的列表页数据。

        :param site: 站点配置
        :param torrent_id: 种子 ID
        :return: 完整副标题；获取失败返回空字符串
        """
        if not torrent_id:
            return ""
        # 命中缓存直接返回，避免重复请求详情页
        if torrent_id in self._subtitle_cache:
            return self._subtitle_cache[torrent_id]

        base_url = (site.get("url") or "").rstrip("/")
        detail_url = f"{base_url}/details.php?id={torrent_id}"

        for attempt in range(2):
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
            # 定位"副标题"字段所在行的下一个单元格
            nodes = page.xpath(
                '//td[text()="副标题" or text()="副標題"]/following-sibling::td[1]'
            )
            if nodes:
                text = nodes[0].xpath('string(.)').strip()
                if text:
                    self._subtitle_cache[torrent_id] = text
                    return text
            # 页面正常但没有副标题字段，无需重试
            return ""

        logger.warning(f"UHD原盘自动下载：详情页副标题获取失败，已重试 {detail_url}")
        return ""

    def __fetch_tmdb_intro(self, title: str, subtitle: str = "") -> str:
        """通过 TMDB 识别媒体并获取影片简介。

        使用种子标题与副标题识别媒体，取 TMDB 的 overview 作为简介。
        识别失败或 TMDB 无简介时返回空字符串。

        :param title: 种子标题
        :param subtitle: 站点副标题（含中文名，有助于识别）
        :return: TMDB 影片简介；获取失败返回空字符串
        """
        if not title and not subtitle:
            return ""
        # 命中缓存直接返回，避免重复识别
        cache_key = f"{title}|{subtitle}"
        if cache_key in self._intro_cache:
            return self._intro_cache[cache_key]

        try:
            meta = MetaInfo(title=title, subtitle=subtitle)
            mediainfo = MediaChain().recognize_media(meta=meta)
        except Exception as err:
            logger.error(f"UHD原盘自动下载：TMDB 识别失败 {title[:60]}，{err}")
            return ""

        if not mediainfo:
            logger.warning(f"UHD原盘自动下载：TMDB 未识别到媒体 {title[:60]}")
            return ""

        intro = self.__clean_intro(mediainfo.overview or "")
        if intro:
            self._intro_cache[cache_key] = intro
            logger.info(
                f"UHD原盘自动下载：TMDB 简介获取成功 {mediainfo.title} - {intro[:40]}"
            )
        return intro

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

        # 抓取列表页
        try:
            res = RequestUtils(
                ua=site.get("ua"),
                cookies=site.get("cookie"),
                proxies=settings.PROXY if site.get("proxy") else None,
                timeout=site.get("timeout") or 20,
            ).get_res(url=site_conf.get("list_url"))
        except Exception as err:
            logger.error(f"UHD原盘自动下载：抓取 {site_name} 列表失败，{err}")
            return items

        if res is None or res.status_code != 200:
            logger.error(f"UHD原盘自动下载：抓取 {site_name} 列表失败，状态码 {res.status_code if res else 'None'}")
            return items

        # 解析种子列表（列表页已按发布时间倒序，只取最新 N 条）
        torrents = self.__parse_list_page(res.text, site_conf.get("filter_mode", "uhd_title"))
        torrents = torrents[:self._latest_count]
        logger.info(f"UHD原盘自动下载：{site_name} 取最新 {len(torrents)} 个 UHD BluRay 原盘")

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
                "action": "",
            }

            # 列表页副标题可能被站点截断（如彩虹岛显示为 "保留Dolb.."），
            # 统一从详情页获取完整副标题
            detail_subtitle = self.__fetch_detail_subtitle(site, torrent_id)
            if detail_subtitle:
                item["subtitle"] = detail_subtitle
                torrent["subtitle"] = detail_subtitle

            # 从 TMDB 获取影片简介
            tmdb_intro = self.__fetch_tmdb_intro(title, item.get("subtitle") or "")
            if tmdb_intro:
                item["intro"] = tmdb_intro

            # 站点进度判断：
            # "-"（我堡）或 "--"（彩虹岛）表示未下载，需要推送；
            # "0%"~"99%" 表示正在下载中，跳过；
            # "100%" 表示已下载完成，跳过。
            if progress not in ("-", "--"):
                if progress == "100%":
                    item["action"] = "已下载完成，跳过"
                elif re.match(r'^\d+(\.\d+)?%$', progress):
                    item["action"] = f"下载中（{progress}），跳过"
                else:
                    item["action"] = "已下载，跳过"
                items.append(item)
                continue

            # 已处理过则跳过，但补齐缺失的副标题与站点（兼容旧版本记录）
            if record_key in processed_map:
                record = processed_map[record_key]
                if isinstance(record, dict):
                    if not record.get("site"):
                        record["site"] = site_name
                    if not record.get("subtitle"):
                        current_subtitle = item.get("subtitle") or ""
                        if current_subtitle:
                            record["subtitle"] = current_subtitle
                            if not record.get("cn_title"):
                                record["cn_title"] = self.__extract_cn_title(current_subtitle)
                            logger.info(
                                f"UHD原盘自动下载：已补齐副标题 {site_name} - {title[:60]}"
                            )
                item["action"] = "已处理，跳过"
                items.append(item)
                continue

            # 下载种子文件并推送到 QB
            success = self.__download_and_push(
                site=site,
                site_conf=site_conf,
                torrent=torrent,
                downloader_obj=downloader_obj,
            )
            if success:
                item["action"] = "已推送"
                processed_map[record_key] = {
                    "title": title,
                    "subtitle": item.get("subtitle") or "",
                    "intro": item.get("intro") or "",
                    "cn_title": self.__extract_cn_title(item.get("subtitle") or ""),
                    "site": site_name,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                logger.info(f"UHD原盘自动下载：已推送 {site_name} - {title[:60]}")
            else:
                item["action"] = "推送失败"
                logger.error(f"UHD原盘自动下载：推送失败 {site_name} - {title[:60]}")
            items.append(item)

        return items

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

        # 下载种子文件
        try:
            res = RequestUtils(
                ua=site.get("ua"),
                cookies=site.get("cookie"),
                proxies=settings.PROXY if site.get("proxy") else None,
                timeout=site.get("timeout") or 30,
            ).get_res(url=download_url)
        except Exception as err:
            logger.error(f"UHD原盘自动下载：下载种子文件失败，{err}")
            return False

        if res is None or res.status_code != 200 or not res.content:
            logger.error(f"UHD原盘自动下载：下载种子文件失败，状态码 {res.status_code if res else 'None'}")
            return False

        # 推送到 QB（带标签 UHD自动下载，便于后续识别与处理）
        success, _ = downloader_obj.add_torrent(
            content=res.content,
            download_dir=site_conf.get("save_path"),
            category=site_conf.get("category"),
            tag=DOWNLOAD_TAG,
            is_paused=False,
        )
        return success

    @staticmethod
    def __parse_list_page(html: str, filter_mode: str) -> List[Dict[str, Any]]:
        """解析站点种子列表页，提取 UHD BluRay 原盘。

        :param html: 页面 HTML
        :param filter_mode: 筛选模式，"uhd_title" 按标题匹配 UHD BluRay；
                            "bluray_only" 仅排除非原盘（站点已按媒介筛选）
        :return: 种子信息列表
        """
        torrents: List[Dict[str, Any]] = []
        page = etree.HTML(html)
        if page is None:
            return torrents

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
            else:
                # 站点已按媒介筛选，仅排除非原盘（WEB-DL/HDTV/Encode 等）
                if re.search(r'WEB-?DL|HDTV|WEBRip|Encode|Remux', title, re.I):
                    continue
                # 需含 BluRay/Blu-ray
                if not re.search(r'Blu-?ray', title, re.I):
                    continue

            # 副标题：优先从 font.subtitle（彩虹岛）提取，其次取 td.embedded 末尾文本（我堡）
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
                        subtitle = texts[-1]

            # 大小
            size = tds[4].xpath('string(.)').strip() if len(tds) > 4 else ""

            # 进度列：我堡为 td[8]，彩虹岛为 td[9]
            # 未下载时值为 "-"（我堡）或 "--"（彩虹岛），已下载为 "100%"
            progress = ""
            for idx in (8, 9):
                if len(tds) > idx:
                    text = tds[idx].xpath('string(.)').strip()
                    if text in ("-", "--", "100%") or re.match(r'^\d+(\.\d+)?%$', text):
                        progress = text
                        break

            # 下载链接
            dl_links = row.xpath('.//a[contains(@href,"download.php")]/@href')
            download_url = ""
            if dl_links:
                href = dl_links[0]
                if href.startswith("http"):
                    download_url = href
                else:
                    download_url = href

            torrents.append(
                {
                    "id": torrent_id,
                    "title": title,
                    "subtitle": subtitle,
                    "size": size,
                    "progress": progress,
                    "download_url": download_url,
                }
            )
        return torrents

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

    @staticmethod
    def __get_site_config(domain: str) -> Optional[dict]:
        """获取站点配置。

        :param domain: 站点域名
        :return: 站点配置字典；未找到返回 None
        """
        site = SiteOper().get_by_domain(domain)
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
