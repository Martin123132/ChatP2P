import argparse

import chatp2p.cli as cli_module


class _FakeIdentity:
    node_id = "worker_test"


class _FakeWorker:
    identity = _FakeIdentity()


def test_worker_loop_recovers_from_transient_timeout(monkeypatch, capsys):
    calls = {"jobs": 0, "sleeps": 0}

    def fake_run_one_remote_job(client, worker):
        calls["jobs"] += 1
        if calls["jobs"] == 1:
            raise TimeoutError("coordinator stalled")
        return {"worker": worker.identity.node_id, "job": None, "status": "idle"}

    monkeypatch.setattr(cli_module, "_load_worker", lambda *args, **kwargs: _FakeWorker())
    monkeypatch.setattr(cli_module, "_coordinator_client", lambda args: object())
    monkeypatch.setattr(cli_module, "_register_worker", lambda client, worker: None)
    monkeypatch.setattr(cli_module, "_run_one_remote_job", fake_run_one_remote_job)
    monkeypatch.setattr(cli_module.time, "sleep", lambda interval: calls.__setitem__("sleeps", calls["sleeps"] + 1))

    cli_module.run_worker_loop(
        argparse.Namespace(
            home=".mesh",
            ollama_base_url="http://127.0.0.1:11434",
            interval=0.5,
            stop_when_idle=True,
            max_jobs=None,
        )
    )

    captured = capsys.readouterr()
    assert calls == {"jobs": 2, "sleeps": 1}
    assert "transient-error TimeoutError: coordinator stalled" in captured.err
    assert "worker_test idle" in captured.out
