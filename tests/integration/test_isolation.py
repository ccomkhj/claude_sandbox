import shutil as _sh
import subprocess
import time
from pathlib import Path

import pytest

from sandbox import cli, session


@pytest.mark.integration
def test_isolation_invariants(
    docker_available, fixture_repo, tiny_dump, fake_creds, sandbox_home, monkeypatch, tmp_path, capsys
):
    if not docker_available:
        pytest.skip("docker daemon not available")

    # Use a modified agent-stub that idles 120s after its normal work so we can
    # docker exec into it.
    # v0.2.0: db/ subdir is gone — the CLI uses upstream postgres:16 and imports the dump
    # at runtime via docker cp + pg_restore. Only agent/ is needed here.
    images_real = Path(__file__).resolve().parents[2] / "images"
    images_patched = tmp_path / "images"
    (images_patched / "agent").mkdir(parents=True)
    _sh.copy(images_real / "agent-stub" / "Dockerfile", images_patched / "agent" / "Dockerfile")

    # v0.3.0: CLI now builds the proxy image from images/proxy — copy it into the
    # patched images root so the monkeypatched _images_root() resolves it correctly.
    _sh.copytree(images_real / "proxy", images_patched / "proxy")

    # Custom entrypoint that performs the stub work then idles. We replace the
    # final stub-marker line with marker + `sleep 120`, leaving the exit trap intact.
    stub_entry = (images_real / "agent-stub" / "entrypoint.sh").read_text()
    patched_entry = stub_entry.replace(
        'echo "$GOAL" > /work/repo/STUB_OUTPUT.md',
        'echo "$GOAL" > /work/repo/STUB_OUTPUT.md\nsleep 120',
    )
    assert patched_entry != stub_entry, "Patched entrypoint sentinel not found — stub entrypoint changed?"
    entry_path = images_patched / "agent" / "entrypoint.sh"
    entry_path.write_text(patched_entry)
    entry_path.chmod(0o755)

    monkeypatch.setattr(cli, "_images_root", lambda: images_patched)
    monkeypatch.setattr(cli.dump, "fetch", lambda b, k: (tiny_dump, "tinyetag"))

    rc = cli.main([
        "start", "--repo", str(fixture_repo), "--goal", "iso",
        "--dump-bucket", "x", "--dump-key", "y", "--db-name", "appdb",
        "--agent-image", "sandbox-agent-iso",
    ])
    assert rc == 0
    sid = capsys.readouterr().out.strip().splitlines()[-1]
    project = sid.lower()
    agent_name = f"{project}-agent-1"
    db_name = f"{project}-db-1"

    try:
        # Wait for both services to be up
        deadline = time.time() + 180
        while time.time() < deadline:
            ps = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"], check=True, capture_output=True, text=True
            ).stdout
            if agent_name in ps and db_name in ps:
                break
            time.sleep(2)
        else:
            pytest.fail("services never came up")

        # Agent CAN reach db
        r = subprocess.run(
            ["docker", "exec", agent_name, "psql", "-h", "db", "-U", "postgres", "-d", "appdb", "-c", "SELECT 1"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"agent could not reach db: {r.stderr}"

        # Agent CANNOT reach host on common ports (host.docker.internal)
        r = subprocess.run(
            ["docker", "exec", agent_name, "bash", "-c",
             "curl --max-time 3 -sS http://host.docker.internal:5432/ ; echo rc=$?"],
            capture_output=True, text=True,
        )
        # rc=0 would mean it connected — must NOT happen for isolation.
        # Any non-zero rc is acceptable (connection refused, timeout, dns failure).
        assert "rc=0" not in r.stdout, f"agent reached host.docker.internal — isolation broken. stdout={r.stdout!r}"

        # db CANNOT resolve external hostnames
        r = subprocess.run(
            ["docker", "exec", db_name, "bash", "-c",
             "getent hosts example.com >/dev/null 2>&1; echo rc=$?"],
            capture_output=True, text=True,
        )
        # rc=2 (NXDOMAIN-ish, no such name) or rc=1 (other failure) both prove no public DNS.
        # rc=0 would mean the DB resolved an external name — isolation broken.
        assert "rc=0" not in r.stdout, f"db resolved an external host — isolation broken. stdout={r.stdout!r}"

        # v0.3.0 invariant: allowlisted hosts ARE reachable via the proxy.
        # Any HTTP response code proves the TLS handshake completed; the actual
        # response from Anthropic will be 401 (no auth) or 403, but NOT a curl error.
        res = subprocess.run([
            "docker", "exec", agent_name,
            "curl", "--max-time", "15", "-sS", "-o", "/dev/null",
            "-w", "%{http_code}",
            "https://api.anthropic.com/",
        ], capture_output=True, text=True)
        assert res.returncode == 0, (
            f"allowlisted host unreachable through proxy: rc={res.returncode}\n"
            f"stderr: {res.stderr}\nstdout: {res.stdout}"
        )
        code = res.stdout.strip()
        assert code.isdigit() and code.startswith(("2", "3", "4")), (
            f"unexpected HTTP code from allowlisted host: {code!r} "
            f"(expected 2xx/3xx/4xx — reachability proves the proxy let us out)"
        )

        # v0.3.0 invariant: non-allowlisted hosts are blocked by the proxy.
        # Squid returns 403 for denied CONNECT; curl reports the proxy response code.
        res = subprocess.run([
            "docker", "exec", agent_name,
            "curl", "--max-time", "15", "-sS", "-o", "/dev/null",
            "-w", "%{http_code}",
            "https://example.com/",
        ], capture_output=True, text=True)
        code = res.stdout.strip()
        assert code in ("403", "000"), (
            f"non-allowlisted host returned code {code!r} — expected 403 (proxy denied) "
            f"or 000 (no response). If this returned 200, the egress boundary is broken."
        )
    finally:
        # Always tear down, even if assertions failed.
        cli.main(["stop", sid])
