import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from opencdr_deploy.routing_runner import drain

OWNER = {"account_id": "111111111111", "home_region": "us-east-1", "stage": "test"}
ID = "a" * 32


def args(tmp_path):
    return dict(
        mode="core",
        owner=OWNER,
        approval_id=ID,
        revision=2,
        agent="111111111111:us-east-1:test",
        state=tmp_path / "runner.sqlite",
        reason="Reviewed customer runner",
        max_batches=2,
    )


def receipt(body, **changes):
    return 200, {
        "deployment": OWNER,
        "delivery": {
            "approval_id": ID,
            "request_id": body["request_id"],
            "agent_id": body["agent_id"],
            "consumed": 20,
            "has_more": False,
            **changes,
        },
    }


def test_lost_response_reuses_exact_pending_request_after_restart(tmp_path):
    client = MagicMock()
    seen = []

    def lost(method, path, body):
        seen.append(body)
        raise OSError("lost response")

    client.request.side_effect = lost
    with pytest.raises(OSError):
        drain(client, **args(tmp_path))
    assert client.request.call_count == 1
    client.request.side_effect = lambda method, path, body: receipt(body)
    result = drain(client, **args(tmp_path))
    assert result["consumed"] == 20
    assert client.request.call_args.args[2] == seen[0]
    with sqlite3.connect(args(tmp_path)["state"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert (
            json.loads(db.execute("SELECT pending FROM state").fetchone()[0])["request_id"]
            != seen[0]["request_id"]
        )


def test_foreign_receipt_or_expired_authority_cannot_advance_state(tmp_path):
    client = MagicMock()
    client.request.side_effect = lambda method, path, body: receipt(body, request_id="b" * 32)
    with pytest.raises(Exception, match="receipt mismatch"):
        drain(client, **args(tmp_path))
    first = client.request.call_args.args[2]
    client.request.return_value = (403, {"code": "expired"})
    client.request.side_effect = None
    with pytest.raises(Exception, match="HTTP 403"):
        drain(client, **args(tmp_path))
    assert client.request.call_args.args[2] == first
    with sqlite3.connect(args(tmp_path)["state"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_state_is_bound_and_work_is_bounded(tmp_path):
    client = MagicMock()
    client.request.side_effect = lambda method, path, body: receipt(body, has_more=True)
    result = drain(client, **args(tmp_path))
    assert result["batches"] == 2 and result["has_more"]
    assert (
        client.request.call_args_list[0].args[2]["request_id"]
        != client.request.call_args_list[1].args[2]["request_id"]
    )
    with pytest.raises(Exception, match="another owner"):
        drain(client, **{**args(tmp_path), "revision": 3})
    assert client.request.call_count == 2
