#!/usr/bin/env python3
"""
ORRAS Developer Contribution Report generator.

No local clone required. Everything is read straight from GitHub through the
`gh` CLI (already-authenticated REST API calls) — commits, per-commit diff
stats, merge history, PR review stats, and the docs/dev-tasks/*.csv delivery
plan. --repo takes a GitHub "owner/repo" slug, not a filesystem path.

Usage:
  python orras_report.py --init                          # create orras_report_config.json
  python orras_report.py --repo OWNER/REPO --discover     # list raw git identities (to fill the config)
  python orras_report.py --repo OWNER/REPO                # build the contribution report
  python orras_report.py --init-kpi                       # write the bundled KPI dashboard template

Templates are bundled directly in orras_template.py and rendered from memory —
no template file is ever written to disk, and no network access is needed for
templates; the tool never depends on cdn.stratpoint.io being reachable.

Reading git history this way costs roughly one `gh api` call per non-merge
commit (for per-file diff stats), run through a small thread pool. Expect this
to take a few minutes on a repo with hundreds of commits — that's the tradeoff
for never needing a local checkout.

Stdlib only (uses the `gh` CLI as an external process, not a Python package).
Python 3.8+.
"""
import argparse
import base64
import csv
import datetime as dt
import io
import json
import re
import shlex
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from orras_template import CONTRIBUTION_TEMPLATE, KPI_TEMPLATE

CONFIG_FILE = "orras_report_config.json"
KPI_FILE = "orras-developer-kpi-latest.html"
MAX_WORKERS = 12

LOCKFILES = {"pnpm-lock.yaml", "package-lock.json", "yarn.lock"}
KNOWN_TYPES = {"feat", "fix", "docs", "test", "chore", "ci", "refactor", "style", "perf", "build", "revert"}
CC_RE = re.compile(r"^(\w+)(?:\(([^)]*)\))?!?:\s")
TASK_RE = re.compile(r"\bTASK-\d+\b", re.I)
REVIEW_RE = re.compile(r"\b(review|pr feedback|address(?:ed|ing)? (?:comments|feedback))\b", re.I)
PR_RE = re.compile(r"#(\d+)")
DATA_RE = re.compile(r'const DATA = .*?;(\s*)const REPORT_DATE = "[^"]*";', re.S)
IDENT_RE = re.compile(r"\d+ raw git author identities")

DEFAULT_CONFIG = {
    "github_repo": "stratpoint-engineering/mptc-orras",
    "branch": "develop",
    "start_date": "2026-08-10",
    "tasks_dir": "docs/dev-tasks",
    "output_dir": "reports",
    "modules": {"registration": "registration", "maintenance": "maintenance",
                "management": "management", "packages": "packages"},
    "people": {},
    "exclude": {
        "label": "Merge Sim (session identity)",
        "reason": "distinct git identity, not attributable to a named engineer",
        "emails": [], "git_names": ["Merge Sim"],
    },
    "publish_command": "",
}


# ---------------------------------------------------------------- helpers
def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def monday(d):
    return d - dt.timedelta(days=d.weekday())


def gh_api(endpoint, paginate=False, retries=3):
    args = ["api"]
    if paginate:
        args.append("--paginate")
    args.append(endpoint)
    last_err = None
    for attempt in range(retries):
        r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return r.stdout
        last_err = r.stderr.strip()
        if attempt < retries - 1:
            time.sleep(0.5 * (attempt + 1))
    sys.exit(f"gh api {endpoint} failed after {retries} attempts: {last_err}")


def gh_api_json(endpoint, paginate=False):
    return json.loads(gh_api(endpoint, paginate=paginate))


def normpath(p):
    p = re.sub(r"\{[^}]*? => ([^}]*)\}", r"\1", p)  # dir/{old => new}/file
    if " => " in p:
        p = p.split(" => ", 1)[1]
    return p.replace("//", "/")


def to_num(v):
    try:
        f = float(str(v).strip())
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return 0


def norm_status(s):
    s = (s or "").strip().lower()
    if not s:
        return None
    if "block" in s:
        return "Blocked"
    if any(k in s for k in ("done", "complete", "closed", "merged", "resolved")):
        return "Done"
    if any(k in s for k in ("progress", "review", "qa", "testing", "doing", "ongoing")):
        return "In Progress"
    return "To Do"


