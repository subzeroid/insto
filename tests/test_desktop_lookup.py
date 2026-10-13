"""On-demand lookups: bounded, fail-fast provider reads that write nothing."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from insto.config import Config
from insto.desktop import lookup
from insto.desktop.configuration import config_bytes
from insto.desktop.errors import MESSAGES, DesktopError
from insto.desktop.lookup_params import validate_params
from insto.desktop.profile import Profile
from insto.exceptions import (
    AuthInvalid,
    BackendError,
    Banned,
    PageBudgetExceeded,
    ProfileDeleted,
    ProfileNotFound,
    ProfilePrivate,
    QuotaExhausted,
    RateLimited,
    SchemaDrift,
    Transient,
)
from insto.models import Post, Quota
from insto.models import Profile as ProfileDTO

MOMENT = int(datetime(2026, 9, 15, 10, 30, tzinfo=UTC).timestamp())  # a Tuesday
DAY = 86400


def profile_dto(**overrides):
    values = {
        "pk": "17841400000000001",
        "username": "alice",
        "access": "public",
        "full_name": "Alice Example",
        "biography": "bio line",
        "external_url": "https://example.test/alice",
        "is_verified": True,
        "is_business": False,
        "is_private": False,
        "public_email": "alice@example.test",
        "public_phone": None,
        "business_category": None,
        "follower_count": 1200,
        "following_count": 300,
        "media_count": 87,
        "avatar_url": "https://cdn.example.test/a.jpg",
        "avatar_url_hash": "f" * 64,
    }
    values.update(overrides)
    return ProfileDTO(**values)


def make_post(index, *, place=None, lat=None, lng=None, likes=0, tags=(), mentions=(), when=None):
    return Post(
        pk=str(9000 + index),
        code=f"code{index}",
        taken_at=MOMENT + index * DAY if when is None else when,
        media_type="image",
        caption="caption",
        like_count=likes,
        location_name=place,
        location_pk=None if place is None else f"pk-{place}",
        location_lat=lat,
        location_lng=lng,
        hashtags=list(tags),
        mentions=list(mentions),
    )


class FakeBackend:
    """Records every provider call; nothing here touches a real network."""

    def __init__(
        self,
        *,
        posts=(),
        page=50,
        quota=None,
        profile=None,
        errors=None,
        hang=False,
        endless=False,
    ):
        self.posts = list(posts)
        self.page = page
        # `endless` mimics a provider whose cursor never terminates, so the
        # page ceiling is the only thing that can stop the spending.
        self.endless = endless
        self.max_pages = lookup.MAX_PAGE_REQUESTS
        self.quota = Quota.unknown() if quota is None else Quota.with_remaining(quota)
        self.profile = profile_dto() if profile is None else profile
        self.errors = dict(errors or {})
        self.hang = hang
        self.calls = []
        self.closed = 0

    def _raise(self, slot):
        error = self.errors.get(slot)
        if error is not None:
            raise error

    async def get_profile_by_username(self, username):
        self.calls.append(("profile_by_username", username))
        if self.hang:
            await asyncio.Event().wait()
        self._raise("resolve")
        self._raise("profile")
        return self.profile

    async def resolve_target(self, username):  # pragma: no cover - must never be called
        self.calls.append(("resolve", username))
        raise AssertionError("lookup.profile must read the profile in one request")

    async def get_profile(self, pk):  # pragma: no cover - must never be called
        self.calls.append(("profile", pk))
        raise AssertionError("lookup.profile must read the profile in one request")

    async def get_user_about(self, pk):  # pragma: no cover - must never be called
        self.calls.append(("about", pk))
        raise AssertionError("lookup.profile must not spend a request on user_about")

    async def iter_user_posts(self, pk, *, limit=None):
        index = 0
        pages = 0
        while True:
            if pages >= self.max_pages:
                raise PageBudgetExceeded("user_medias_chunk_v1", self.max_pages)
            self.calls.append(("posts", pk, limit, index))
            if self.hang:
                await asyncio.Event().wait()
            self._raise("posts")
            pages += 1
            chunk = self.posts[index : index + self.page]
            for post in chunk:
                yield post
                index += 1
                if limit is not None and index >= limit:
                    return
            if len(chunk) < self.page and not self.endless:
                return

    def get_quota(self):
        return self.quota

    async def aclose(self):
        self.closed += 1


@pytest.fixture
def lookup_profile(tmp_path):
    """A configured profile with no database and no output directory."""
    profile = Profile(tmp_path / "desktop")
    with profile.locked(initialize=True):
        profile.write_config(config_bytes(profile, "offline-desktop-token"))
        profile.write_state(profile.new_state(remaining=8, desired="stopped"))
    return profile


@pytest.fixture
def constructed(monkeypatch):
    """Capture how the backend was built, and hand back the injected fake."""
    record = {}

    def install(backend):
        def construct(token, *, proxy=None, retry_decorator=None, max_pages=None):
            record.update(
                token=token,
                proxy=proxy,
                retry_decorator=retry_decorator,
                max_pages=max_pages,
            )
            return backend

        monkeypatch.setattr(lookup, "make_backend", construct)
        return record

    return install


async def call(profile, operation, params):
    return await lookup.run(profile, operation, validate_params(operation, params))


# --------------------------------------------------------------- lookup.profile


async def test_profile_returns_the_tracked_vocabulary_and_costs_one_request(
    lookup_profile, constructed
):
    from insto.service.history import _PROFILE_TRACKED_FIELDS

    backend = FakeBackend(quota=4211)
    record = constructed(backend)
    result = await call(lookup_profile, "lookup.profile", {"username": "@Alice"})
    assert result == {
        "kind": "lookup_profile",
        "target_pk": "17841400000000001",
        "access": "public",
        "fields": {
            "username": "alice",
            "full_name": "Alice Example",
            "biography": "bio line",
            "external_url": "https://example.test/alice",
            "is_verified": True,
            "is_business": False,
            "is_private": False,
            "follower_count": 1200,
            "following_count": 300,
            "media_count": 87,
            "public_email": "alice@example.test",
            "public_phone": None,
            "business_category": None,
        },
        "unknown_fields": [],
        "quota_remaining": 4211,
    }
    # Exactly the saved-history vocabulary and order, without the media hashes.
    assert list(result["fields"]) == list(_PROFILE_TRACKED_FIELDS)
    assert not {"avatar", "banner"} & set(result["fields"])
    assert backend.calls == [("profile_by_username", "alice")]
    assert backend.closed == 1
    assert record["token"] == "offline-desktop-token" and record["proxy"] is None
    assert record["max_pages"] == lookup.MAX_PAGE_REQUESTS

    # The captured policy itself must fail fast: a five-attempt ladder here
    # would sleep on the cooldown instead of raising on the first attempt.
    attempts = []

    @record["retry_decorator"]
    async def rate_limited():
        attempts.append(True)
        raise RateLimited(retry_after=300.0)

    started = asyncio.get_running_loop().time()
    with pytest.raises(RateLimited):
        await rate_limited()
    assert attempts == [True] and asyncio.get_running_loop().time() - started < 1.0


async def test_profile_reports_a_private_account_without_refusing_it(lookup_profile, constructed):
    constructed(FakeBackend(profile=profile_dto(access="private", is_private=True)))
    result = await call(lookup_profile, "lookup.profile", {"username": "alice"})
    assert result["access"] == "private"
    assert result["fields"]["is_private"] is True


async def test_profile_field_the_provider_cannot_supply_is_named_not_invented(
    lookup_profile, constructed, monkeypatch
):
    monkeypatch.setattr(
        lookup, "_PROFILE_TRACKED_FIELDS", (*lookup._PROFILE_TRACKED_FIELDS, "pronouns")
    )
    constructed(FakeBackend())
    result = await call(lookup_profile, "lookup.profile", {"username": "alice"})
    assert result["unknown_fields"] == ["pronouns"]
    assert "pronouns" not in result["fields"]


async def test_profile_strings_are_bounded(lookup_profile, constructed):
    overlong = {name: name[0] * 6000 for name in lookup._TEXT_CHARACTERS}
    constructed(FakeBackend(profile=profile_dto(**overlong)))
    fields = (await call(lookup_profile, "lookup.profile", {"username": "alice"}))["fields"]
    assert set(lookup._TEXT_CHARACTERS) <= set(fields)
    for name, characters in lookup._TEXT_CHARACTERS.items():
        assert fields[name] == overlong[name][:characters], name


@pytest.mark.parametrize(
    "backend",
    [
        FakeBackend(profile=profile_dto(access="deleted")),
        FakeBackend(errors={"resolve": ProfileNotFound("alice")}),
        FakeBackend(errors={"profile": ProfileDeleted("alice")}),
    ],
    ids=["deleted", "unresolvable", "vanished"],
)
async def test_profile_missing_account_is_target_not_found(lookup_profile, constructed, backend):
    constructed(backend)
    with pytest.raises(DesktopError, match="target_not_found"):
        await call(lookup_profile, "lookup.profile", {"username": "alice"})
    assert backend.closed == 1


async def test_profile_identity_the_app_cannot_reuse_is_refused(lookup_profile, constructed):
    constructed(FakeBackend(profile=profile_dto(pk="017")))
    with pytest.raises(DesktopError, match="provider_response_invalid"):
        await call(lookup_profile, "lookup.profile", {"username": "alice"})
    # A permanent property of the answer: the host must not offer a paid retry.
    assert MESSAGES["provider_response_invalid"][1] is False


@pytest.mark.parametrize("access", ["private", "followed", "blocked", "something_new"])
async def test_only_a_public_answer_is_reported_as_public(lookup_profile, constructed, access):
    """The permissive value is the one that means it; anything else is private."""
    constructed(FakeBackend(profile=profile_dto(access=access)))
    result = await call(lookup_profile, "lookup.profile", {"username": "alice"})
    assert result["access"] == "private"


# -------------------------------------------------------------- lookup.activity


def geotagged_window():
    return [
        make_post(0, place="Cafe Zero", lat=52.37, lng=4.89, likes=10, tags=["ams", "coffee"]),
        make_post(1, place="Cafe Zero", lat=52.37, lng=4.89, likes=30, tags=["ams"]),
        make_post(2, place="Museum", lat=52.36, lng=4.88, likes=20, mentions=["@bob"]),
        make_post(3, likes=40, tags=["coffee"], mentions=["Bob"]),
    ]


async def test_activity_analyses_one_window_of_posts(lookup_profile, constructed):
    backend = FakeBackend(posts=geotagged_window(), quota=4100)
    constructed(backend)
    result = await call(
        lookup_profile, "lookup.activity", {"target_pk": "17841400000000001", "window": 50}
    )
    assert result["kind"] == "lookup_activity"
    assert result["target_pk"] == "17841400000000001"
    assert result["window"] == 50 and result["analyzed"] == 4
    geo = result["geo"]
    assert geo["geotagged"] == 3
    assert geo["anchor"] == {"name": "Cafe Zero", "lat": 52.37, "lng": 4.89, "count": 2}
    assert geo["centroid"]["lat"] == pytest.approx(52.3666, abs=1e-3)
    assert 0.0 <= geo["radius_km"] < 5.0
    assert [place["name"] for place in geo["places"]] == ["Cafe Zero", "Museum"]
    timeline = result["timeline"]
    assert len(timeline["weekday_hours"]) == 7
    assert all(len(row) == 24 for row in timeline["weekday_hours"])
    assert sum(map(sum, timeline["weekday_hours"])) == 4
    assert timeline["weekday_hours"][1][10] == 1
    assert sum(timeline["hour_of_day"]) == 4 and timeline["hour_of_day"][10] == 4
    assert len(timeline["hour_of_day"]) == 24 and len(timeline["day_of_week"]) == 7
    assert timeline["day_of_week"][1] == 1  # the window starts on a Tuesday
    assert timeline["first_post_at"] == MOMENT
    assert timeline["last_post_at"] == MOMENT + 3 * DAY
    assert result["hashtags"] == [{"key": "ams", "count": 2}, {"key": "coffee", "count": 2}]
    assert result["mentions"] == [{"key": "bob", "count": 2}]
    assert result["locations"] == [{"key": "Cafe Zero", "count": 2}, {"key": "Museum", "count": 1}]
    assert result["likes"]["total"] == 100
    assert result["likes"]["average"] == 25.0
    assert result["likes"]["top_posts"][0] == {"code": "code3", "like_count": 40}
    assert len(result["likes"]["top_posts"]) == 4
    assert result["quota_remaining"] == 4100
    # One posts fetch, no per-post request, and the username is not resolved again.
    assert backend.calls == [("posts", "17841400000000001", 50, 0)]
    assert backend.closed == 1


async def test_activity_of_an_account_without_posts_is_the_same_shape(lookup_profile, constructed):
    constructed(FakeBackend(posts=[]))
    result = await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})
    assert result == {
        "kind": "lookup_activity",
        "target_pk": "7",
        "window": 12,
        "analyzed": 0,
        "geo": {
            "geotagged": 0,
            "anchor": None,
            "centroid": None,
            "radius_km": None,
            "places": [],
        },
        "timeline": {
            "weekday_hours": [[0] * 24 for _ in range(7)],
            "hour_of_day": [0] * 24,
            "day_of_week": [0] * 7,
            "first_post_at": None,
            "last_post_at": None,
        },
        "hashtags": [],
        "mentions": [],
        "locations": [],
        "likes": {"total": 0, "average": 0.0, "top_posts": []},
        "quota_remaining": None,
    }


async def test_activity_heatmap_preserves_joint_utc_buckets(lookup_profile, constructed):
    constructed(
        FakeBackend(
            posts=[
                make_post(0, when=int(datetime(2026, 10, 12, 23, 59, tzinfo=UTC).timestamp())),
                make_post(1, when=int(datetime(2026, 10, 13, 0, 1, tzinfo=UTC).timestamp())),
                make_post(2, when=0),
            ]
        )
    )
    result = await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})
    matrix = result["timeline"]["weekday_hours"]
    assert matrix[0][23] == matrix[1][0] == 1
    assert sum(map(sum, matrix)) == 2
    assert [sum(row) for row in matrix] == result["timeline"]["day_of_week"]
    assert [sum(row[hour] for row in matrix) for hour in range(24)] == result["timeline"][
        "hour_of_day"
    ]


@pytest.mark.parametrize(
    "window,page,fetches,analyzed", [(12, 50, 1, 12), (30, 12, 3, 30), (50, 50, 1, 50)]
)
async def test_activity_stops_at_the_window(
    lookup_profile, constructed, window, page, fetches, analyzed
):
    backend = FakeBackend(posts=[make_post(i) for i in range(60)], page=page)
    constructed(backend)
    result = await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": window})
    assert result["analyzed"] == analyzed == result["window"]
    assert len(backend.calls) == fetches
    assert all(call[0] == "posts" and call[2] == window for call in backend.calls)


async def test_activity_bounds_every_list_and_string(lookup_profile, constructed):
    posts = [
        make_post(
            index,
            place=f"{index:03d}-" + "P" * 300,
            lat=1.0 + index / 1000,
            lng=2.0,
            likes=index,
            tags=[f"tag{index}", "x" * 300],
            mentions=[f"user{index}"],
        )
        for index in range(50)
    ]
    constructed(FakeBackend(posts=posts))
    result = await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 50})
    assert len(result["hashtags"]) == 20
    assert len(result["mentions"]) == 20
    assert len(result["locations"]) == 20
    assert len(result["geo"]["places"]) == 10
    assert len(result["likes"]["top_posts"]) == 5
    assert all(len(item["key"]) <= 120 for item in result["hashtags"] + result["locations"])
    assert len(result["geo"]["anchor"]["name"]) == 120
    assert len({item["key"] for item in result["locations"]}) == 20
    assert all(len(item["code"]) <= 64 for item in result["likes"]["top_posts"])
    encoded = json.dumps(result, ensure_ascii=True, allow_nan=False).encode("ascii")
    assert len(encoded) < 64 * 1024


@pytest.mark.parametrize(
    "error,code",
    [
        (ProfilePrivate("alice"), "target_private"),
        # A bare 403 names no cause: it may be the target or this account's own
        # access, and the user must not be told their plan problem is privacy.
        (Banned("login-walled"), "target_unavailable"),
    ],
)
async def test_activity_of_a_private_or_refused_account(lookup_profile, constructed, error, code):
    backend = FakeBackend(errors={"posts": error})
    constructed(backend)
    with pytest.raises(DesktopError) as raised:
        await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})
    assert raised.value.code == code
    assert MESSAGES[code][1] is False
    assert backend.closed == 1


async def test_activity_of_a_vanished_account(lookup_profile, constructed):
    constructed(FakeBackend(errors={"posts": ProfileNotFound("7")}))
    with pytest.raises(DesktopError, match="target_not_found"):
        await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})


# ------------------------------------------------------------- budget and errors


@pytest.mark.parametrize(
    "error,code",
    [
        (AuthInvalid("token-secret"), "invalid_token"),
        (QuotaExhausted("balance"), "quota_exhausted"),
        (RateLimited(300.0, "cooldown"), "rate_limited"),
        (Transient("blip"), "network_error"),
        (SchemaDrift("user", "pk"), "provider_response_invalid"),
        (Banned("login-walled"), "target_unavailable"),
        (ProfilePrivate("alice"), "target_private"),
        (BackendError("surprise"), "access_unconfirmed"),
        (RuntimeError("bug"), "access_unconfirmed"),
        (TimeoutError("slow"), "operation_timeout"),
    ],
)
async def test_provider_failures_map_to_static_codes(lookup_profile, constructed, error, code):
    backend = FakeBackend(errors={"resolve": error})
    constructed(backend)
    with pytest.raises(DesktopError) as raised:
        await call(lookup_profile, "lookup.profile", {"username": "alice"})
    assert raised.value.code == code
    assert str(error) not in str(raised.value)
    assert code in MESSAGES
    assert backend.closed == 1


async def test_rate_limit_is_reported_at_once_and_a_blip_is_retried_once():
    loop = asyncio.get_running_loop()
    attempts = []

    @lookup._fail_fast()
    async def rate_limited():
        attempts.append(True)
        raise RateLimited(retry_after=300.0)

    started = loop.time()
    with pytest.raises(RateLimited):
        await rate_limited()
    # The CLI ladder would have slept for the announced cooldown here.
    assert attempts == [True] and loop.time() - started < 1.0

    transient = []

    @lookup._fail_fast()
    async def once():
        transient.append(True)
        if len(transient) == 1:
            raise Transient("blip")
        return "ok"

    started = loop.time()
    assert await once() == "ok"
    assert len(transient) == 2
    assert loop.time() - started <= lookup.TRANSIENT_RETRY_SECONDS + 1.0

    @lookup._fail_fast()
    async def broken():
        transient.append(True)
        raise Transient("blip")

    with pytest.raises(Transient):
        await broken()
    assert len(transient) == 4  # one retry, never a ladder


async def test_hanging_provider_is_cancelled_within_the_budget(
    lookup_profile, constructed, monkeypatch
):
    monkeypatch.setattr(lookup, "NETWORK_READ_SECONDS", 0.05)
    backend = FakeBackend(hang=True)
    constructed(backend)
    started = asyncio.get_running_loop().time()
    with pytest.raises(DesktopError, match="operation_timeout"):
        await call(lookup_profile, "lookup.profile", {"username": "alice"})
    assert asyncio.get_running_loop().time() - started < 5.0
    assert backend.closed == 1


async def test_unconfigured_profile_never_constructs_a_provider(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(lookup, "make_backend", lambda *a, **k: called.append(a))
    profile = Profile(tmp_path / "empty")
    with pytest.raises(DesktopError, match="not_configured"):
        await call(profile, "lookup.profile", {"username": "alice"})
    assert called == []


async def test_the_profiles_own_proxy_is_used(lookup_profile, constructed, monkeypatch):
    payload = lookup_profile.read_config()
    config = lookup.parse_profile_config(lookup_profile, payload)
    monkeypatch.setattr(
        lookup,
        "parse_profile_config",
        lambda profile, raw: Config(
            **{
                **{
                    field: getattr(config, field)
                    for field in config.__dataclass_fields__
                    if field != "hiker_proxy"
                },
                "hiker_proxy": "socks5://127.0.0.1:9050",
            }
        ),
    )
    record = constructed(FakeBackend())
    await call(lookup_profile, "lookup.profile", {"username": "alice"})
    assert record["proxy"] == "socks5://127.0.0.1:9050"


# ------------------------------------------------------------------------ purity


def inventory(root):
    return {
        str(path.relative_to(root)): path.stat().st_mtime_ns
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def test_a_lookup_writes_nothing_at_all(lookup_profile, constructed):
    before = inventory(lookup_profile.root)
    constructed(FakeBackend(posts=geotagged_window(), quota=12))
    await call(lookup_profile, "lookup.profile", {"username": "alice"})
    await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})
    assert inventory(lookup_profile.root) == before
    assert not (lookup_profile.home / "store.db").exists()
    assert not (lookup_profile.home / "output").exists()
    assert not (lookup_profile.home / "cli_history").exists()
    assert lookup_profile.read_state()["quota_remaining"] == 8


async def test_a_lookup_opens_no_database_and_takes_no_lock(
    lookup_profile, constructed, monkeypatch
):
    import sqlite3

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: pytest.fail("a lookup must not open a database")
    )
    monkeypatch.setattr(
        Profile, "locked", lambda *a, **k: pytest.fail("a lookup must not lock the profile")
    )
    constructed(FakeBackend(posts=geotagged_window()))
    await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})


@pytest.mark.parametrize(
    "operation,params",
    [
        ("lookup.profile", {"username": "alice"}),
        # The generator path finalises an async iterator on cancellation; its
        # stderr must stay as empty as the plain coroutine's.
        ("lookup.activity", {"target_pk": "7", "window": 12}),
    ],
)
async def test_the_process_path_answers_a_hanging_provider_with_operation_timeout(
    lookup_profile, tmp_path, operation, params
):
    """The whole child process: a cancelled worker becomes one static envelope."""
    import os
    import subprocess
    import sys

    script = """
