from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import Identity, administrator


router = APIRouter(prefix="/api/v1/admin/skills", tags=["skills"])


def manager(request: Request):
    value = getattr(request.app.state, "skill_manager", None)
    if value is None:
        raise HTTPException(503, "Skill 管理器尚未初始化")
    return value


@router.get("")
async def listing(request: Request, identity: Identity = Depends(administrator)):
    return manager(request).summary()


@router.post("/reload")
async def reload_skills(request: Request, identity: Identity = Depends(administrator)):
    value = manager(request)
    value.reload()
    return value.summary()
