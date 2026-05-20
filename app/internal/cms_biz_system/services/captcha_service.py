"""
验证码服务

功能：
1. 生成随机验证码
2. 生成验证码图片
3. 存储验证码（内存存储，支持过期）
4. 校验验证码
"""

import io
import os
import random
import string
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont
from loguru import logger
from app.common.core.i18n import get_msg

# 内存存储验证码：{captcha_id: {code: str, expire_at: float}}
_captcha_store: dict[str, dict] = {}

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
    """按优先级加载字体：项目内置 > 系统字体 > 默认字体"""
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
    """生成随机验证码字符串"""
    # 排除容易混淆的字符：0O1lI
    chars = string.ascii_uppercase.replace('O', '').replace('I', '').replace('L', '')
    chars += string.digits.replace('0', '').replace('1', '')
    return ''.join(random.choices(chars, k=length))


def generate_captcha_image(code: str, width: int = 120, height: int = 40) -> bytes:
    """生成验证码图片，返回PNG格式的bytes"""
    # 创建图片
    image = Image.new('RGB', (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)

    font = _get_font()

    # 字符颜色
    colors = [(0, 102, 204), (204, 0, 0), (0, 153, 0), (153, 0, 153), (204, 102, 0), (102, 0, 153)]

    # 计算每个字符的宽度和位置
    char_count = len(code)
    char_spacing = width // (char_count + 1)

    # 绘制验证码文字
    for i, char in enumerate(code):
        # 获取单个字符的边界框
        char_bbox = draw.textbbox((0, 0), char, font=font)
        char_width = char_bbox[2] - char_bbox[0]
        char_height = char_bbox[3] - char_bbox[1]

        # 计算字符位置（确保在图片范围内）
        char_x = char_spacing * (i + 1) - char_width // 2
        char_y = (height - char_height) // 2

        # 添加随机偏移，但保持在边界内
        char_x = max(2, min(char_x + random.randint(-3, 3), width - char_width - 2))
        char_y = max(2, min(char_y + random.randint(-2, 2), height - char_height - 2))

        color = colors[i % len(colors)]
        draw.text((char_x, char_y), char, font=font, fill=color)

    # 绘制干扰线
    for _ in range(4):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        draw.line([(x1, y1), (x2, y2)], fill=(200, 200, 200), width=1)

    # 绘制干扰点
    for _ in range(30):
        px = random.randint(0, width - 1)
        py = random.randint(0, height - 1)
        draw.point((px, py), fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)))

    # 转换为bytes
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def create_captcha(expire_minutes: int = 5) -> tuple[str, bytes]:
    """
    创建验证码

    Args:
        expire_minutes: 验证码过期时间（分钟）

    Returns:
        tuple[str, bytes]: (captcha_id, image_bytes)
    """
    # 清理过期的验证码
    _clean_expired_captchas()

    # 生成验证码
    captcha_id = str(uuid4())
    code = generate_captcha_code()
    image_bytes = generate_captcha_image(code)

    # 存储验证码
    _captcha_store[captcha_id] = {
        'code': code.lower(),  # 存储小写，比较时不区分大小写
        'expire_at': time.time() + expire_minutes * 60,
    }

    return captcha_id, image_bytes


def verify_captcha(captcha_id: str, code: str) -> bool:
    """
    校验验证码

    Args:
        captcha_id: 验证码ID
        code: 用户输入的验证码

    Returns:
        bool: 是否验证通过
    """
    if not captcha_id or not code:
        return False

    captcha_data = _captcha_store.get(captcha_id)
    if not captcha_data:
        return False

    # 检查是否过期
    if time.time() > captcha_data['expire_at']:
        del _captcha_store[captcha_id]
        return False

    # 校验后删除验证码（一次性使用）
    stored_code = captcha_data['code']
    del _captcha_store[captcha_id]

    # 不区分大小写比较
    return stored_code == code.lower()


def _clean_expired_captchas() -> None:
    """清理过期的验证码"""
    current_time = time.time()
    expired_ids = [
        captcha_id for captcha_id, data in _captcha_store.items()
        if current_time > data['expire_at']
    ]
    for captcha_id in expired_ids:
        del _captcha_store[captcha_id]