import asyncio
import sys

import insto.desktop.lookup as lookup


class Hanging:
    async def get_profile_by_username(self, username):
        await asyncio.Event().wait()

    async def iter_user_posts(self, pk, *, limit=None):
        await asyncio.Event().wait()
        yield None

    def get_quota(self):
        raise AssertionError("never reached")

    async def aclose(self):
        return None


lookup.NETWORK_READ_SECONDS = 0.2
lookup.make_backend = lambda *args, **kwargs: Hanging()

from insto.desktop.dispatch import handle

sys.stdout.buffer.write(asyncio.run(handle(sys.stdin.buffer.read())))
"""
    request = (
        json.dumps(
            {
                "protocol_version": 1,
                "request_id": "hang",
                "operation": operation,
                "params": params,
            }
        )
        + "\n"
    ).encode()
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        input=request,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "INSTO_DESKTOP_ROOT": str(lookup_profile.root)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    response = json.loads(result.stdout)
    assert response["request_id"] == "hang"
    assert response["error"] == {
        "code": "operation_timeout",
        "message": MESSAGES["operation_timeout"][0],
        "retryable": False,
    }
    assert not (lookup_profile.home / "store.db").exists()


# ------------------------------------------------- the paid-request ceiling


def transport_backend(handler, *, max_pages=None):
    """A real `HikerBackend` with the lookup's own policies over a mock transport.

    The page ceiling and the retry policy are the ones `lookup.run` installs, so
    these tests count the HTTP requests a real provider would actually be paid
    for, not the calls a fake chose to record.
    """
    import hikerapi
    import httpx

    from insto.backends.hiker import HikerBackend

    sdk = hikerapi.AsyncClient(token="offline-desktop-token", timeout=5.0)
    client = httpx.AsyncClient(base_url=sdk._url, transport=httpx.MockTransport(handler))
    client.headers.update(sdk._headers)
    sdk._client = client
    return HikerBackend(
        client=sdk,
        retry_decorator=lookup._fail_fast(),
        max_pages=lookup.MAX_PAGE_REQUESTS if max_pages is None else max_pages,
    )


async def test_lookup_installs_shared_ledger_and_maps_exhausted_cap(lookup_profile, constructed):
    import httpx

    from insto.service.usage import UsageLedger, usage_path

    ledger = UsageLedger(usage_path(lookup_profile.home / "store.db"))
    await ledger.configure(1)
    await ledger.reserve()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    backend = transport_backend(handler)
    constructed(backend)
    with pytest.raises(DesktopError, match="daily_budget_reached"):
        await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})
    assert requests == []
    assert MESSAGES["daily_budget_reached"][1] is False


async def test_lookup_counts_failed_http_attempts(lookup_profile, constructed):
    import httpx

    from insto.service.usage import UsageLedger, usage_path

    backend = transport_backend(lambda request: httpx.Response(503))
    constructed(backend)
    with pytest.raises(DesktopError, match="network_error"):
        await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12})
    state = await UsageLedger(usage_path(lookup_profile.home / "store.db")).inspect()
    assert state["used"] == 2


def media(index):
    return {
        "pk": str(9000 + index),
        "code": f"code{index}",
        "taken_at": MOMENT + index,
        "media_type": 1,
        "like_count": index,
    }


def chunk_handler(requests, *, per_page, endless=True, total=None):
    """A provider whose cursor never terminates unless `total` posts are served."""
    import httpx

    def handler(request):
        requests.append(str(request.url))
        served = len(requests) - 1
        if total is not None and served * per_page >= total:
            return httpx.Response(200, json=[[], None])
        items = [media(served * per_page + i) for i in range(per_page)]
        cursor = f"cursor-{served + 1}" if endless or total is not None else None
        return httpx.Response(200, json=[items, cursor])

    return handler


@pytest.mark.parametrize("per_page", [0, 1], ids=["empty-pages", "one-item-pages"])
async def test_a_never_terminating_cursor_stops_at_the_page_ceiling(per_page):
    """A thin or empty page with a live cursor must not keep spending money."""
    requests = []
    backend = transport_backend(chunk_handler(requests, per_page=per_page))
    try:
        result = await lookup._read(backend, "lookup.activity", {"target_pk": "7", "window": 50})
    finally:
        await backend.aclose()
    assert len(requests) == lookup.MAX_PAGE_REQUESTS <= 6
    # What was already paid for is answered, with the honest count.
    assert result["kind"] == "lookup_activity"
    assert result["analyzed"] == per_page * lookup.MAX_PAGE_REQUESTS
    assert result["window"] == 50
    assert result["likes"]["total"] == sum(range(result["analyzed"]))


@pytest.mark.parametrize(
    "lat, lng",
    [(90.5, 4.9), (-91.0, 4.9), (52.4, 180.5), (52.4, -181.0)],
    ids=["lat-high", "lat-low", "lng-high", "lng-low"],
)
def test_a_coordinate_outside_its_range_is_not_a_location(lat, lng):
    """The desktop host refuses such a value; the core never sends one."""
    post = lookup._locatable(make_post(0, place="Somewhere", lat=lat, lng=lng))
    assert post.location_lat is None and post.location_lng is None
    assert lookup._centroid(lat, lng) is None


@pytest.mark.parametrize("radius", [-1.0, 20_100.5, float("inf")])
def test_a_radius_no_place_on_earth_can_have_is_not_reported(radius):
    assert lookup._distance(radius) is None


def test_the_largest_real_radius_and_the_poles_are_kept():
    assert lookup._distance(20_100.0) == 20_100.0
    assert lookup._centroid(90.0, -180.0) == {"lat": 90.0, "lng": -180.0}


async def test_lookup_activity_worst_case_is_twelve_paid_requests():
    """Six pages, each failing once and then answering: the two factors multiplied.

    `MAX_PAGE_REQUESTS` bounds the pages and the retry policy allows one more
    attempt per page; the documented ceiling is their product, so this pins
    the multiplication itself rather than trusting the two tests that prove
    each factor alone.
    """
    import httpx

    requests = []
    inner = chunk_handler(requests, per_page=1)

    def handler(request):
        # Every page fails once before it answers, so each costs two requests.
        attempt = sum(1 for url in requests if url == str(request.url)) + 1
        if attempt == 1:
            requests.append(str(request.url))
            return httpx.Response(500, json={"detail": "provider blip"})
        return inner(request)

    backend = transport_backend(handler)
    try:
        result = await lookup._read(backend, "lookup.activity", {"target_pk": "7", "window": 50})
    finally:
        await backend.aclose()
    assert len(requests) == 2 * lookup.MAX_PAGE_REQUESTS == 12
    assert result["analyzed"] == lookup.MAX_PAGE_REQUESTS


async def test_a_normal_page_reaches_the_largest_window_inside_the_ceiling():
    requests = []
    backend = transport_backend(chunk_handler(requests, per_page=12, total=120))
    try:
        result = await lookup._read(backend, "lookup.activity", {"target_pk": "7", "window": 50})
    finally:
        await backend.aclose()
    # 50 posts at a 12-item page is 5 requests — one below the ceiling.
    assert len(requests) == 5 < lookup.MAX_PAGE_REQUESTS
    assert result["analyzed"] == 50


async def test_lookup_profile_worst_case_is_two_paid_requests():
    """One call with at most the one quick retry the policy allows."""
    import httpx

    requests = []

    def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(500, json={"detail": "provider blip"})
        return httpx.Response(
            200,
            json={"user": {"pk": "7", "username": "alice", "follower_count": 3}},
            headers={"x-quota-remaining": "4199"},
        )

    backend = transport_backend(handler)
    try:
        result = await lookup._read(backend, "lookup.profile", {"username": "alice"})
    finally:
        await backend.aclose()
    assert requests == ["/v2/user/by/username", "/v2/user/by/username"]
    assert result["target_pk"] == "7" and result["fields"]["follower_count"] == 3
    assert result["quota_remaining"] == 4199


async def test_a_provider_that_answers_cleanly_costs_one_request():
    import httpx

    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"user": {"pk": "7", "username": "alice"}})

    backend = transport_backend(handler)
    try:
        await lookup._read(backend, "lookup.profile", {"username": "alice"})
    finally:
        await backend.aclose()
    assert requests == ["/v2/user/by/username"]


async def test_the_recorded_username_answer_carries_every_tracked_field():
    """The one-request read rests on this: a real `user_by_username_v2` answer
    maps to the same profile the by-pk read would give, field for field."""
    import httpx

    from insto.backends._hiker_map import map_profile

    recorded = json.loads(
        (Path(__file__).parent / "fixtures" / "hiker" / "profile_by_username_v2.json").read_text()
    )
    expected = map_profile(recorded["user"])
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=recorded)

    backend = transport_backend(handler)
    try:
        result = await lookup._read(backend, "lookup.profile", {"username": "instagram"})
    finally:
        await backend.aclose()
    assert requests == ["/v2/user/by/username"]
    assert result["target_pk"] == expected.pk == "25025320"
    assert result["unknown_fields"] == []
    assert result["fields"]["username"] == "instagram"
    assert result["fields"]["follower_count"] == expected.follower_count > 0
    assert result["fields"]["media_count"] == expected.media_count > 0
    assert result["fields"]["is_verified"] is True
    assert result["fields"]["biography"] == expected.biography != ""
    assert result["fields"]["external_url"] == expected.external_url
    # Empty contact strings are reported as absent values, not invented ones.
    assert result["fields"]["public_email"] is None


async def test_the_fake_ceiling_is_the_one_the_lookup_asks_for(lookup_profile, constructed):
    backend = FakeBackend(posts=[make_post(i) for i in range(4)], page=1, endless=True)
    record = constructed(backend)
    result = await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 50})
    assert record["max_pages"] == lookup.MAX_PAGE_REQUESTS
    assert len(backend.calls) == lookup.MAX_PAGE_REQUESTS
    assert result["analyzed"] == 4
    assert backend.closed == 1


# ------------------------------------------------------ honesty of the answer


async def test_a_coordinate_json_cannot_carry_is_not_counted_as_geotagged(
    lookup_profile, constructed
):
    """One NaN used to poison the centroid and radius of the whole window."""
    posts = [
        make_post(0, place="Cafe Zero", lat=52.37, lng=4.89),
        make_post(1, place="Cafe Zero", lat=52.37, lng=4.89),
        make_post(2, place="Nowhere", lat=float("nan"), lng=float("inf")),
    ]
    constructed(FakeBackend(posts=posts))
    geo = (await call(lookup_profile, "lookup.activity", {"target_pk": "7", "window": 12}))["geo"]
    assert geo["geotagged"] == 2
    assert geo["anchor"] == {"name": "Cafe Zero", "lat": 52.37, "lng": 4.89, "count": 2}
    assert geo["centroid"] == {"lat": 52.37, "lng": 4.89}
    assert geo["radius_km"] == 0.0
    assert [place["name"] for place in geo["places"]] == ["Cafe Zero"]


async def test_no_provider_text_ever_reaches_the_wire(lookup_profile, constructed, monkeypatch):
    from insto.desktop.dispatch import handle

    monkeypatch.setenv("INSTO_DESKTOP_ROOT", str(lookup_profile.root))
    constructed(FakeBackend(errors={"resolve": AuthInvalid("offline-token-sentinel")}))
    raw = await handle(
        (
            json.dumps(
                {
                    "protocol_version": 1,
                    "request_id": "leak",
                    "operation": "lookup.profile",
                    "params": {"username": "alice"},
                }
            )
            + "\n"
        ).encode()
    )
    assert b"offline-token-sentinel" not in raw
    assert json.loads(raw)["error"] == {
        "code": "invalid_token",
        "message": MESSAGES["invalid_token"][0],
        "retryable": False,
    }
