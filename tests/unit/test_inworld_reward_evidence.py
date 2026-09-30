import json
import os
from datetime import UTC, datetime

import pytest

from runtime_agent.gameworker.reward_evidence import InWorldClaimEvidence


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
