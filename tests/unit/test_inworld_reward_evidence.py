import json
import os
import time
from datetime import UTC, datetime

import pytest

from runtime_agent.gameworker.reward_evidence import (
    InWorldClaimEvidence,
    SessionClaimEvidence,
)


def evidence(tmp_path, *, modified=150, error=False, item_id=7, status=200):
    baseline = tmp_path / 'before.json'
    baseline.write_text(json.dumps({'Error': False, 'Items': [
        {'ItemID': 7, 'ItemType': 'LEGS_PINSTRIPE_PANTS_BROWN_CHOCOLATE', 'Context': 3},
    ]}))
    os.utime(baseline, (100, 100))
    log = tmp_path / 'client_log.txt'
    log.write_text(f'[SetItemOpened_Complete Success:{status}] '+json.dumps({
        'Error': error, 'ItemID': item_id, 'Modified': modified,
    })+'\n')
    return InWorldClaimEvidence(baseline, log, tmp_path / 'receipt.json')


def closed():
    return {
        'received_frame_id': 'r1-w1005-f1', 'received_sequence': 1,
        'received_at': datetime.fromtimestamp(140, UTC).isoformat(),
        'evidence_frame_id': 'r1-w1005-f3', 'evidence_sequence': 3,
        'observed_at': datetime.fromtimestamp(160, UTC).isoformat(),
        'action_id': 'runtime-1:worker-1005:action-1',
    }


def test_native_ack_and_fresh_close_are_atomically_persisted(tmp_path):
    verifier = evidence(tmp_path)
    receipt = verifier.confirm(closed())
    assert receipt['semantic'] == 'IN_WORLD_GIFT_CONFIRMED'
    assert receipt['item_id'] == 7
    assert receipt['backend']['http_status'] == 200
    assert json.loads((tmp_path / 'receipt.json').read_text()) == receipt
    assert verifier.confirm(closed()) == receipt


@pytest.mark.parametrize('kwargs', [{'modified': 99}, {'error': True},
                                 {'item_id': 8}, {'status': 500}])
def test_old_wrong_or_failed_backend_ack_never_confirms(tmp_path, kwargs):
    assert evidence(tmp_path, **kwargs).confirm(closed()) is None
    assert not (tmp_path / 'receipt.json').exists()


def test_backend_ack_without_fresh_received_then_close_never_confirms(tmp_path):
    result = closed()
    result['received_sequence'] = result['evidence_sequence']
    assert evidence(tmp_path).confirm(result) is None


def test_session_claims_require_correct_account_ack_and_independent_runtime(tmp_path):
    providers = []
    for account, runtime in ((1, 1), (2, 4)):
        root = tmp_path / f"guest-{account}"
        cache = root / "steam-account/client_save/inventory_cache_prod"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"Error": False, "UserID": f"KU_{account}", "Items": []}))
        provider = SessionClaimEvidence(root, root / "session", {
            "account_id": account, "runtime_id": runtime, "experiment_session_id": f"session-{account}"
        })
        provider.before_tick()
        assert not provider.ready
        cache.write_text(json.dumps({"Error": False, "UserID": f"KU_{account}", "Items": [
            {"ItemID": 7, "ItemType": "TEST_ITEM", "Context": 3}
        ]}))
        provider.before_tick()
        assert provider.ready
        providers.append(provider)
    a, b = providers
    started = a.provider.started_at
    result = {**closed(), "runtime_id": 1,
              "received_at": datetime.fromtimestamp(started + 1, UTC).isoformat(),
              "observed_at": datetime.fromtimestamp(started + 3, UTC).isoformat()}
    log = a.user_root / "client_log.txt"
    ack = {"Error": False, "ItemID": 7, "Modified": started + 2, "UserID": "KU_2"}
    log.write_text("[SetItemOpened_Complete Success:200] " + json.dumps(ack))
    assert a.confirm(result) is None
    ack["UserID"] = "KU_1"
    log.write_text("[SetItemOpened_Complete Success:200] " + json.dumps(ack))
    assert b.confirm(result) is None
    receipt = a.confirm(result)
    assert receipt["account_id"] == 1
    assert receipt["experiment_session_id"] == "session-1"
    assert (a.evidence / "claim.json").exists()
    assert b.provider.receipt is None
    assert not (b.evidence / "claim.json").exists()


