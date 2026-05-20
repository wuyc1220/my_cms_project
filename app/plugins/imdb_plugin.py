"""
IMDb 爬虫插件

基于 Playwright 无头浏览器 + JSON-LD 结构化数据解析 IMDb 页面。
遵守 IMDb robots.txt 约定：不爬取 /find、/search 等禁止路径，
仅通过 DuckDuckGo 搜索获取 IMDb ID，再爬取允许的 /title/ 和 /name/ 详情页。
通过 pluggy hookimpl 实现 CMS 爬虫插件接口。

反爬策略：
  - 随机 User-Agent 轮换
  - 随机视口大小
  - 页面间随机延迟（模拟人类操作节奏）
  - 每次请求使用新的浏览器上下文（避免 Cookie/指纹关联）
  - 遵守 robots.txt，仅爬取允许的路径
"""

import random
import re
from typing import Any
from urllib.parse import quote_plus

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.config import settings
from app.plugins.hooks import hookimpl

BASE_URL = "https://www.imdb.com"
PAGE_LOAD_TIMEOUT = 60000
WAIT_AFTER_LOAD = 8000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
]

MOVIE_FIELD_MAPPING: dict[str, tuple[str, str]] = {
    "title": ("title", "标题"),
    "year": ("release_date", "上映日期"),
    "imdb_rating": ("score", "评分"),
    "poster_url": ("poster_url", "海报URL"),
    "plot": ("description", "描述"),
    "director": ("director", "导演"),
    "cast": ("actors", "演员"),
    "genres": ("genre", "题材"),
    "runtime": ("duration", "时长(分钟)"),
    "imdb_id": ("external_id", "外部ID"),
}

SERIES_FIELD_MAPPING: dict[str, tuple[str, str]] = {
    "title": ("title", "标题"),
    "year": ("release_date", "首播日期"),
    "imdb_rating": ("score", "评分"),
    "poster_url": ("poster_url", "海报URL"),
    "plot": ("description", "描述"),
    "director": ("director", "导演"),
    "cast": ("actors", "演员"),
    "genres": ("genre", "题材"),
    "imdb_id": ("external_id", "外部ID"),
}

CAST_FIELD_MAPPING: dict[str, tuple[str, str]] = {
    "name": ("name", "姓名"),
    "photo_url": ("photo_url", "照片URL"),
    "imdb_id": ("external_id", "外部ID"),
}


def _get_field_mapping(object_type: str) -> dict[str, tuple[str, str]]:
    """根据对象类型获取字段映射规则"""
    mapping = {
        "Movie": MOVIE_FIELD_MAPPING,
        "Series": SERIES_FIELD_MAPPING,
        "Cast": CAST_FIELD_MAPPING,
    }
    return mapping.get(object_type, MOVIE_FIELD_MAPPING)


def _random_delay(min_ms: int = 1000, max_ms: int = 3000) -> int:
    """生成随机延迟毫秒数，模拟人类操作节奏"""
    return random.randint(min_ms, max_ms)


