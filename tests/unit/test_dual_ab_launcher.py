from types import SimpleNamespace

from scripts import dual_ab


def test_one_confirmed_gift_does_not_stop_other_session(monkeypatch, tmp_path):
    calls, reads, identities = [], {1: 0, 2: 0}, {}
    class FakeAPI:
        def __init__(self, _base):
            self.client = SimpleNamespace(close=lambda: None)
        def start(self, account):
            return {"runtime_id": account, "external_id": f"container-{account}"}
        def reserve(self, accounts):
            assert accounts == [1, 2]
        def command(self, account, mode, **values):
            calls.append((account, mode))
            if values:
                identities[account] = values["experiment_session_id"]
        def request(self, method, path):
            account = int(path.split("/")[1])
            reads[account] += 1
            gift = account == 1 or reads[account] >= 3
            receipt = {"experiment_session_id": identities[account], "semantic": "IN_WORLD_GIFT_CONFIRMED"} if gift else None
            report = {"mode": "ACTIVE", "state": "WAITING", "telemetry": {
                "locomotion": {"session_id": identities[account]},
                "inworld_gift_confirmation": receipt,
            }}
            return {"details": {"diagnostics": {"worker": report}}}
    monkeypatch.setattr(dual_ab, "API", FakeAPI)
    monkeypatch.setattr(dual_ab.time, "sleep", lambda _seconds: None)
    assert dual_ab.main(["--until-gift", "--max-seconds", "10", "--output", str(tmp_path)]) == 0
    assert reads[2] >= 3
    assert identities[1] != identities[2]
    # The first post-start disable belongs to A; B reaches its own later receipt.
    assert calls[:5] == [(1, "DISABLED"), (2, "DISABLED"), (1, "ACTIVE"), (2, "ACTIVE"), (1, "DISABLED")]
    assert len(list(tmp_path.rglob("claim.json"))) == 2


def test_launcher_drains_both_accounts_before_starting_input(monkeypatch):
    api = object.__new__(dual_ab.API)
    calls = []
    def request(method, path, data):
        calls.append(path)
        return {"active_jobs": [42] if path.endswith('1/schedule/pause') and calls.count(path) == 1 else []}
    api.request = request
    monkeypatch.setattr(dual_ab.time, 'sleep', lambda _seconds: None)
    api.reserve([1, 2])
    assert calls == ['accounts/1/schedule/pause', 'accounts/2/schedule/pause', 'accounts/1/schedule/pause']


def test_append_only_event_log_and_summary_ignore_preexisting_interval(tmp_path):
    path = tmp_path / "events.jsonl"
    dual_ab.append_jsonl(path, {"event": "RUN_STARTED"})
    dual_ab.append_jsonl(path, {"event": "GIFT_DETECTED"})
    assert [__import__("json").loads(line)["event"] for line in path.read_text().splitlines()] == [
        "RUN_STARTED", "GIFT_DETECTED"
    ]

    state = {"identity": {"account_id": 1, "runtime_id": 1, "profile": "CONTROL"},
             "started_epoch": 100, "gifts": [
                 {"valid_online_world_seconds_since_previous_claim": 300, "preexisting_at_run_start": True},
                 {"valid_online_world_seconds_since_previous_claim": 7200, "preexisting_at_run_start": False}],
             "totals": {"valid": 7200, "active": 7200, "moving": 300, "idle": 6900,
                        "commands": 100, "direction_changes": 90},
             "measurement_base": {"valid": 300, "active": 300, "moving": 20, "idle": 280,
                                  "commands": 5, "direction_changes": 4},
             "disconnect_seconds": 0, "recovery_seconds": 0, "runtime_restarts": 0,
             "worker_restarts": 0, "stop_reason": "TARGET_VALID_ONLINE_REACHED"}
    summary = dual_ab.summarize_account(state, 14, 16)
    assert summary["clean_measured_intervals"] == 1
    assert summary["intervals_valid_hours"] == [2.0]
    assert summary["valid_online_hours"] == round(6900 / 3600, 4)