def test_native_ack_waits_for_fresh_world_and_active_icon_disappearance(tmp_path):
    from types import SimpleNamespace

    from runtime_agent.gameworker.vision import DSTScreen

    cache = tmp_path / 'account/client_save/inventory_cache_prod'
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({'Error': False, 'UserID': 'KU_1', 'Items': [
        {'ItemID': 7, 'ItemType': 'TEST_ITEM', 'Context': 3},
    ]}))
    provider = SessionClaimEvidence(tmp_path, tmp_path / 'evidence', {
        'account_id': 1, 'runtime_id': 1, 'experiment_session_id': 'unknown-reward-frame',
    })
    provider.before_tick()
    before_click = SimpleNamespace(runtime_id=1, source_frame_id='before-click',
        source_sequence=1, worker_generation=2, timestamp=datetime.now(UTC).isoformat())
    provider.start_attempt(before_click, 'canonical-click-1')
    attempt = datetime.fromisoformat(provider.attempt['claim_attempt_at']).timestamp()
    (tmp_path / 'client_log.txt').write_text(
        '[SetItemOpened_Complete Success:200] ' + json.dumps({
            'Error': False, 'ItemID': 7, 'ItemType': 'TEST_ITEM',
            'Modified': attempt + .01, 'UserID': 'KU_1',
        }) + '\n'
    )
    unknown = SimpleNamespace(runtime_id=1, screen=DSTScreen.UNKNOWN,
        source_frame_id='unknown-after-click', production_ready=True,
        is_fresh=lambda: True, detections=[])
    assert provider.observe(unknown) is None
    world = SimpleNamespace(runtime_id=1, screen=DSTScreen.IN_WORLD_IDLE,
        source_frame_id='world-after-click', source_sequence=3, worker_generation=2,
        production_ready=True, is_fresh=lambda: True, detections=[],
        timestamp=datetime.now(UTC).isoformat())
    for availability in ('IN_WORLD_GIFT_PENDING', 'GIFT_AVAILABLE'):
        blocked = SimpleNamespace(**{**vars(world),
            'source_frame_id': f'gift-remains-{availability}',
            'as_dict': lambda: {'source_frame_id': 'gift-remains'},
            'detections': [SimpleNamespace(kind='gift_icon', detected=True,
                verified=True, confidence=.99, metadata=(('availability', availability),))]})
        assert provider.observe(blocked) is None
        assert provider.provider.receipt is None
    receipt = provider.observe(world)
    assert receipt['active_gift_icon_disappeared'] is True
    assert receipt['evidence_frame_id'] == 'world-after-click'
    assert receipt['backend']['operation'] == 'SetItemOpened_Complete'
    assert receipt['backend']['http_status'] == 200
    assert receipt['action_id'] == 'canonical-click-1'


