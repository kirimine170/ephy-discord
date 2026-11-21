"""Trusted publisher：fixed base，new branch，existing Git credentials，Draft PR only．"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import httpx

from ephy_discord.parliament import ParliamentError, unlinked


class GitHubPublisher:
    def __init__(self, git: Path, execute, *, transport=None, credentials=None):
        self.git, self.execute, self.transport = git, execute, transport
        self.credentials = credentials

    async def credential(self, repository: str, cwd: Path) -> str:
        if self.credentials is not None:
            return await self.credentials(repository)
        # Only this trusted broker can read the user's existing Git credential helper．
        env = {
            key: os.environ[key]
            for key in (
                "SystemRoot",
                "WINDIR",
                "PATH",
                "USERPROFILE",
                "HOME",
                "APPDATA",
                "LOCALAPPDATA",
                "TEMP",
                "TMP",
                "PATHEXT",
                "OS",
            )
            if key in os.environ
        }
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        value = await self.execute(
            [str(self.git), "credential", "fill"],
            cwd=cwd,
            env=env,
            timeout=20,
            stdin=("protocol=https\nhost=github.com\npath=" + repository + ".git\n\n").encode(),
        )
        fields = dict(line.split("=", 1) for line in value.decode().splitlines() if "=" in line)
        token = fields.get("password", "")
        if not token or any(c in token for c in "\r\n"):
            raise ParliamentError("existing_github_credential_unavailable")
        return token

    async def publish(
        self,
        *,
        policy,
        request,
        job,
        source,
        frozen,
        base,
        files,
        full_manifest,
        root,
        env,
        reauthorize,
    ) -> str:
        from ephy_discord.parliament_codex import fingerprint

        repo = policy.repositories[request.repository]
        repository, base_branch = repo["github_repository"], repo["base_branch"]
        branch = "parliament/job-" + job[:32]
        remote = "https://github.com/" + repository + ".git"
        prefix = [
            str(self.git),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            f"core.hooksPath={root / 'empty-hooks'}",
            "-c",
            "commit.gpgsign=false",
        ]
        actual = (
            (
                await self.execute(
                    [*prefix, "-C", str(source), "config", "--get", "remote.origin.url"],
                    cwd=root,
                    env=env,
                )
            )
            .decode()
            .strip()
        )
        if actual != remote:
            raise ParliamentError("publisher_repository_mismatch")
        token = await self.credential(repository, root)
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=20,
        ) as client:
            response = await client.get(f"/repos/{repository}/git/ref/heads/{base_branch}")
            if response.status_code != 200:
                raise ParliamentError("publisher_remote_base_unverified")
            if response.json().get("object", {}).get("sha") != base:
                raise ParliamentError("publisher_remote_base_changed")
            await reauthorize()
            target = root / "publish"
            await self.execute(
                [*prefix, "-C", str(source), "worktree", "add", "-b", branch, str(target), base],
                cwd=root,
                env=env,
            )
            for relative, digest in files.items():
                file = unlinked(target / relative)
                if not file.is_relative_to(target):
                    raise ParliamentError("publisher_path_escape")
                if digest is None:
                    file.unlink()
                else:
                    body = unlinked(frozen / relative).read_bytes()
                    if hashlib.sha256(body).hexdigest() != digest:
                        raise ParliamentError("validated_artifact_changed")
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(body)
            if fingerprint(target) != full_manifest:
                raise ParliamentError("published_worktree_differs_from_validation")
            await self.execute([*prefix, "-C", str(target), "add", "--", *files], cwd=root, env=env)
            commit_env = {
                **env,
                "GIT_AUTHOR_NAME": "Parliament development",
                "GIT_COMMITTER_NAME": "Parliament development",
                "GIT_AUTHOR_EMAIL": "parliament@users.noreply.github.com",
                "GIT_COMMITTER_EMAIL": "parliament@users.noreply.github.com",
            }
            await self.execute(
                [*prefix, "-C", str(target), "commit", "-m", "Parliament request " + job[:16]],
                cwd=root,
                env=commit_env,
            )
            head = (
                (
                    await self.execute(
                        [*prefix, "-C", str(target), "rev-parse", "HEAD"], cwd=root, env=env
                    )
                )
                .decode()
                .strip()
            )
            audit = root / "published-audit"
            await self.execute(
                [*prefix, "-C", str(target), "worktree", "add", "--detach", str(audit), head],
                cwd=root,
                env=env,
            )
            if fingerprint(audit) != full_manifest:
                raise ParliamentError("published_commit_differs_from_validation")
            stage = {
                "repository": repository,
                "base": base,
                "branch": branch,
                "head": head,
                "validated_files": files,
                "stage": "pushing",
                "published_tree_verified": True,
            }
            receipt = root / "publication.json"
            receipt.write_text(json.dumps(stage), encoding="utf-8")
            await reauthorize()
            basic = base64.b64encode(("x-access-token:" + token).encode()).decode()
            push_env = {
                **env,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + basic,
            }
            try:
                await self.execute(
                    [
                        *prefix,
                        "-C",
                        str(target),
                        "push",
                        "--porcelain",
                        "--force-with-lease=refs/heads/" + branch + ":",
                        remote,
                        head + ":refs/heads/" + branch,
                    ],
                    cwd=root,
                    env=push_env,
                )
            except Exception:
                raise RuntimeError("publication_unconfirmed_no_retry") from None
            stage["stage"] = "opening_draft_pr"
            receipt.write_text(json.dumps(stage), encoding="utf-8")
            await reauthorize()
            response = await client.post(
                f"/repos/{repository}/pulls",
                json={
                    "title": "Parliament development request " + job[:16],
                    "head": branch,
                    "base": base_branch,
                    "draft": True,
                    "maintainer_can_modify": False,
                    "body": "Validated parliament request.\n\nBase: `"
                    + base
                    + "`\nHead: `"
                    + head
                    + "`\n\nFixed checks passed on the frozen artifact. No merge or deployment "
                    "authorization is granted. Participant messages are not copied into this PR.",
                },
            )
            if response.status_code != 201:
                raise RuntimeError("draft_pr_result_unconfirmed_no_retry")
            data = response.json()
            number = data.get("number")
            url = f"https://github.com/{repository}/pull/{number}"
            if (
                type(number) is not int
                or number < 1
                or data.get("html_url") != url
                or data.get("draft") is not True
                or data.get("head", {}).get("sha") != head
                or data.get("head", {}).get("ref") != branch
                or data.get("head", {}).get("repo", {}).get("full_name") != repository
                or data.get("base", {}).get("ref") != base_branch
                or data.get("base", {}).get("repo", {}).get("full_name") != repository
            ):
                raise RuntimeError("draft_pr_identity_unconfirmed_no_retry")
            stage.update(stage="draft_pr_created", url=url)
            receipt.write_text(json.dumps(stage), encoding="utf-8")
            return url
