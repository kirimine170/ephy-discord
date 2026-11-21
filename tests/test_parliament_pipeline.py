from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from ephy_discord.parliament import Parliament, ParliamentError, Request, load_policy
from ephy_discord.parliament_codex import CodexPipeline
from ephy_discord.parliament_executor import command, safe_git_source
from ephy_discord.parliament_publisher import GitHubPublisher
from ephy_discord.parliament_sandbox import NativeSandbox, minimal_environment
from tests.test_parliament import setup as shared_setup

setup = shared_setup


def git_call(git, root, env, *args):
    result = subprocess.run(
        [
            str(git),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={root / 'no-hooks'}",
            *args,
        ],
        env=env,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def pipeline(request):
    setup = request.getfixturevalue("setup")
    x = setup
    git = Path(shutil.which("git")).resolve()
    source = Path(x.value["repositories"]["demo"]["path"])
    home, temp = x.path.parent / "existing-home", x.path.parent / "temporary"
    home.mkdir()
    temp.mkdir()
    env = minimal_environment(home, temp)
    env.update(
        GIT_AUTHOR_NAME="Synthetic",
        GIT_AUTHOR_EMAIL="synthetic@example.invalid",
        GIT_COMMITTER_NAME="Synthetic",
        GIT_COMMITTER_EMAIL="synthetic@example.invalid",
    )
    git_call(git, source, env, "init", "-b", "main", str(source))
    (source / "src").mkdir()
    (source / "tests").mkdir()
    (source / "src/demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "tests/test_existing.py").write_text(
        "def test_existing(): assert True\n", encoding="utf-8"
    )
    git_call(git, source, env, "-C", str(source), "add", ".")
    git_call(git, source, env, "-C", str(source), "commit", "-m", "Synthetic base")
    base = git_call(git, source, env, "-C", str(source), "rev-parse", "HEAD")
    bare = x.path.parent / "synthetic-remote.git"
    git_call(git, source, env, "init", "--bare", str(bare))
    git_call(git, source, env, "-C", str(source), "push", str(bare), "HEAD:refs/heads/main")
    git_call(
        git,
        source,
        env,
        "-C",
        str(source),
        "remote",
        "add",
        "origin",
        "https://github.com/synthetic/parliament.git",
    )
    binary = x.path.parent / "synthetic-codex.exe"
    binary.write_bytes(b"synthetic-no-model-execution")
    config = {
        "enabled": True,
        "binary": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "git": str(git),
        "git_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
        "home": str(home),
        "read_roots": [sys.base_prefix],
        "denied_roots": [str(x.path.parent / "private")],
        "publish_draft_pr": True,
    }
    x.value["codex"] = config
    x.value["repositories"]["demo"]["checks"] = [
        {
            "argv": [
                sys.executable,
                "-I",
                "-c",
                "import runpy; assert runpy.run_path('src/demo.py')['VALUE'] == 2",
            ],
            "sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        }
    ]
    x.path.write_text(json.dumps(x.value), encoding="utf-8")
    x.policy = load_policy(x.path)
    x.git, x.source, x.bare, x.base, x.env, x.config = git, source, bare, base, env, config
    x.api_calls, x.commands, x.credentials, x.models = [], [], [], []
    x.change = "src/demo.py"
    x.base_moved, x.pr_uncertain, x.control_failure, x.verifier_failure = False, False, False, False
    x.early_exit = x.corrupt_publication = x.revoke_on_checks = False

    async def execute(argv, *, cwd, env, **kwargs):
        x.commands.append(argv)
        if argv[0] == str(binary):
            x.models.append(argv)
            assert "GITHUB_TOKEN" not in env and "DISCORD_BOT_TOKEN" not in env
            assert "GIT_CONFIG_VALUE_0" not in env
            assert "--sandbox" not in argv  # Legacy flags must not override granular profiles．
            assert 'default_permissions="parliament"' in argv
            assert "--ignore-user-config" in argv and "--ephemeral" in argv
            target = Path(cwd) / x.change
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "import os; os._exit(0)\n" if x.early_exit else "VALUE = 2\n", encoding="utf-8"
            )
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                '{"text":"Synthetic model result."}', encoding="utf-8"
            )
            return b""
        if "push" in argv and "https://github.com/synthetic/parliament.git" in argv:
            assert "GIT_CONFIG_VALUE_0" in env
            argv = [
                str(bare) if arg == "https://github.com/synthetic/parliament.git" else arg
                for arg in argv
            ]
        if "add" in argv and "--" in argv and x.corrupt_publication:
            (Path(argv[argv.index("-C") + 1]) / "src/demo.py").write_text("VALUE = 999\n")
        return await command(argv, cwd=cwd, env=env, **kwargs)

    class Sandbox:
        def __init__(self, *args):
            pass

        async def controls(self, root, candidate, *, writable=True):
            if x.control_failure:
                raise ParliamentError("native_sandbox_controls_failed")

        def profile(self, candidate, temporary, *, writable, protected):
            return "synthetic-writable" if writable else "synthetic-readonly"

        def argv(self, profile, cwd, argv):
            assert profile == "synthetic-readonly"
            if x.revoke_on_checks:
                x.directory.role = False
            if x.verifier_failure:
                return [sys.executable, "-I", "-c", "raise SystemExit(1)"]
            return argv

    def api(request):
        x.api_calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"object": {"sha": "b" * 40 if x.base_moved else base}})
        if x.pr_uncertain:
            raise httpx.ReadTimeout("synthetic ambiguous PR creation")
        body = json.loads(request.content)
        assert body["draft"] is True and body["base"] == "main"
        assert x.event.content not in body["body"]
        head = git_call(
            git, source, x.env, "--git-dir", str(bare), "rev-parse", "refs/heads/" + body["head"]
        )
        return httpx.Response(
            201,
            json={
                "number": 1,
                "html_url": "https://github.com/synthetic/parliament/pull/1",
                "draft": True,
                "head": {
                    "sha": head,
                    "ref": body["head"],
                    "repo": {"full_name": "synthetic/parliament"},
                },
                "base": {"ref": "main", "repo": {"full_name": "synthetic/parliament"}},
            },
        )

    async def credentials(repository):
        x.credentials.append(repository)
        return "synthetic-not-a-real-token"

    def publisher(git, execute):
        return GitHubPublisher(
            git, execute, transport=httpx.MockTransport(api), credentials=credentials
        )

    x.backend = CodexPipeline(
        x.directory, execute=execute, sandbox_type=Sandbox, publisher_type=publisher
    )
    x.engine = Parliament(x.policy, x.directory, x.backend)
    return x