def pick_col(cols, names):
    for n in names:
        if n in cols:
            return cols[n]
    for n in names:
        for low, orig in cols.items():
            if n in low:
                return orig
    return None


# ---------------------------------------------------------------- identity
class Resolver:
    EXCLUDED = object()

    def __init__(self, cfg):
        self.by_email, self.by_name, self.by_login = {}, {}, {}
        for person, ids in cfg["people"].items():
            self.by_name[person.lower()] = person
            for e in ids.get("emails", []):
                self.by_email[e.lower()] = person
            for n in ids.get("git_names", []):
                self.by_name[n.lower()] = person
            for g in ids.get("github", []):
                self.by_login[g.lower()] = person
        ex = cfg.get("exclude", {})
        self.ex_emails = {e.lower() for e in ex.get("emails", [])}
        self.ex_names = {n.lower() for n in ex.get("git_names", [])}

    def resolve(self, name, email):
        if email.lower() in self.ex_emails or name.lower() in self.ex_names:
            return self.EXCLUDED
        return self.by_email.get(email.lower()) or self.by_name.get(name.lower())


# ---------------------------------------------------------------- git (via GitHub API)
def _commit_date(c):
    return dt.datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00")).date()


def _commit_dict(c, files):
    ca = c["commit"]["author"]
    subject = c["commit"]["message"].split("\n", 1)[0]
    return dict(hash=c["sha"], name=ca["name"], email=ca["email"], date=_commit_date(c),
                subject=subject.strip(), files=files)


def _fetch_commit_list(repo, ref, start):
    since = f"{start.isoformat()}T00:00:00Z"
    return gh_api_json(f"repos/{repo}/commits?sha={ref}&since={since}&per_page=100", paginate=True)


def read_commits(repo, ref, start, merges):
    raw = _fetch_commit_list(repo, ref, start)
    by_sha = {c["sha"]: c for c in raw}

    if merges:
        head = gh_api_json(f"repos/{repo}/commits/{ref}")
        commits, sha, seen = [], head["sha"], set()
        while sha and sha not in seen:
            seen.add(sha)
            c = by_sha.get(sha)
            if c is None or _commit_date(c) < start:
                break
            if len(c["parents"]) >= 2:
                commits.append(_commit_dict(c, files=[]))
            sha = c["parents"][0]["sha"] if c["parents"] else None
        return commits

    candidates = [c for c in raw if len(c["parents"]) < 2 and _commit_date(c) >= start]
    print(f"  fetching per-commit diff stats for {len(candidates)} commits via gh api "
          f"(this is the slow part — no local clone) …")
    details, done = {}, 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(gh_api_json, f"repos/{repo}/commits/{c['sha']}"): c["sha"] for c in candidates}
        for fut in as_completed(futs):
            details[futs[fut]] = fut.result()
            done += 1
            if done % 100 == 0 or done == len(candidates):
                print(f"    {done}/{len(candidates)} …")

    commits = []
    for c in candidates:
        detail = details[c["sha"]]
        files = [(f.get("additions", 0), f.get("deletions", 0), normpath(f["filename"]))
                 for f in detail.get("files", [])]
        commits.append(_commit_dict(c, files))
    return commits


def verify_branch(repo, branch):
    r = subprocess.run(["gh", "api", f"repos/{repo}/branches/{branch}"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"Branch '{branch}' not found in {repo} (or repo/auth issue): {r.stderr.strip()}")
    return branch


# ---------------------------------------------------------------- github
def read_pr_reviews(repo, branch, start, resolver):
    empty = ({}, {"prs_total": 0, "prs_with_review": 0})
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "merged", "--base", branch, "--limit", "1000",
             "--json", "number,mergedAt,reviews"],
            capture_output=True, check=True, text=True, encoding="utf-8").stdout
    except FileNotFoundError:
        warn("gh CLI not installed; PR review section will be empty")
        return empty
    except subprocess.CalledProcessError as e:
        warn(f"gh failed ({e.stderr.strip()}); PR review section will be empty. Run: gh auth login")
        return empty

    prs = [p for p in json.loads(out) if p.get("mergedAt")
           and dt.datetime.fromisoformat(p["mergedAt"].replace("Z", "+00:00")).date() >= start]
    stats = defaultdict(lambda: {"reviewed": 0, "approved": 0, "changes_requested": 0, "commented": 0})
    with_review = 0
    state_key = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested", "COMMENTED": "commented"}
    for p in prs:
        per = defaultdict(set)
        for r in p.get("reviews") or []:
            login = (r.get("author") or {}).get("login")
            if not login:
                continue
            who = resolver.by_login.get(login.lower()) or f"{login} (unmapped GitHub login)"
            per[who].add(r.get("state", ""))
        if per:
            with_review += 1
        for who, states in per.items():
            stats[who]["reviewed"] += 1
            for s in states:
                if s in state_key:
                    stats[who][state_key[s]] += 1
    return dict(stats), {"prs_total": len(prs), "prs_with_review": with_review}


