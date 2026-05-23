import socket

from chatp2p.cli import build_parser
from chatp2p.quickstart import QUICKSTART_REPORT_SCHEMA, QuickstartConfig, run_quickstart


def test_quickstart_runs_one_visible_product_loop(tmp_path):
    report = run_quickstart(
        QuickstartConfig(
            home=tmp_path / ".mesh",
            port=_free_port(),
            prompt="hello quickstart",
            timeout_seconds=20.0,
            poll_interval=0.2,
            worker_interval=0.2,
            stop_after_job=True,
        )
    )

    assert report["schema"] == QUICKSTART_REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["job"]["status"] == "verified"
    assert report["result"]["output"]["answer"] == "hello quickstart"
    assert report["final_status"]["verified_jobs"] == 1
    assert any(step["step"] == "connect_worker" for step in report["steps"])
    assert any(step["step"] == "see_result" for step in report["steps"])


def test_quickstart_cli_parses():
    parser = build_parser()

    args = parser.parse_args(
        [
            "quickstart",
            "--home",
            "D:\\ChatP2PData\\quickstart",
            "--port",
            "8766",
            "--prompt",
            "hello",
            "--json",
        ]
    )

    assert args.func.__name__ == "run_quickstart_command"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
