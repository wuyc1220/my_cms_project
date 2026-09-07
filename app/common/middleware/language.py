"""
请求语言中间件（纯 ASGI 实现）

每个 HTTP 请求根据 Accept-Language 头设置请求级语言（写入 ContextVar），
使 get_msg / get_message 按请求语言返回本地化消息，请求结束后重置。

设计要点：
- 采用纯 ASGI 中间件（非 BaseHTTPMiddleware）：ContextVar 在同一 asyncio
  上下文中设置，可正确传播到下游中间件与 endpoint。
- 必须注册为最外层（最先执行）中间件，先于 master_platform /
  sensitive_word 等会调用 get_msg 的中间件，保证其读取到正确的请求语言。
"""

from app.common.core.i18n import (
    get_accept_language,
    set_current_lang,
    reset_current_lang,
)


class LanguageMiddleware:
    """根据 Accept-Language 设置请求级语言的纯 ASGI 中间件"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        accept_language = ""
        for name, value in scope.get("headers", []):
            if name == b"accept-language":
                accept_language = value.decode("latin-1")
                break

        lang = get_accept_language(accept_language)
        token = set_current_lang(lang)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_lang(token)
