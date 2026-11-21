"""Observe fixed-check normal completion；exit 0 alone never authorizes publishing．"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from ephy_discord.parliament import ParliamentError

PYTHON_CHECK = r"""
import hashlib,json,pathlib,subprocess,sys
candidate,record,nonce,raw=sys.argv[1:]
argv=json.loads(raw);args=argv[1:]
if args and args[0]=='-I':args=args[1:]
pytest=None
if args[:2]==['-m','pytest']:import pytest
sys.path[:0]=[str(pathlib.Path(candidate)/'src'),candidate]
sys.argv=['fixed-check',*args]
if args and args[0]=='-c' and len(args)==2:
 exec(compile(args[1],'<fixed-check>','exec'),{'__name__':'__main__'})
 code=0
elif args[:2]==['-m','pytest']:
 code=int(pytest.main(args[2:]))
elif args[:2]==['-m','ruff']:
 code=subprocess.run([sys.executable,'-I',*args],check=False).returncode
else:raise RuntimeError('unsupported_fixed_python_check')
if code:raise SystemExit(code)
pathlib.Path(record).write_text(json.dumps({'nonce':nonce,
 'argv_sha256':hashlib.sha256(raw.encode()).hexdigest(),'completed':True,'exit_code':0}))
"""

NODE_CHECK = r"""
import fs from 'node:fs';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const [candidate,record,nonce,raw]=process.argv.slice(2),argv=JSON.parse(raw);
const args=argv.slice(1),AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
if(args[0]!=='-e'||args.length!==2)throw Error('unsupported_fixed_node_check');
await new AsyncFunction('require',args[1])(createRequire(candidate+'/package.json'));
fs.writeFileSync(record,JSON.stringify({nonce,
 argv_sha256:crypto.createHash('sha256').update(raw).digest('hex'),completed:true,exit_code:0}));
"""


def validate_fixed_check(argv):
    executable = Path(argv[0]).name.lower()
    args = argv[1:]
    if args and args[0] == "-I":
        args = args[1:]
    if executable.startswith("python") and (
        len(args) == 2 and args[0] == "-c" or args[:2] in (["-m", "pytest"], ["-m", "ruff"])
    ):
        return
    if executable in {"node", "node.exe"} and len(args) == 2 and args[0] == "-e":
        return
    raise ParliamentError("unsupported_fixed_check_runtime")


async def fixed_check(*, sandbox, profile, candidate, temporary, wrapper_root, argv, env, execute):
    validate_fixed_check(argv)
    executable = Path(argv[0]).name.lower()
    raw = json.dumps(argv, ensure_ascii=True)
    nonce = uuid.uuid4().hex
    record = temporary / ("completion-" + nonce + ".json")
    wrapper_root.mkdir(parents=True, exist_ok=True)
    if executable.startswith("python"):
        wrapper = wrapper_root / "python-check.py"
        wrapper.write_text(PYTHON_CHECK, encoding="utf-8")
        command = [argv[0], "-I", str(wrapper), str(candidate), str(record), nonce, raw]
    elif executable in {"node", "node.exe"}:
        wrapper = wrapper_root / "node-check.mjs"
        wrapper.write_text(NODE_CHECK, encoding="utf-8")
        command = [argv[0], str(wrapper), str(candidate), str(record), nonce, raw]
    else:
        raise ParliamentError("unsupported_fixed_check_runtime")
    profile = profile.replace(
        "filesystem={",
        "filesystem={" + json.dumps(str(wrapper_root).replace("\\", "/")) + '="read",',
        1,
    )
    await execute(sandbox.argv(profile, candidate, command), cwd=candidate, env=env, timeout=30)
    if not record.is_file() or record.stat().st_size > 1024:
        raise ParliamentError("check_normal_completion_missing")
    expected = {
        "nonce": nonce,
        "argv_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "completed": True,
        "exit_code": 0,
    }
    if json.loads(record.read_bytes()) != expected:
        raise ParliamentError("check_normal_completion_mismatch")
    return {"argv": argv, "exit_code": 0, "normal_completion_verified": True}
