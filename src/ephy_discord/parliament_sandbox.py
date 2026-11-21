"""Official Codex permission profiles with freshly measured parent/child controls．"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from ephy_discord.parliament import ParliamentError, unlinked

PROBE = r"""
import errno,json,pathlib,socket,subprocess,sys
nonce,protected,temporary,candidate,writable,child=sys.argv[1:]
def denied(fn):
 try: fn();return False
 except PermissionError:return True
def network():
 sock=socket.socket();sock.settimeout(1)
 try: sock.connect(('1.1.1.1',443));return False
 except OSError as exc:
  return exc.errno in (errno.EACCES,errno.EPERM) or getattr(exc,'winerror',0)==10013
 finally:sock.close()
p=pathlib.Path(protected);t=pathlib.Path(temporary)/('write-'+child)
t.write_text(nonce)
out={'read_denied':denied(p.read_bytes),'write_denied':denied(lambda:p.open('r+b')),
     'network_denied':network(),'inside_write':t.read_text()==nonce}
c=pathlib.Path(candidate)
out['candidate_read']=c.read_text()==nonce
out['candidate_write_matches_mode']=(not denied(lambda:c.open('r+b').close()))==(writable=='true')
if child=='parent':
 result=subprocess.run([sys.executable,'-I',__file__,nonce,protected,temporary,candidate,writable,'child'],
                       capture_output=True,timeout=10)
 if result.returncode:raise RuntimeError('child_probe_failed')
 out['child']=json.loads(result.stdout)['controls']
print(json.dumps({'nonce':nonce,'controls':out}))
"""


def minimal_environment(home: Path, temporary: Path) -> dict:
    env = {
        key: os.environ[key]
        for key in (
            "SystemRoot",
            "SystemDrive",
            "WINDIR",
            "OS",
            "PATHEXT",
            "COMSPEC",
            "LOCALAPPDATA",
            "APPDATA",
            "USERPROFILE",
        )
        if key in os.environ
    }
    env.update(
        CODEX_HOME=str(home),
        TEMP=str(temporary),
        TMP=str(temporary),
        TMPDIR=str(temporary),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
    )
    return env


class NativeSandbox:
    def __init__(self, binary: Path, config: dict, home: Path, execute):
        self.binary, self.config, self.home, self.execute = binary, config, home, execute

    def profile(self, candidate: Path, temporary: Path, *, writable: bool, protected: Path) -> str:
        roots = {":root": "deny", ":minimal": "read"}
        protected_roots = [
            unlinked(p) for p in [*self.config["denied_roots"], self.home, protected]
        ]
        for path in self.config["read_roots"]:
            root = unlinked(path)
            if root == Path(root.anchor) or root == Path.home().resolve():
                raise ParliamentError("overbroad_sandbox_read_root")
            if any(root.is_relative_to(p) or p.is_relative_to(root) for p in protected_roots):
                raise ParliamentError("sandbox_read_root_reopens_protected_path")
            roots[str(root).replace("\\", "/")] = "read"
        for path in [*self.config["denied_roots"], self.home, protected]:
            roots[str(unlinked(path)).replace("\\", "/")] = "deny"
        roots[str(candidate).replace("\\", "/")] = "write" if writable else "read"
        roots[str(candidate / ".git").replace("\\", "/")] = "deny"
        roots[str(temporary).replace("\\", "/")] = "write"
        entries = ",".join(
            json.dumps(path) + "=" + json.dumps(mode) for path, mode in roots.items()
        )
        return "permissions.parliament={filesystem={" + entries + "},network={enabled=false}}"

    def argv(self, profile: str, cwd: Path, argv: list[str]) -> list[str]:
        return [
            str(self.binary),
            "-c",
            'windows.sandbox="mxc"',
            "sandbox",
            "-C",
            str(cwd),
            "-P",
            "parliament",
            "--include-managed-config",
            "-c",
            profile,
            "--",
            *argv,
        ]

    async def controls(self, root: Path, candidate: Path, *, writable=True) -> dict:
        unlinked(root).mkdir(parents=True, exist_ok=True)
        private, temporary = root / "private-control", root / "control-temp"
        private.mkdir()
        temporary.mkdir()
        protected = private / "canary"
        protected.write_bytes(uuid.uuid4().bytes)
        probe = temporary / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        nonce = uuid.uuid4().hex
        candidate_control = candidate / (".parliament-control-" + nonce)
        candidate_control.write_text(nonce, encoding="utf-8")
        profile = self.profile(candidate, temporary, writable=writable, protected=private)
        try:
            output = await self.execute(
                self.argv(
                    profile,
                    candidate,
                    [
                        sys.executable,
                        "-I",
                        str(probe),
                        nonce,
                        str(protected),
                        str(temporary),
                        str(candidate_control),
                        "true" if writable else "false",
                        "parent",
                    ],
                ),
                cwd=candidate,
                env=minimal_environment(self.home, temporary),
                timeout=30,
            )
        finally:
            candidate_control.unlink()
        try:
            result = json.loads(output)
            controls = result["controls"]
            valid = result["nonce"] == nonce and all(
                type(group[key]) is bool and group[key]
                for group in [controls, controls["child"]]
                for key in [
                    "read_denied",
                    "write_denied",
                    "network_denied",
                    "inside_write",
                    "candidate_read",
                    "candidate_write_matches_mode",
                ]
            )
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ParliamentError("native_sandbox_controls_failed")
        record = {
            "official_native_sandbox": True,
            "fresh_parent_child_controls": True,
            "profile": profile,
            "controls": controls,
            "candidate_writable": writable,
        }
        (root / "sandbox-controls.json").write_text(json.dumps(record), encoding="utf-8")
        return record
