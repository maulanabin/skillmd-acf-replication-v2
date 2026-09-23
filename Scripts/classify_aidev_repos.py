from __future__ import annotations
import json, os, time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
import requests

try:
    from datasets import load_dataset
    HF_AVAILABLE = True
except ImportError:
    HF_AVAILABLE = False

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OUTPUT_DIR   = "./output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept"       : "application/vnd.github.v3+json",
    "User-Agent"   : "aidev-skill-study",
}

USE_POP_ONLY = True

MAX_REPOS: int | None = None  
API_DELAY = 0.4

ACF_FILENAMES = {
    "claude.md",
    "agents.md",
    ".cursorrules",
    "copilot-instructions.md",
}
SKILL_FILENAMES = {"skill.md", "skills.md"}

SKILL_DIRS = {
    ".claude/skills",
    ".codex/skills",
    ".cursor/skills",
    ".cursor/rules",
    ".cursor/rules/skills",
    ".github/skills",
    ".windsurf/skills",
}


def load_aidev_repos() -> list[dict]:
    """Join repository config with pull_request config to attach agent labels.

    The agent field only lives on PR records, not on the repo records
    themselves, so we build a repo_id -> full_name lookup from the repo
    config and use it to fold agent labels from the PR config back onto
    each repo.
    """
    if not HF_AVAILABLE:
        raise SystemExit("Install datasets + huggingface_hub first: pip install datasets huggingface_hub")

    repo_config = "repository"     if USE_POP_ONLY else "all_repository"
    pr_config   = "pull_request"   if USE_POP_ONLY else "all_pull_request"
    subset_name = "AIDev-pop" if USE_POP_ONLY else "Full AIDev"

    print(f"\nLoading AIDev ({subset_name})...")
    print(f"  repo config: {repo_config}")
    print(f"  PR config  : {pr_config}")

    try:
        ds_repo = load_dataset("hao-li/AIDev", repo_config, split="train")
        print(f"  repository records: {len(ds_repo):,}")
    except Exception as e:
        raise SystemExit(f"could not load '{repo_config}': {e}")

    repo_map: dict[str, dict] = {}
    for record in ds_repo.to_list():
        repo_name = (
            record.get("full_name") or
            record.get("repo_name") or
            record.get("name") or
            record.get("repo") or ""
        )
        if not repo_name:
            continue

        # some records only have a bare repo name, not owner/repo
        if "/" not in repo_name:
            owner = record.get("owner") or record.get("owner_login") or ""
            if owner:
                repo_name = f"{owner}/{repo_name}"
            else:
                continue

        stars = int(
            record.get("stargazers_count", 0) or
            record.get("stars", 0) or
            record.get("stargazers", 0) or 0
        )

        repo_map[repo_name] = {
            "full_name"     : repo_name,
            "agent"         : "unknown",   # filled in below from PR config
            "stars"         : stars,
            "language"      : record.get("language", "") or "",
            "clone_url"     : (record.get("clone_url") or
                               f"https://github.com/{repo_name}.git"),
            "html_url"      : (record.get("html_url") or
                               f"https://github.com/{repo_name}"),
            "description"   : record.get("description", "") or "",
            "topics"        : record.get("topics", []) or [],
            "created_at"    : record.get("created_at", "") or "",
            "pushed_at"     : record.get("pushed_at", "") or "",
            "size_kb"       : record.get("size", 0) or 0,
            "default_branch": record.get("default_branch", "main") or "main",
            "pr_count"      : 0,
        }

    print(f"  unique repos: {len(repo_map):,}")

    print(f"\nLoading '{pr_config}' for agent labels...")
    try:
        ds_pr = load_dataset("hao-li/AIDev", pr_config, split="train")
        print(f"  PR records: {len(ds_pr):,}")

        if len(ds_pr) > 0:
            first_pr = ds_pr[0]

            agent_field = None
            for candidate in ["agent", "agent_label", "agent_type",
                               "tool", "source", "coding_agent"]:
                if candidate in first_pr:
                    agent_field = candidate
                    print(f"  agent field: '{agent_field}'")
                    break

            if not agent_field:
                print(f"  warning: no agent field found in PR config, fields available: {list(first_pr.keys())}")
            else:
                id_to_name: dict[int, str] = {
                    r["id"]: r["full_name"]
                    for r in ds_repo.to_list()
                    if r.get("id") and r.get("full_name")
                }

                repo_agent: dict[str, str] = {}
                repo_pr_count: dict[str, int] = defaultdict(int)

                for pr in ds_pr.to_list():
                    rid       = pr.get("repo_id")
                    repo_name = id_to_name.get(rid, "")
                    if not repo_name:
                        continue

                    agent_val = str(pr.get(agent_field, "") or "unknown")
                    # keep first non-unknown agent label seen per repo
                    if repo_name not in repo_agent and agent_val != "unknown":
                        repo_agent[repo_name] = agent_val
                    repo_pr_count[repo_name] += 1

                updated = 0
                for repo_name, agent_val in repo_agent.items():
                    if repo_name in repo_map:
                        repo_map[repo_name]["agent"]    = agent_val
                        repo_map[repo_name]["pr_count"] = repo_pr_count[repo_name]
                        updated += 1

                print(f"  agent labels attached to {updated:,} repos")

                agent_dist = Counter(r["agent"] for r in repo_map.values())
                for agent, count in agent_dist.most_common():
                    print(f"    {agent:<30}: {count:,}")

    except Exception as e:
        print(f"  warning: could not load '{pr_config}': {e}, continuing without agent labels")

    repos = list(repo_map.values())

    if MAX_REPOS:
        repos = repos[:MAX_REPOS]

    print(f"\ntotal repos to process: {len(repos):,}")
    return repos