# ---------------------------------------------------------------- delivery plan
def read_plan(repo, branch, tasks_dir, modules):
    rollup = {m: {"tasks": 0, "status": {}, "sp_total": 0, "sp_done": 0} for m in modules}
    n_files = n_rows = 0
    prefix = tasks_dir.strip("/")

    tree = gh_api_json(f"repos/{repo}/git/trees/{branch}?recursive=1")
    if tree.get("truncated"):
        warn(f"{tasks_dir} tree listing was truncated by the GitHub API; some CSVs may be missed")

    paths_by_module = defaultdict(list)
    for e in tree.get("tree", []):
        path = e.get("path", "")
        if e.get("type") != "blob" or not path.startswith(prefix + "/") or not path.endswith(".csv"):
            continue
        rest = path[len(prefix) + 1:]
        top = rest.split("/", 1)[0]
        if top in modules:
            paths_by_module[top].append(path)

    if not paths_by_module:
        warn(f"no CSVs found under {tasks_dir}/<module>/ in {repo}@{branch}; delivery-plan section will be empty")

    for m in modules:
        for path in sorted(paths_by_module.get(m, [])):
            n_files += 1
            blob = gh_api_json(f"repos/{repo}/contents/{path}?ref={branch}")
            text = base64.b64decode(blob["content"]).decode("utf-8-sig")
            rd = csv.DictReader(io.StringIO(text))
            cols = {c.strip().lower(): c for c in (rd.fieldnames or []) if c}
            sc = pick_col(cols, ("status", "state"))
            pc = pick_col(cols, ("story points", "story_points", "storypoints", "points", "sp", "estimate"))
            if not sc:
                warn(f"no Status column in {path}; skipped")
                continue
            for row in rd:
                st = norm_status(row.get(sc))
                if not st:
                    continue
                n_rows += 1
                r = rollup[m]
                r["tasks"] += 1
                r["status"][st] = r["status"].get(st, 0) + 1
                sp = to_num(row.get(pc)) if pc else 0
                r["sp_total"] += sp
                if st == "Done":
                    r["sp_done"] += sp
    return rollup, n_files, n_rows


# ---------------------------------------------------------------- build
def new_week():
    return {"commits": 0, "ins": 0, "del": 0, "files": 0, "types": Counter(), "scopes": Counter(),
            "top_dirs": Counter(), "modules": Counter(), "tasks": set(), "review": 0}


def module_of(path, modules):
    for m, prefix in modules.items():
        prefix = prefix.strip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return m
    return None


