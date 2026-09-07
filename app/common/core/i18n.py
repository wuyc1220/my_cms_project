"""
多语言支持
"""
import json
import contextvars
from typing import Dict, Optional
from pathlib import Path

from loguru import logger

I18N_DIR = Path(__file__).parent.parent.parent / "i18n"

_language_cache: Dict[str, Dict] = {}

# 支持的语言集合（资源文件为 cn.json / en.json）
_SUPPORTED_LANGS = {"cn", "en"}

# 进程级默认语言：启动时由数据库配置 / settings.default_language 写入，
# 当请求级 ContextVar 未设置时回退到它。
_default_lang: str = "cn"

# 请求级语言：使用 ContextVar 存储，避免 async 并发下跨请求语言串写。
_current_lang_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_lang", default=None
)


def set_default_lang(lang: str) -> None:
    """设置进程级默认语言（启动时调用一次）"""
    global _default_lang
    if lang in _SUPPORTED_LANGS:
        _default_lang = lang


def set_current_lang(lang: str):
    """
    设置当前请求语言（写入 ContextVar），返回 token 供 reset_current_lang 使用。
    仅接受受支持语言，其余忽略（保持默认）。
    """
    if lang not in _SUPPORTED_LANGS:
        lang = _default_lang
    return _current_lang_ctx.set(lang)


def reset_current_lang(token) -> None:
    """请求结束时重置 ContextVar（配合 set_current_lang 的 token）"""
    if token is None:
        return
    try:
        _current_lang_ctx.reset(token)
    except (ValueError, LookupError):
        pass


def get_current_lang() -> str:
    """获取当前语言：优先请求级 ContextVar，否则回退进程默认语言"""
    lang = _current_lang_ctx.get()
    return lang if lang in _SUPPORTED_LANGS else _default_lang


def get_msg(key: str, **kwargs) -> str:
    """
    便捷方法：使用当前请求语言获取消息
    用法: get_msg("USER_NOT_FOUND", username="admin")
    """
    return get_message(key, lang=get_current_lang(), **kwargs)


def load_language(lang: str = "en") -> Dict:
    """加载指定语言的资源文件"""
    if lang in _language_cache:
        return _language_cache[lang]
    
    lang_file = I18N_DIR / f"{lang}.json"
    if not lang_file.exists():
        # 默认使用英文
        lang_file = I18N_DIR / "en.json"
    
    try:
        with open(lang_file, 'r', encoding='utf-8') as f:
            resources = json.load(f)
        _language_cache[lang] = resources
        return resources
    except Exception:
        # 如果加载失败，返回空字典
        return {}


def get_message(key: str, lang: str = "en", **kwargs) -> str:
    """
    获取指定key的多语言消息
    支持格式化: get_message("USER_NOT_FOUND", lang="zh", username="admin")
    """
    resources = load_language(lang)
    message = resources.get(key, key)
    
    # 格式化消息
    if kwargs:
        try:
            message = message.format(**kwargs)
        except KeyError as e:
            # 模板占位符缺少对应参数：保留原始消息兜底，同时告警暴露调用方参数错位
            logger.warning(f"i18n 消息格式化失败: key={key}, lang={lang}, 缺少参数 {e}, 已提供参数={list(kwargs.keys())}")
            
    return message


def get_accept_language(accept_language_header: Optional[str] = None) -> str:
    """
    从 Accept-Language 头解析语言偏好，归一为受支持语言（cn/en）。
    无头或无法识别时回退到进程默认语言。
    """
    if not accept_language_header:
        return _default_lang

    # 简单解析，取第一个语言
    languages = [lang.strip().split(';')[0] for lang in accept_language_header.split(',')]
    if languages:
        first_lang = languages[0].lower()
        # 映射常见语言代码到资源文件语言（cn.json / en.json）
        lang_map = {
            'zh-cn': 'cn',
            'zh-hans': 'cn',
            'zh': 'cn',
            'cn': 'cn',
            'en-us': 'en',
            'en-gb': 'en',
            'en': 'en',
        }
        return lang_map.get(first_lang, _default_lang)

    return _default_lang
