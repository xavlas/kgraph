"""Repository loading: local folder or git URL, with safe file walking."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

IGNORE_DIRS = {
    ".git", "node_modules", "target", "build", "dist", "out", ".gradle", ".idea",
    ".vscode", "vendor", "__pycache__", ".angular", ".next", "coverage", ".mvn",
    "bower_components", ".cache", ".svn", "workspaces",
}
CODE_EXTS = {".java", ".kt", ".ts", ".tsx", ".js", ".jsx", ".html", ".sql",
             ".properties", ".yml", ".yaml"}
MAX_FILE_BYTES = 1_500_000
_URL_RE = re.compile(r"^(https?://|ssh://|git@[\w.-]+:)")


class RepoError(Exception):
    pass


class Repo:
    def __init__(self, root: Path, name: str, origin: str, commit: str | None = None):
        self.root = Path(root)
        self.name = name
        self.origin = origin
        self.commit = commit
        self._files: list[str] | None = None
        self._cache: dict[str, str] = {}

    def files(self, *exts: str) -> list[str]:
        if self._files is None:
            self._files = self._walk()
        return [f for f in self._files if not exts or f.endswith(exts)]

    def _walk(self) -> list[str]:
        out = []
        for dp, dns, fns in os.walk(self.root, followlinks=False):
            dns[:] = [d for d in dns if d not in IGNORE_DIRS]
            for fn in fns:
                p = Path(dp) / fn
                if p.suffix not in CODE_EXTS or p.is_symlink():
                    continue
                try:
                    if p.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                out.append(p.relative_to(self.root).as_posix())
        return sorted(out)

    def read(self, rel: str) -> str:
        if rel not in self._cache:
            try:
                self._cache[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._cache[rel] = ""
        return self._cache[rel]

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()


def is_test_path(rel: str) -> bool:
    low = rel.lower()
    return bool(re.search(r"(^|/)(src/test|test|tests|__tests__|e2e|cypress)/", low)
                or re.search(r"\.(spec|test|stories)\.[jt]sx?$", low)
                or low.endswith(".d.ts"))


def _git(args: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user", *args],
                       cwd=cwd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RepoError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def load_repository(source: str, workdir: str | Path = "workspaces", branch: str | None = None,
                    log=lambda m: None) -> Repo:
    source = source.strip()
    if _URL_RE.match(source):
        if source.startswith("-"):
            raise RepoError("invalid repository URL")
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w.-]+", "_", source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
        dest = workdir / f"{slug}-{hashlib.sha1((source + (branch or '')).encode()).hexdigest()[:8]}"
        if (dest / ".git").is_dir():
            log(f"updating existing clone {dest.name}")
            _git(["pull", "--ff-only"], cwd=dest)
        else:
            log(f"cloning {source}")
            args = ["clone", "--depth", "1"]
            if branch:
                args += ["--branch", branch]
            _git(args + ["--", source, str(dest)])
        root, name = dest, slug
    else:
        root = Path(os.path.expanduser(source)).resolve()
        if not root.is_dir():
            raise RepoError(f"not a directory or supported git URL: {source}")
        name = root.name
        if branch and (root / ".git").exists():
            log("branch ignored for local paths: scanning the working tree as is")
    commit = None
    if (root / ".git").exists():
        try:
            commit = _git(["rev-parse", "--short", "HEAD"], cwd=root)
        except (RepoError, OSError, subprocess.SubprocessError):
            pass
    return Repo(root, name, source, commit)
