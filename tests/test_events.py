from tow.log import log_event, read_events


def test_event_has_operation_identity_and_redacts_secrets():
    log_event(
        "client_add_failed",
        operation_id="op-123",
        component="client",
        integration_id="qbit-main",
        topic_id="topic-1",
        item="S01E02",
        status="failed",
        password="not-for-log",
    )
    row = read_events(limit=1)[0]
    assert row["event_id"]
    assert row["operation_id"] == "op-123"
    assert row["component"] == "client"
    assert row["integration_id"] == "qbit-main"
    assert row["status"] == "failed"
    assert row["password"] == "***"
    assert row["created_at"] == row["ts"]
