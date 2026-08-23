"""Persistence and observation-state tests for the asynchronous bridge."""

# isort: off
import _helpers  # noqa: F401
from pi_agent_bridge import TaskRegistry, TaskStatus  # noqa: E402
from pi_agent_bridge.registry import InvalidTaskTransition, TaskNotFoundError  # noqa: E402
# isort: on


def test_task_lifecycle_and_resume(tmp_path):
    with TaskRegistry(tmp_path / "tasks.db") as registry:
        task = registry.create_task(owner_key="qq:1", prompt="research", context={"role": "system"})
        assert task.status is TaskStatus.QUEUED
        assert registry.transition_status(task.task_id, TaskStatus.RUNNING).status is TaskStatus.RUNNING
        for index in range(2):
            updated, _, inserted = registry.record_snapshot(task.task_id, {"phase": "waiting"}, has_meaningful_event=False)
            assert inserted is (index == 0)
            assert updated.status is TaskStatus.RUNNING
        observed, _, _ = registry.record_snapshot(task.task_id, {"phase": "waiting", "tick": 3}, has_meaningful_event=False)
        assert observed.status is TaskStatus.RUNNING
        assert observed.no_meaningful_event_count == 3
        assert registry.get_task(task.task_id).status is TaskStatus.RUNNING
        assert registry.get_task(task.task_id).no_meaningful_event_count == 3


def test_meaningful_event_clears_counter_and_updates_cursor(tmp_path):
    with TaskRegistry(tmp_path / "tasks.db") as registry:
        task = registry.create_task(owner_key="owner", prompt="x")
        registry.transition_status(task.task_id, TaskStatus.RUNNING)
        registry.record_snapshot(task.task_id, {"n": 1}, has_meaningful_event=False)
        updated, snapshot, inserted = registry.record_snapshot(task.task_id, {"n": 2}, has_meaningful_event=True, event_cursor="42")
        assert inserted and snapshot is not None
        assert updated.no_meaningful_event_count == 0
        assert updated.event_cursor == "42"
        assert registry.get_latest_snapshot(task.task_id).payload == {"n": 2}
        assert [item.payload for item in registry.list_snapshots(task.task_id)] == [{"n": 1}, {"n": 2}]


def test_repeated_snapshot_is_deduplicated_but_poll_still_counts(tmp_path):
    with TaskRegistry(tmp_path / "tasks.db") as registry:
        task = registry.create_task(owner_key="owner", prompt="x")
        registry.transition_status(task.task_id, TaskStatus.RUNNING)
        _, first, inserted = registry.record_snapshot(task.task_id, {"same": True}, has_meaningful_event=False)
        assert inserted and first is not None
        _, second, inserted = registry.record_snapshot(task.task_id, {"same": True}, has_meaningful_event=False)
        assert not inserted and second.snapshot_id == first.snapshot_id
        assert registry.get_task(task.task_id).no_meaningful_event_count == 2


def test_latest_snapshot_replaces_previous_observation(tmp_path):
    with TaskRegistry(tmp_path / "tasks.db") as registry:
        task = registry.create_task(owner_key="owner", prompt="x")
        registry.transition_status(task.task_id, TaskStatus.RUNNING)
        _, first, _ = registry.record_snapshot(
            task.task_id,
            {"phase": "first"},
            has_meaningful_event=True,
            event_cursor="1",
        )
        registry.record_snapshot(
            task.task_id,
            {"phase": "second"},
            has_meaningful_event=True,
            event_cursor="2",
        )
        _, repeated, inserted = registry.record_snapshot(
            task.task_id,
            {"phase": "first"},
            has_meaningful_event=True,
            event_cursor="3",
        )

        assert inserted
        assert first is not None and repeated is not None
        assert repeated.snapshot_id != first.snapshot_id
        latest = registry.get_latest_snapshot(task.task_id)
        assert latest is not None
        assert latest.snapshot_id == repeated.snapshot_id
        assert latest.event_cursor == "3"
        assert latest.payload == {"phase": "first"}

        with registry._lock:
            count = registry._connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE task_id=?", (task.task_id,)
            ).fetchone()[0]
        assert count == 3


def test_artifacts_are_metadata_and_delete_cascades(tmp_path):
    with TaskRegistry(tmp_path / "tasks.db") as registry:
        task = registry.create_task(owner_key="owner", prompt="x")
        artifact = registry.add_artifact(task.task_id, kind="markdown", path="result.md", metadata={"title": "done"})
        assert registry.list_artifacts(task.task_id)[0] == artifact
        registry.delete_task(task.task_id)
        assert registry.list_tasks() == []
        try:
            registry.get_task(task.task_id)
        except TaskNotFoundError:
            pass
        else:
            raise AssertionError("deleted task should not be readable")


def test_state_survives_registry_reopen_and_cursor_update(tmp_path):
    database = tmp_path / "tasks.db"
    with TaskRegistry(database) as registry:
        task = registry.create_task(owner_key="owner", prompt="x")
        registry.update_event_cursor(task.task_id, "cursor-1")
        registry.add_artifact(task.task_id, kind="json", metadata={"ok": True})
    with TaskRegistry(database) as reopened:
        loaded = reopened.get_task(task.task_id)
        assert loaded.event_cursor == "cursor-1"
        assert reopened.list_artifacts(task.task_id)[0].metadata == {"ok": True}
        assert reopened.journal_mode == "wal"


def test_invalid_transition_is_rejected(tmp_path):
    with TaskRegistry(tmp_path / "tasks.db") as registry:
        task = registry.create_task(owner_key="owner", prompt="x")
        registry.transition_status(task.task_id, TaskStatus.RUNNING)
        registry.transition_status(task.task_id, TaskStatus.COMPLETED)
        try:
            registry.transition_status(task.task_id, TaskStatus.RUNNING)
        except InvalidTaskTransition:
            pass
        else:
            raise AssertionError("terminal task must not resume")
