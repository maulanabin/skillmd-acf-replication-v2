from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

DEFAULT_INPUT      = "output/discovered_repos.json"
DEFAULT_CLONE_DIR  = "cloned_repos"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_WORKERS    = 8
TIMEOUT_FULL       = 600
TIMEOUT_SHALLOW    = 120
MAX_RETRY          = 2
CHECKPOINT_INTERVAL = 20
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VALID_PO_DIRS = (
    ".claude/skills",
    ".codex/skills",
    ".cursor/skills",
    ".github/skills",
    ".windsurf/skills",
)

_PERMANENT_PATTERNS = (
    "repository not found", "does not exist", "remote: not found",
    "invalid username or password", "authentication failed", "access denied",
    "permission denied", "remote: error: repository access denied",
    "this repository has been disabled",
    "the requested url returned error: 404",
    "the requested url returned error: 403",
    "repository access denied", "not found",
)

_print_lock = threading.Lock()


def tprint(*args, **kwargs) -> None:
    with _print_lock:
        print(*args, **kwargs)


def save_json_atomic(data: Any, path: str) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def save_json(data: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  {os.path.basename(path)} ({os.path.getsize(path):,} bytes)")


def clone_url_clean(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def clone_url(full_name: str) -> str:
    if GITHUB_TOKEN:
        return f"https://{GITHUB_TOKEN}@github.com/{full_name}.git"
    return clone_url_clean(full_name)


def repo_key(full_name: str) -> str:
    return full_name.replace("/", "__")


def clone_path_for(full_name: str, clone_dir: str) -> str:
    return os.path.join(clone_dir, repo_key(full_name))


def get_origin_url(clone_path: str) -> str:
    res = subprocess.run(
        ["git", "-C", clone_path, "remote", "get-url", "origin"],
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def remote_matches(origin_url: str, full_name: str) -> bool:
    if not origin_url:
        return False
    url = origin_url.lower().rstrip("/")
    url = re.sub(r"https://[^@]+@", "https://", url)
    url = url.removesuffix(".git")
    expected = full_name.lower()
    return url.endswith(f"github.com/{expected}") or url.endswith(f"github.com:{expected}")


def fix_remote_url(clone_path: str, full_name: str) -> None:
    subprocess.run(
        ["git", "-C", clone_path, "remote", "set-url", "origin", clone_url_clean(full_name)],
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def is_valid_clone(clone_path: str, full_name: str = "") -> bool:
    if not os.path.isdir(os.path.join(clone_path, ".git")):
        return False
    res = subprocess.run(
        ["git", "-C", clone_path, "rev-parse", "HEAD"],
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if res.returncode != 0:
        return False
    if full_name:
        return remote_matches(get_origin_url(clone_path), full_name)
    return True


def is_shallow(clone_path: str) -> bool:
    res = subprocess.run(
        ["git", "-C", clone_path, "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return res.stdout.strip() == "true"


def is_permanent_error(stderr: str) -> bool:
    low = stderr.lower()
    return any(p in low for p in _PERMANENT_PATTERNS)


def git_ls_tree(clone_path: str) -> set[str]:
    res = subprocess.run(
        ["git", "-C", clone_path, "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=30,
    )
    if res.returncode != 0:
        return set()
    paths = set()
    for line in res.stdout.splitlines():
        line = line.strip().replace("\\", "/")
        paths.add(line)
        paths.add(line.lower())
    return paths


def clone_state(clone_path: str, full_name: str) -> str:
    if not os.path.isdir(os.path.join(clone_path, ".git")):
        return "missing"
    res = subprocess.run(
        ["git", "-C", clone_path, "rev-parse", "HEAD"],
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if res.returncode != 0:
        return "broken"
    if remote_matches(get_origin_url(clone_path), full_name):
        return "valid_correct"
    return "valid_wrong_remote"


def _run_fresh_clone(full_name: str, clone_path: str, depth: int | None, attempt: int) -> dict:
    url     = clone_url(full_name)
    timeout = TIMEOUT_SHALLOW if depth is not None else TIMEOUT_FULL
    cmd     = ["git", "clone", "--quiet"]
    if depth is not None:
        cmd += ["--depth", str(depth)]
    cmd += [url, clone_path]

    start = time.time()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        elapsed = round(time.time() - start, 1)
        if res.returncode == 0:
            fix_remote_url(clone_path, full_name)
            return {"status": "success", "elapsed_sec": elapsed,
                    "clone_type": "shallow" if depth else "full",
                    "attempt": attempt, "error": None, "is_permanent": False}
        stderr = res.stderr.strip()[:500]
        return {"status": "failed", "elapsed_sec": elapsed,
                "clone_type": "shallow" if depth else "full",
                "attempt": attempt, "error": stderr,
                "is_permanent": is_permanent_error(stderr)}
    except subprocess.TimeoutExpired:
        shutil.rmtree(clone_path, ignore_errors=True)
        return {"status": "timeout", "elapsed_sec": round(time.time() - start, 1),
                "clone_type": "shallow" if depth else "full",
                "attempt": attempt, "error": f"timeout after {timeout}s",
                "is_permanent": False}
    except FileNotFoundError:
        return {"status": "error", "elapsed_sec": 0,
                "clone_type": "shallow" if depth else "full",
                "attempt": attempt, "error": "git not found", "is_permanent": True}


def clone_fresh(repo: dict, clone_path: str, full_history: bool) -> dict:
    depth = None if full_history else 1
    for attempt in range(1, MAX_RETRY + 2):
        if attempt > 1:
            shutil.rmtree(clone_path, ignore_errors=True)
            time.sleep(3 * attempt)
        res = _run_fresh_clone(repo["full_name"], clone_path, depth, attempt)
        if res["status"] == "success" or res.get("is_permanent"):
            return res
        if res["status"] == "timeout" and depth is None and attempt == MAX_RETRY + 1:
            tprint(f"  fallback to shallow: {repo['full_name']}")
            res = _run_fresh_clone(repo["full_name"], clone_path, 1, attempt)
            if res["status"] == "success":
                res["clone_type"] = "shallow_fallback"
            return res
    return res


def update_existing(repo: dict, clone_path: str, full_history: bool) -> dict:
    start = time.time()
    default_branch = repo.get("default_branch", "main")
    res = subprocess.run(
        ["git", "-C", clone_path, "fetch", "--all", "--prune", "--quiet"],
        capture_output=True, text=True, timeout=TIMEOUT_FULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    fetch_ok = res.returncode == 0

    if full_history and is_shallow(clone_path):
        tprint(f"  unshallowing: {repo['full_name']}")
        unshallow = subprocess.run(
            ["git", "-C", clone_path, "fetch", "--unshallow", "--quiet"],
            capture_output=True, text=True, timeout=TIMEOUT_FULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if unshallow.returncode != 0:
            tprint(f"  unshallow failed {repo['full_name']}: {unshallow.stderr.strip()[:80]}")

    for git_cmd in [
        ["checkout", "-q", default_branch],
        ["reset", "--hard", f"origin/{default_branch}", "--quiet"],
    ]:
        subprocess.run(["git", "-C", clone_path] + git_cmd,
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})

    fix_remote_url(clone_path, repo["full_name"])
    elapsed    = round(time.time() - start, 1)
    clone_type = "shallow" if is_shallow(clone_path) else "full"
    status     = "updated" if fetch_ok else "update_partial"
    return {"status": status, "elapsed_sec": elapsed, "clone_type": clone_type,
            "attempt": 0, "error": None if fetch_ok else res.stderr.strip()[:200],
            "is_permanent": False}


def ensure_clone(repo: dict, clone_path: str, full_history: bool,
                 update_existing_flag: bool) -> dict:
    full_name = repo["full_name"]
    state     = clone_state(clone_path, full_name)

    if state == "valid_correct":
        if update_existing_flag:
            return update_existing(repo, clone_path, full_history)
        clone_type = "shallow" if is_shallow(clone_path) else "full"
        return {"status": "cached", "elapsed_sec": 0, "clone_type": clone_type,
                "attempt": 0, "error": None, "is_permanent": False}

    if state == "valid_wrong_remote":
        origin = get_origin_url(clone_path)
        tprint(f"  wrong remote {full_name} -> {origin!r}, re-cloning")
        shutil.rmtree(clone_path, ignore_errors=True)
        res = clone_fresh(repo, clone_path, full_history)
        if res["status"] == "success":
            res["status"] = "wrong_remote_recloned"
        return res

    if state == "broken":
        tprint(f"  broken .git for {full_name}, re-cloning")
        shutil.rmtree(clone_path, ignore_errors=True)
        res = clone_fresh(repo, clone_path, full_history)
        if res["status"] == "success":
            res["status"] = "broken_recloned"
        return res

    return clone_fresh(repo, clone_path, full_history)


def _sha256_file(fpath: str) -> str:
    h = hashlib.sha256()
    with open(fpath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_yaml_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    block = content[3:end]
    out = {}
    for line in block.splitlines():
        m = re.match(r'^(name|description)\s*:\s*(.+)', line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"\'')
    return out


def _skill_dir(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    for vd in VALID_PO_DIRS:
        vparts = vd.split("/")
        for i in range(len(parts) - len(vparts)):
            if parts[i:i+len(vparts)] == vparts:
                return vd
    p   = path.replace("\\", "/")
    idx = p.upper().rfind("/SKILL.MD")
    if idx > 0:
        parent = p[:idx]
        return parent.rsplit("/", 1)[0] if "/" in parent else parent
    return os.path.dirname(path)


def build_inventory_row(repo: dict, skill_file_meta: dict,
                         clone_path: str, clone_result: dict) -> dict:
    rel_path   = skill_file_meta["path"].replace("\\", "/")
    local_path = os.path.join(clone_path, rel_path.replace("/", os.sep))
    full_name  = repo["full_name"]

    exists_fs  = os.path.isfile(local_path)
    exists_git = False
    sha256     = yaml_name = yaml_desc = None
    size_bytes = size_lines = None
    has_yaml   = False
    missing_reason = None

    if is_valid_clone(clone_path, full_name):
        try:
            git_tree  = git_ls_tree(clone_path)
            norm      = rel_path
            exists_git = norm in git_tree or norm.lower() in git_tree
        except Exception:
            pass

    if exists_fs:
        try:
            sha256     = _sha256_file(local_path)
            size_bytes = os.path.getsize(local_path)
            content    = open(local_path, encoding="utf-8", errors="replace").read()
            size_lines = content.count("\n") + 1
            has_yaml   = content.startswith("---")
            ym         = _parse_yaml_frontmatter(content)
            yaml_name  = ym.get("name")
            yaml_desc  = ym.get("description")
        except OSError:
            pass
    elif exists_git:
        missing_reason = "in_git_tree_but_not_on_disk"
    else:
        cs = clone_result.get("clone_status", clone_result.get("status", "unknown"))
        if cs in ("failed", "timeout", "error"):
            missing_reason = f"clone_{cs}"
        elif cs in ("success", "updated", "cached", "update_partial",
                    "valid_correct", "wrong_remote_recloned", "broken_recloned"):
            missing_reason = "not_in_git_tree"
        elif cs == "valid_wrong_remote":
            missing_reason = "wrong_remote_not_recloned"
        elif cs in ("missing", "broken", "broken_not_cloned"):
            missing_reason = "broken_clone_needs_reclone"
        elif cs == "skipped_unavailable":
            missing_reason = "repo_unavailable"
        else:
            missing_reason = f"clone_{cs}"

    clone_type    = clone_result.get("clone_type", "unknown")
    rq3_available = clone_type not in ("shallow", "shallow_fallback")

    return {
        "full_name"             : full_name,
        "repo_key"              : repo_key(full_name),
        "agent"                 : repo.get("agent"),
        "group"                 : repo.get("group"),
        "expected_po_skill_count": repo.get("project_owned_skill_count", 0),
        "skill_rel_path"        : rel_path,
        "skill_dir"             : _skill_dir(rel_path),
        "filename"              : os.path.basename(rel_path),
        "location"              : skill_file_meta.get("location", ""),
        "exists_on_filesystem"  : exists_fs,
        "exists_in_git_tree"    : exists_git,
        "content_sha256"        : sha256,
        "size_bytes"            : size_bytes if exists_fs else skill_file_meta.get("size_bytes"),
        "size_lines"            : size_lines,
        "has_yaml_frontmatter"  : has_yaml,
        "yaml_name"             : yaml_name,
        "yaml_description"      : yaml_desc,
        "clone_status"          : clone_result.get("status"),
        "clone_type"            : clone_type,
        "rq3_history_available" : rq3_available,
        "missing_reason"        : missing_reason,
        "default_branch"        : repo.get("default_branch", "main"),
        "html_url"              : repo.get("html_url", f"https://github.com/{full_name}"),
    }


def clone_all(repos, clone_dir, full_history, update_existing_flag,
              max_workers, validate_only):
    os.makedirs(clone_dir, exist_ok=True)
    if validate_only:
        print("  validate-only: checking existing state, no cloning")

    print(f"\n  {'repo':<46} {'type':<10} {'status':<16} {'elapsed'}")
    print(f"  {'-'*46} {'-'*10} {'-'*16} {'-'*8}")

    all_results, counter = [], Counter()
    completed, lock = [0], threading.Lock()
    n_total = len(repos)

    def worker(repo):
        full_name = repo["full_name"]
        cp        = clone_path_for(full_name, clone_dir)

        if validate_only:
            state = clone_state(cp, full_name)
            status_map = {
                "valid_correct"      : "valid_correct",
                "valid_wrong_remote" : "valid_wrong_remote",
                "broken"             : "broken_not_cloned",
                "missing"            : "missing",
            }
            result = {"status": status_map[state], "elapsed_sec": 0,
                      "clone_type": ("shallow" if is_shallow(cp) else "full")
                                    if state == "valid_correct" else "none",
                      "attempt": 0, "error": None, "is_permanent": False}
            if state == "valid_wrong_remote":
                result["error"] = f"wrong origin: {get_origin_url(cp)}"
        elif repo.get("check_status") == "unavailable":
            result = {"status": "skipped_unavailable", "elapsed_sec": 0,
                      "clone_type": "none", "attempt": 0, "error": None,
                      "is_permanent": False}
        else:
            result = ensure_clone(repo, cp, full_history, update_existing_flag)

        record = {
            "full_name"                : full_name,
            "agent"                    : repo.get("agent"),
            "clone_status"             : result["status"],
            "clone_type"               : result["clone_type"],
            "clone_path"               : cp if result["status"] in
                                         ("success","updated","cached","update_partial",
                                          "valid_correct","wrong_remote_recloned","broken_recloned")
                                         else None,
            "rq3_history_available"    : result["clone_type"] not in ("shallow","shallow_fallback"),
            "elapsed_sec"              : result["elapsed_sec"],
            "clone_attempt"            : result["attempt"],
            "clone_error"              : result.get("error"),
            "default_branch"           : repo.get("default_branch"),
            "project_owned_skill_count": repo.get("project_owned_skill_count", 0),
        }

        with lock:
            all_results.append(record)
            counter[result["status"]] += 1
            completed[0] += 1
            n = completed[0]
            tprint(f"  [{n:>4}/{n_total}] {full_name[:45]:<46} "
                   f"{result['clone_type']:<10} {result['status']:<16} {result['elapsed_sec']:.1f}s")
            if n % CHECKPOINT_INTERVAL == 0:
                save_json_atomic(all_results, os.path.join(DEFAULT_OUTPUT_DIR, "clone_results.json"))
        return record

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(worker, repo): repo for repo in repos}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                tprint(f"  error {futures[future]['full_name']}: {exc}")

    return all_results, counter


def build_inventory(repos, clone_results, clone_dir):
    clone_map = {r["full_name"]: r for r in clone_results}
    rows, n = [], len(repos)
    for i, repo in enumerate(repos, 1):
        full_name = repo["full_name"]
        cp        = clone_path_for(full_name, clone_dir)
        cr        = clone_map.get(full_name, {"status": "unknown", "clone_type": "unknown"})
        po_files  = [sf for sf in repo.get("skill_files", []) if sf.get("is_project_owned")]
        if i % 50 == 0 or i == n:
            print(f"  inventorying {i}/{n}: {full_name}")
        for sf in po_files:
            rows.append(build_inventory_row(repo, sf, cp, cr))
    return rows


def save_inventory_csv(rows, path):
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {os.path.basename(path)} ({os.path.getsize(path):,} bytes, {len(rows):,} rows)")


def compute_summary(repos, clone_results, inventory):
    expected_po  = sum(r.get("project_owned_skill_count", 0) for r in repos)
    resolved_fs  = sum(1 for row in inventory if row["exists_on_filesystem"])
    resolved_git = sum(1 for row in inventory if row["exists_in_git_tree"])
    resolved     = sum(1 for row in inventory if row["exists_on_filesystem"] or row["exists_in_git_tree"])

    success_statuses = ("success","updated","cached","update_partial",
                        "valid_correct","wrong_remote_recloned","broken_recloned")
    avail_repos    = [r for r in clone_results if r.get("clone_status") in success_statuses]
    unavail_repos  = [r for r in clone_results
                      if r.get("clone_status") not in success_statuses
                      and r.get("clone_status") != "skipped_unavailable"]
    repos_with_any = {row["full_name"] for row in inventory
                      if row["exists_on_filesystem"] or row["exists_in_git_tree"]}

    missing_rows      = [row for row in inventory if not row["exists_on_filesystem"] and not row["exists_in_git_tree"]]
    missing_by_reason = Counter(row.get("missing_reason") for row in missing_rows)
    top_missing       = Counter(row["full_name"] for row in missing_rows).most_common(15)

    full_n    = sum(1 for r in clone_results if r.get("clone_type") == "full")
    shallow_n = sum(1 for r in clone_results if r.get("clone_type") in ("shallow","shallow_fallback"))
    rq3_n     = sum(1 for r in clone_results if r.get("rq3_history_available"))

    agent_stats: dict = defaultdict(lambda: {
        "input_repos": 0, "cloned_ok": 0, "expected_skills": 0,
        "resolved_skills": 0, "full_clones": 0, "shallow_clones": 0,
    })
    clone_map = {r["full_name"]: r for r in clone_results}
    for repo in repos:
        ag = repo.get("agent", "unknown")
        cr = clone_map.get(repo["full_name"], {})
        agent_stats[ag]["input_repos"]     += 1
        agent_stats[ag]["expected_skills"] += repo.get("project_owned_skill_count", 0)
        if cr.get("clone_status") in success_statuses:
            agent_stats[ag]["cloned_ok"] += 1
        if cr.get("clone_type") == "full":
            agent_stats[ag]["full_clones"] += 1
        elif cr.get("clone_type") in ("shallow","shallow_fallback"):
            agent_stats[ag]["shallow_clones"] += 1
    for row in inventory:
        if row["exists_on_filesystem"] or row["exists_in_git_tree"]:
            agent_stats[row.get("agent","unknown")]["resolved_skills"] += 1

    return {
        "generated_at"                   : datetime.now().isoformat(),
        "input_source"                   : DEFAULT_INPUT,
        "input_repos_expected"           : len(repos),
        "expected_po_skill_files"        : expected_po,
        "repos_available_locally"        : len(avail_repos),
        "repos_unavailable"              : len(unavail_repos),
        "repos_with_at_least_one_skill"  : len(repos_with_any),
        "skill_files_resolved_filesystem": resolved_fs,
        "skill_files_resolved_git_tree"  : resolved_git,
        "skill_files_resolved_total"     : resolved,
        "skill_files_missing"            : expected_po - resolved,
        "resolution_rate_pct"            : round(resolved / expected_po * 100, 1) if expected_po else 0,
        "full_clone_count"               : full_n,
        "shallow_clone_count"            : shallow_n,
        "rq3_history_available_count"    : rq3_n,
        "clone_status_counts"            : dict(Counter(r.get("clone_status") for r in clone_results)),
        "valid_correct_remote"           : sum(1 for r in clone_results if r.get("clone_status") == "valid_correct"),
        "wrong_remote"                   : sum(1 for r in clone_results if r.get("clone_status") == "valid_wrong_remote"),
        "wrong_remote_recloned"          : sum(1 for r in clone_results if r.get("clone_status") == "wrong_remote_recloned"),
        "broken_recloned"                : sum(1 for r in clone_results if r.get("clone_status") == "broken_recloned"),
        "clone_failed"                   : sum(1 for r in clone_results if r.get("clone_status") in ("failed","timeout","error")),
        "missing_by_reason"              : dict(missing_by_reason),
        "top_missing_repos"              : [{"full_name": fn, "missing_count": c} for fn, c in top_missing],
        "agent_breakdown"                : dict(agent_stats),
        "unavailable_repos"              : [{"full_name": r["full_name"], "error": r.get("clone_error")} for r in unavail_repos],
    }


def print_summary(summary):
    expected = summary["expected_po_skill_files"]
    resolved = summary["skill_files_resolved_total"]
    rate     = summary["resolution_rate_pct"]

    print(f"\nskill.md inventory")
    print(f"  expected          : {expected:>6,}")
    print(f"  resolved (fs)     : {summary['skill_files_resolved_filesystem']:>6,}")
    print(f"  resolved (git)    : {summary['skill_files_resolved_git_tree']:>6,}")
    print(f"  resolved total    : {resolved:>6,}  ({rate:.1f}%)")
    print(f"  missing           : {summary['skill_files_missing']:>6,}")
    print(f"\n  repos available   : {summary['repos_available_locally']:>6,} / {summary['input_repos_expected']}")
    print(f"  repos with skill  : {summary['repos_with_at_least_one_skill']:>6,}")
    print(f"\n  clone status:")
    for status, count in sorted(Counter(summary["clone_status_counts"]).items()):
        print(f"    {status:<25}: {count:,}")
    print(f"\n  full clones       : {summary['full_clone_count']:>6,}")
    print(f"  shallow clones    : {summary['shallow_clone_count']:>6,}")

    if rate < 70:
        print(f"\n  warning: resolution rate {rate:.1f}% below 70%")
        print("    - re-run with --update-existing true to fix broken clones")
        print("    - re-run with --full-history true to unshallow shallow clones")

    print(f"\n  per-agent:")
    print(f"  {'agent':<22} {'repos':>6} {'ok':>5} {'exp':>8} {'res':>8} {'%':>6}")
    print(f"  {'-'*22} {'-'*6} {'-'*5} {'-'*8} {'-'*8} {'-'*6}")
    for ag, info in sorted(summary["agent_breakdown"].items()):
        r = info["resolved_skills"] / info["expected_skills"] * 100 if info["expected_skills"] else 0
        print(f"  {ag:<22} {info['input_repos']:>6} {info['cloned_ok']:>5} "
              f"{info['expected_skills']:>8,} {info['resolved_skills']:>8,} {r:>5.1f}%")


def parse_args():
    p = argparse.ArgumentParser(description="Clone AIDev repos and build SKILL.md inventory")
    def boolarg(v): return v.lower() in ("true","1","yes")
    p.add_argument("--input",           default=DEFAULT_INPUT)
    p.add_argument("--clone-dir",       default=DEFAULT_CLONE_DIR)
    p.add_argument("--output-dir",      default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--full-history",    type=boolarg, default=True, metavar="true/false")
    p.add_argument("--max-workers",     type=int, default=DEFAULT_WORKERS)
    p.add_argument("--update-existing", type=boolarg, default=True, metavar="true/false")
    p.add_argument("--validate-only",   type=boolarg, default=False, metavar="true/false")
    p.add_argument("--debug-repo",      default="",
                   help="print remote/state info for one repo then exit")
    return p.parse_args()


def main():
    args = parse_args()

    if args.debug_repo:
        cp    = clone_path_for(args.debug_repo, args.clone_dir)
        state = clone_state(cp, args.debug_repo)
        print(f"\n[debug] {args.debug_repo}")
        print(f"  path    : {cp}")
        print(f"  state   : {state}")
        print(f"  expected: {clone_url_clean(args.debug_repo)}")
        print(f"  actual  : {get_origin_url(cp) or '(none)'}")
        sys.exit(0)

    started = datetime.now()
    print(f"started: {started.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("git not found."); sys.exit(1)

    if not os.path.exists(args.input):
        print(f"input not found: {args.input}")
        print("run classify_aidev_repos.py first."); sys.exit(1)

    with open(args.input, encoding="utf-8") as f:
        repos = json.load(f)

    os.makedirs(args.output_dir, exist_ok=True)
    total_po   = sum(r.get("project_owned_skill_count", 0) for r in repos)
    agent_dist = Counter(r.get("agent") for r in repos)

    print(f"input: {args.input} ({len(repos):,} repos, {total_po:,} expected SKILL.md files)")
    print(f"workers: {args.max_workers} | full-history: {args.full_history} | update-existing: {args.update_existing}")
    print("\nagent distribution:")
    for ag, c in agent_dist.most_common():
        print(f"  {ag:<22}: {c:,}")

    print(f"\nphase 1: cloning {len(repos)} repos...")
    clone_results, counter = clone_all(
        repos, args.clone_dir, args.full_history,
        args.update_existing, args.max_workers, args.validate_only,
    )
    results_path = os.path.join(args.output_dir, "clone_results.json")
    save_json(clone_results, results_path)
    print("\nclone status counts:")
    for status, count in counter.most_common():
        print(f"  {status:<25}: {count:,}")

    print(f"\nphase 2: building SKILL.md inventory...")
    inventory = build_inventory(repos, clone_results, args.clone_dir)
    inv_json  = os.path.join(args.output_dir, "skill_inventory.json")
    inv_csv   = os.path.join(args.output_dir, "skill_inventory.csv")
    save_json(inventory, inv_json)
    save_inventory_csv(inventory, inv_csv)

    print(f"\nphase 3: computing summary...")
    summary      = compute_summary(repos, clone_results, inventory)
    summary_path = os.path.join(args.output_dir, "clone_summary.json")
    save_json(summary, summary_path)
    print_summary(summary)

    finished = datetime.now()
    print(f"\nfinished: {finished.strftime('%Y-%m-%d %H:%M:%S')} (elapsed {finished - started})")
    print(f"\noutputs: {results_path}  {inv_json}  {inv_csv}  {summary_path}")
    print(f"next: python classify_provenance.py --input {inv_json}")


if __name__ == "__main__":
    main()
