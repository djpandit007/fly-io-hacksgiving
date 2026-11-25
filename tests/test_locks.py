import asyncio

from app.locks import LockRegistry


async def test_same_key_runs_in_order_different_keys_run_together():
    reg = LockRegistry()
    log: list[str] = []

    async def job(key: str, name: str, delay: float):
        async with reg.hold(key):
            log.append(f"start {name}")
            await asyncio.sleep(delay)
            log.append(f"end {name}")

    await asyncio.gather(job("chat:1", "a1", 0.05), job("chat:1", "a2", 0.0), job("chat:2", "b", 0.0))
    # b (other chat) finished while a1 was still running; a2 waited for a1.
    assert log.index("end b") < log.index("end a1") < log.index("start a2")
    assert len(reg) == 0  # entries are cleaned up


async def test_lock_released_on_error():
    reg = LockRegistry()
    try:
        async with reg.hold("k"):
            raise RuntimeError
    except RuntimeError:
        pass
    async with asyncio.timeout(1):
        async with reg.hold("k"):
            pass