@pytest.mark.asyncio
async def test_normal_intake_to_tests_real_synthetic_remote_and_draft_url(pipeline):
    x = pipeline
    job = await x.engine.admit(x.event)
    send = AsyncMock(return_value=20000)
    await x.engine.run(job, send)
    row = x.engine.ledger.get(job)
    assert row["execution"] == "succeeded" and row["delivery"] == "sent"
    assert "https://github.com/synthetic/parliament/pull/1" in send.call_args.args[2]
    assert x.credentials == ["synthetic/parliament"]
    assert len(x.models) == 1
    assert x.directory.calls.count("roles") >= 5
    assert git_call(x.git, x.source, x.env, "-C", str(x.source), "rev-parse", "HEAD") == x.base
    assert (x.source / "src/demo.py").read_text() == "VALUE = 1\n"
    branch = "refs/heads/parliament/job-" + job[:32]
    assert (
        git_call(x.git, x.source, x.env, "--git-dir", str(x.bare), "show", branch + ":src/demo.py")
        == "VALUE = 2"
    )
    assert sum(r.method == "POST" for r in x.api_calls) == 1
    await x.engine.run(job, send)
    assert len(x.models) == 1 and send.await_count == 1
    assert sum(r.method == "POST" for r in x.api_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "controls",
        "tests",
        "scope",
        "weaken_test",
        "base_moved",
        "early_exit_zero",
        "publication_corrupt",
        "role_revoked",
    ],
)
async def test_no_publication_on_invalid_artifact_or_changed_base(pipeline, failure):
    x = pipeline
    if failure == "controls":
        x.control_failure = True
    elif failure == "tests":
        x.verifier_failure = True
    elif failure == "scope":
        x.change = "outside.py"
    elif failure == "weaken_test":
        x.change = "tests/test_existing.py"
    elif failure == "early_exit_zero":
        x.early_exit = True
    elif failure == "publication_corrupt":
        x.corrupt_publication = True
    elif failure == "role_revoked":
        x.revoke_on_checks = True
    else:
        x.base_moved = True
    job = await x.engine.admit(x.event)
    await x.engine.run(job, AsyncMock(return_value=20000))
    assert x.engine.ledger.get(job)["execution"] != "succeeded"
    assert all(r.method != "POST" for r in x.api_calls)
    assert not any("push" in args for args in x.commands)


@pytest.mark.asyncio
async def test_unknown_pr_result_cannot_replay_model_push_or_pr(pipeline):
    x = pipeline
    x.pr_uncertain = True
    job = await x.engine.admit(x.event)
    await x.engine.run(job, AsyncMock(return_value=20000))
    assert x.engine.ledger.get(job)["execution"] == "unknown"
    restarted = Parliament(x.policy, x.directory, x.backend)
    await restarted.run(job, AsyncMock(return_value=20000))
    assert len(x.models) == 1
    assert sum("push" in args for args in x.commands) == 1
    assert sum(r.method == "POST" for r in x.api_calls) == 1


