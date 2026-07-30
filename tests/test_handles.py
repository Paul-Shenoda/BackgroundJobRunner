"""Handle allocation and recycling — the most scrutinised behaviour in the
brief, verified here purely through what the API exposes."""

from tests.helpers import cancel_and_confirm, submit, wait_for_status


async def test_numbering_is_per_lane(client_a):
    scrape = await submit(client_a, "scrape", {"duration_ms": 5000})
    report = await submit(client_a, "report", {"duration_ms": 5000})

    assert scrape["handle"] == "scrape-1"
    assert report["handle"] == "report-1", (
        f"expected report's own counter to start at 1, got {report['handle']!r} — "
        f"lanes must not share one sequence"
    )

    await cancel_and_confirm(client_a, scrape["handle"], scrape["created_at"])
    await cancel_and_confirm(client_a, report["handle"], report["created_at"])


async def test_second_submit_while_active_gets_next_number(client_a):
    first = await submit(client_a, "scrape", {"duration_ms": 8000})
    assert first["handle"] == "scrape-1"

    second = await submit(client_a, "scrape", {"duration_ms": 8000})
    assert second["handle"] == "scrape-2", (
        f"expected scrape-2 while scrape-1 is still active, got {second['handle']!r}"
    )

    await cancel_and_confirm(client_a, first["handle"], first["created_at"])
    await cancel_and_confirm(client_a, second["handle"], second["created_at"])


async def test_handle_recycles_after_collect_no_collision_while_active(client_a):
    # Part 1: ready -> collect -> the freed number gets reused.
    original = await submit(client_a, "scrape", {"duration_ms": 500})
    assert original["handle"] == "scrape-1"
    await wait_for_status(client_a, "scrape-1", {"ready"}, original["created_at"], timeout=10)

    collect_resp = await client_a.get("/tasks/scrape-1/result")
    assert collect_resp.status_code == 200
    assert collect_resp.json()["collected"] is True

    recycled = await submit(client_a, "scrape", {"duration_ms": 8000})
    assert recycled["handle"] == "scrape-1", (
        f"expected the freed number to be reused as scrape-1, got {recycled['handle']!r}"
    )
    # From here, "scrape-1" the STRING names a different row than `original`
    # did — the old one is terminal+collected, this one is active. Every
    # further wait on this handle uses recycled['created_at'], never
    # original['created_at'], so a stale poll can't silently reattach to
    # the dead row (see wait_for_status's docstring).
    assert recycled["created_at"] != original["created_at"]

    # Part 2: while `recycled` is still active (uncollected), a fresh
    # submission must NOT reuse its number.
    while_active = await submit(client_a, "scrape", {"duration_ms": 500})
    assert while_active["handle"] == "scrape-2", (
        f"expected scrape-2 while scrape-1 is still active (uncollected), "
        f"got {while_active['handle']!r} — handle collision risk"
    )

    await cancel_and_confirm(client_a, "scrape-1", recycled["created_at"])
    await wait_for_status(
        client_a, "scrape-2", {"ready", "failed"}, while_active["created_at"], timeout=10
    )


async def test_handle_recycles_after_collecting_a_failure(client_a):
    task = await submit(client_a, "scrape", {"fail_permanent": True, "duration_ms": 300})
    assert task["handle"] == "scrape-1"
    failed = await wait_for_status(client_a, "scrape-1", {"failed"}, task["created_at"], timeout=10)
    assert failed["collected"] is False

    collect_resp = await client_a.get("/tasks/scrape-1/result")
    assert collect_resp.status_code == 200
    assert collect_resp.json()["collected"] is True

    reused = await submit(client_a, "scrape", {"duration_ms": 500})
    assert reused["handle"] == "scrape-1", (
        f"collecting a FAILED task should free its number just like a ready one; "
        f"got {reused['handle']!r} — handle-holding must depend on `collected`, not outcome"
    )
    await wait_for_status(client_a, "scrape-1", {"ready", "failed"}, reused["created_at"], timeout=10)


async def test_cancelled_handle_is_immediately_reusable(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 8000})
    assert task["handle"] == "scrape-1"
    await cancel_and_confirm(client_a, "scrape-1", task["created_at"])

    reused = await submit(client_a, "scrape", {"duration_ms": 500})
    assert reused["handle"] == "scrape-1", (
        f"a cancelled task's number should be reusable without any collect step, "
        f"got {reused['handle']!r}"
    )
    await wait_for_status(client_a, "scrape-1", {"ready", "failed"}, reused["created_at"], timeout=10)
