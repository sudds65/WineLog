"""Guards for the installer shell script.

The installer runs under `set -euo pipefail`, where a pipeline whose last
command "fails" aborts the whole run. `grep` finding nothing counts as a
failure, which is easy to write by accident and only shows up on the happy
path — a free port, an empty listing — so these check the real thing.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "deploy" / "install.sh"
SCRIPT = INSTALLER.read_text()

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is needed to check the installer"
)


def run_bash(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{body}"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def extract(name: str) -> str:
    """Pull one shell function out of the installer, closing at its own indent."""
    match = re.search(
        rf"^([ \t]*){name}\(\) \{{.*?^\1\}}", SCRIPT, re.MULTILINE | re.DOTALL
    )
    assert match, f"{name}() not found in install.sh"
    return match.group(0)


def test_the_installer_parses():
    assert subprocess.run(["bash", "-n", str(INSTALLER)]).returncode == 0


def test_port_holder_succeeds_when_the_port_is_free():
    """The common case. This aborted the installer before `|| true` was added."""
    result = run_bash(extract("port_holder") + '\nholder="$(port_holder 65533)"\necho "[$holder]"')
    assert result.returncode == 0, result.stderr
    assert "[]" in result.stdout


def test_port_holder_survives_ss_being_missing():
    result = run_bash(
        'ss() { return 127; }\n'
        + extract("port_holder")
        + '\nholder="$(port_holder 65533)"\necho "ok"'
    )
    assert result.returncode == 0, result.stderr


def test_port_holder_names_the_process_holding_a_port():
    fake_ss = (
        'ss() { echo \'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* '
        "users:((\"nginx\",pid=42,fd=6))'; }\n"
    )
    result = run_bash(fake_ss + extract("port_holder") + "\nport_holder 80")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nginx"


def test_set_env_adds_a_key_that_is_not_there_yet(tmp_path):
    """Upgrades land on env files written before a setting existed."""
    env_file = tmp_path / "winelog.env"
    env_file.write_text("WINELOG_PORT=8071\n")
    body = (
        f'ENV_FILE="{env_file}"\n'
        + extract("set_env")
        + "\nset_env WINELOG_TLS_CERT /etc/winelog/tls/winelog.crt"
        + "\nset_env WINELOG_PORT 443"
    )
    assert run_bash(body).returncode == 0
    written = env_file.read_text()
    assert "WINELOG_PORT=443" in written
    assert "WINELOG_TLS_CERT=/etc/winelog/tls/winelog.crt" in written
    assert written.count("WINELOG_PORT=") == 1


def sans_for(*values: str) -> str:
    """Run the installer's SAN builder over some names and return the list."""
    calls = "\n".join(f'add_san "{value}"' for value in values)
    result = run_bash(f'ALT=""\n{extract("add_san")}\n{calls}\necho "$ALT"')
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_names_and_addresses_are_tagged_correctly():
    """A browser only accepts an IP if it is an IP SAN, not a DNS one."""
    assert sans_for("winelog.suddarth.local", "192.168.86.150") == (
        "DNS:winelog.suddarth.local,IP:192.168.86.150"
    )


def test_a_repeated_name_is_only_listed_once():
    assert sans_for("winelog", "winelog") == "DNS:winelog"


def test_an_empty_name_is_skipped_without_aborting():
    """`hostname -I` comes back empty on a machine with no LAN address."""
    assert sans_for("", "winelog") == "DNS:winelog"


def test_strays_is_quiet_when_nothing_else_is_running():
    """Runs on every install, including boxes with no systemctl at all."""
    result = run_bash(
        "systemctl() { return 127; }\n"
        'pgrep() { return 1; }\n'
        + extract("strays")
        + '\nout="$(strays)"\necho "[$out]"'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_strays_reports_a_hand_started_winelog():
    """The service PID is systemd's; anything else is someone's stray shell."""
    result = run_bash(
        "systemctl() { echo 111; }\n"
        "pgrep() { printf '111\\n222\\n'; }\n"
        + extract("strays")
        + "\nstrays"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["222"]


def test_help_lists_the_tls_options():
    result = subprocess.run(
        ["bash", str(INSTALLER), "--help"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    for flag in ("--https", "--tls-cert", "--tls-key", "--port"):
        assert flag in result.stdout
