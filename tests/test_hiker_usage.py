"""The real HTTP boundary counts retries and blocks before transport."""

import hikerapi
import httpx
import pytest

from insto.backends._retry import with_retry
from insto.backends.hiker import HikerBackend


async def backend_for(handler, ledger):
    sdk = hikerapi.AsyncClient(token="offline-token")
    await sdk._client.aclose()
    sdk._client = httpx.AsyncClient(base_url=sdk._url, transport=httpx.MockTransport(handler))

    async def no_sleep(_):
        pass

    return HikerBackend(
        client=sdk, usage_ledger=ledger, retry_decorator=with_retry(max_attempts=5, sleep=no_sleep)
    )


async def test_each_retry_reserves_before_transport_and_exhaustion_stops_retries(tmp_path):
    from insto.exceptions import DailyBudgetReached
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    await ledger.configure(2)
    seen = []

    async def handler(request):
        seen.append((await ledger.inspect())["used"])
        return httpx.Response(503)

    backend = await backend_for(handler, ledger)
    try:
        with pytest.raises(DailyBudgetReached):
            await backend._call(lambda: backend._client._client.get("/paid"))
        assert seen == [1, 2]
        assert (await ledger.inspect())["used"] == 2
    finally:
        await backend.aclose()


async def test_free_balance_validation_ignores_an_exhausted_cap(tmp_path):
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")
    await ledger.configure(1)
    await ledger.reserve()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"requests": 42})

    backend = await backend_for(handler, ledger)
    try:
        await backend.validate_access()
        assert paths == ["/sys/balance"]
        assert (await ledger.inspect())["used"] == 1
    finally:
        await backend.aclose()


async def test_failed_transport_attempt_remains_counted(tmp_path):
    from insto.exceptions import Transient
    from insto.service.usage import UsageLedger

    ledger = UsageLedger(tmp_path / "usage.db")

    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    backend = await backend_for(handler, ledger)
    try:
        with pytest.raises(Transient):
            await backend._call(lambda: backend._client._client.get("/paid"))
        assert (await ledger.inspect())["used"] == 5
    finally:
        await backend.aclose()
