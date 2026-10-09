"""V3 milestone 2: run queue, catch-up, file and webhook triggers."""

from __future__ import annotations

import http.client
import json
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from miniflow.schedule import parse_schedules
from miniflow.store import StateStore
from miniflow.ui import DashboardServer
from miniflow.ui.control import ControlError, RunController
from miniflow.ui.schedules import ScheduleService


def at(*args: int) -> float:
    return datetime(*args).timestamp()


def write_flow(folder: Path, name: str, steps: dict, **extra) -> None:
    (folder / f"{name}.json").write_text(json.dumps({"steps": steps, **extra}), encoding="utf-8")


HOLD = {"hold": {"block": "wait", "settings": {"seconds": 1.2}}}
SAY = {"say": {"block": "log", "settings": {"message": "hello"}}}
TRIGGER = {"say": {"block": "log", "settings": {"message": "{{ env.MINIFLOW_TRIGGER }}"}}}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    folder = tmp_path / "flows"
    folder.mkdir()
    write_flow(folder, "hold", HOLD)
    write_flow(folder, "quick", SAY)
    write_flow(folder, "other", SAY)
    write_flow(folder, "trigger", TRIGGER)
    return folder


@pytest.fixture
def store(tmp_path: Path):
    with StateStore(tmp_path / "runs.db") as s:
        yield s


@pytest.fixture
def controller(flows: Path, store: StateStore):
    c = RunController(flows, store, grace=0.5)
    yield c
    c.shutdown()


