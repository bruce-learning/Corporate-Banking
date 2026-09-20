from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from starlette.concurrency import run_in_threadpool

# OWASP Argon2id 最低推荐组合，线程池避免阻塞事件循环。
hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
dummy_hash = hasher.hash("not-a-user-password-" * 3)


async def hash_password(password: str) -> str:
    return await run_in_threadpool(hasher.hash, password)


async def verify_password(encoded: str, password: str) -> bool:
    try:
        return await run_in_threadpool(hasher.verify, encoded, password)
    except (VerificationError, InvalidHashError):
        return False