def recording(tmp_path, *, screen='IN_WORLD_GIFT_RECEIVED', mode='ACTIVE', later=False):
    root = tmp_path / 'recording'
    root.mkdir()
    now = datetime.now(UTC).isoformat()
    (root / 'manifest.json').write_text(json.dumps({
        'worker_mode': mode, 'capture_source': {'kind': 'x11-pillow'},
        'runtime_id': 1, 'runtime_generation': 1,
    }))
    source = {'screen': screen, 'validity': 'VALID', 'screen_confidence': .99,
                  'assets_verified': True, 'calibration_verified': True,
                  'runtime_id': 1, 'runtime_generation': 1, 'worker_generation': 2,
                  'source_frame_id': 'r1-w2-f2', 'source_sequence': 2, 'timestamp': now,
                  'detections': [{'kind': k, 'confidence': .99, 'detected': True, 'verified': True}
                              for k in ('inworld_gift_received_title', 'inworld_gift_use_later',
                                        'inworld_gift_use_now')]}
    events = [
        {'event_type': 'OBSERVATION_PRODUCED', 'frame_id': 'r1-w2-f2',
         'payload': {'observation': source}},
        {'event_type': 'ACTION_RESULT', 'frame_id': 'r1-w2-f2', 'timestamp': now,
         'payload': {'result': {'action': 'CLICK_INWORLD_USE_LATER',
                               'action_id': 'close-1', 'status': 'VERIFYING'}}},
    ]
    if later:
        events.append({'event_type': 'ACTION_RESULT', 'frame_id': 'later',
                       'payload': {'result': {'action': 'MOVE_BACKWARD',
                                             'action_id': 'move-2', 'status': 'SENT'}}})
    (root / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    return root


def test_recorded_close_verification_can_resume_without_replaying_input(tmp_path):
    verifier = evidence(tmp_path)
    verifier.resume_recording(recording(tmp_path))
    assert verifier.pending_close['action_id'] == 'close-1'
    assert verifier.pending_close['verification'] == 'RECORDED_CANONICAL_CLOSE_FRESH_WORLD'
    assert verifier.receipt is None  # still needs fresh world + backend ACK


@pytest.mark.parametrize('kwargs', [ {'mode': 'REPLAY'}, {'screen': 'LOGIN_REWARD_AVAILABLE'},
                                   {'later': True}])
def test_replay_wrong_modal_or_subsequent_input_cannot_resume_receipt(tmp_path, kwargs):
    with pytest.raises(ValueError):
        evidence(tmp_path).resume_recording(recording(tmp_path, **kwargs))


def test_armed_claim_stops_new_input_when_native_service_becomes_unhealthy(tmp_path):
    cache = tmp_path / 'account/client_save/inventory_cache_prod'
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({'Error': False, 'UserID': 'KU_1', 'Items': [
        {'ItemID': 7, 'ItemType': 'TEST_ITEM', 'Context': 3},
    ]}))
    provider = SessionClaimEvidence(tmp_path, tmp_path / 'evidence', {
        'account_id': 1, 'runtime_id': 1, 'experiment_session_id': 'native-service-loss',
    })
    provider.before_tick()
    assert provider.ready
    original = provider.provider
    cache.write_text(json.dumps({'Error': True}))
    provider.before_tick()
    assert not provider.ready
    assert provider.provider is original


def test_live_gift_can_precede_cache_but_cannot_reclaim_known_item(tmp_path):
    from types import SimpleNamespace

    from runtime_agent.gameworker.vision import DSTScreen
    cache = tmp_path / 'account/client_save/inventory_cache_prod'
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({'Error': False, 'UserID': 'KU_2', 'Items': [
        {'ItemID': 7, 'ItemType': 'ALREADY_OPENED', 'Context': 0},
    ]}))
    provider = SessionClaimEvidence(tmp_path, tmp_path / 'evidence', {
        'account_id': 2, 'runtime_id': 4, 'experiment_session_id': 'uncached-live-gift',
    })
    provider.before_tick()
    assert not provider.ready
    base = time.monotonic()
    obs = None
    for sequence in range(3):
        obs = SimpleNamespace(runtime_id=4, production_ready=True, is_fresh=lambda: True,
            screen=DSTScreen.IN_WORLD_IDLE, source_frame_id=f"r4-new-{sequence}",
            source_sequence=sequence + 1, observed_monotonic=base + sequence * .1,
            timestamp=datetime.now(UTC).isoformat(),
            as_dict=lambda sequence=sequence: {'source_frame_id': f'r4-new-{sequence}'},
            detections=[SimpleNamespace(kind='gift_icon', detected=True, verified=True,
                confidence=.99, metadata=(('availability', 'GIFT_AVAILABLE'),))])
        if sequence == 0:
            first_active_at = obs.timestamp
        provider.observe(obs)
    assert provider.ready
    assert (provider.evidence / 'detection.json').exists()
    started = provider.provider.started_at
    provider = SessionClaimEvidence(tmp_path, tmp_path / 'evidence', {
        'account_id': 2, 'runtime_id': 4, 'experiment_session_id': 'uncached-live-gift',
    })
    provider.before_tick()
    assert provider.provider.started_at == started
    assert provider.ready
    result = {**closed(), 'runtime_id': 4,
        'received_at': datetime.fromtimestamp(started + 1, UTC).isoformat(),
        'observed_at': datetime.fromtimestamp(started + 3, UTC).isoformat()}
    log = tmp_path / 'client_log.txt'
    ack = {'Error': False, 'ItemID': 7, 'Modified': started + 2, 'UserID': 'KU_2'}
    log.write_text('[SetItemOpened_Complete Success:200] ' + json.dumps(ack))
    assert provider.confirm(result) is None
    ack.update(ItemID=8, UserID='KU_1')
    log.write_text('[SetItemOpened_Complete Success:200] ' + json.dumps(ack))
    assert provider.confirm(result) is None
    ack['UserID'] = 'KU_2'
    log.write_text('[SetItemOpened_Complete Success:200] ' + json.dumps(ack))
    receipt = provider.confirm(result)
    assert receipt['item_id'] == 8
    assert receipt['experiment_session_id'] == 'uncached-live-gift'
    assert receipt['detection_timestamp'] == first_active_at


