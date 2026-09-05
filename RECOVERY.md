# Recovery

## Backup

Stop neither runtime containers nor Incus for a PostgreSQL logical backup. Set DATABASE_URL, DST_FARM_SECRET_KEY_FILE and optional CONTROL_PLANE_ENV_FILE, then run scripts/backup_control_plane.sh. Copy the result to encrypted off-host storage.

Node persistent volumes/config require a separate, workload-aware backup policy. Do not snapshot every running container every five minutes.

## Restore

1. Stop dst-orchestrator.
2. Provision an empty target PostgreSQL database.
3. Verify the selected backup checksum.
4. Set explicit DATABASE_URL and RESTORE_CONFIRM=RESTORE_DST_CONTROL_PLANE.
5. Run scripts/restore_control_plane.sh BACKUP_DIRECTORY.
6. Restore encryption key/config with mode 0600.
7. Run alembic upgrade head.
8. Start service; verify ready/version/jobs.
9. Keep Nodes drained while reconciling actual Incus state.

The restore script uses pg_restore --clean and is intentionally destructive only against the explicitly supplied DATABASE_URL.

## Crash recovery

- PENDING jobs remain durable.
- Expired executor leases are reclaimed and the attempt is marked ABANDONED.
- Expired slot lease remains a capacity reservation until the reconciler confirms STOPPED or a missing instance. Expiry itself only triggers investigation.
- Unknown provider outcome becomes STALE, not DESTROYED.
- Account remains after any runtime generation failure.

## Lost key

There is no recovery for encrypted account secrets without the matching Fernet key. Restore key and DB from the same protected backup set.
