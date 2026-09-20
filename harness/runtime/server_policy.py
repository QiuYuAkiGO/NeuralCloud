"""Runtime-owned classification of remote Linux commands.

The model cannot declare risk. Unparseable text is high-risk; forbidden patterns
are refused even after a researcher approval.
"""
from __future__ import annotations

import re
import shlex
from typing import Literal

from harness.embodiment.server_contracts import ServerError


RiskClass = Literal["read_only", "routine", "high_risk"]

_PREFIXES = {"sudo", "env", "nice", "nohup", "time", "timeout", "stdbuf", "ionice", "chrt", "command"}
_READ_ONLY = {
    "true", "false", "echo", "printf", "uname", "hostname", "whoami", "id", "date", "uptime",
    "pwd", "df", "free", "ps", "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg",
    "stat", "du", "wc", "file", "which", "type", "basename", "dirname", "realpath", "readlink",
    "md5sum", "sha256sum", "sha1sum", "cksum", "journalctl", "ss", "ping", "host", "dig", "nslookup",
    "who", "w", "last", "lastlog", "users", "groups", "getent", "locale", "lscpu", "lsblk",
    "lsusb", "lspci", "nproc", "arch", "sysctl", "dmesg", "namei", "test", "[",
}
_HIGH_RISK = {
    "rm", "rmdir", "shred", "unlink", "truncate", "dd", "wipefs", "fdisk", "parted", "sgdisk",
    "userdel", "groupdel", "reboot", "shutdown", "halt", "poweroff", "init", "telinit",
    "killall", "pkill", "mkfs", "mkswap", "lvremove", "vgremove", "pvremove",
}
_INTERPRETERS = {"bash", "sh", "dash", "zsh", "ksh", "csh", "tcsh", "fish", "python", "python3",
                 "perl", "ruby", "node", "nodejs", "php", "lua", "awk"}
_PACKAGE = {"apt", "apt-get", "dnf", "yum", "zypper", "pacman", "apk", "pip", "pip3", "npm", "pnpm", "yarn"}
_PACKAGE_REMOVE = {"remove", "purge", "uninstall", "autoremove", "erase", "-R", "-r"}
_SYSTEMCTL_RISK = {"stop", "disable", "mask", "isolate", "rescue", "emergency", "kexec",
                   "hibernate", "hybrid-sleep", "suspend", "poweroff", "reboot", "halt", "kill", "reset-failed"}
_SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files",
                   "list-jobs", "show", "cat", "help", "list-timers", "list-sockets", "list-dependencies"}
_DOCKER_RISK = {"rm", "rmi", "prune", "kill", "stop"}
_DOCKER_READ = {"ps", "logs", "inspect", "images", "info", "version", "stats", "top", "events", "port"}
_GIT_READ = {"status", "log", "diff", "show", "rev-parse", "branch", "remote", "ls-files", "describe", "cat-file"}
_FIND_MUTATE = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
_FORK_BOMB = re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;")


class CommandClassification:
    __slots__ = ("risk_class", "reason", "segments")

    def __init__(self, risk_class: RiskClass, reason: str, segments: tuple[str, ...] = ()):
        self.risk_class = risk_class
        self.reason = reason
        self.segments = segments


def classify_command(
    command: str,
    *,
    extra_high_risk_patterns: tuple[str, ...] = (),
    forbidden_patterns: tuple[str, ...] = (),
    sudo_requires_approval: bool = False,
) -> CommandClassification:
    if not isinstance(command, str) or not command.strip():
        raise ServerError("server_command_invalid")
    text = command.strip()
    if _forbidden(text, forbidden_patterns):
        raise ServerError("server_command_forbidden")
    extra = _compile_extra(extra_high_risk_patterns)
    if any(pattern.search(text) for pattern in extra):
        return CommandClassification("high_risk", "extra_high_risk_pattern")
    segments = _split_segments(text)
    if segments is None:
        return CommandClassification("high_risk", "unparseable")
    classes: list[RiskClass] = []
    reasons: list[str] = []
    for segment, kind in segments:
        if kind == "overwrite":
            classes.append("high_risk")
            reasons.append("overwrite_redirect")
            continue
        if kind == "append":
            classes.append("routine")
            reasons.append("append_redirect")
        item = _classify_segment(segment, sudo_requires_approval=sudo_requires_approval)
        if item.risk_class == "high_risk" and item.reason == "forbidden":
            raise ServerError("server_command_forbidden")
        classes.append(item.risk_class)
        reasons.append(item.reason)
    if "high_risk" in classes:
        return CommandClassification("high_risk", next(reason for cls, reason in zip(classes, reasons) if cls == "high_risk"),
                                     tuple(segment for segment, _ in segments))
    if "routine" in classes:
        return CommandClassification("routine", next(reason for cls, reason in zip(classes, reasons) if cls == "routine"),
                                     tuple(segment for segment, _ in segments))
    return CommandClassification("read_only", "read_only", tuple(segment for segment, _ in segments))


