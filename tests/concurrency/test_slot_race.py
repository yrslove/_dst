from concurrent.futures import ThreadPoolExecutor

from app.models import Account, AccountState, RuntimeInstance, RuntimeState, utcnow
from tests.helpers import create_account


def test_twenty_concurrent_starts_never_exceed_four(client, app):
    accounts = []
    for index in range(20):
        account = create_account(client, f"race-{index}")
        assert app.state.executor.execute_next()
        with app.state.db.session() as session:
            stored_account = session.get(Account, account["id"])
            runtime = session.get(RuntimeInstance, account["runtime_id"])
            stored_account.status = AccountState.READY
            runtime.state = RuntimeState.READY
            runtime.verified_at = utcnow()
        accounts.append(account)

    def queue(account):
        return app.state.accounts.command(
            account["id"],
            "START_RUNTIME",
            actor="test",
            request_id=f"race-{account['id']}",
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        queued = list(pool.map(queue, accounts))
    assert len({job.id for job in queued}) == 20

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _: app.state.executor.execute_next(), range(20)))

    assert app.state.leases.active_count(app.state.node_id) <= 4
    with app.state.db.session() as session:
        active = (
            session.query(RuntimeInstance)
            .filter(RuntimeInstance.state.in_(["STARTING", "RUNNING"]))
            .count()
        )
    assert active <= 4
