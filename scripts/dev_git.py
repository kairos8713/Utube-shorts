"""
dev_git.py — minimal git helper built on dulwich (system git is not installed
on this machine). Local-only: there is intentionally no push command.

Usage:
  python scripts/dev_git.py status
  python scripts/dev_git.py commit "message"
  python scripts/dev_git.py branch <name>        # create + switch
  python scripts/dev_git.py log [N]
"""
import os
import sys

from dulwich import porcelain
from dulwich.repo import Repo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTHOR = b"Claude (automated) <noreply@anthropic.com>"


def _repo() -> Repo:
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        porcelain.init(ROOT)
    return Repo(ROOT)


def _changed_paths(repo: Repo) -> list:
    st = porcelain.status(repo, untracked_files="all")
    paths = set()
    for group in st.staged.values():
        paths.update(group)
    paths.update(st.unstaged)
    paths.update(st.untracked)
    out = []
    for p in paths:
        p = p.decode() if isinstance(p, bytes) else p
        out.append(p.replace("\\", "/"))
    return sorted(out)


def status():
    repo = _repo()
    for p in _changed_paths(repo):
        print(p)


def commit(message: str):
    repo = _repo()
    paths = _changed_paths(repo)
    if not paths:
        print("nothing to commit")
        return
    existing = [p for p in paths if os.path.exists(os.path.join(ROOT, p))]
    deleted = [p for p in paths if p not in existing]
    if existing:
        porcelain.add(repo, [os.path.join(ROOT, p) for p in existing])
    if deleted:
        porcelain.remove(repo, [os.path.join(ROOT, p) for p in deleted], cached=True)
    sha = porcelain.commit(repo, message.encode("utf-8"), author=AUTHOR, committer=AUTHOR)
    print(f"[{sha.decode()[:10]}] {message.splitlines()[0]}  ({len(paths)} paths)")


def branch(name: str):
    repo = _repo()
    porcelain.branch_create(repo, name)
    porcelain.checkout(repo, name)
    print(f"switched to {name}")


def log(n: int = 20):
    repo = _repo()
    for i, entry in enumerate(repo.get_walker()):
        if i >= n:
            break
        c = entry.commit
        print(c.id.decode()[:10], c.message.decode("utf-8", "replace").splitlines()[0])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "commit":
        commit(sys.argv[2])
    elif cmd == "branch":
        branch(sys.argv[2])
    elif cmd == "log":
        log(int(sys.argv[2]) if len(sys.argv) > 2 else 20)
    else:
        print(__doc__)