def build(repo, cfg):
    start = monday(dt.date.fromisoformat(cfg["start_date"]))
    today = dt.date.today()
    weeks, w = [], start
    while w <= monday(today):
        weeks.append(w.isoformat())
        w += dt.timedelta(days=7)

    resolver = Resolver(cfg)
    ref = verify_branch(repo, cfg["branch"])
    print(f"Reading {repo}@{ref} since {start} via GitHub API …")
    commits = read_commits(repo, ref, start, merges=False)
    merges = read_commits(repo, ref, start, merges=True)
    modules = cfg["modules"]

    weekly = defaultdict(lambda: defaultdict(new_week))
    dates = defaultdict(list)
    idents, unmapped = set(), Counter()
    excluded, lock_churn = 0, 0

    for c in commits:
        who = resolver.resolve(c["name"], c["email"])
        if who is Resolver.EXCLUDED:
            excluded += 1
            continue
        if who is None:
            who = c["name"]
            unmapped[f'{c["name"]} <{c["email"]}>'] += 1
        else:
            idents.add((c["name"].lower(), c["email"].lower()))
        wk = monday(c["date"]).isoformat()
        if wk not in weeks:
            continue
        W = weekly[who][wk]
        dates[who].append(c["date"])
        m = CC_RE.match(c["subject"])
        typ = m.group(1).lower() if m and m.group(1).lower() in KNOWN_TYPES else "other"
        W["commits"] += 1
        W["types"][typ] += 1
        if m and m.group(2):
            for s in m.group(2).split(","):
                if s.strip():
                    W["scopes"][s.strip().lower()] += 1
        W["tasks"].update(t.upper() for t in TASK_RE.findall(c["subject"]))
        if REVIEW_RE.search(c["subject"]):
            W["review"] += 1
        for a, d, p in c["files"]:
            W["files"] += 1
            W["top_dirs"][p.split("/")[0]] += 1
            mod = module_of(p, modules)
            if mod:
                W["modules"][mod] += 1
            if Path(p).name in LOCKFILES:
                lock_churn += a + d
                continue
            W["ins"] += a
            W["del"] += d

    for ident, n in unmapped.most_common():
        warn(f"unmapped git identity ({n} commits): {ident} -> add to config 'people' or 'exclude'")

    people = sorted(set(cfg["people"]) | set(weekly))
    out_weekly, totals = {}, {}
    module_person = {m: {} for m in modules}
    for p in people:
        out_weekly[p] = {}
        agg = new_week()
        active = 0
        for wk in weeks:
            W = weekly[p][wk] if wk in weekly[p] else new_week()
            out_weekly[p][wk] = {
                "commits": W["commits"], "ins": W["ins"], "del": W["del"], "files": W["files"],
                "types": dict(W["types"]), "scopes": [list(x) for x in W["scopes"].most_common(8)],
                "top_dirs": [list(x) for x in W["top_dirs"].most_common(8)],
                "modules": dict(W["modules"]), "tasks": sorted(W["tasks"]),
            }
            active += W["commits"] > 0
            for k in ("commits", "ins", "del", "files", "review"):
                agg[k] += W[k]
            for k in ("types", "scopes", "top_dirs", "modules"):
                agg[k].update(W[k])
            agg["tasks"] |= W["tasks"]
        ds = sorted(dates[p])
        totals[p] = {
            "commits": agg["commits"], "ins": agg["ins"], "del": agg["del"], "files": agg["files"],
            "first": ds[0].isoformat() if ds else None, "last": ds[-1].isoformat() if ds else None,
            "active_weeks": active, "types": dict(agg["types"]),
            "top_scopes": [list(x) for x in agg["scopes"].most_common(8)],
            "top_dirs": [list(x) for x in agg["top_dirs"].most_common(8)],
            "distinct_tasks": sorted(agg["tasks"]), "review_commits": agg["review"],
            "modules_touched": [m for m in modules if agg["modules"][m] > 0],
        }
        for m, n in agg["modules"].items():
            module_person[m][p] = n

    merge_rows = []
    merges_per_week = Counter()
    for mc in merges:
        wk = monday(mc["date"]).isoformat()
        merges_per_week[wk] += 1
        who = resolver.resolve(mc["name"], mc["email"])
        who = mc["name"] if who in (None, Resolver.EXCLUDED) else who
        pr = PR_RE.search(mc["subject"])
        merge_rows.append({"hash": mc["hash"], "week": wk, "date": mc["date"].isoformat(),
                           "person": who, "subject": mc["subject"], "pr": pr.group(1) if pr else ""})

    team_weekly = {}
    for wk in weeks:
        rows = [out_weekly[p][wk] for p in people]
        team_weekly[wk] = {"commits": sum(r["commits"] for r in rows), "ins": sum(r["ins"] for r in rows),
                           "del": sum(r["del"] for r in rows), "prs_merged": merges_per_week[wk],
                           "authors": sum(r["commits"] > 0 for r in rows)}

    rollup, n_files, n_rows = read_plan(repo, cfg["branch"], cfg["tasks_dir"], modules)
    pr_reviews, coverage = read_pr_reviews(repo, cfg["branch"], start, resolver)
    ex = cfg.get("exclude", {})

    data = {
        "generated": dt.datetime.now().isoformat(),
        "weeks": weeks, "people": people, "weekly": out_weekly, "totals": totals,
        "team_weekly": team_weekly, "module_rollup": rollup, "module_person": module_person,
        "merge_count_total": len(merges), "csv_files_scanned": n_files, "csv_rows_valid": n_rows,
        "lockfile_churn_excluded": lock_churn, "total_commits_all_authors": len(commits),
        "recent_merges": merge_rows[:10],
        "excluded_identity": {"name": ex.get("label", "excluded identity"), "commits": excluded,
                              "reason": ex.get("reason", "")},
        "pr_reviews": pr_reviews, "pr_review_coverage": coverage,
    }
    return data, len(idents)