def test_received_claim_recovers_from_archived_ack_after_log_rotation(tmp_path):
    from types import SimpleNamespace

    from runtime_agent.gameworker.vision import DSTScreen
    cache = tmp_path / 'account/client_save/inventory_cache_prod'
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({'Error': False, 'UserID': 'KU_2', 'Items': [
        {'ItemID': 7, 'ItemType': 'TEST_GIFT', 'Context': 3},
    ]}))
    identity = {'account_id': 2, 'runtime_id': 4, 'experiment_session_id': 'recover-received'}
    provider = SessionClaimEvidence(tmp_path, tmp_path / 'evidence', identity)
    provider.before_tick()
    started = provider.provider.started_at
    def observation(screen, sequence):
        return SimpleNamespace(runtime_id=4, runtime_generation=2, worker_generation=1,
            source_sequence=sequence, source_frame_id=f'r4-{sequence}', production_ready=True,
            is_fresh=lambda: True, screen=screen, screen_confidence=.99,
            timestamp=datetime.fromtimestamp(started + sequence / 10, UTC).isoformat(),
            as_dict=lambda: {'source_frame_id': f'r4-{sequence}'}, detections=[
                SimpleNamespace(kind='gift_icon', detected=True, verified=True, confidence=.99,
                    metadata=(('availability', 'GIFT_AVAILABLE'),))])
    provider.observe(observation(DSTScreen.IN_WORLD_IDLE, 1))
    ack = {'Error': False, 'ItemID': 7, 'Modified': started + .1, 'UserID': 'KU_2'}
    log = tmp_path / 'client_log.txt'
    log.write_text('[SetItemOpened_Complete Success:200] ' + json.dumps(ack))
    provider.observe(observation(DSTScreen.IN_WORLD_GIFT_RECEIVED, 2))
    assert (provider.evidence / 'native-ack.log').exists()
    assert (provider.evidence / 'received.json').exists()
    log.write_text('new process log without old ACK')
    resumed = SessionClaimEvidence(tmp_path, tmp_path / 'evidence', identity)
    resumed.before_tick()
    world = observation(DSTScreen.IN_WORLD_IDLE, 3)
    assert resumed.observe(world) is None
    assert resumed.observe(world) is None
    clear = observation(DSTScreen.IN_WORLD_IDLE, 4)
    clear.detections = []
    assert resumed.observe(clear) is None
    clear = observation(DSTScreen.IN_WORLD_IDLE, 5)
    clear.detections = []
    receipt = resumed.observe(clear)
    assert receipt['item_id'] == 7
    assert receipt['completion_path'] == 'FRESH_WORLD_AFTER_RECEIVED_UI_RECOVERY'
    assert receipt['experiment_session_id'] == identity['experiment_session_id']
