from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import Identity, current_identity, require_model_client, require_store, require_vector_store
from app.core.rag.hybrid import HybridSearchService


router = APIRouter(prefix="/api/v1/search", tags=["search"])


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    domain: Literal["product", "process", "policy"] | None = None
    top_k: int = Field(default=5, ge=1, le=10)
    as_of: datetime | None = None


@router.post("")
async def search(body: SearchInput, request: Request, identity: Identity = Depends(current_identity)):
    service = HybridSearchService(
        require_store(request), require_model_client(request), require_vector_store(request)
    )
    try:
        result = await service.search(body.query, domain=body.domain, top_k=body.top_k, as_of=body.as_of)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return result.model_dump()
