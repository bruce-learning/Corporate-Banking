from enum import Enum


class Domain(str, Enum):
    PRODUCT = "product"
    PROCESS = "process"
    POLICY = "policy"


class ExecutionMode(str, Enum):
    FIXED = "fixed"
    PARALLEL = "parallel"
    PLAN_EXECUTE = "plan_execute"


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    INDEXED = "indexed"
    PUBLISHED = "published"
    FAILED = "failed"
    INACTIVE = "inactive"
