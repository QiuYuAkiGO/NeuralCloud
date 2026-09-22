"""Command risk classification is Runtime-owned and independent of model claims."""

import pytest

from harness.embodiment.server_contracts import ServerError
from harness.runtime.server_policy import classify_command


def test_read_only_whitelist_and_pipelines():
    for command in (
        "uptime", "df -h", "free -m", "ps aux", "ls -la /var/log",
        "cat /etc/os-release", "head -n 20 /var/log/syslog", "tail -n 5 /var/log/syslog",
        "grep misty /var/log/syslog", "stat /tmp", "du -sh /var/log",
        "journalctl -u neuralcloud-runtime -n 20 --no-pager",
        "systemctl status neuralcloud-runtime", "systemctl is-active neuralcloud-runtime",
        "systemctl list-units --type=service", "ss -lnt", "ip addr", "ip -br link",
        "docker ps", "docker logs abc", "git status", "git log -1",
        "find /tmp -name '*.log'", "uname -a; id", "ls /tmp | grep foo",
        "echo hello", "whoami",
    ):
        item = classify_command(command)
        assert item.risk_class == "read_only", (command, item.reason)


def test_high_risk_deletes_and_destructive_forms():
    cases = {
        "rm /tmp/file": "rm",
        "rmdir /tmp/old": "rmdir",
        "sudo rm /tmp/file": "rm",
        "find /var/tmp -delete": "find_mutate",
        "find /tmp -exec rm {} +": "find_mutate",
        "rsync --delete a/ b/": "rsync_delete",
        "git clean -fd": "git_destructive",
        "git reset --hard": "git_destructive",
        "git branch -D topic": "git_destructive",
        "git push -f origin main": "git_destructive",
        "docker rm container": "docker_destructive",
        "docker rmi image": "docker_destructive",
        "docker system prune -f": "docker_destructive",
        "docker volume rm data": "docker_destructive",
        "apt-get remove pkg": "package_remove",
        "pip uninstall requests": "package_remove",
        "npm uninstall left-pad": "package_remove",
        "userdel alice": "userdel",
        "crontab -r": "crontab",
        "reboot": "reboot",
        "systemctl stop nginx": "systemctl_mutate",
        "systemctl disable nginx": "systemctl_mutate",
        "systemctl mask nginx": "systemctl_mutate",
        "kill -9 42": "kill_force",
        "killall python": "killall",
        "chmod -R 777 /tmp/x": "recursive_ownership",
        "chown -R root /tmp/x": "recursive_ownership",
        "echo hi > /tmp/out": "overwrite_redirect",
        "dd if=/dev/zero of=/tmp/x": "dd",
    }
    for command, reason in cases.items():
        item = classify_command(command)
        assert item.risk_class == "high_risk", command
        assert item.reason == reason, (command, item.reason)


def test_append_redirect_is_routine_not_high_risk():
    item = classify_command("echo hello >> /tmp/out")
    assert item.risk_class == "routine"
    assert item.reason == "append_redirect"


def test_unparseable_forms_are_high_risk():
    for command in (
        "eval ls", "bash -c 'ls'", "sh -c ls", "xargs rm", "source ./env", ". ./env",
        "echo $(reboot)", "echo `reboot`", "echo 'unterminated",
    ):
        item = classify_command(command)
        assert item.risk_class == "high_risk", command
        assert item.reason == "unparseable", (command, item.reason)


def test_sudo_requires_approval_switch():
    assert classify_command("sudo ls /tmp").risk_class == "read_only"
    item = classify_command("sudo ls /tmp", sudo_requires_approval=True)
    assert item.risk_class == "high_risk" and item.reason == "sudo"


def test_forbidden_patterns_rejected_even_with_wrappers():
    for command in ("rm -rf /", "rm -rf / ", "rm -rf --no-preserve-root /", "bash -c 'rm -rf /'", ":(){ :|:& };:"):
        with pytest.raises(ServerError, match="server_command_forbidden"):
            classify_command(command)


def test_operator_extra_high_risk_and_forbidden_regex():
    item = classify_command("systemctl restart nginx", extra_high_risk_patterns=(r"\brestart\b",))
    assert item.risk_class == "high_risk" and item.reason == "extra_high_risk_pattern"
    with pytest.raises(ServerError, match="server_command_forbidden"):
        classify_command("touch /etc/passwd", forbidden_patterns=(r"/etc/passwd",))
    with pytest.raises(ServerError, match="server_policy_pattern_invalid"):
        classify_command("ls", extra_high_risk_patterns=("(unterminated",))


def test_routine_unknown_binaries_are_not_read_only():
    item = classify_command("systemctl restart nginx")
    assert item.risk_class == "routine"
    item = classify_command("mkdir /tmp/workspace-misty")
    assert item.risk_class == "routine"
    item = classify_command("git pull")
    assert item.risk_class == "routine"
