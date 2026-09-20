from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse


router = APIRouter(tags=["observability"])


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics(request: Request):
    return PlainTextResponse(
        request.app.state.metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