class IMDbCrawlerPlugin:
    """IMDb 爬虫插件（基于 Playwright 无头浏览器 + JSON-LD）"""

    def __init__(self):
        self._browser: Browser | None = None
        self._pw = None

    async def _ensure_browser(self) -> Browser:
        """确保浏览器实例已启动"""
        if self._browser is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                channel="chrome",
            )
        return self._browser

    async def _new_context(self) -> BrowserContext:
        """创建新的浏览器上下文（每次请求独立，避免指纹关联）"""
        browser = await self._ensure_browser()
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)
        return await browser.new_context(
            user_agent=ua,
            locale="en-US",
            viewport=vp,
        )

    async def _new_page(self) -> tuple[Page, BrowserContext]:
        """创建新页面和上下文，返回 (page, context)"""
        context = await self._new_context()
        page = await context.new_page()
        return page, context

    @staticmethod
    async def _close_page(page: Page, context: BrowserContext) -> None:
        """安全关闭页面和上下文"""
        try:
            await page.close()
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass

    @hookimpl
    def crawler_name(self) -> str:
        return "imdb"

    @hookimpl
    def crawler_supported_sources(self) -> list[str]:
        return ["imdb"]

    @hookimpl
    def crawler_supported_types(self) -> list[str]:
        return ["Movie", "Series", "Cast"]

    @hookimpl
    async def crawler_search(
        self,
        source_url: str,
        query: str,
        object_type: str,
    ) -> list[dict[str, Any]]:
        """
        通过 DuckDuckGo HTML 搜索获取 IMDb ID

        遵守 IMDb robots.txt，不爬取 /find、/search 等禁止路径。
        使用 DuckDuckGo HTML 版搜索，从搜索结果文本中正则提取 IMDb ID，
        再去爬取允许的详情页。
        """
        if object_type == "Cast":
            search_query = f"{query} IMDb actor"
        else:
            search_query = f"{query} IMDb movie"

        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}"

        pg, ctx = await self._new_page()
        try:
            await pg.goto(ddg_url, wait_until="commit", timeout=PAGE_LOAD_TIMEOUT)
            try:
                await pg.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await pg.wait_for_timeout(_random_delay(2000, 4000))

            results = await pg.evaluate("""
            () => {
                const out = [];
                const idSet = new Set();

                const resultItems = document.querySelectorAll('.result');
                for (const item of resultItems) {
                    const titleEl = item.querySelector('.result__a');
                    const snippetEl = item.querySelector('.result__snippet');
                    const urlEl = item.querySelector('.result__url');
                    if (!titleEl) continue;

                    const titleText = titleEl.textContent.trim();
                    const snippetText = snippetEl ? snippetEl.textContent.trim() : '';
                    const urlText = urlEl ? urlEl.textContent.trim() : '';

                    const combinedText = titleText + ' ' + snippetText + ' ' + urlText;
                    const idMatch = combinedText.match(/(tt|nm)(\\d{7,8})/);
                    if (!idMatch) continue;

                    const imdbId = idMatch[1] + idMatch[2];
                    if (idSet.has(imdbId)) continue;
                    idSet.add(imdbId);

                    const yearMatch = combinedText.match(/\\((\\d{4})\\)/);
                    const year = yearMatch ? yearMatch[1] : '';

                    const cleanTitle = titleText
                        .replace(/\\s*-\\s*IMDb$/i, '')
                        .replace(/\\s*\\(\\d{4}\\)\\s*/, (m) => '')
                        .trim();

                    out.push({
                        id: imdbId,
                        title: cleanTitle || imdbId,
                        year: year,
                    });
                }

                if (out.length === 0) {
                    const bodyText = document.body.innerText || '';
                    const allIds = bodyText.match(/(tt|nm)\\d{7,8}/g) || [];
                    const uniqueIds = [...new Set(allIds)];
                    for (const rawId of uniqueIds.slice(0, 10)) {
                        if (idSet.has(rawId)) continue;
                        idSet.add(rawId);
                        out.push({ id: rawId, title: rawId, year: '' });
                    }
                }

                return out;
            }
            """)

            if object_type == "Cast":
                results = [r for r in results if r["id"].startswith("nm")]
            else:
                results = [r for r in results if r["id"].startswith("tt")]

            if not results:
                logger.warning("DuckDuckGo搜索未找到IMDb结果: query={}", query)

            return results[:10]

        except Exception as e:
            logger.error("DuckDuckGo搜索IMDb失败: query={} error={}", query, str(e))
            return []
        finally:
            await self._close_page(pg, ctx)

    @hookimpl
    async def crawler_get_detail(
        self,
        source_url: str,
        external_id: str,
        object_type: str,
    ) -> dict[str, Any] | None:
        """获取 IMDb 详情（基于 JSON-LD 结构化数据）"""
        base_url = (source_url or settings.imdb_base_url).rstrip("/")

        if object_type == "Cast":
            url = f"{base_url}/name/{external_id}/"
        else:
            url = f"{base_url}/title/{external_id}/"

        pg, ctx = await self._new_page()
        try:
            await pg.goto(url, wait_until="commit", timeout=PAGE_LOAD_TIMEOUT)
            try:
                await pg.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass

            try:
                await pg.wait_for_selector(
                    'script[type="application/ld+json"]',
                    timeout=20000,
                )
                await pg.wait_for_timeout(_random_delay(1000, 2000))
            except Exception:
                logger.info("等待JSON-LD标签超时，尝试直接提取: id={}", external_id)

            jsonld_data = await self._extract_jsonld(pg)

            if not jsonld_data:
                logger.info("首次加载未获取到JSON-LD，等待重试: id={}", external_id)
                await pg.wait_for_timeout(_random_delay(5000, 8000))
                jsonld_data = await self._extract_jsonld(pg)

            if object_type in ("Movie", "Series"):
                return self._parse_title_jsonld(jsonld_data, external_id)
            elif object_type == "Cast":
                return self._parse_name_jsonld(jsonld_data, external_id)

        except Exception as e:
            logger.error("IMDb详情获取失败: id={} error={}", external_id, str(e))
            return None
        finally:
            await self._close_page(pg, ctx)
        return None

    @hookimpl
    def crawler_map_fields(
        self,
        raw_data: dict,
        object_type: str,
        requested_field_codes: list[dict],
    ) -> list[dict[str, Any]]:
        """将 IMDb 原始数据映射为 CMS 字段候选值"""
        field_mapping = _get_field_mapping(object_type)
        code_to_imdb: dict[str, tuple[str, str]] = {}
        for imdb_key, (cms_code, cms_name) in field_mapping.items():
            code_to_imdb[cms_code] = (imdb_key, cms_name)

        requested_codes = {f["code"] for f in requested_field_codes}
        requested_names = {f["code"]: f["name"] for f in requested_field_codes}

        results = []
        for code in requested_codes:
            if code not in code_to_imdb:
                continue
            imdb_key, _ = code_to_imdb[code]
            value = raw_data.get(imdb_key)
            if value is None:
                continue
            str_value = str(value) if not isinstance(value, str) else value
            if not str_value.strip():
                continue

            field_display_name = requested_names.get(code, code)
            results.append({
                "field_code": code,
                "field_name": field_display_name,
                "crawl_data": str_value,
            })

        return results

    @hookimpl
    async def crawler_close(self) -> None:
        """关闭浏览器释放资源"""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            self._pw = None

    async def _extract_jsonld(self, page: Page) -> list[dict]:
        """从页面中提取所有 JSON-LD 数据"""
        return await page.evaluate("""
        () => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            const results = [];
            for (const s of scripts) {
                try {
                    results.push(JSON.parse(s.textContent));
                } catch(e) {}
            }
            return results;
        }
        """)

    @staticmethod
    def _parse_duration(duration_str: str | None) -> str | None:
        """将 ISO 8601 时长 (PT1H48M) 转换为分钟数"""
        if not duration_str:
            return None
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration_str)
        if not m:
            return None
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        total = hours * 60 + minutes
        return str(total) if total > 0 else None

    @staticmethod
    def _extract_names(field: Any) -> str | None:
        """从 JSON-LD 的 director/actor 字段提取名字列表"""
        if field is None:
            return None
        if isinstance(field, str):
            return field
        if isinstance(field, dict):
            return field.get("name", str(field))
        if isinstance(field, list):
            names = []
            for item in field:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    name = item.get("name")
                    if name:
                        names.append(name)
            return ", ".join(names) if names else None
        return None

    def _parse_title_jsonld(
        self, jsonld_list: list[dict], imdb_id: str
    ) -> dict[str, Any] | None:
        """从 JSON-LD 数据解析电影/剧集信息"""
        data: dict[str, Any] = {"imdb_id": imdb_id}

        movie_data = None
        for item in jsonld_list:
            if item.get("@type") in ("Movie", "TVSeries"):
                movie_data = item
                break
        if movie_data is None:
            for item in jsonld_list:
                if item.get("name"):
                    movie_data = item
                    break

        if movie_data is None:
            logger.warning("IMDb页面未找到JSON-LD Movie/TVSeries数据: id={}", imdb_id)
            return data

        data["title"] = movie_data.get("name", "")
        data["plot"] = movie_data.get("description", "")

        date_published = movie_data.get("datePublished")
        if date_published:
            year_match = re.match(r"(\d{4})", date_published)
            if year_match:
                data["year"] = year_match.group(1)

        rating = movie_data.get("aggregateRating")
        if rating:
            data["imdb_rating"] = str(rating.get("ratingValue", ""))

        data["poster_url"] = movie_data.get("image", "")

        data["director"] = self._extract_names(movie_data.get("director"))
        data["cast"] = self._extract_names(movie_data.get("actor"))

        genre = movie_data.get("genre")
        if isinstance(genre, list):
            data["genres"] = ", ".join(genre)
        elif isinstance(genre, str):
            data["genres"] = genre

        duration = movie_data.get("duration")
        data["runtime"] = self._parse_duration(duration)

        return data

    def _parse_name_jsonld(
        self, jsonld_list: list[dict], imdb_id: str
    ) -> dict[str, Any] | None:
        """从 JSON-LD 数据解析演员信息"""
        data: dict[str, Any] = {"imdb_id": imdb_id}

        person_data = None
        for item in jsonld_list:
            me = item.get("mainEntity")
            if me and me.get("@type") == "Person":
                person_data = me
                break

        if person_data is None:
            for item in jsonld_list:
                if item.get("@type") == "Person":
                    person_data = item
                    break

        if person_data is None:
            for item in jsonld_list:
                if item.get("name"):
                    person_data = item
                    break

        if person_data is None:
            logger.warning("IMDb页面未找到JSON-LD Person数据: id={}", imdb_id)
            return data

        data["name"] = person_data.get("name", "")
        data["photo_url"] = person_data.get("image", "")

        return data
