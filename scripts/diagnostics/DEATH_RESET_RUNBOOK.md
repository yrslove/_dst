# Farm 01 death/reset validation

Use the Control Plane for STOP/START and GameWorker mode changes. Keep GameWorker
DISABLED during baseline and recovery checks. Use ACTIVE only to enter the saved world,
then return it to DISABLED. The fixture mods are one-shot diagnostics, never runtime
recovery controllers.

## Required pre-death gate

With the runtime managed STOPPED, take an Incus `PREPARED_ALIVE_PRE_DEATH_<timestamp>`
snapshot. Verify the latest Master save has exactly one `researchlab`, the temporary
mods are disabled, and the current save matches the snapshot:

```sh
sudo python3 scripts/diagnostics/pre_death_guard.py --snapshot <snapshot-name>
```

Only a passing gate permits `--enable-controlled-death`. Keep the snapshot until
recovery and a later managed restart are verified. The post-reset forensic export is
not a pre-death fixture.

## State decisions

Classify a fresh frame before issuing any in-world action. A saved `researchlab`
record and live entity observation together establish the prepared fixture.

| State | Evidence | Next action |
| --- | --- | --- |
| ALIVE | Fresh `IN_WORLD_IDLE`, living player, one `researchlab` | Verify control, then run the guarded death fixture. |
| DYING/DEAD | Death or ghost frame | Stop alive-only actions; inspect the death UI. |
| DEATH_COUNTDOWN | All-dead countdown visible | Wait or use the canonical input controller on the verified Reset Now UI. |
| WORLD_RESET_PENDING | Verified `WORLD_RESET_PENDING` perception | Expect a destructive save change; keep the rollback point. |
| RESET_OCCURRED | DST reset request and a new Master session | Count `researchlab` in that session. |
| RECOVERY_REQUIRED | Reset occurred and prepared fixture is missing | Managed STOP, preserve compact reset evidence, restore the pre-death snapshot. Never treat the new world as recovered. |
| RECOVERING | Snapshot restored, managed START in progress | Keep GameWorker disabled except for bounded world entry. |
| ALIVE_RECOVERED | Fresh living `IN_WORLD_IDLE`, one live machine and one saved record | Disable GameWorker; repeat a clean managed STOP/START and verify again. |

If the first attached frame is already dead, start at DYING/DEAD or
DEATH_COUNTDOWN. Do not run movement, setup, or controlled death actions from that
state. If it is already reset, begin at RESET_OCCURRED and verify the fixture before
choosing recovery.

Restore only after a Control Plane STOP has completed. Use
`incus snapshot restore <instance> <snapshot-name>` while stopped, then Control Plane
START. The pre-death guard can be rerun after the next managed STOP to prove the
prepared save still matches its rollback source.
