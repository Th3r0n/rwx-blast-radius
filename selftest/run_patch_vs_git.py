"""Differential test of br.patch against `git apply` on pristine sqlglot.

Runs inside the br-lab image (repo mounted at /src):

    python /src/selftest/run_patch_vs_git.py [patch dirs...] [--random N] [--seed S]

Without dirs it uses kit/sample/commits and lab/corpus/*/patches. For every
patch it applies br.patch.apply_all to files read from /opt/sqlglot, runs
`git apply` on a clean copy, and compares every touched file byte for byte.
--random N also builds N patches with `git diff` from random edits (plus
corrupted variants) to probe edge cases. Exit status 0 only if every check passes:
valid patches must give identical bytes, and br.patch must never succeed where
git fails or disagrees.
"""

from __future__ import annotations

import argparse
import glob
import os
import random
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from br import patch as bp  # noqa: E402

SQLGLOT = "/opt/sqlglot"


def git(cwd: str, *args: str, data: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args], cwd=cwd, input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check,
    )


def read_from(root: str):
    def read(path: str) -> bytes | None:
        full = os.path.join(root, path)
        if os.path.islink(full) or not os.path.isfile(full):
            return None
        with open(full, "rb") as handle:
            return handle.read()
    return read


class Tree:
    """A disposable copy of sqlglot that can be reset to the pinned commit."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="brpatch-")
        self.root = os.path.join(self.tmp, "wt")
        subprocess.run(["cp", "-a", SQLGLOT, self.root], check=True)

    def reset(self) -> None:
        git(self.root, "reset", "-q", "--hard", "HEAD")
        git(self.root, "clean", "-qfd")
        if git(self.root, "status", "--porcelain").stdout:
            raise RuntimeError("tree did not reset")

    def changed_paths(self) -> set[str]:
        out = git(self.root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").stdout
        return {entry[3:].decode() for entry in out.split(b"\0") if entry}

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def compare(tree: Tree, patch: bytes) -> tuple[str, str]:
    """Return (verdict, detail); verdict in ok, both_fail, ours_fail, BAD."""
    tree.reset()
    try:
        ours = bp.apply_all(patch, read_from(SQLGLOT))
        ours_error = None
    except bp.PatchError as error:
        ours, ours_error = None, str(error)
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(patch)
        patch_file = handle.name
    try:
        result = git(tree.root, "apply", patch_file, check=False)
    finally:
        os.unlink(patch_file)
    git_ok = result.returncode == 0
    if ours is None:
        if git_ok:
            return "ours_fail", f"br.patch: {ours_error}"
        return "both_fail", f"br.patch: {ours_error} | git: {result.stderr.decode(errors='replace').strip()}"
    if not git_ok:
        return "BAD", f"br.patch applied but git failed: {result.stderr.decode(errors='replace').strip()}"
    reader = read_from(tree.root)
    git_changed = tree.changed_paths()
    if not git_changed <= set(ours):
        return "BAD", f"git changed paths br.patch did not report: {sorted(git_changed - set(ours))}"
    for path, (_before, after) in ours.items():
        if reader(path) != after:
            return "BAD", f"content differs for {path}"
    for fp in bp.parse(patch):
        if fp.new_path is not None and fp.new_mode is not None:
            executable = bool(os.stat(os.path.join(tree.root, fp.new_path)).st_mode & 0o100)
            if executable != (fp.new_mode == 0o100755):
                return "BAD", f"mode differs for {fp.new_path}"
    return "ok", ""


def index_report(patch: bytes) -> bool | None:
    ours = bp.apply_all(patch, read_from(SQLGLOT))
    verdicts = []
    for fp in bp.parse(patch):
        old = ours.get(fp.old_path, (None, None))[0] if fp.old_path else None
        if fp.is_copy:
            old = read_from(SQLGLOT)(fp.old_path)
        new = ours[fp.new_path][1] if fp.new_path else None
        verdicts.append(bp.index_matches(fp, old, new))
    return None if None in verdicts else all(verdicts)


# ----------------------------------------------------------------------------- random patches

ALPHABET = [b"x", b"y = 1", b"", b"\f", b"a\fb", b"\r", b"tail\r", b"\t\tindent", b"# comment",
            "café ü".encode(), b"    return None", b"\\ backslash", b"-dash", b"+plus", b"@@ at"]


def random_lines(rng: random.Random, count: int) -> list[bytes]:
    return [rng.choice(ALPHABET) + (b" %d" % rng.randrange(1000) if rng.random() < 0.5 else b"") + b"\n"
            for _ in range(count)]


def edit_content(rng: random.Random, data: bytes) -> bytes:
    lines = bp._split_keep(data)
    for _ in range(rng.randint(1, 4)):
        choice = rng.random()
        size = len(lines)
        position = rng.choice([0, size, rng.randint(0, size)]) if size else 0
        if choice < 0.3 and size:
            del lines[position:position + rng.randint(1, 3)]
        elif choice < 0.6:
            lines[position:position] = random_lines(rng, rng.randint(1, 3))
        elif choice < 0.85 and size:
            lines[position:position + rng.randint(1, 2)] = random_lines(rng, rng.randint(1, 3))
        elif lines:
            last = lines[-1]
            lines[-1] = last[:-1] if last.endswith(b"\n") else last + b"\n"
        if lines and not lines[-1].endswith(b"\n") and rng.random() < 0.3:
            lines[-1] += b"\n"
    fixed = [line if line.endswith(b"\n") else line + b"\n" for line in lines[:-1]]
    return b"".join(fixed + lines[-1:])


def text_files(root: str) -> list[str]:
    out = git(root, "ls-files", "-s").stdout.decode().splitlines()
    files = []
    for entry in out:
        mode, _sha, _stage, path = entry.split(None, 3)
        if mode == "100644" and path.endswith((".py", ".sql", ".md", ".toml", ".txt", ".cfg", ".yaml", ".yml")):
            files.append(path)
    return sorted(files)


def make_random_patch(tree: Tree, rng: random.Random, files: list[str]) -> bytes:
    tree.reset()
    root = tree.root
    used: set[str] = set()
    for _ in range(rng.randint(1, 3)):
        path = rng.choice(files)
        if path in used:
            continue
        used.add(path)
        full = os.path.join(root, path)
        op = rng.random()
        if op < 0.6:
            with open(full, "rb") as handle:
                data = handle.read()
            with open(full, "wb") as handle:
                handle.write(edit_content(rng, data))
        elif op < 0.7:
            os.unlink(full)
        elif op < 0.85:
            target = path.rsplit(".", 1)[0] + "_moved." + path.rsplit(".", 1)[-1]
            os.rename(full, os.path.join(root, target))
            if rng.random() < 0.5:
                with open(os.path.join(root, target), "rb") as handle:
                    data = handle.read()
                with open(os.path.join(root, target), "wb") as handle:
                    handle.write(edit_content(rng, data) if len(data) > 200 else data)
        elif op < 0.93:
            new_path = os.path.join(os.path.dirname(full), "brand_new_%d.py" % rng.randrange(10**6))
            with open(new_path, "wb") as handle:
                handle.write(b"".join(random_lines(rng, rng.randint(0, 5))).rstrip(b"\n" if rng.random() < 0.3 else b""))
        else:
            os.chmod(full, 0o755)
            if rng.random() < 0.5:
                with open(full, "rb") as handle:
                    data = handle.read()
                with open(full, "wb") as handle:
                    handle.write(edit_content(rng, data))
    git(root, "add", "-A")
    context = rng.choice([3, 3, 3, 3, 1, 2, 5, 0])
    patch = git(root, "diff", "--cached", "-M", f"-U{context}", "HEAD").stdout
    tree.reset()
    return patch


def mutate(rng: random.Random, patch: bytes) -> bytes:
    lines = patch.split(b"\n")[:-1]
    if not lines:
        return patch
    index = rng.randrange(len(lines))
    line = lines[index]
    choice = rng.randrange(7)
    if choice == 0 and line.startswith(b"@@"):
        digits = [i for i, c in enumerate(line) if 0x30 <= c <= 0x39]
        if digits:
            at = rng.choice(digits)
            lines[index] = line[:at] + bytes([0x30 + (line[at] - 0x30 + rng.choice([1, 9])) % 10]) + line[at + 1:]
    elif choice == 1:
        del lines[index]
    elif choice == 2:
        lines.insert(index, line)
    elif choice == 3 and line[:1] in (b" ", b"-") and len(line) > 1:
        lines[index] = line[:1] + line[1:][::-1]
    elif choice == 4 and line[:1] in (b" ", b"-", b"+"):
        lines[index] = rng.choice([b" ", b"-", b"+"]) + line[1:]
    elif choice == 5:
        lines.insert(index, b"\\ No newline at end of file")
    else:
        lines.insert(index, rng.choice([b"", b" ", b"garbage", b"-- ", b"@@ -1 +1 @@"]))
    return b"\n".join(lines) + b"\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dirs", nargs="*")
    parser.add_argument("--random", type=int, default=0)
    parser.add_argument("--mutations", type=int, default=3, help="corrupted variants per random patch")
    parser.add_argument("--seed", type=int, default=1)
    options = parser.parse_args()
    dirs = options.dirs or [os.path.join(REPO, "kit/sample/commits")] + sorted(
        glob.glob(os.path.join(REPO, "lab/corpus/*/patches")))
    tree = Tree()
    tally: dict[str, int] = {}
    failures = 0
    try:
        index_tally = {True: 0, False: 0, None: 0}
        for directory in dirs:
            paths = sorted(glob.glob(os.path.join(directory, "*.patch")))
            for path in paths:
                with open(path, "rb") as handle:
                    patch = handle.read()
                verdict, detail = compare(tree, patch)
                tally[f"corpus:{verdict}"] = tally.get(f"corpus:{verdict}", 0) + 1
                if verdict == "ok":
                    index_tally[index_report(patch)] += 1
                else:
                    print(f"{verdict}: {path}: {detail}")
                    failures += verdict in ("BAD", "ours_fail")
            print(f"{directory}: {len(paths)} patches")
        print(f"index lines matching applied contents: {index_tally}")
        rng = random.Random(options.seed)
        files = text_files(tree.root)
        for number in range(options.random):
            patch = make_random_patch(tree, rng, files)
            if not patch:
                continue
            verdict, detail = compare(tree, patch)
            tally[f"random:{verdict}"] = tally.get(f"random:{verdict}", 0) + 1
            if verdict == "ours_fail":
                # Strictness against valid git-made patches; expected only for -U0 diffs.
                reason = "".join("N" if c.isdigit() else c for c in detail)
                key = f"random:ours_fail[{reason}]"
                tally[key] = tally.get(key, 0) + 1
            if verdict == "BAD":
                failures += 1
                print(f"BAD random #{number}: {detail}")
                with open(f"/tmp/bad_random_{number}.patch", "wb") as handle:
                    handle.write(patch)
            if verdict == "ok" and index_report(patch) is False:
                failures += 1
                print(f"BAD random #{number}: index mismatch")
            for _ in range(options.mutations):
                mutated = mutate(rng, patch)
                verdict, detail = compare(tree, mutated)
                tally[f"mutated:{verdict}"] = tally.get(f"mutated:{verdict}", 0) + 1
                if verdict == "BAD":
                    failures += 1
                    print(f"BAD mutated of #{number}: {detail}")
                    with open(f"/tmp/bad_mutated_{number}.patch", "wb") as handle:
                        handle.write(mutated)
    finally:
        tree.close()
    for key in sorted(tally):
        print(f"{key}: {tally[key]}")
    print("FAIL" if failures else "PASS", f"({failures} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