def wait_idle(c: RunController, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        status = c.status()
        if status["active"] is None and not status["queued"]:
            return
        assert time.monotonic() < deadline, f"still busy: {status}"
        time.sleep(0.05)


def statuses(store: StateStore) -> list[str]:
    return [e.status for e in reversed(store.list_schedule_events())]


# --------------------------------------------------------------------- queue
def test_enqueue_starts_at_once_when_free(controller, store):
    assert controller.enqueue("quick", source="schedule:a") == "started"
    wait_idle(controller)
    assert store.latest_run("quick") is not None


def test_busy_controller_queues_runs_in_order(controller, store):
    controller.start("hold", 0.0)
    assert controller.enqueue("quick", source="schedule:a") == "queued"
    assert controller.enqueue("other", source="schedule:b") == "queued"
    queued = controller.status()["queued"]
    assert [(q["flow"], q["source"]) for q in queued] == [
        ("quick", "schedule:a"),
        ("other", "schedule:b"),
    ]
    wait_idle(controller)
    runs = {r.flow: r for r in store.list_runs()}
    assert runs["hold"].finished_at <= runs["quick"].started_at <= runs["other"].started_at
    assert {r.status for r in runs.values()} == {"SUCCESS"}


def test_a_source_waits_in_the_queue_only_once(controller):
    controller.start("hold", 0.0)
    controller.enqueue("quick", source="schedule:a")
    with pytest.raises(ControlError) as err:
        controller.enqueue("quick", source="schedule:a")
    assert err.value.status == HTTPStatus.CONFLICT
    assert "already waiting" in str(err.value)
    controller.enqueue("quick", source="schedule:b")  # another source is fine


def test_the_queue_has_a_limit(controller):
    controller.max_queue = 2
    controller.start("hold", 0.0)
    controller.enqueue("quick", source="s:1")
    controller.enqueue("quick", source="s:2")
    with pytest.raises(ControlError) as err:
        controller.enqueue("quick", source="s:3")
    assert err.value.status == HTTPStatus.TOO_MANY_REQUESTS


def test_callbacks_report_start_and_finish(controller, flows):
    write_flow(flows, "broken", {"x": {"block": "no_such_block", "settings": {}}})
    controller.add_flow("broken", flows / "broken.json")
    log: list[tuple] = []
    controller.start("hold", 0.0)
    controller.enqueue("quick", source="s:ok", on_start=lambda: log.append(("start", "ok")),
                       on_finish=lambda error: log.append(("finish", "ok", error)))  # fmt: skip
    controller.enqueue("broken", source="s:bad", on_start=lambda: log.append(("start", "bad")),
                       on_finish=lambda error: log.append(("finish", "bad", error)))  # fmt: skip
    wait_idle(controller)
    assert log[:3] == [("start", "ok"), ("finish", "ok", None), ("start", "bad")]
    assert log[3][:2] == ("finish", "bad")
    assert "no_such_block" in log[3][2]


def test_shutdown_drops_the_queue(flows, store):
    c = RunController(flows, store, grace=0.5)
    c.start("hold", 0.0)
    c.enqueue("quick", source="s:1")
    c.shutdown()
    assert c.status()["queued"] == []
    time.sleep(0.3)
    assert store.latest_run("quick") is None  # it never started


def test_trigger_details_reach_the_flow(controller, store):
    trigger = {"type": "webhook", "body": {"order": 7}}
    controller.enqueue("trigger", source="webhook:hook", trigger=trigger)
    wait_idle(controller)
    message = store.load_completed(store.latest_run("trigger"))["say"]["message"]
    assert json.loads(message) == trigger


def test_manual_runs_join_the_queue_when_busy(controller, store):
    controller.start("hold", 0.0)
    assert controller.start("quick", 0.0)["queued"] is True
    wait_idle(controller)
    assert store.latest_run("quick") is not None


def test_two_different_flows_run_at_once_when_capacity_is_two(flows, store):
    write_flow(flows, "hold_two", HOLD)
    controller = RunController(flows, store, max_concurrent=2, grace=0.5)
    try:
        controller.start("hold", 0.0)
        controller.start("hold_two", 0.0)
        assert {item["flow"] for item in controller.status()["running"]} == {"hold", "hold_two"}
        wait_idle(controller)
        runs = {run.flow: run for run in store.list_runs()}
        assert max(runs["hold"].started_at, runs["hold_two"].started_at) < min(
            runs["hold"].finished_at, runs["hold_two"].finished_at
        )
    finally:
        controller.shutdown()


def test_same_flow_waits_while_another_flow_uses_free_slot(flows, store):
    controller = RunController(flows, store, max_concurrent=2, grace=0.5)
    try:
        controller.start("hold", 0.0)
        assert controller.start("hold", 0.0)["queued"] is True
        assert controller.start("other", 0.0).get("queued") is None
        assert [item["flow"] for item in controller.status()["queued"]] == ["hold"]
        wait_idle(controller)
        assert len([run for run in store.list_runs() if run.flow == "hold"]) == 2
    finally:
        controller.shutdown()


# ------------------------------------------------------- scheduled launches
def due_flow(flows: Path, name: str, schedule: dict) -> None:
    write_flow(flows, name, SAY, schedules=[schedule])


def test_schedule_waits_in_the_queue_when_the_slot_is_busy(flows, store, controller):
    due_flow(flows, "job", {"name": "often", "interval_seconds": 1})
    controller.add_flow("job", flows / "job.json")
    service = ScheduleService(controller, store)
    base = time.time()
    service.refresh(base)
    controller.start("hold", 0.0)
    service.tick(base + 1.1)
    assert statuses(store) == ["QUEUED"]
    assert "hold was running" in store.list_schedule_events()[0].message
    service.tick(base + 2.2)  # due again while the first is still waiting
    assert statuses(store) == ["QUEUED", "MISSED"]
    assert "already waiting" in store.list_schedule_events()[0].message
    wait_idle(controller)
    assert statuses(store) == ["QUEUED", "MISSED", "LAUNCHED"]
    assert store.latest_run("job") is not None


def test_failed_launch_is_recorded(flows, store, controller):
    write_flow(flows, "bad", {"x": {"block": "nope", "settings": {}}})
    controller.add_flow("bad", flows / "bad.json")
    service = ScheduleService(controller, store)
    service._items[("bad", "s")] = (
        parse_schedules([{"name": "s", "interval_seconds": 1}])[0][0],
        0.0,
        0,
    )
    service._seen_files["bad"] = (str(flows / "bad.json"), (flows / "bad.json").stat().st_mtime_ns)
    service.tick(time.time())
    wait_idle(controller)
    events = list(reversed(store.list_schedule_events()))
    assert [e.status for e in events] == ["LAUNCHED", "FAILED"]
    assert "nope" in events[1].message


# ------------------------------------------------------------------ catch-up
def test_catch_up_runs_once_for_all_missed_launches(flows, store, controller):
    due_flow(flows, "job", {"name": "nightly", "cron": "0 2 * * *", "catch_up": True})
    controller.add_flow("job", flows / "job.json")
    store.record_schedule_event("job", "nightly", at(2026, 10, 7, 2, 0), "LAUNCHED")
    service = ScheduleService(controller, store)
    now = at(2026, 10, 9, 10, 0)
    service.refresh(now)
    service.tick(now)
    wait_idle(controller)
    event = store.list_schedule_events()[0]
    assert event.status == "LAUNCHED"
    assert "caught up" in event.message
    assert "2 missed" in event.message  # 8 Oct 02:00 and 9 Oct 02:00
    assert event.due_at == at(2026, 10, 9, 2, 0)
    assert len(store.list_runs()) == 1

    restarted = ScheduleService(controller, store)  # nothing is missed any more
    restarted.refresh(now)
    restarted.tick(now)
    wait_idle(controller)
    assert len(store.list_runs()) == 1


def test_no_catch_up_unless_asked_or_without_history(flows, store, controller):
    due_flow(flows, "plain", {"name": "nightly", "cron": "0 2 * * *"})
    due_flow(flows, "fresh", {"name": "nightly", "cron": "0 2 * * *", "catch_up": True})
    for name in ("plain", "fresh"):
        controller.add_flow(name, flows / f"{name}.json")
    store.record_schedule_event("plain", "nightly", at(2026, 10, 7, 2, 0), "LAUNCHED")
    service = ScheduleService(controller, store)
    now = at(2026, 10, 9, 10, 0)
    service.refresh(now)
    service.tick(now)
    wait_idle(controller)
    assert store.list_runs() == []


# ------------------------------------------------------------------- watches
def test_new_files_start_a_run_once_they_stop_growing(flows, store, controller):
    inbox = flows / "inbox"
    inbox.mkdir()
    (inbox / "old.csv").write_text("a\n", encoding="utf-8")
    write_flow(
        flows,
        "watcher",
        TRIGGER,
        schedules=[{"name": "inbox", "watch": "inbox/*.csv", "poll_seconds": 1}],
    )
    controller.add_flow("watcher", flows / "watcher.json")
    service = ScheduleService(controller, store)
    t = time.time()
    service.refresh(t)
    service.tick(t)  # the first look only records what is already there
    assert store.list_schedule_events() == []

    (inbox / "new.csv").write_text("x\n", encoding="utf-8")
    service.tick(t + 1.1)  # seen once: it might still be being written
    assert store.list_schedule_events() == []
    (inbox / "new.csv").write_text("x\ny\n", encoding="utf-8")  # it grew
    service.tick(t + 2.2)
    assert store.list_schedule_events() == []
    service.tick(t + 3.3)  # unchanged since the last look: ready
    wait_idle(controller)
    event = store.list_schedule_events()[0]
    assert event.status == "LAUNCHED"
    assert "1 new file: new.csv" in event.message
    trigger = json.loads(store.load_completed(store.latest_run("watcher"))["say"]["message"])
    assert trigger["type"] == "file"
    assert [Path(f).name for f in trigger["files"]] == ["new.csv"]

    service.tick(t + 4.4)  # nothing new
    assert len(store.list_schedule_events()) == 1


def test_several_new_files_make_one_run(flows, store, controller):
    inbox = flows / "inbox"
    inbox.mkdir()
    write_flow(
        flows,
        "watcher",
        TRIGGER,
        schedules=[{"name": "inbox", "watch": "inbox/*.csv", "poll_seconds": 1}],
    )
    controller.add_flow("watcher", flows / "watcher.json")
    service = ScheduleService(controller, store)
    t = time.time()
    service.refresh(t)
    service.tick(t)
    for n in ("b.csv", "a.csv"):
        (inbox / n).write_text("x\n", encoding="utf-8")
    service.tick(t + 1.1)
    service.tick(t + 2.2)
    wait_idle(controller)
    assert "2 new files: a.csv, b.csv" in store.list_schedule_events()[0].message
    assert len(store.list_runs()) == 1


def test_a_file_that_comes_back_triggers_again(flows, store, controller):
    inbox = flows / "inbox"
    inbox.mkdir()
    write_flow(
        flows,
        "watcher",
        SAY,
        schedules=[{"name": "inbox", "watch": "inbox/*.csv", "poll_seconds": 1}],
    )
    controller.add_flow("watcher", flows / "watcher.json")
    service = ScheduleService(controller, store)
    t = time.time()
    service.refresh(t)
    service.tick(t)
    f = inbox / "daily.csv"
    for round_ in range(2):
        f.write_text("x\n", encoding="utf-8")
        service.tick(t + 10 * round_ + 1.1)
        service.tick(t + 10 * round_ + 2.2)
        wait_idle(controller)
        f.unlink()
        service.tick(t + 10 * round_ + 5.5)  # noticed that it went away
    assert statuses(store) == ["LAUNCHED", "LAUNCHED"]


# ------------------------------------------------------------------ webhooks
def hook_flow(flows: Path, controller: RunController) -> None:
    write_flow(
        flows, "hooked", TRIGGER, schedules=[{"name": "hook", "webhook": "MINIFLOW_TEST_TOKEN"}]
    )
    controller.add_flow("hooked", flows / "hooked.json")


def test_webhook_needs_the_right_token(flows, store, controller, monkeypatch):
    hook_flow(flows, controller)
    service = ScheduleService(controller, store)
    service.refresh(time.time())
    monkeypatch.setenv("MINIFLOW_TEST_TOKEN", "s3cret-token")
    assert service.fire_webhook("hooked", "hook", "s3cret-token", {"n": 1}) == "started"
    wait_idle(controller)
    message = store.load_completed(store.latest_run("hooked"))["say"]["message"]
    assert json.loads(message) == {"type": "webhook", "body": {"n": 1}}
    assert statuses(store) == ["LAUNCHED"]

    for token, status in [("wrong", HTTPStatus.UNAUTHORIZED), ("", HTTPStatus.UNAUTHORIZED)]:
        with pytest.raises(ControlError) as err:
            service.fire_webhook("hooked", "hook", token, None)
        assert err.value.status == status
    with pytest.raises(ControlError) as err:
        service.fire_webhook("hooked", "nope", "s3cret-token", None)
    assert err.value.status == HTTPStatus.NOT_FOUND
    assert len(store.list_runs()) == 1  # refused calls started nothing


def test_webhook_without_a_configured_token_is_refused(flows, store, controller, monkeypatch):
    hook_flow(flows, controller)
    monkeypatch.delenv("MINIFLOW_TEST_TOKEN", raising=False)
    service = ScheduleService(controller, store)
    service.refresh(time.time())
    with pytest.raises(ControlError) as err:
        service.fire_webhook(
            "hooked", "hook", "", None
        )  # an empty token must not match an unset one
    assert err.value.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert "MINIFLOW_TEST_TOKEN" in str(err.value)


def post(server, path, body=b"{}", headers=None):
    url = urlsplit(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        conn.request(
            "POST", path, body=body, headers={"Content-Type": "application/json", **(headers or {})}
        )
        res = conn.getresponse()
        return res.status, json.loads(res.read() or b"null")
    finally:
        conn.close()


def test_webhook_over_http(tmp_path, monkeypatch):
    flows = tmp_path / "flows"
    flows.mkdir()
    write_flow(
        flows, "hooked", TRIGGER, schedules=[{"name": "hook", "webhook": "MINIFLOW_TEST_TOKEN"}]
    )
    monkeypatch.setenv("MINIFLOW_TEST_TOKEN", "tok-123")
    with DashboardServer(tmp_path / "runs.db", port=0, flows=flows, schedules=True) as server:
        server.schedule_service.refresh()
        good = {"Authorization": "Bearer tok-123"}
        status, body = post(server, "/api/hooks/hooked/hook", json.dumps({"n": 5}).encode(), good)
        assert (status, body["status"]) == (202, "started")
        assert (
            post(server, "/api/hooks/hooked/hook", b"{}", {"X-MiniFlow-Token": "tok-123"})[0] == 202
        )  # queued
        assert (
            post(server, "/api/hooks/hooked/hook", b"{}", {"Authorization": "Bearer nope"})[0]
            == 401
        )
        assert post(server, "/api/hooks/hooked/hook")[0] == 401
        assert post(server, "/api/hooks/hooked/missing", b"{}", good)[0] == 404
        assert (
            post(server, "/api/hooks/hooked/hook", b"{}", {**good, "Host": "evil.example"})[0]
            == 403
        )
        assert (
            post(
                server, "/api/hooks/hooked/hook", b"{}", {**good, "Origin": "http://evil.example"}
            )[0]
            == 403
        )
        server.controller.shutdown()


def test_webhooks_are_not_available_without_schedules(tmp_path):
    flows = tmp_path / "flows"
    flows.mkdir()
    with DashboardServer(tmp_path / "runs.db", port=0, flows=flows) as server:
        assert post(server, "/api/hooks/a/b", b"{}", {"Authorization": "Bearer x"})[0] == 404


# --------------------------------------------------------- schedule parsing
def test_new_schedule_kinds_parse():
    schedules, problems = parse_schedules(
        [
            {"name": "w", "watch": "inbox/*.csv"},
            {"name": "w2", "watch": "/data/in/*", "poll_seconds": 2},
            {"name": "h", "webhook": "HOOK_TOKEN"},
            {"name": "c", "cron": "0 2 * * *", "catch_up": True},
        ]
    )
    assert problems == []
    kinds = [(s.name, s.kind) for s in schedules]
    assert kinds == [("w", "watch"), ("w2", "watch"), ("h", "webhook"), ("c", "cron")]
    assert schedules[0].poll_seconds == 5
    assert schedules[1].poll_seconds == 2
    assert schedules[3].catch_up is True


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"name": "x", "watch": ""}, "watch should be a file pattern"),
        ({"name": "x", "watch": "in/*", "poll_seconds": 0}, "poll_seconds should be at least 1"),
        (
            {"name": "x", "interval_seconds": 5, "poll_seconds": 2},
            "poll_seconds only applies to watch",
        ),
        (
            {"name": "x", "webhook": "not a name"},
            "webhook should be the name of an environment variable",
        ),
        ({"name": "x", "webhook": True}, "webhook should be the name of an environment variable"),
        ({"name": "x", "cron": "* * * * *", "catch_up": "yes"}, "catch_up should be true or false"),
        ({"name": "x", "watch": "in/*", "catch_up": True}, "catch_up only applies to"),
        ({"name": "x", "webhook": "T", "catch_up": True}, "catch_up only applies to"),
        ({"name": "x", "watch": "in/*", "webhook": "T"}, "exactly one"),
    ],
)
def test_bad_trigger_schedules_are_explained(item, message):
    _, problems = parse_schedules([item])
    assert any(message in p for p in problems), problems
