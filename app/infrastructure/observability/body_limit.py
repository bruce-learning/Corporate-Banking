from fastapi import HTTPException
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """读取过程中限制请求体，避免只检查不可信 Content-Length。"""
    def __init__(self, app, maximum=65536, upload_maximum=10 * 1024 * 1024):
        self.app, self.maximum, self.upload_maximum = app, maximum, upload_maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        maximum = self.upload_maximum if scope.get("path") in {
            "/api/v1/admin/documents/preview", "/api/v1/admin/documents",
        } else self.maximum
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "请求长度不合法"}, status_code=400)(scope, receive, send)
        if length < 0 or length > maximum:
            return await JSONResponse({"detail": "请求体过大"}, status_code=413)(scope, receive, send)
        size = 0
        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > maximum:
                raise HTTPException(413, "请求体过大")
            return message
        await self.app(scope, bounded_receive, send)