def github_get(url: str, params: dict = None, size_limit_mb: int = 50):
    """GET with retry on rate limit / transient 5xx, cap response size so a
    huge tree blob doesn't blow up memory on some monorepo."""
    import json as _json
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, params=params,
                             timeout=30, stream=True)
            if r.status_code == 200:
                chunks = []
                total  = 0
                limit  = size_limit_mb * 1024 * 1024
                for chunk in r.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > limit:
                        r.close()
                        return {"_too_large": True}
                    chunks.append(chunk)
                return _json.loads(b"".join(chunks))
            if r.status_code == 404:
                return None
            if r.status_code == 403:
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait  = max(reset - int(time.time()), 15)
                print(f"    rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503):
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    return None


def check_files_via_git_tree(full_name: str, default_branch: str = "main") -> dict:
    """One Git Trees API call per repo, recursive, to grab the whole file
    listing at once instead of walking directories manually."""
    data = None
    for branch in [default_branch, "master", "develop", "trunk"]:
        data = github_get(
            f"https://api.github.com/repos/{full_name}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        if data:
            break

    if data and data.get("_too_large"):
        return {
            "check_status": "too_large", "has_acf": False,
            "has_skill_md": False, "acf_files": [], "skill_files": [],
            "skill_count": 0, "project_owned_skill_count": 0,
            "group": "unknown", "acf_types": [], "tree_truncated": True,
        }

    if not data:
        return {
            "check_status": "unavailable", "has_acf": False,
            "has_skill_md": False, "acf_files": [], "skill_files": [],
            "skill_count": 0, "project_owned_skill_count": 0,
            "group": "unknown", "acf_types": [], "tree_truncated": False,
        }

    tree      = data.get("tree", [])
    truncated = bool(data.get("truncated", False))

    acf_files_found = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path  = item["path"]
        fname = os.path.basename(path).lower()
        if fname not in ACF_FILENAMES:
            continue
        if fname == "claude.md":          acf_type = "claude_md"
        elif fname == "agents.md":        acf_type = "agents_md"
        elif fname == ".cursorrules":     acf_type = "cursor_rules"
        elif "copilot" in fname:          acf_type = "copilot_instructions"
        else:                             acf_type = "other_acf"
        acf_files_found.append({"path": path, "acf_type": acf_type})

    skill_files_found = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path  = item["path"]
        fname = os.path.basename(path).lower()
        if fname not in SKILL_FILENAMES:
            continue
        plow             = path.lower()
        location         = "other"
        is_project_owned = False
        for sd in SKILL_DIRS:
            if plow.startswith(sd + "/") or ("/" + sd + "/") in ("/" + plow):
                location         = sd.replace("/", "_").replace(".", "")
                is_project_owned = True
                break
        if not is_project_owned:
            if path.count("/") == 0:
                location = "root"
            elif "/skills/" in plow or plow.startswith("skills/"):
                location = "generic_skills_dir"
        skill_files_found.append({
            "path"            : path,
            "location"        : location,
            "is_project_owned": is_project_owned,
            "size_bytes"      : item.get("size", 0),
        })

    has_acf      = len(acf_files_found) > 0
    has_skill_md = len(skill_files_found) > 0
    proj_owned   = sum(1 for f in skill_files_found if f["is_project_owned"])

    if has_acf and has_skill_md:       group = "both"
    elif has_acf:                      group = "only_acf"
    elif has_skill_md:                 group = "only_skills"
    else:                              group = "neither"

    return {
        "check_status"             : "ok",
        "tree_truncated"           : truncated,
        "has_acf"                  : has_acf,
        "acf_files"                : acf_files_found,
        "acf_types"                : list({f["acf_type"] for f in acf_files_found}),
        "has_skill_md"             : has_skill_md,
        "skill_files"              : skill_files_found,
        "skill_count"              : len(skill_files_found),
        "project_owned_skill_count": proj_owned,
        "has_both"                 : has_acf and has_skill_md,
        "group"                    : group,
    }


def classify_all_repos(repos: list[dict]) -> list[dict]:
    total   = len(repos)
    checkpoint_path = os.path.join(OUTPUT_DIR, "classify_checkpoint.json")

    done_map: dict[str, dict] = {}
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, encoding="utf-8") as f:
                done_list = json.load(f)
            done_map = {r["full_name"]: r for r in done_list}
            print(f"  resuming from checkpoint: {len(done_map):,} repos already done")
        except Exception:
            done_map = {}

    results = []
    print(f"\nClassifying {total:,} repos via Git Trees API...")
    remaining = total - len(done_map)
    print(f"  estimated time: ~{remaining * 0.6 / 60:.0f} min")

    for i, repo in enumerate(repos, 1):
        full_name = repo["full_name"]

        if full_name in done_map:
            results.append(done_map[full_name])
            continue

        if i == 1 or i % 100 == 0 or i == total:
            gc = Counter(r.get("group", "?") for r in results)
            print(f"  [{i:>5}/{total}] agent={repo.get('agent', '?')[:15]} | "
                  f"both={gc.get('both', 0):,} | "
                  f"only_acf={gc.get('only_acf', 0):,} | "
                  f"only_skills={gc.get('only_skills', 0):,} | "
                  f"neither={gc.get('neither', 0):,}")

        branch     = repo.get("default_branch", "main")
        file_check = check_files_via_git_tree(full_name, branch)
        result     = {**repo, **file_check}
        results.append(result)
        done_map[full_name] = result
        time.sleep(API_DELAY)

        if len(done_map) % 50 == 0:
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                json.dump(list(done_map.values()), f)

    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(results, f)

    return results


def pct(part, total) -> float:
    return round(part / total * 100, 1) if total else 0.0


def compute_rq1_prevalence(results: list[dict]) -> dict:
    valid = [r for r in results if r.get("group") not in ("unknown", None)]
    total = len(valid)
    gd    = Counter(r["group"] for r in valid)

    agent_group: dict[str, Counter] = defaultdict(Counter)
    for r in valid:
        agent_group[r.get("agent", "unknown")][r["group"]] += 1

    agent_breakdown = {}
    for agent, counts in sorted(agent_group.items()):
        at = sum(counts.values())
        agent_breakdown[agent] = {
            "total_repos": at,
            **{g: {"count": counts.get(g, 0), "percentage": pct(counts.get(g, 0), at)}
               for g in ("both", "only_acf", "only_skills", "neither")},
        }

    repos_w_skills = [r for r in valid if r.get("has_skill_md")]
    skill_counts   = [r.get("skill_count", 0) for r in repos_w_skills]
    skill_stats    = {}
    if skill_counts:
        s = sorted(skill_counts); n = len(s)
        skill_stats = {
            "repos_with_skills" : n,
            "min"    : s[0],   "max"   : s[-1],
            "mean"   : round(sum(s) / n, 2),
            "median" : s[n // 2],
            "total_skill_files": sum(s),
        }

    acf_types: Counter = Counter()
    for r in valid:
        for t in r.get("acf_types", []):
            acf_types[t] += 1

    return {
        "total_repos_analyzed": total,
        "group_distribution"  : {
            k: {"count": v, "percentage": pct(v, total)}
            for k, v in gd.most_common()
        },
        "agent_breakdown"      : agent_breakdown,
        "skill_count_stats"    : skill_stats,
        "acf_type_distribution": dict(acf_types.most_common()),
        "narrative"            : (
            f"Of {total:,} AIDev repositories analyzed, "
            f"{gd.get('both', 0):,} ({pct(gd.get('both', 0), total)}%) "
            f"contain both an ACF and SKILL.md, "
            f"{gd.get('only_acf', 0):,} ({pct(gd.get('only_acf', 0), total)}%) "
            f"contain only an ACF, "
            f"{gd.get('only_skills', 0):,} "
            f"({pct(gd.get('only_skills', 0), total)}%) contain only SKILL.md, "
            f"and {gd.get('neither', 0):,} "
            f"({pct(gd.get('neither', 0), total)}%) contain neither."
        ),
    }


def save_json(data: Any, filename: str) -> None:
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  saved {filename} ({os.path.getsize(path):,} bytes)")


def main() -> None:
    started = datetime.now()
    print(f"started: {started.strftime('%Y-%m-%d %H:%M:%S')}")

    if not GITHUB_TOKEN:
        raise SystemExit(
            "GITHUB_TOKEN not set.\n"
            "Windows: $env:GITHUB_TOKEN='token'\n"
            "Linux/Mac: export GITHUB_TOKEN='token'\n"
        )

    classified_cache = os.path.join(OUTPUT_DIR, "classified_repos.json")

    if os.path.exists(classified_cache):
        print("found existing classified_repos.json, loading cache...")
        with open(classified_cache, "r", encoding="utf-8") as f:
            classified = json.load(f)

        agents = set(r.get("agent", "unknown") for r in classified)
        if agents == {"unknown"}:
            print("  all agents are 'unknown' in cache, re-loading AIDev to fix labels...")
            repos = load_aidev_repos()
            save_json(repos, "aidev_repos.json")

            repo_agent = {r["full_name"]: r["agent"] for r in repos}
            fixed = 0
            for r in classified:
                fn = r.get("full_name", "")
                if fn in repo_agent and repo_agent[fn] != "unknown":
                    r["agent"] = repo_agent[fn]
                    fixed += 1
            print(f"  fixed agent labels for {fixed:,} repos")
            save_json(classified, "classified_repos.json")
    else:
        repos = load_aidev_repos()
        save_json(repos, "aidev_repos.json")
        classified = classify_all_repos(repos)
        save_json(classified, "classified_repos.json")

    print("\ncomputing RQ1 prevalence...")
    rq1 = compute_rq1_prevalence(classified)
    save_json(rq1, "rq1_prevalence_raw.json")

    repos_with_skills = [
        r for r in classified
        if r.get("has_skill_md") and r.get("group") in ("both", "only_skills")
    ]
    save_json(repos_with_skills, "discovered_repos.json")

    finished = datetime.now()
    total    = rq1["total_repos_analyzed"]
    gd       = rq1["group_distribution"]

    print(f"\nRQ1 prevalence report")
    print(f"total repos analyzed: {total:,}")
    for grp, lbl in [
        ("both",        "both ACF + SKILL.md  "),
        ("only_acf",    "only ACF (no SKILL.md)"),
        ("only_skills", "only SKILL.md (no ACF)"),
        ("neither",     "neither              "),
    ]:
        info = gd.get(grp, {"count": 0, "percentage": 0})
        print(f"  {lbl}: {info['count']:>5,} ({info['percentage']}%)")

    if rq1["skill_count_stats"]:
        sk = rq1["skill_count_stats"]
        print(f"\nSKILL.md stats:")
        print(f"  repos with SKILL.md : {sk['repos_with_skills']:,}")
        print(f"  total skill files   : {sk['total_skill_files']:,}")
        print(f"  mean / median / max : {sk['mean']} / {sk['median']} / {sk['max']}")

    print(f"\nACF type distribution:")
    for t, c in rq1["acf_type_distribution"].items():
        print(f"  {t:<30}: {c:,} ({pct(c, total)}%)")

    print(f"\nper-agent breakdown:")
    for agent, info in rq1["agent_breakdown"].items():
        has_skill = info["both"]["percentage"] + info["only_skills"]["percentage"]
        print(f"  {agent:<30}: total={info['total_repos']:,} | "
              f"has_skill={has_skill}% | both={info['both']['percentage']}%")

    print(f"\n{rq1['narrative']}")
    print(f"\ndiscovered_repos.json -> {len(repos_with_skills):,} repos (input for clone_repositories.py)")
    print(f"finished: {finished.strftime('%Y-%m-%d %H:%M:%S')} (elapsed {finished - started})")


if __name__ == "__main__":
    main()
