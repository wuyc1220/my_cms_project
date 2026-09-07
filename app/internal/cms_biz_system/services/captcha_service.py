"""
验证码服务

功能：
1. 生成随机验证码
2. 生成验证码图片
3. 存储验证码（通过 CacheService 存入 cache_store 表，支持多实例共享）
4. 校验验证码
"""

import io
import random
import string
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.services.cache_service import CacheService

_CAPTCHA_KEY_PREFIX = "captcha:"

_FONT_SIZE = 24

_FONT_SEARCH_PATHS = [
    Path(__file__).resolve().parent.parent.parent.parent / "fonts",
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/Helvetica.ttc"),
]


_LOADED_FONT: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None


def _get_font(size: int = _FONT_SIZE) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _LOADED_FONT
    if _LOADED_FONT is not None:
        return _LOADED_FONT

    for font_dir in _FONT_SEARCH_PATHS:
        logger.debug(f"检查字体路径: {font_dir}, is_dir={font_dir.is_dir()}, is_file={font_dir.is_file()}")
        if font_dir.is_dir():
            for pattern in ["*.ttf", "*.TTF", "*.ttc", "*.TTC"]:
                for font_file in sorted(font_dir.glob(pattern)):
                    try:
                        _LOADED_FONT = ImageFont.truetype(str(font_file), size)
                        logger.info(f"验证码字体加载成功: {font_file}, size={size}")
                        return _LOADED_FONT
                    except (OSError, IOError) as e:
                        logger.warning(f"字体加载失败: {font_file}, error={e}")
                        continue
        elif font_dir.is_file():
            try:
                _LOADED_FONT = ImageFont.truetype(str(font_dir), size)
                logger.info(f"验证码字体加载成功: {font_dir}, size={size}")
                return _LOADED_FONT
            except (OSError, IOError) as e:
                logger.warning(f"字体加载失败: {font_dir}, error={e}")
                continue

    logger.warning("未找到可用的 TrueType 字体，使用 PIL 默认字体，验证码可能显示异常。"
                   "请在 app/fonts/ 目录下放置 .ttf 字体文件")
    _LOADED_FONT = ImageFont.load_default()
    return _LOADED_FONT


def generate_captcha_code(length: int = 4) -> str:
    chars = string.ascii_uppercase.replace('O', '').replace('I', '').replace('L', '')
    chars += string.digits.replace('0', '').replace('1', '')
    return ''.join(random.choices(chars, k=length))


def generate_captcha_image(code: str, width: int = 120, height: int = 40) -> bytes:
    image = Image.new('RGB', (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)

    font = _get_font()

    colors = [(0, 102, 204), (204, 0, 0), (0, 153, 0), (153, 0, 153), (204, 102, 0), (102, 0, 153)]

    char_count = len(code)
    char_spacing = width // (char_count + 1)

    for i, char in enumerate(code):
        char_bbox = draw.textbbox((0, 0), char, font=font)
        char_width = char_bbox[2] - char_bbox[0]
        char_height = char_bbox[3] - char_bbox[1]

        char_x = char_spacing * (i + 1) - char_width // 2
        char_y = (height - char_height) // 2

        char_x = max(2, min(char_x + random.randint(-3, 3), width - char_width - 2))
        char_y = max(2, min(char_y + random.randint(-2, 2), height - char_height - 2))

        color = colors[i % len(colors)]
        draw.text((char_x, char_y), char, font=font, fill=color)

    for _ in range(4):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        draw.line([(x1, y1), (x2, y2)], fill=(200, 200, 200), width=1)

    for _ in range(30):
        px = random.randint(0, width - 1)
        py = random.randint(0, height - 1)
        draw.point((px, py), fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)))

    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


async def create_captcha(db: AsyncSession, cache: CacheService, expire_minutes: int = 5, test_mode: bool = False) -> tuple[str, bytes]:
    await cache.clean_expired(db)

    if test_mode:
        captcha_id = "test-captcha-id"
        code = "T3ST"
        await cache.set(db, f"{_CAPTCHA_KEY_PREFIX}{captcha_id}", {"code": code.lower()}, ttl_seconds=86400)
        image_bytes = generate_captcha_image(code)
        return captcha_id, image_bytes

    captcha_id = str(uuid4())
    code = generate_captcha_code()
    image_bytes = generate_captcha_image(code)

    await cache.set(db, f"{_CAPTCHA_KEY_PREFIX}{captcha_id}", {"code": code.lower()}, ttl_seconds=expire_minutes * 60)

    return captcha_id, image_bytes


async def verify_captcha(db: AsyncSession, cache: CacheService, captcha_id: str, code: str) -> bool:
    if not captcha_id or not code:
        return False

    cache_key = f"{_CAPTCHA_KEY_PREFIX}{captcha_id}"
    captcha_data = await cache.get(db, cache_key)
    if not captcha_data:
        return False

    stored_code = captcha_data.get("code", "")

    if captcha_id != "test-captcha-id":
        await cache.delete(db, cache_key)

    return stored_code == code.lower()
