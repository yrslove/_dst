def test_readiness_matches_current_alembic_head(client):
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": True,
        "schema_revision": "0013_durable_worker_intent",
        "expected_schema_revision": "0013_durable_worker_intent",
    }
