"""Git 저장소에서 에이전트 코드를 가져온다.

* 원격 URL  → 캐시 디렉터리에 bare clone 후 fetch (체크아웃 없이 ``git show`` 로 파일 읽기)
* 로컬 git  → 저장소를 직접 읽음. ref 에 ``WORKTREE`` 를 주면 커밋되지 않은 작업 트리를 읽음
* 로컬 디렉터리(비 git) → 파일 시스템에서 읽음 (commit = "WORKTREE")

인증: git credential helper 를 그대로 사용하거나, 환경변수 ``RCA_GIT_AUTH_HEADER``
(예: "Authorization: Basic <base64(user:token)>") 를 http.extraHeader 로 전달한다. 값은 로그에 남기지 않는다.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional

WORKTREE = "WORKTREE"


class GitError(RuntimeError):
    pass


def _redact(args: list[str]) -> list[str]:
    return ["http.extraHeader=***" if a.startswith("http.extraHeader=") else a for a in args]


class GitRepo:
    def __init__(self, source: str, cache_dir: str | Path = ".rca_cache/repos"):
        self.source = source
        p = Path(os.path.expanduser(source))
        if p.exists():
            self.local_path: Optional[Path] = p.resolve()
            self.mode = "local" if self._is_git_dir(p) else "plain"
            self.git_dir: Optional[Path] = self.local_path if self.mode == "local" else None
        else:
            self.local_path = None
            self.mode = "remote"
            digest = hashlib.sha1(source.encode()).hexdigest()[:16]
            self.git_dir = Path(cache_dir).expanduser().resolve() / digest
        self._prepared = False

    # ------------------------------------------------------------------ 내부
    @staticmethod
    def _is_git_dir(p: Path) -> bool:
        return (p / ".git").exists() or ((p / "HEAD").exists() and (p / "objects").exists())

    def _base_args(self) -> list[str]:
        args = ["git", "-c", "core.quotepath=off", "-c", "color.ui=false"]
        hdr = os.environ.get("RCA_GIT_AUTH_HEADER")
        if hdr and self.mode == "remote":
            args += ["-c", f"http.extraHeader={hdr}"]
        if self.git_dir is not None:
            if self.mode == "local":
                args += ["-C", str(self.git_dir)]
            else:
                args += ["--git-dir", str(self.git_dir)]
        return args

    def _run(self, *args: str, binary: bool = False, check: bool = True, use_repo: bool = True):
        cmd = (self._base_args() if use_repo else ["git", "-c", "core.quotepath=off"]) + list(args)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8")
        proc = subprocess.run(cmd, capture_output=True, env=env)
        if check and proc.returncode != 0:
            raise GitError(f"git 실패({proc.returncode}): {' '.join(_redact(cmd[1:]))}\n{proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    # ------------------------------------------------------------------ 공개 API
    def prepare(self, fetch: bool = True) -> None:
        if self._prepared:
            return
        if self.mode == "remote":
            assert self.git_dir is not None
            if not (self.git_dir / "HEAD").exists():
                self.git_dir.parent.mkdir(parents=True, exist_ok=True)
                cmd = ["clone", "--bare", "--quiet", self.source, str(self.git_dir)]
                base = ["git", "-c", "core.quotepath=off"]
                hdr = os.environ.get("RCA_GIT_AUTH_HEADER")
                if hdr:
                    base += ["-c", f"http.extraHeader={hdr}"]
                env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
                proc = subprocess.run(base + cmd, capture_output=True, env=env)
                if proc.returncode != 0:
                    raise GitError(f"git clone 실패: {self.source}\n{proc.stderr.decode('utf-8', 'replace').strip()}")
            elif fetch:
                self._run("fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
        self._prepared = True

    def resolve(self, ref: Optional[str]) -> str:
        if self.mode == "plain" or ref == WORKTREE:
            return WORKTREE
        self.prepare()
        ref = ref or "HEAD"
        candidates = [ref]
        if self.mode == "local" and "/" not in ref:
            candidates.append(f"origin/{ref}")
        for r in candidates:
            out = self._run("rev-parse", "--verify", "--quiet", f"{r}^{{commit}}", check=False).strip()
            if out:
                return out
        raise GitError(f"ref 를 찾을 수 없음: {ref} ({self.source})")

    def list_files(self, commit: str, paths: Optional[list[str]] = None) -> list[str]:
        paths = [p.strip("/") for p in (paths or []) if p and p.strip("/")]
        if commit == WORKTREE:
            root = self.local_path
            assert root is not None, "WORKTREE 는 로컬 저장소에서만 사용 가능"
            roots = [root / p for p in paths] if paths else [root]
            out = []
            for r in roots:
                if r.is_file():
                    out.append(str(r.relative_to(root)).replace(os.sep, "/"))
                    continue
                for f in r.rglob("*"):
                    if f.is_file() and ".git" not in f.relative_to(root).parts:
                        out.append(str(f.relative_to(root)).replace(os.sep, "/"))
            return sorted(set(out))
        raw = self._run("ls-tree", "-r", "-z", "--name-only", commit, "--", *paths)
        return sorted(x for x in raw.split("\0") if x)

    def read_bytes(self, commit: str, file: str) -> bytes:
        if commit == WORKTREE:
            assert self.local_path is not None
            return (self.local_path / file).read_bytes()
        return self._run("cat-file", "blob", f"{commit}:{file}", binary=True)

    def read_text(self, commit: str, file: str) -> str:
        return self.read_bytes(commit, file).decode("utf-8", "replace")

    def diff(self, base: str, head: str, paths: Optional[list[str]] = None) -> str:
        paths = [p.strip("/") for p in (paths or []) if p and p.strip("/")]
        if base == WORKTREE:
            raise GitError("base 에는 WORKTREE 를 쓸 수 없습니다")
        if head == WORKTREE:
            return self._run("diff", "--no-color", "--no-ext-diff", "-U3", "--find-renames", base, "--", *paths)
        return self._run("diff", "--no-color", "--no-ext-diff", "-U3", "--find-renames", base, head, "--", *paths)

    def log(self, commit: str, paths: Optional[list[str]] = None, n: int = 20) -> list[dict]:
        if commit == WORKTREE:
            return []
        paths = [p.strip("/") for p in (paths or []) if p and p.strip("/")]
        raw = self._run("log", f"-n{n}", "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=iso", commit, "--", *paths)
        out = []
        for line in raw.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                out.append({"commit": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
        return out
