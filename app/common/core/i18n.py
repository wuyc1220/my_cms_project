"""
多语言支持
"""
import json
from typing import Dict, Optional
from pathlib import Path

I18N_DIR = Path(__file__).parent.parent.parent / "i18n"

_language_cache: Dict[str, Dict] = {}

_current_lang: str = "cn"


def set_current_lang(lang: str) -> None:
    """设置当前全局语言"""
    global _current_lang
    _current_lang = lang


def get_current_lang() -> str:
    """获取当前全局语言"""
    return _current_lang


def get_msg(key: str, **kwargs) -> str:
    """
    便捷方法：使用当前全局语言获取消息
    用法: get_msg("USER_NOT_FOUND", username="admin")
    """
    return get_message(key, lang=_current_lang, **kwargs)


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
        except KeyError:
            pass  # 如果格式化失败，返回原始消息
            
    return message


def get_accept_language(accept_language_header: Optional[str] = None) -> str:
    """
    从Accept-Language头解析语言偏好
    """
    if not accept_language_header:
        return "en"
    
    # 简单解析，取第一个语言
    languages = [lang.strip().split(';')[0] for lang in accept_language_header.split(',')]
    if languages:
        first_lang = languages[0].lower()
        # 映射常见语言代码
        lang_map = {
            'zh-cn': 'zh',
            'zh': 'zh',
            'en-us': 'en',
            'en': 'en',
        }
        return lang_map.get(first_lang, 'en')
    
    return "en"
