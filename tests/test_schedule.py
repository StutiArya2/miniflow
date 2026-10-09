import http.client
import json
import time
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from miniflow.cli import build_parser
from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.schedule import parse_schedules
from miniflow.store import StateStore
from miniflow.ui import DashboardServer
from miniflow.ui.control import RunController
from miniflow.ui.schedules import ScheduleService


def test_interval_and_daily_next_run():
    schedules, problems = parse_schedules(
        [
            {"name": "frequent", "interval_seconds": 15},
            {"name": "morning", "daily": "09:30"},
        ]
    )
    assert problems == []
    noon = datetime(2026, 10, 9, 12).timestamp()
    assert schedules[0].next_after(noon) == noon + 15
    assert datetime.fromtimestamp(schedules[1].next_after(noon)).date().day == 10


def test_bad_schedules_report_all_problems():
    _, problems = parse_schedules(
        [
            {"name": "duplicate", "interval_seconds": 0},
            {"name": "duplicate", "daily": "25:00"},
            {"name": "both", "daily": "12:00", "interval_seconds": 2},
        ]
    )
    assert len(problems) == 4
    assert "repeated" in "\n".join(problems)


def test_invalid_schedule_rejected_before_run(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "schedules": [{"name": "bad", "daily": "tomorrow"}],
                "steps": {"say": {"block": "log", "settings": {"message": "hello"}}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FlowFileError, match="daily should be local time"):
        load_json_flow(path)


def test_serve_parser_and_schedule_api(tmp_path):
    args = build_parser().parse_args(["serve", "--flows", str(tmp_path), "--no-browser"])
    assert args.schedules
    with DashboardServer(tmp_path / "runs.db", port=0, flows=tmp_path, schedules=True) as server:
        url = urlsplit(server.url)
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        try:
            conn.request("GET", "/api/schedules")
            response = conn.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["schedules"] == []
        finally:
            conn.close()


def test_due_schedule_launches_a_normal_run(tmp_path):
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "daily_job.json").write_text(
        json.dumps(
            {
                "schedules": [{"name": "often", "interval_seconds": 1}],
                "steps": {"say": {"block": "log", "settings": {"message": "hello"}}},
            }
        ),
        encoding="utf-8",
    )
    with StateStore(tmp_path / "runs.db") as store:
        controller = RunController(flows, store)
        service = ScheduleService(controller, store)
        base = time.time()
        service.refresh(base)
        service.tick(base + 1.1)
        deadline = time.monotonic() + 5
        while controller.status()["active"] is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        run_id = store.latest_run("daily_job")
        assert run_id is not None
        assert store.get_states(run_id)["say"].value == "SUCCESS"
        assert store.list_schedule_events()[0].status == "LAUNCHED"


def test_busy_controller_queues_schedule(tmp_path):
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "daily_job.json").write_text(
        json.dumps(
            {
                "schedules": [{"name": "often", "interval_seconds": 1}],
                "steps": {"say": {"block": "log", "settings": {"message": "hello"}}},
            }
        ),
        encoding="utf-8",
    )
    (flows / "busy.py").write_text(
        "import time\nfrom miniflow import task\n@task\ndef wait():\n    time.sleep(5)\n",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "runs.db") as store:
        controller = RunController(flows, store)
        service = ScheduleService(controller, store)
        base = time.time()
        service.refresh(base)
        controller.start("busy", 0.0)
        try:
            service.tick(base + 1.1)
            event = store.list_schedule_events()[0]
            assert event.status == "QUEUED"
            assert "busy" in (event.message or "")
        finally:
            controller.cancel()
