from __future__ import annotations

from app.core.application.ingestion import process_ingestion_job


class JobProcessor:
    def __init__(self, queue, memory_service, store, model_client, vector_store, settings):
        self.queue = queue
        self.memory_service = memory_service
        self.store = store
        self.model_client = model_client
        self.vector_store = vector_store
        self.settings = settings

    async def process_one(self, job_id: str | None = None) -> bool:
        job = await self.queue.claim(job_id)
        if not job:
            return False
        try:
            if job.job_type == "memory_sync":
                result = await self.memory_service.remember_from_conversation(
                    str(job.payload_json["user_id"]), str(job.payload_json["conversation_id"]),
                    int(job.payload_json["total_sequence"]),
                )
            elif job.job_type == "ingestion":
                ingestion_job_id = str(job.payload_json["ingestion_job_id"])
                ingestion_result = await process_ingestion_job(
                    self.store, self.model_client, self.vector_store,
                    self.settings.data_dir, ingestion_job_id, allow_ocr=True,
                )
                if ingestion_result.get("status") == "failed":
                    raise RuntimeError(str(ingestion_result.get("error_code") or "ingestion_failed"))
                result = {"ingestion_job_id": ingestion_job_id, **ingestion_result}
            else:
                raise ValueError("unsupported_job_type")
            if result.get("available", True) is False:
                raise RuntimeError(str(result.get("reason") or "dependency_unavailable"))
            await self.queue.complete(job.id, result)
        except Exception as error:
            await self.queue.fail(job.id, getattr(error, "category", "job_failed"), str(error))
        return True