# ---------------------------------------------------------------- html
def load_template():
    if not DATA_RE.search(CONTRIBUTION_TEMPLATE):
        sys.exit("CONTRIBUTION_TEMPLATE (orras_template.py) doesn't contain the expected "
                  "'const DATA = …; const REPORT_DATE = …;' block")
    return CONTRIBUTION_TEMPLATE


def render(template, data, n_idents):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    today = dt.date.today().isoformat()
    html = DATA_RE.sub(lambda m: f'const DATA = {payload};{m.group(1)}const REPORT_DATE = "{today}";',
                       template, count=1)
    return IDENT_RE.sub(f"{n_idents} raw git author identities", html)


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="Build the ORRAS Developer Contribution Report")
    ap.add_argument("--repo", default=None,
                     help="GitHub 'owner/repo' slug (default: config's github_repo)")
    ap.add_argument("--config", default=CONFIG_FILE)
    ap.add_argument("--init", action="store_true", help="write a starter config and exit")
    ap.add_argument("--init-kpi", action="store_true",
                     help="write the bundled KPI dashboard template to output_dir and exit")
    ap.add_argument("--discover", action="store_true", help="list raw git identities and exit")
    ap.add_argument("--publish", action="store_true", help="run publish_command from config after building")
    a = ap.parse_args()

    if a.init:
        if Path(a.config).exists():
            sys.exit(f"{a.config} already exists")
        Path(a.config).write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"Wrote {a.config}. Next: python {Path(__file__).name} --repo OWNER/REPO --discover")
        return

    if a.init_kpi:
        out_dir = Path("reports")
        if Path(a.config).exists():
            out_dir = Path(json.loads(Path(a.config).read_text(encoding="utf-8")).get("output_dir", "reports"))
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / KPI_FILE
        if dest.exists():
            sys.exit(f"{dest} already exists")
        dest.write_text(KPI_TEMPLATE, encoding="utf-8")
        print(f"Wrote {dest} from the bundled KPI template.\n"
              f"This is a static snapshot, not regenerated from git/gh — edit its ROWS/INSIGHTS "
              f"by hand (or with an LLM's help) to refresh it with new data.")
        return

    if not Path(a.config).exists():
        sys.exit(f"{a.config} not found. Run with --init first.")
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    repo = a.repo or cfg.get("github_repo")
    if not repo:
        sys.exit("No repo given: pass --repo OWNER/REPO or set 'github_repo' in the config.")

    if a.discover:
        ref = verify_branch(repo, cfg["branch"])
        start = monday(dt.date.fromisoformat(cfg["start_date"]))
        res = Resolver(cfg)
        cnt = Counter((c["name"], c["email"]) for c in read_commits(repo, ref, start, merges=False))
        print(f"{'COMMITS':>7}  {'GIT NAME':<28} {'EMAIL':<40} RESOLVES TO")
        for (n, e), k in cnt.most_common():
            r = res.resolve(n, e)
            r = "(excluded)" if r is Resolver.EXCLUDED else (r or "-- UNMAPPED --")
            print(f"{k:>7}  {n[:28]:<28} {e[:40]:<40} {r}")
        return

    data, n_idents = build(repo, cfg)
    html = render(load_template(), data, n_idents)

    out_dir = Path(cfg.get("output_dir", "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "orras-dev_contribution_report-latest.html"
    dated = out_dir / f"orras-dev_contribution_report-{dt.date.today().isoformat()}.html"
    for f in (latest, dated):
        f.write_text(html, encoding="utf-8")

    named = sum(t["commits"] for t in data["totals"].values())
    print(f"Done: {named} commits by {sum(t['commits'] > 0 for t in data['totals'].values())} devs, "
          f"{data['merge_count_total']} PRs merged, {len(data['weeks'])} weeks")
    print(f"  -> {latest}\n  -> {dated}")

    if a.publish:
        cmd = cfg.get("publish_command", "").strip()
        if not cmd:
            sys.exit("--publish given but publish_command is empty in config")
        cmd = cmd.replace("{file}", shlex.quote(str(latest)))
        print(f"Publishing: {cmd}")
        subprocess.run(cmd, shell=True, check=True)


if __name__ == "__main__":
    main()