@pytest.mark.asyncio
async def test_readonly_questions_do_not_implicitly_read_an_implementation_repository(pipeline):
    x = pipeline
    x.models.clear()

    async def readonly(argv, *, cwd, env, **kwargs):
        if argv[0] == x.config["binary"]:
            assert list(Path(cwd).iterdir()) == [Path(cwd) / ".git"]
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                '{"text":"Synthetic conversation answer."}', encoding="utf-8"
            )
            x.models.append(argv)
            return b""
        return await command(argv, cwd=cwd, env=env, **kwargs)

    backend = CodexPipeline(x.directory, execute=readonly, sandbox_type=x.backend.sandbox_type)
    request = Request(replace_event(x.event, author=600), "ask", None, "Synthetic")
    assert "conversation" in await backend.execute(request, x.policy, "readonly")
    assert x.credentials == [] and x.api_calls == []


def replace_event(event, **kwargs):
    from dataclasses import replace

    return replace(event, **kwargs)


@pytest.mark.asyncio
async def test_restart_cannot_reinterpret_pending_request_under_new_policy(request):
    setup = request.getfixturevalue("setup")
    x = setup
    job = await x.engine.admit(x.event)
    x.value["repositories"]["demo"]["member_ids"] = [100]
    x.path.write_text(json.dumps(x.value), encoding="utf-8")
    restarted = Parliament(load_policy(x.path), x.directory, x.backend)
    send = AsyncMock(return_value=20000)
    with pytest.raises(ParliamentError, match="admission_policy_changed"):
        await restarted.run(job, send)
    assert x.backend.calls == []
    send.assert_not_awaited()


def test_git_execution_hooks_are_denied_before_preparation(pipeline):
    x = pipeline
    marker, script = x.path.parent / "marker", x.path.parent / "fsmonitor.py"
    script.write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n"
    )
    dangerous = (
        '"' + sys.executable.replace("\\", "/") + '" "' + str(script).replace("\\", "/") + '"'
    )
    git_call(x.git, x.source, x.env, "-C", str(x.source), "config", "core.fsmonitor", dangerous)
    # Positive control: this real Git setting can execute a harmless canary program．
    subprocess.run(
        [str(x.git), "-C", str(x.source), "status", "--porcelain"],
        env=x.env,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert marker.read_text() == "executed"
    marker.unlink()
    with pytest.raises(ParliamentError, match="execution_capable_git_configuration_denied"):
        safe_git_source(x.source)
    assert not marker.exists()


def test_native_profile_uses_official_cli_and_no_legacy_override(tmp_path):
    config = {"read_roots": [sys.base_prefix], "denied_roots": [str(tmp_path / "private")]}
    native = NativeSandbox(Path(sys.executable), config, tmp_path / "home", command)
    profile = native.profile(
        tmp_path / "candidate", tmp_path / "temp", writable=False, protected=tmp_path / "state"
    )
    argv = native.argv(profile, tmp_path, [sys.executable, "-I", "-c", "print('synthetic')"])
    assert "sandbox" in argv and "-P" in argv and "--include-managed-config" in argv
    assert 'windows.sandbox="mxc"' in argv
    assert '":root"="deny"' in profile and "network={enabled=false}" in profile
    assert "sandbox_mode" not in profile and "--sandbox" not in argv


@pytest.mark.asyncio
async def test_publisher_cannot_update_an_existing_remote_branch_even_fast_forward(pipeline):
    x = pipeline
    job = await x.engine.admit(x.event)
    branch = "refs/heads/parliament/job-" + job[:32]
    git_call(x.git, x.source, x.env, "--git-dir", str(x.bare), "update-ref", branch, x.base)
    await x.engine.run(job, AsyncMock(return_value=20000))
    assert git_call(x.git, x.source, x.env, "--git-dir", str(x.bare), "rev-parse", branch) == x.base
    assert x.engine.ledger.get(job)["execution"] != "succeeded"
    assert all(r.method != "POST" for r in x.api_calls)


def test_case_only_rename_is_rejected_before_publication():
    from ephy_discord.parliament_codex import changes

    with pytest.raises(ParliamentError, match="case_ambiguous_artifact"):
        changes({"src/Foo.py": "old"}, {"src/foo.py": "new"}, ["src/"])


@pytest.mark.parametrize("which", ["home", "private", "state"])
def test_configured_read_root_cannot_reopen_protected_data(tmp_path, which):
    home, private, state = [tmp_path / name for name in ("home", "private", "state")]
    roots = {"home": home, "private": private, "state": state}
    config = {"read_roots": [str(roots[which] / "auth.json")], "denied_roots": [str(private)]}
    native = NativeSandbox(Path(sys.executable), config, home, command)
    with pytest.raises(ParliamentError, match="sandbox_read_root_reopens_protected_path"):
        native.profile(tmp_path / "candidate", tmp_path / "temp", writable=True, protected=state)
