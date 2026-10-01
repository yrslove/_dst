from types import SimpleNamespace

from scripts import dual_ab


def test_one_confirmed_gift_does_not_stop_other_session(monkeypatch, tmp_path):
    calls, reads, identities = [], {1: 0, 2: 0}, {}
    class FakeAPI:
        def __init__(self, _base):
            self.client = SimpleNamespace(close=lambda: None)
        def start(self, account):
            return {"runtime_id": account, "external_id": f"container-{account}"}
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
