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


def test_native_baseline_rejects_daily_or_other_account_receipts():
    native = {"account_id": 2, "semantic": "IN_WORLD_GIFT_CONFIRMED",
              "claim_timestamp": "2026-10-01T05:41:52.227272Z",
              "backend": {"operation": "SetItemOpened_Complete", "http_status": 200, "error": False}}
    assert dual_ab.confirmed_claim_timestamp(native, 2) == "2026-10-01T05:41:52.227272+00:00"
    assert dual_ab.confirmed_claim_timestamp(native, 1) is None
    assert dual_ab.confirmed_claim_timestamp({**native, "semantic": "DAILY_GIFT_CONFIRMED"}, 2) is None
    assert dual_ab.confirmed_claim_timestamp(None, 2) is None


def test_failed_preexisting_claim_preserves_other_account_run(monkeypatch, tmp_path):
    import json
    from datetime import UTC, datetime
    calls, sessions, modes = [], {}, {}
    prior = {"account_id": 2, "semantic": "IN_WORLD_GIFT_CONFIRMED",
             "claim_timestamp": "2026-10-01T05:41:52.227272Z",
             "backend": {"operation": "SetItemOpened_Complete", "http_status": 200, "error": False}}

    class FakeAPI:
        client = SimpleNamespace(close=lambda: None)
        def reserve(self, accounts):
            pass
        def start(self, account):
            return {"runtime_id": account, "external_id": f"container-{account}"}
        def command(self, account, mode, **values):
            calls.append((account, mode, values))
            modes[account] = mode
            if values:
                sessions[account] = values["experiment_session_id"]
        def request(self, method, path):
            account = int(path.split('/')[1])
            preparing = account == 1 and modes.get(account) == 'ACTIVE'
            running = account == 2 and modes.get(account) == 'ACTIVE'
            report = {"mode": modes.get(account, "DISABLED"), "state": "WAITING",
                      "last_tick_at": datetime.now(UTC).isoformat(),
                      "error_code": "WORKER_INTERVENTION_REQUIRED" if preparing else None,
                      "details": {"observation": {"screen": "IN_WORLD_IDLE", "validity": "VALID"}},
                      "telemetry": {"gift_availability_evidence": {
                          "observed_at": datetime.now(UTC).isoformat(), "icon_present": account == 1,
                          "availability": "IN_WORLD_GIFT_PENDING" if account == 1 else "NO_REWARD_AVAILABLE",
                          "identity_confidence": 1}, "inworld_gift_confirmation": prior if account == 2 and not running else None,
                          "locomotion": {"session_id": sessions.get(account), "valid_online_world_elapsed": 3601 if running else 0}}}
            return {"details": {"diagnostics": {"worker": report}}}

    def run(command, **kwargs):
        stdout = ("test-commit" if command[:2] == ["git", "rev-parse"] else
                  "" if command[:2] == ["git", "status"] else '{}')
        return SimpleNamespace(stdout=stdout, returncode=0)
    monkeypatch.setattr(dual_ab.subprocess, 'run', run)
    monkeypatch.setattr(dual_ab.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(dual_ab, 'host_resources', lambda: {"cpu_ticks": [0,0,0,0,0]})
    monkeypatch.setattr(dual_ab, 'runtime_resources', lambda container: {"memory_events": {}, "cpu_usage_usec": 0})
    args = SimpleNamespace(accounts=[1,2], run_id='failure-isolated', output=tmp_path,
                           target_valid_hours=1, max_wall_hours=1, preclaim_max_seconds=30)
    assert dual_ab.run_characterization(args, FakeAPI()) == 0
    root = tmp_path / args.run_id
    result = json.loads((root/'final_summary.json').read_text())
    assert result['accounts']['1']['stop_reason'] == 'TECHNICAL_FAILURE'
    assert result['accounts']['2']['stop_reason'] == 'TARGET_VALID_ONLINE_REACHED'
    metadata = json.loads((root/'metadata.json').read_text())
    assert metadata['baseline_t0']['2'] == prior['claim_timestamp'].replace('Z', '+00:00')
    assert metadata['baseline_t0']['1'] is None
    assert not any(a == 1 and v.get('experiment_continue_after_claim') for a, _, v in calls)
    assert any(a == 2 and v.get('experiment_continue_after_claim') for a, _, v in calls)


def test_baseline_rejects_stale_gift_even_when_cached_observation_is_valid(monkeypatch):
    monkeypatch.setattr(dual_ab.time, 'time', lambda: 100)
    report = {"details": {"observation": {"screen": "IN_WORLD_IDLE", "validity": "VALID"}},
              "telemetry": {"gift_availability_evidence": {"observed_at": "1970-01-01T00:01:10Z"}}}
    assert dual_ab.fresh_baseline_gift(report, 60) is None
    report['telemetry']['gift_availability_evidence']['observed_at'] = '1970-01-01T00:01:35Z'
    assert dual_ab.fresh_baseline_gift(report, 90)
    assert dual_ab.fresh_baseline_gift(report, 99) is None
