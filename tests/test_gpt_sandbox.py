"""Run these checks in addition to regression tests inside the real verifier."""
import errno
import os
import socket
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("KINVEST_GPT_SANDBOX") != "1", reason="isolated verifier only")
def test_verifier_has_no_credentials_network_capabilities_or_writable_source():
    assert os.getuid() != 0
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    assert int(status["CapEff"].strip(), 16) == 0
    assert int(status["CapBnd"].strip(), 16) == 0
    assert status["NoNewPrivs"].strip() == "1"
    for path in ("/home/ubuntu/.codex", "/home/ubuntu/git_token.txt", "/workspace/.git",
                 "/workspace/.env", "/workspace/data/trading.db", "/workspace/state/gpt_bridge/settings.json"):
        assert not Path(path).exists()
    with socket.socket() as connection:
        connection.settimeout(0.2)
        with pytest.raises(OSError):
            connection.connect(("1.1.1.1", 443))
    with pytest.raises(OSError) as error:
        Path("/workspace/.write_probe").write_text("not allowed")
    assert error.value.errno in {errno.EROFS, errno.EACCES}