def _compile_extra(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            raise ServerError("server_policy_pattern_invalid") from None
    return tuple(compiled)


def _forbidden(text: str, extra: tuple[str, ...]) -> bool:
    if _FORK_BOMB.search(text) or _rm_root(text):
        return True
    return any(pattern.search(text) for pattern in _compile_extra(extra))


def _rm_root(text: str) -> bool:
    for match in re.finditer(r"\brm\b([^;&\n|]*)", text, flags=re.I):
        tokens = match.group(1).split()
        flags = "".join(token[1:] for token in tokens if token.startswith("-") and not token.startswith("--"))
        longs = {token for token in tokens if token.startswith("--")}
        targets = [token.strip("'\"") for token in tokens if not token.startswith("-")]
        destructive = ("r" in flags and "f" in flags) or "--no-preserve-root" in longs
        if destructive and any(target in {"/", "/*"} or target.startswith("/*") for target in targets):
            return True
        if "--no-preserve-root" in longs:
            return True
    return False


def _split_segments(command: str) -> list[tuple[str, str]] | None:
    segments: list[tuple[str, str]] = []
    current: list[str] = []
    quote = ""
    escaped = False
    i = 0
    redirect = "none"

    def flush():
        nonlocal redirect
        text = "".join(current).strip()
        current.clear()
        kind = redirect
        redirect = "none"
        if text:
            segments.append((text, kind))

    while i < len(command):
        char = command[i]
        if quote:
            current.append(char)
            if quote == '"' and char == "\\" and not escaped:
                escaped = True
                i += 1
                continue
            if char == quote and not escaped:
                quote = ""
            escaped = False
            i += 1
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            i += 1
            continue
        if char == "\\":
            if i + 1 >= len(command):
                return None
            current.append(char)
            current.append(command[i + 1])
            i += 2
            continue
        if char == "`" or command.startswith("$(", i) or command.startswith("<(", i) or command.startswith(">(", i):
            return None
        if command.startswith("&&", i) or command.startswith("||", i) or command.startswith("|&", i):
            flush()
            i += 2
            continue
        if char in {";", "|", "\n"}:
            flush()
            i += 1
            continue
        if char == ">":
            if command.startswith(">>", i):
                redirect = "append" if redirect != "overwrite" else "overwrite"
                current.append(">>")
                i += 2
                continue
            if i + 1 < len(command) and command[i + 1] == "&":
                current.append(char)
                i += 1
                continue
            redirect = "overwrite"
            current.append(char)
            i += 1
            continue
        current.append(char)
        i += 1
    if quote:
        return None
    flush()
    return segments or None


def _classify_segment(segment: str, *, sudo_requires_approval: bool) -> CommandClassification:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return CommandClassification("high_risk", "unparseable")
    if not tokens:
        return CommandClassification("high_risk", "unparseable")
    used_sudo = False
    index = 0
    while index < len(tokens):
        name = _basename(tokens[index])
        if name in _PREFIXES:
            if name == "sudo":
                used_sudo = True
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                flag = tokens[index]
                index += 1
                if flag in {"-u", "-g", "-C", "-U", "-s"} or (name == "timeout" and flag in {"-k", "-s", "--signal", "--kill-after"}):
                    if index < len(tokens):
                        index += 1
                if flag == "--":
                    break
            if name in {"timeout", "nice", "stdbuf", "ionice"} and index < len(tokens) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[smhd]?", tokens[index]):
                index += 1
            continue
        if "=" in tokens[index] and not tokens[index].startswith("=") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
            index += 1
            continue
        break
    if index >= len(tokens):
        return CommandClassification("high_risk", "unparseable")
    executable = _basename(tokens[index])
    args = tokens[index + 1:]
    if used_sudo and sudo_requires_approval:
        return CommandClassification("high_risk", "sudo")
    if executable in {"eval", "xargs", "source"} or executable == ".":
        return CommandClassification("high_risk", "unparseable")
    if executable in _INTERPRETERS and any(arg in {"-c", "-e", "--eval", "-x"} for arg in args):
        return CommandClassification("high_risk", "unparseable")
    if executable in _HIGH_RISK or executable.startswith("mkfs"):
        return CommandClassification("high_risk", executable)
    if executable == "find" and any(arg in _FIND_MUTATE or arg.startswith("-exec") for arg in args):
        return CommandClassification("high_risk", "find_mutate")
    if executable == "rsync" and any(arg == "--delete" or arg.startswith("--delete-") for arg in args):
        return CommandClassification("high_risk", "rsync_delete")
    if executable == "git" and _git_risk(args):
        return CommandClassification("high_risk", "git_destructive")
    if executable in {"docker", "podman"} and _docker_risk(args):
        return CommandClassification("high_risk", "docker_destructive")
    if executable in _PACKAGE and any(arg in _PACKAGE_REMOVE for arg in args):
        return CommandClassification("high_risk", "package_remove")
    if executable in {"userdel", "groupdel"} or (executable == "crontab" and "-r" in args):
        return CommandClassification("high_risk", executable)
    if executable == "systemctl" and _systemctl_risk(args):
        return CommandClassification("high_risk", "systemctl_mutate")
    if executable == "kill" and _kill_dash_nine(args):
        return CommandClassification("high_risk", "kill_force")
    if executable in {"chmod", "chown", "chgrp"} and any(arg in {"-R", "--recursive"} for arg in args):
        return CommandClassification("high_risk", "recursive_ownership")
    if executable == "find" or (executable in {"docker", "podman"} and _docker_read(args)) or (
            executable == "git" and _git_read(args)) or (executable == "systemctl" and _systemctl_read(args)) or (
            executable == "ip" and _ip_read(args)) or executable in _READ_ONLY:
        if executable == "ip" and not _ip_read(args):
            return CommandClassification("routine", "ip_mutate")
        if executable in {"docker", "podman"} and not _docker_read(args) and not _docker_risk(args):
            return CommandClassification("routine", "docker_other")
        if executable == "git" and not _git_read(args) and not _git_risk(args):
            return CommandClassification("routine", "git_other")
        if executable == "systemctl" and not _systemctl_read(args) and not _systemctl_risk(args):
            return CommandClassification("routine", "systemctl_other")
        return CommandClassification("read_only", "read_only")
    return CommandClassification("routine", executable or "routine")


def _basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _git_risk(args: list[str]) -> bool:
    if not args:
        return False
    verb = args[0]
    rest = args[1:]
    if verb == "clean":
        return True
    if verb == "reset" and "--hard" in rest:
        return True
    if verb == "branch" and any(arg in {"-D", "-d"} for arg in rest):
        return True
    if verb == "push" and any(arg in {"-f", "--force", "--force-with-lease"} for arg in rest):
        return True
    return False


def _git_read(args: list[str]) -> bool:
    return bool(args) and args[0] in _GIT_READ and not _git_risk(args)


def _docker_risk(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "volume" and len(args) > 1 and args[1] in {"rm", "prune"}:
        return True
    if args[0] == "system" and "prune" in args:
        return True
    if args[0] in {"container", "image", "network"} and len(args) > 1 and args[1] in _DOCKER_RISK:
        return True
    return args[0] in _DOCKER_RISK


def _docker_read(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] in {"container", "image"} and len(args) > 1 and args[1] in {"ls", "list", "inspect", "logs"}:
        return True
    return args[0] in _DOCKER_READ


def _systemctl_risk(args: list[str]) -> bool:
    verbs = [arg for arg in args if not arg.startswith("-")]
    return bool(verbs) and verbs[0] in _SYSTEMCTL_RISK


def _systemctl_read(args: list[str]) -> bool:
    verbs = [arg for arg in args if not arg.startswith("-")]
    return bool(verbs) and verbs[0] in _SYSTEMCTL_READ


def _ip_read(args: list[str]) -> bool:
    mutating = {"add", "delete", "del", "set", "change", "replace", "flush", "up", "down"}
    return not any(arg in mutating for arg in args)


def _kill_dash_nine(args: list[str]) -> bool:
    return any(arg in {"-9", "-KILL", "-SIGKILL"} or arg.upper() == "-KILL" for arg in args)
