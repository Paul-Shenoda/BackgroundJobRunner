"""Per-user scoping, proven rather than assumed: independent handle
numbering, and a user must never see or act on another user's tasks."""

import asyncio

from tests.helpers import cancel_and_confirm, submit, wait_for_status


async def test_users_get_independent_handle_numbering(client_a, client_b):
    task_a, task_b = await asyncio.gather(
        submit(client_a, "scrape", {"duration_ms": 5000}),
        submit(client_b, "scrape", {"duration_ms": 5000}),
    )
    assert task_a["handle"] == "scrape-1"
    assert task_b["handle"] == "scrape-1", (
        f"expected user B's independent counter to also start at scrape-1, "
        f"got {task_b['handle']!r} — numbering must not be shared across users"
    )

    await cancel_and_confirm(client_a, task_a["handle"], task_a["created_at"])
    await cancel_and_confirm(client_b, task_b["handle"], task_b["created_at"])


async def test_user_cannot_see_others_tasks(client_a, client_b):
    task_a = await submit(client_a, "scrape", {"duration_ms": 5000})

    b_list = await client_b.get("/tasks")
    assert b_list.status_code == 200
    b_handles = {t["handle"] for t in b_list.json()}
    assert task_a["handle"] not in b_handles, "user B's task list must not contain user A's handle"

    b_get = await client_b.get(f"/tasks/{task_a['handle']}")
    assert b_get.status_code == 404

    await cancel_and_confirm(client_a, task_a["handle"], task_a["created_at"])


async def test_user_cannot_cancel_or_collect_others_tasks(client_a, client_b):
    running_task = await submit(client_a, "scrape", {"duration_ms": 6000})
    ready_task = await submit(client_a, "scrape", {"duration_ms": 300})

    await wait_for_status(
        client_a, ready_task["handle"], {"ready"}, ready_task["created_at"], timeout=10
    )

    cancel_resp = await client_b.post(f"/tasks/{running_task['handle']}/cancel")
    assert cancel_resp.status_code == 404, "cross-user cancel must 404, never 403 (don't confirm existence)"

    collect_resp = await client_b.get(f"/tasks/{ready_task['handle']}/result")
    assert collect_resp.status_code == 404, "cross-user collect must 404, never confirm the task exists"

    await cancel_and_confirm(client_a, running_task["handle"], running_task["created_at"])
