"""Append gift timing evidence to an existing run; never sends worker commands."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from scripts.dual_ab import append_jsonl


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def records(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            pass
    return result


def timing(samples, account, previous, detected, confirmed):
    last = None
    waiting = pending = 0.0
    for row in samples:
        if row.get('account_id') != account:
            continue
        at = epoch(row['sampled_at'])
        value = (row.get('telemetry') or {}).get('locomotion', {}).get('valid_online_world_elapsed')
        if value is None:
            continue
        value = float(value)
        delta = value if last is None or value < last[1] else value - last[1]
        start = last[0] if last else at - delta
        last = (at, value)
        duration = at - start
        if duration <= 0 or at <= previous or start >= confirmed:
            continue
        waiting += delta * max(0, min(at, detected) - max(start, previous)) / duration
        pending += delta * max(0, min(at, confirmed) - max(start, detected, previous)) / duration
    return round(waiting, 3), round(pending, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_root', type=Path)
    root = parser.parse_args().run_root
    metadata = json.loads((root / 'metadata.json').read_text())
    started = epoch(metadata['actual_started_at'])
    deadline = started + metadata['max_wall_clock_hours'] * 3600
    attempts_seen = {}
    samples_stream = (root / 'samples.jsonl').open()
    while time.time() < deadline:
        while True:
            position = samples_stream.tell()
            line = samples_stream.readline()
            if not line or not line.endswith("\n"):
                samples_stream.seek(position)
                break
            row = json.loads(line)
            attempt = (row.get('telemetry') or {}).get('gift_claim_attempt')
            if not attempt:
                continue
            account = row['account_id']
            key = (attempt['before_frame_id'], attempt['status'], attempt.get('after_frame_id'))
            if attempts_seen.get(account) == key:
                continue
            attempts_seen[account] = key
            append_jsonl(root / 'events.jsonl', {
                'event': 'CLAIM_ATTEMPT' if attempt['status'] == 'CLAIM_ATTEMPT' else 'CLAIM_ATTEMPT_RESULT',
                'at': row['sampled_at'], 'run_id': metadata['run_id'], 'account_id': account,
                'profile': row['profile'], **attempt,
            })
        gifts = records(root / 'gifts.jsonl')
        seen = {row.get('metrics_id') for row in gifts if row.get('record_type') == 'GIFT_OPERATION_METRICS'}
        for gift in gifts:
            if gift.get('record_type') == 'GIFT_OPERATION_METRICS':
                continue
            key = f"{gift['account_id']}:{gift['item_id']}:{gift['claim_confirmed_at']}"
            if key in seen:
                continue
            receipt = gift['durable_receipt']
            detected = epoch(gift['gift_first_detected_at'])
            confirmed = epoch(gift['claim_confirmed_at'])
            previous = gift.get('previous_claim_confirmed_at')
            waiting, pending_valid = timing(records(root / 'samples.jsonl'), gift['account_id'],
                                           max(started, epoch(previous)) if previous else started,
                                           detected, confirmed)
            metrics = {
                **gift, 'record_type': 'GIFT_OPERATION_METRICS', 'metrics_id': key,
                'claim_started_at': receipt.get('claim_started_at') or gift.get('claim_started_at'),
                'wall_seconds_since_previous_claim': gift.get('wall_clock_since_previous_claim'),
                'valid_online_seconds_since_previous_claim': gift.get('valid_online_world_seconds_since_previous_claim'),
                'waiting_for_gift_seconds': waiting,
                'waiting_for_gift_wall_seconds': max(0, detected - epoch(previous)) if previous else None,
                'gift_pending_unclaimed_seconds': max(0, confirmed - detected),
                'gift_pending_valid_online_seconds': pending_valid,
                'claim_delay_seconds': max(0, confirmed - detected),
                'before_screenshot': receipt.get('before_screenshot'),
                'detected_screenshot': receipt.get('detected_screenshot'),
                'claimed_screenshot': receipt.get('claimed_screenshot'),
                'native_completion_evidence': receipt['backend'],
                'waiting_interval_left_censored': bool(gift.get('interval_left_censored') or detected <= started),
            }
            append_jsonl(root / 'events.jsonl', {**metrics, 'event': 'GIFT_CYCLE_COMPLETE', 'at': gift['claim_confirmed_at']})
            append_jsonl(root / 'events.jsonl', {'event': 'NEW_T0', 'at': gift['claim_confirmed_at'],
                                               'run_id': gift['run_id'], 'account_id': gift['account_id'],
                                               'profile': gift['profile'], 'gift_sequence_in_run': gift['gift_sequence_in_run'],
                                               'new_t0': gift['claim_confirmed_at'], 'metrics_id': key})
            append_jsonl(root / 'gifts.jsonl', metrics)
            seen.add(key)
        metadata = json.loads((root / 'metadata.json').read_text())
        if metadata.get('status') not in {'RUNNING', 'BASELINE_READY', 'STARTING'}:
            break
        time.sleep(5)


if __name__ == '__main__':
    main()
