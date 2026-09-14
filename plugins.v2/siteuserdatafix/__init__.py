import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.helper.browser import PlaywrightHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import SiteUserData
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class SiteUserDataFix(_PluginBase):
    """
    站点用户数据修正插件。

    针对部分站点页面改版后，MoviePilot 内置解析器抓取做种数、做种体积、
    积分等字段错误的问题，通过劫持 refresh_userdata 模块方法，对指定站点
    重新抓取并修正这些字段。
    """

    plugin_name = "站点数据修正"
    plugin_desc = "修正猫站、春天等站点做种数、做种体积、积分解析错误。"
    plugin_icon = "world.png"
    plugin_version = "1.0"
    plugin_author = "local"
    plugin_config_prefix = "siteuserdatafix_"
    plugin_order = 1
    auth_level = 1

    # 需要修正的站点域名
    _fix_domains = {
        "pterclub.net": "pterclub",
        "springsunday.net": "springsunday",
    }

    _enabled = False

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self._enabled = bool(config.get("enabled")) if config else False

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "text": "启用后自动修正猫站(pterclub.net)做种数/做种体积、"
                            "春天(springsunday.net)积分。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        return None

    def get_module(self) -> Dict[str, Any]:
        """劫持系统模块的 refresh_userdata 方法。"""
        if not self._enabled:
            return {}
        return {
            "refresh_userdata": self.refresh_userdata,
        }

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        pass

    def refresh_userdata(self, site: dict = None) -> Optional[SiteUserData]:
        """
        刷新站点用户数据，并对指定站点修正解析错误的字段。

        :param site: 站点索引配置
        :return: 修正后的用户数据
        """
        if not site:
            return None
        domain = StringUtils.get_url_domain(site.get("domain") or site.get("url") or "")
        if domain not in self._fix_domains:
            # 非目标站点，返回 None 交由系统模块处理
            return None

        # 先调用系统模块获取原始数据（保留正确的上传/下载/分享率等字段）
        userdata = self.__call_system_refresh_userdata(site)
        if not userdata:
            return None

        try:
            if domain == "pterclub.net":
                self.__fix_pterclub(site, userdata)
            elif domain == "springsunday.net":
                self.__fix_springsunday(site, userdata)
        except Exception as err:
            logger.error(f"站点数据修正失败 {domain}：{err}")

        return userdata

    @staticmethod
    def __call_system_refresh_userdata(site: dict) -> Optional[SiteUserData]:
        """
        直接调用系统模块的 refresh_userdata，绕过插件层避免递归。

        :param site: 站点索引配置
        :return: 原始用户数据
        """
        from app.core.module import ModuleManager
        modules = list(ModuleManager().get_running_modules("refresh_userdata"))
        for module in modules:
            try:
                result = module.refresh_userdata(site)
                if result:
                    return result
            except Exception as err:
                logger.error(f"调用系统模块 refresh_userdata 失败：{err}")
        return None

    @staticmethod
    def __get_page(site: dict, path: str) -> Optional[str]:
        """
        抓取站点页面文本。

        :param site: 站点索引配置
        :param path: 页面路径
        :return: 页面 HTML 文本
        """
        url = (site.get("url") or "").rstrip("/") + path
        res = RequestUtils(
            ua=site.get("ua"),
            cookies=site.get("cookie"),
            proxies=settings.PROXY if site.get("proxy") else None,
            timeout=site.get("timeout") or 15,
        ).get_res(url)
        if not res or res.status_code != 200:
            return None
        return res.text

    @staticmethod
    def __get_page_render(site: dict, path: str) -> Optional[str]:
        """
        使用浏览器渲染抓取站点页面文本，用于绕过反爬。

        :param site: 站点索引配置
        :param path: 页面路径
        :return: 页面 HTML 文本
        """
        url = (site.get("url") or "").rstrip("/") + path
        proxy = settings.PROXY_SERVER if site.get("proxy") else None
        try:
            return PlaywrightHelper().get_page_source(
                url=url,
                cookies=site.get("cookie"),
                ua=site.get("ua"),
                proxies=proxy,
                timeout=site.get("timeout") or 30,
            )
        except Exception as err:
            logger.error(f"浏览器渲染抓取失败 {url}：{err}")
            return None

    def __fix_pterclub(self, site: dict, userdata: SiteUserData) -> None:
        """
        修正猫站做种数与做种体积。

        :param site: 站点索引配置
        :param userdata: 待修正的用户数据
        """
        # 1. 从首页解析做种数/下载数
        html = self.__get_page(site, "/usercp.php")
        if html:
            seeding = self.__parse_pterclub_activity(html, "seeding")
            leeching = self.__parse_pterclub_activity(html, "leeching")
            if seeding is not None:
                userdata.seeding = seeding
            if leeching is not None:
                userdata.leeching = leeching

        # 2. 从做种列表累加做种体积
        userid = userdata.userid or self.__parse_pterclub_userid(html or "")
        if userid:
            seeding_size = self.__calc_pterclub_seeding_size(site, str(userid))
            if seeding_size is not None:
                userdata.seeding_size = seeding_size

    @staticmethod
    def __parse_pterclub_activity(html: str, kind: str) -> Optional[int]:
        """
        解析猫站首页当前活动中的做种数或下载数。

        :param html: 首页 HTML
        :param kind: seeding 或 leeching
        :return: 数量
        """
        pattern = rf'type={kind}"[^>]*>\s*(?:<img[^>]*/>)?\s*(\d+)\s*</a>'
        match = re.search(pattern, html)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def __parse_pterclub_userid(html: str) -> Optional[str]:
        """
        从猫站首页解析用户 ID。

        :param html: 首页 HTML
        :return: 用户 ID
        """
        match = re.search(r'userid=(\d+)', html)
        if match:
            return match.group(1)
        return None

    def __calc_pterclub_seeding_size(self, site: dict, userid: str) -> Optional[int]:
        """
        累加猫站做种列表中的种子体积，返回字节数。

        做种列表页存在反爬，普通 HTTP 请求会被 Forbidden，因此先尝试普通
        请求，失败后回退到浏览器渲染抓取。

        :param site: 站点索引配置
        :param userid: 用户 ID
        :return: 做种体积（字节）
        """
        path = f"/getusertorrentlist.php?userid={userid}&type=seeding"
        html = self.__get_page(site, path)
        if not html or "Forbidden" in html:
            # 反爬拦截，回退到浏览器渲染
            html = self.__get_page_render(site, path)
        if not html:
            return None
        unit_map = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
        total_bytes = 0
        count = 0
        # 每个种子行的体积是紧跟"认领种子"后的第一个 GB/TB 值
        # 用行内第一个体积单元格匹配
        for row in re.findall(r'<tr[^>]*>.*?</tr>', html, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            for cell in cells:
                text = re.sub(r'<[^>]+>', '', cell).strip()
                match = re.match(r'^([\d.]+)\s*(KB|MB|GB|TB)$', text)
                if match:
                    total_bytes += float(match.group(1)) * unit_map[match.group(2)]
                    count += 1
                    break
        if count == 0:
            return None
        return int(total_bytes)

    def __fix_springsunday(self, site: dict, userdata: SiteUserData) -> None:
        """
        修正春天站点积分。

        :param site: 站点索引配置
        :param userdata: 待修正的用户数据
        """
        html = self.__get_page(site, "/usercp.php")
        if not html:
            return
        # 积分精确值在 title 属性中，如 title="茉莉: 19,992.4"
        match = re.search(r'title="[^"]*茉莉[：:]\s*([\d,]+(?:\.\d+)?)"', html)
        if match:
            bonus = float(match.group(1).replace(",", ""))
            userdata.bonus = bonus
