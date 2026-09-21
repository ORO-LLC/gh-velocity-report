#!/usr/bin/env python3
"""Collect one year's personal GitHub velocity into data/<year>.json.

Read-only against GitHub: every gh call goes through gh_read.py (GET REST and
mutation-free GraphQL only); git is used only for shallow read clones + log into
a cache outside any repo. Nothing remote is ever mutated. Python 3.9+ stdlib
only (no third-party dependencies).

Pipeline (documented fully in README.md):
  1. Discover repos: union of every configured org's repos, every personal
     owner's repos, and every repo in each logged-in account's
     contributionsCollection for the year.
  2. Expand identity emails with each login's two GitHub noreply variants.
  3. Cheap commit-presence check per repo via GraphQL history(totalCount),
     batched to keep queries small.
  4. Clone (shallow, cached) only repos with your commits; git log --numstat over
     the year window on the default branch, merges excluded; split code/docs/excluded.
  5. PRs / issues / reviews via the search API per login, split by month.
  6. Aggregate into scopes: the combined "all" scorecard plus one block per
     org / owner group, each fully recomputed (totals, streaks, cycle time,
     ratios, insights) from only that group's repos.

    python3 collect.py --year 2026
    python3 collect.py --year 2026 --repos example-org/repo-a,octocat/repo-b --no-lines
"""
import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import time

import gh_read

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

HERE = pathlib.Path(__file__).resolve().parent
TOOL_VERSION = "2.1.0"
DEFAULT_CACHE_ROOT = "~/.cache/velocity-report"
GIT_TIMEOUT = 600
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LOG_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%cI"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- config / tz ----------

def load_config(path):
    return json.loads(pathlib.Path(path).read_text())


GROUP_SLOT_RE = re.compile(r"^g[1-8]$")
GROUP_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def validate_group_colors(cfg, warnings):
    """Validate the optional config `group_colors` map (group name -> palette slot
    `g1`..`g8` or a hex colour). Returns the validated map for meta.group_colors;
    anything malformed is dropped with a warning rather than crashing. The neutral
    catch-all `other` cannot be recoloured. Hex values are lowercased; slots are
    kept verbatim. Contrast/derivation of hex values happens later, at build time."""
    raw = cfg.get("group_colors")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        warnings.append("config group_colors is not an object; ignoring it")
        return {}
    out = {}
    for name, val in raw.items():
        if not isinstance(name, str) or not name:
            warnings.append(f"group_colors: ignoring non-string group key {name!r}")
            continue
        if name == "other":
            warnings.append("group_colors: 'other' is neutral and cannot be recoloured; ignoring it")
            continue
        if not isinstance(val, str):
            warnings.append(f"group_colors: ignoring non-string value for group {name!r}")
            continue
        v = val.strip()
        if GROUP_SLOT_RE.match(v):
            out[name] = v
        elif GROUP_HEX_RE.match(v):
            out[name] = v.lower()
        else:
            warnings.append(f"group_colors: group {name!r} value {val!r} is not a palette "
                            f"slot (g1-g8) or hex colour; ignoring it")
    return out


def default_persona_name(cfg):
    """The single persona everything folds into when no personas are configured.
    Taken from config `display_name`, defaulting to "Me"."""
    name = (cfg.get("display_name") or "").strip()
    return name or "Me"


def resolve_cache_root(cli_value):
    """Cache dir precedence: --cache-dir > $VELOCITY_CACHE_DIR > ~/.cache/velocity-report."""
    val = cli_value or os.environ.get("VELOCITY_CACHE_DIR") or DEFAULT_CACHE_ROOT
    return pathlib.Path(os.path.expanduser(val))


def get_tz(name):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            log(f"warning: timezone {name!r} not found; falling back to US Eastern rule")
    return _EasternFallback()


class _EasternFallback(dt.tzinfo):
    """US Eastern with DST (2nd Sun Mar .. 1st Sun Nov), for the unlikely case
    zoneinfo is unavailable. Only correct for America/New_York."""
    def _dst_on(self, d):
        y = d.year
        mar = dt.date(y, 3, 8 + (6 - dt.date(y, 3, 1).weekday()) % 7)
        nov = dt.date(y, 11, 1 + (6 - dt.date(y, 11, 1).weekday()) % 7)
        return mar <= d.date() < nov if isinstance(d, dt.datetime) else mar <= d < nov

    def utcoffset(self, d):
        return dt.timedelta(hours=-4 if self._dst_on(d) else -5)

    def dst(self, d):
        return dt.timedelta(hours=1 if self._dst_on(d) else 0)

    def tzname(self, d):
        return "EDT" if self._dst_on(d) else "EST"


def parse_ts(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- caching of raw reads ----------

class Cache:
    def __init__(self, cache_root, year, refresh):
        self.dir = cache_root / "api" / str(year)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh

    def get_or(self, key, fn):
        h = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:32]
        path = self.dir / f"{h}.json"
        if not self.refresh and path.exists():
            try:
                return json.loads(path.read_text())["value"]
            except Exception:
                pass
        value = fn()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "value": value}))
        tmp.replace(path)
        return value


# ---------- path classification ----------

def make_classifier(rules):
    doc_ext = set(rules["doc_ext"])
    doc_dirs = set(d.lower() for d in rules["doc_dirs"])
    excl_dirs = set(d.lower() for d in rules["excluded_dirs"])
    excl_ext = set(rules["excluded_ext"])
    lockfiles = set(rules["lockfiles"])

    def normalise(path):
        path = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\2", path)
        if " => " in path:
            path = path.split(" => ")[-1]
        return path

    def classify(path):
        parts = [p for p in normalise(path).split("/") if p]
        if not parts:
            return "excluded"
        name = parts[-1]
        dirs = [p.lower() for p in parts[:-1]]
        ext = os.path.splitext(name)[1].lower()
        if any(d in excl_dirs for d in dirs):
            return "excluded"
        if name in lockfiles or ext in excl_ext:
            return "excluded"
        low = name.lower()
        if ".min." in low or ".generated." in low or "_generated." in low:
            return "excluded"
        if any(d in doc_dirs for d in dirs) or ext in doc_ext:
            return "docs"
        return "code"

    return classify


def normalise_email(email):
    """Lowercase and strip GitHub's numeric noreply prefix so
    "12345+octocat@users.noreply.github.com" == "octocat@users.noreply.github.com"."""
    e = (email or "").strip().lower()
    m = re.fullmatch(r"\d+\+(.+@users\.noreply\.github\.com)", e)
    return m.group(1) if m else e


def persona_maps(cfg):
    """login->persona and email->persona from config.identities.personas.
    Empty when no personas are configured (everything then folds to the default)."""
    personas = (cfg["identities"].get("personas") or {})
    login_p, email_p = {}, {}
    for name, spec in personas.items():
        for lg in spec.get("logins", []):
            login_p[lg] = name
        for em in spec.get("emails", []):
            email_p[normalise_email(em)] = name
    return login_p, email_p


# ---------- identity expansion ----------

def expand_identities(cfg, account, cache, default_persona):
    """Return (emails, login_ids, email_persona). Adds each login's two noreply
    email forms; assigns every email to a persona (login noreply forms take that
    login's persona, otherwise the default persona)."""
    login_p, email_p = persona_maps(cfg)
    emails = {normalise_email(e) for e in cfg["identities"]["emails"]}
    login_ids = {}
    for login in cfg["identities"]["logins"]:
        info = cache.get_or(["user", login], lambda l=login: _fetch_user(l, account))
        if not info:
            continue
        login_ids[login] = info
        for form in (f"{info['id']}+{login}@users.noreply.github.com",
                     f"{login}@users.noreply.github.com"):
            e = normalise_email(form)
            emails.add(e)
            email_p.setdefault(e, login_p.get(login, default_persona))
    for e in list(emails):
        email_p.setdefault(e, default_persona)
    return emails, login_ids, email_p


def _fetch_user(login, account):
    try:
        u = gh_read.rest_get(f"users/{login}", account)
        return {"id": u["id"], "node_id": u["node_id"], "login": u["login"]}
    except gh_read.GhReadError as e:
        log(f"  warning: users/{login} unreadable: {e}")
        return None


# ---------- repo discovery ----------

def discover_repos(cfg, warnings, cache):
    """Return ({full_name: repo dict}, add_fn). repo dict: owner, name, full_name,
    private, fork, archived, default_branch, pushed_at, sources (set)."""
    repos = {}

    def add(obj, source):
        fn = obj["full_name"]
        r = repos.get(fn)
        if r is None:
            owner, name = fn.split("/", 1)
            r = {"owner": owner, "name": name, "full_name": fn,
                 "private": obj.get("private", False), "fork": obj.get("fork", False),
                 "archived": obj.get("archived", False),
                 "default_branch": obj.get("default_branch") or "main",
                 "pushed_at": obj.get("pushed_at") or "", "sources": set()}
            repos[fn] = r
        r["sources"].add(source)
        # keep richest metadata when a later source has it
        for k in ("private", "fork", "archived"):
            if obj.get(k) is not None:
                r[k] = obj[k]
        if obj.get("default_branch"):
            r["default_branch"] = obj["default_branch"]
        if obj.get("pushed_at"):
            r["pushed_at"] = obj["pushed_at"]
        return r

    accounts = cfg["gh_accounts"]
    for org in cfg["orgs"]:
        try:
            items = cache.get_or(["org_repos", org], lambda o=org: gh_read.rest_get_all(
                f"orgs/{o}/repos", accounts[0], params={"per_page": "100", "type": "all"}))
            for o in items:
                add(o, f"org:{org}")
            log(f"  org {org}: {len(items)} repos")
        except gh_read.GhReadError as e:
            warnings.append(f"org repos for {org} unreadable: {e}")
    logged_in = set(accounts)
    for owner in cfg["personal_owners"]:
        try:
            if owner in logged_in:
                items = cache.get_or(["own_repos", owner], lambda o=owner: gh_read.rest_get_all(
                    "user/repos", o, params={"per_page": "100", "affiliation": "owner"}))
            else:
                items = cache.get_or(["user_repos", owner], lambda o=owner: gh_read.rest_get_all(
                    f"users/{o}/repos", accounts[0], params={"per_page": "100", "type": "owner"}))
            for o in items:
                add(o, f"owner:{owner}")
            log(f"  owner {owner}: {len(items)} repos")
        except gh_read.GhReadError as e:
            warnings.append(f"owned repos for {owner} unreadable: {e}")
    return repos, add


def contributed_repos(cfg, year, cache, warnings):
    """Per logged-in account, the repos in its contributionsCollection, with the
    per-repo commit totals. Returns (repo_meta_by_fullname, commit_totals[full][login])."""
    q = """query($login:String!,$from:DateTime!,$to:DateTime!){
      user(login:$login){contributionsCollection(from:$from,to:$to){
        commitContributionsByRepository(maxRepositories:100){repository{nameWithOwner isFork isPrivate isArchived defaultBranchRef{name} pushedAt} contributions{totalCount}}
        pullRequestContributionsByRepository(maxRepositories:100){repository{nameWithOwner isFork isPrivate isArchived defaultBranchRef{name} pushedAt}}
        issueContributionsByRepository(maxRepositories:100){repository{nameWithOwner isFork isPrivate isArchived defaultBranchRef{name} pushedAt}}
        pullRequestReviewContributionsByRepository(maxRepositories:100){repository{nameWithOwner isFork isPrivate isArchived defaultBranchRef{name} pushedAt}}
      }}}"""
    frm, to = f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z"
    meta, commit_totals = {}, collections.defaultdict(dict)

    def repo_obj(node):
        r = node["repository"]
        dbr = r.get("defaultBranchRef") or {}
        return {"full_name": r["nameWithOwner"], "fork": r.get("isFork", False),
                "private": r.get("isPrivate", False), "archived": r.get("isArchived", False),
                "default_branch": dbr.get("name") or "main", "pushed_at": r.get("pushedAt") or ""}

    for login in cfg["identities"]["logins"]:
        account = login if login in cfg["gh_accounts"] else cfg["gh_accounts"][0]
        try:
            data = cache.get_or(["contrib", login], lambda l=login, a=account: gh_read.graphql(
                q, {"login": l, "from": frm, "to": to}, a))
        except gh_read.GhReadError as e:
            warnings.append(f"contributionsCollection for {login} unreadable: {e}")
            continue
        cc = ((data or {}).get("user") or {}).get("contributionsCollection") or {}
        for key in ("commitContributionsByRepository", "pullRequestContributionsByRepository",
                    "issueContributionsByRepository", "pullRequestReviewContributionsByRepository"):
            for node in cc.get(key) or []:
                obj = repo_obj(node)
                meta.setdefault(obj["full_name"], obj)
                if key == "commitContributionsByRepository":
                    commit_totals[obj["full_name"]][login] = node["contributions"]["totalCount"]
        n = len(cc.get("commitContributionsByRepository") or [])
        log(f"  contrib {login}: {n} commit repos")
    return meta, commit_totals


# ---------- commit presence (GraphQL, batched) ----------

def presence_counts(repos, emails, year, account, cache, warnings, batch=25):
    """{full_name: totalCount of in-year default-branch commits by our emails}."""
    frm, to = f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z"
    email_list = sorted(emails)
    # gh -F cannot pass a JSON array to a GraphQL variable (it arrives as a
    # string), so the email list is inlined as a GraphQL array literal. Emails
    # come from config; JSON-encoding == GraphQL string encoding for these.
    emails_lit = json.dumps(email_list)
    out = {}
    items = list(repos.values())
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        aliases = []
        for j, r in enumerate(chunk):
            aliases.append(
                f'r{j}:repository(owner:"{r["owner"]}",name:"{r["name"]}"){{'
                f'defaultBranchRef{{target{{... on Commit{{'
                f'history(since:$from,until:$to,author:{{emails:{emails_lit}}}){{totalCount}}}}}}}}}}')
        query = "query($from:GitTimestamp!,$to:GitTimestamp!){" + " ".join(aliases) + "}"
        key = ["presence", [r["full_name"] for r in chunk], email_list]

        def fetch(query=query):
            return gh_read.graphql(query, {"from": frm, "to": to}, account)

        try:
            data = cache.get_or(key, fetch)
            for j, r in enumerate(chunk):
                node = (data or {}).get(f"r{j}") or {}
                tgt = ((node.get("defaultBranchRef") or {}).get("target") or {})
                hist = tgt.get("history") or {}
                out[r["full_name"]] = hist.get("totalCount", 0)
        except gh_read.GhReadError:
            # fall back to per-repo so one bad repo does not sink the batch
            for r in chunk:
                out[r["full_name"]] = _presence_one(r, email_list, frm, to, account, cache, warnings)
    return out


def _presence_one(r, email_list, frm, to, account, cache, warnings):
    emails_lit = json.dumps(email_list)
    query = ('query($from:GitTimestamp!,$to:GitTimestamp!){'
             f'repository(owner:"{r["owner"]}",name:"{r["name"]}"){{'
             'defaultBranchRef{target{... on Commit{'
             f'history(since:$from,until:$to,author:{{emails:{emails_lit}}}){{totalCount}}}}}}}}')
    try:
        data = cache.get_or(["presence1", r["full_name"], email_list],
                            lambda: gh_read.graphql(query, {"from": frm, "to": to}, account))
        node = (data or {}).get("repository") or {}
        return (((node.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {}).get("totalCount", 0)
    except gh_read.GhReadError as e:
        warnings.append(f"presence check failed for {r['full_name']}: {e}")
        return 0


def login_presence_counts(repos, login_ids, year, account, cache, warnings, batch=20):
    """{full_name: {login: totalCount}} of in-year default-branch commits GitHub
    attributes to each login (by user node id), regardless of commit email. This
    catches a repo whose only matching commits are under a known login with an
    unknown email."""
    frm, to = f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z"
    out = {r["full_name"]: {} for r in repos.values()}
    items = list(repos.values())
    for login, info in login_ids.items():
        nid = info["node_id"]
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            aliases = []
            for j, r in enumerate(chunk):
                aliases.append(
                    f'r{j}:repository(owner:"{r["owner"]}",name:"{r["name"]}"){{'
                    f'defaultBranchRef{{target{{... on Commit{{'
                    f'history(since:$from,until:$to,author:{{id:"{nid}"}}){{totalCount}}}}}}}}}}')
            query = "query($from:GitTimestamp!,$to:GitTimestamp!){" + " ".join(aliases) + "}"
            key = ["login_presence", login, [r["full_name"] for r in chunk]]

            def fetch(query=query):
                return gh_read.graphql(query, {"from": frm, "to": to}, account)
            try:
                data = cache.get_or(key, fetch)
                for j, r in enumerate(chunk):
                    node = (data or {}).get(f"r{j}") or {}
                    hist = (((node.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {})
                    ct = hist.get("totalCount", 0)
                    if ct:
                        out[r["full_name"]][login] = ct
            except gh_read.GhReadError as e:
                warnings.append(f"login presence failed for {login} batch: {e}")
    return out


# ---------- account resolution for a repo ----------

def readable_account(repo, cfg, cache):
    """First gh account (in preference order) that can read the repo; None if none."""
    def probe(acct):
        try:
            gh_read.rest_get(f"repos/{repo['full_name']}", acct, jq=".full_name")
            return True
        except gh_read.GhReadError:
            return False
    for acct in cfg["gh_accounts"]:
        ok = cache.get_or(["readable", repo["full_name"], acct], lambda a=acct: probe(a))
        if ok:
            return acct
    return None


# ---------- git scan ----------

def git_env(token):
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_TOKEN": token or ""}


def git_auth_args():
    helper = ("!f() { test \"$1\" = get && "
              "printf 'username=x-access-token\\npassword=%s\\n' \"$GH_TOKEN\"; }; f")
    return ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]


def git_run(args, cwd=None, token=None, timeout=GIT_TIMEOUT):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env=git_env(token), timeout=timeout)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def sync_repo(repo, token, year, cache_root):
    owner, name, branch = repo["owner"], repo["name"], repo["default_branch"]
    d = cache_root / owner / f"{name}.git"
    since = f"{year}-01-01"
    url = f"https://github.com/{owner}/{name}.git"
    auth = git_auth_args()

    def fresh():
        if d.exists():
            shutil.rmtree(d)
        d.parent.mkdir(parents=True, exist_ok=True)
        base = ["clone", "--quiet", "--no-checkout", "--no-tags", "--single-branch", "--branch", branch]
        try:
            git_run([*auth, *base, f"--shallow-since={since}", url, str(d)], token=token)
        except RuntimeError:
            if d.exists():
                shutil.rmtree(d)
            git_run([*auth, *base, "--depth=1", url, str(d)], token=token)

    if (d / "HEAD").exists() or (d / ".git").exists():
        try:
            git_run(["remote", "set-branches", "origin", branch], cwd=d, token=token)
            git_run([*auth, "fetch", "--quiet", "--no-tags", f"--shallow-since={since}", "origin"], cwd=d, token=token)
            git_run(["rev-parse", "--verify", "--quiet", f"origin/{branch}"], cwd=d, token=token)
        except RuntimeError:
            fresh()
    else:
        fresh()
    try:
        git_run([*auth, "fetch", "--quiet", "--no-tags", "--deepen=1", "origin"], cwd=d, token=token)
    except RuntimeError:
        pass
    # belt and braces: this cache clone can never push
    try:
        git_run(["remote", "set-url", "--push", "origin", "DISABLED"], cwd=d, token=token)
    except RuntimeError:
        pass
    return d, branch


def parse_log(text):
    commits = []
    for rec in text.split("\x1e"):
        if not rec.strip():
            continue
        lines = rec.split("\n")
        fields = lines[0].split("\x1f")
        if len(fields) < 5:
            continue
        files = []
        for line in lines[1:]:
            line = line.rstrip("\r")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            a, dd, path = parts[0], parts[1], "\t".join(parts[2:])
            try:
                files.append((None if a == "-" else int(a), None if dd == "-" else int(dd), path))
            except ValueError:
                continue
        commits.append({"sha": fields[0], "parents": fields[1].split(),
                        "author_name": fields[2], "author_email": fields[3],
                        "date": fields[4], "files": files})
    return commits


def parse_name_status(text):
    """{sha: [(status_letter, path)]} from `git log --name-status`. Stores only
    the status and path for A/D file counting; paths are never kept in output."""
    out = {}
    for rec in text.split("\x1e"):
        if not rec.strip():
            continue
        lines = rec.split("\n")
        fields = lines[0].split("\x1f")
        if len(fields) < 5:
            continue
        rows = []
        for line in lines[1:]:
            line = line.rstrip("\r")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0]:
                continue
            rows.append((parts[0][0], parts[-1]))
        out[fields[0]] = rows
    return out


COMMIT_FIELDS = ("code_add", "code_del", "docs_add", "docs_del", "excl_add", "excl_del",
                 "code_files_add", "code_files_del", "docs_files_add", "docs_files_del")


def scan_repo_git(repo, token, year, emails, classify, no_lines, cache_root):
    """Per-commit records for in-year commits authored by our emails, with line
    counts (numstat) and file add/remove counts (name-status)."""
    d, branch = sync_repo(repo, token, year, cache_root)
    ref = f"origin/{branch}"
    base = ["log", ref, "--no-merges",
            f"--since={year}-01-01 00:00:00 +0000", f"--until={year}-12-31 23:59:59 +0000",
            f"--format={LOG_FORMAT}"]
    text = git_run(base + (["--numstat"] if not no_lines else []), cwd=d, token=token)
    names = {}
    if not no_lines:
        names = parse_name_status(git_run(base + ["--name-status", "--diff-filter=AD"], cwd=d, token=token))
    shallow = pathlib.Path(git_run(["rev-parse", "--git-path", "shallow"], cwd=d, token=token).strip())
    if not shallow.is_absolute():
        shallow = d / shallow
    grafted = set(shallow.read_text().split()) if shallow.exists() else set()
    out = []
    for c in parse_log(text):
        if c["sha"] in grafted:
            continue
        if normalise_email(c["author_email"]) not in emails:
            continue
        rec = {"sha": c["sha"], "email": normalise_email(c["author_email"]), "date": c["date"]}
        for k in COMMIT_FIELDS:
            rec[k] = 0
        for added, deleted, path in c["files"]:
            if added is None or deleted is None:
                continue
            cls = classify(path)
            pfx = "excl" if cls == "excluded" else cls[:4]
            rec[f"{pfx}_add"] += added
            rec[f"{pfx}_del"] += deleted
        for status, path in names.get(c["sha"], []):
            cls = classify(path)
            if cls == "excluded":
                continue
            pfx = "docs" if cls == "docs" else "code"
            if status == "A":
                rec[f"{pfx}_files_add"] += 1
            elif status == "D":
                rec[f"{pfx}_files_del"] += 1
        out.append(rec)
    return out


# ---------- discovered emails (REST author cross-check) ----------

def discover_emails_for_repo(repo, logins, known_logins, year, account, cache):
    """Emails GitHub attributes to the given identity logins in this repo's
    in-year default-branch commits. Returns {email: {"count": n, "login": lg}}
    so the caller can assign a persona. REST calls are cached per (repo, login)."""
    frm, to = f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z"
    found = {}
    for login in logins:
        def fetch(login=login):
            return gh_read.rest_get_all(
                f"repos/{repo['full_name']}/commits", account,
                params={"author": login, "since": frm, "until": to, "per_page": "100"})
        try:
            items = cache.get_or(["author_commits", repo["full_name"], login, year], fetch)
        except gh_read.GhReadError:
            continue
        for it in items:
            gh_login = ((it.get("author") or {}) or {}).get("login")
            em = normalise_email(((it.get("commit") or {}).get("author") or {}).get("email"))
            if em and (gh_login in known_logins or login in known_logins):
                rec = found.setdefault(em, {"count": 0, "login": login})
                rec["count"] += 1
    return found


# ---------- search phase ----------

def month_windows(year, through):
    out = []
    for m in range(1, 13):
        start = dt.date(year, m, 1)
        nxt = dt.date(year + (m // 12), (m % 12) + 1, 1)
        end = nxt - dt.timedelta(days=1)
        if start > through:
            break
        out.append((m, start.isoformat(), min(end, through).isoformat()))
    return out


PROJECT = (".items | map({node_id, number, state, created_at, closed_at, updated_at, "
           "repo: .repository_url, user: (.user.login // null), merged_at: (.pull_request.merged_at // null)})")

# (kind, measure, query template). {L} = login, {R} = date range.
FAMILIES = [
    ("pr", "opened", "is:pr author:{L} created:{R}"),
    ("pr", "merged", "is:pr is:merged author:{L} merged:{R}"),
    ("pr", "closed_unmerged", "is:pr is:unmerged author:{L} closed:{R}"),
    ("issue", "opened", "is:issue author:{L} created:{R}"),
    ("issue", "closed", "is:issue is:closed author:{L} closed:{R}"),
]
REVIEW_TMPL = "is:pr reviewed-by:{L} updated:{R}"


def search_phase(cfg, year, through, searcher, warnings):
    """PRs/issues/reviews per login. A cheap full-year total_count probe per
    family skips the 12 monthly pages when a login has zero of that kind.
    Reviews keep self-reviews (author is one of our logins) so they can be
    excluded in aggregation; each review records its reviewer login(s)."""
    logins = cfg["identities"]["logins"]
    windows = month_windows(year, through)
    yr = f"{year}-01-01..{through.isoformat()}"
    items = {}          # node_id -> PR/issue record
    reviews = {}        # node_id -> review record (PR reviewed by us)
    for login in logins:
        active = []
        for kind, measure, tmpl in FAMILIES:
            try:
                c = searcher.count(tmpl.format(L=login, R=yr))
            except gh_read.GhReadError as e:
                warnings.append(f"probe failed [{tmpl.format(L=login, R=yr)}]: {e}")
                c = 0
            if c > 0:
                active.append((kind, measure, tmpl))
        try:
            rev_c = searcher.count(REVIEW_TMPL.format(L=login, R=yr))
        except gh_read.GhReadError as e:
            warnings.append(f"probe failed [review {login}]: {e}")
            rev_c = 0
        for m, a, b in windows:
            rng = f"{a}..{b}"
            for kind, measure, tmpl in active:
                q = tmpl.format(L=login, R=rng)
                try:
                    got = searcher.items(q, jq=PROJECT)
                except gh_read.GhReadError as e:
                    warnings.append(f"search failed [{q}]: {e}")
                    continue
                for it in got:
                    rec = items.setdefault(it["node_id"], _new_item(it, kind))
                    rec["flags"].add(measure)
            if rev_c > 0:
                q = REVIEW_TMPL.format(L=login, R=rng)
                try:
                    got = searcher.items(q, jq=PROJECT)
                except gh_read.GhReadError as e:
                    warnings.append(f"search failed [{q}]: {e}")
                    got = []
                for it in got:
                    rr = reviews.setdefault(it["node_id"], _new_item(it, "pr"))
                    rr.setdefault("reviewer_logins", set()).add(login)
        log(f"  search {login}: families {len(active)}/5, reviews={'y' if rev_c else 'n'} "
            f"-> {len(items)} items, {len(reviews)} reviews so far")
    return items, reviews, windows


def _new_item(it, kind):
    repo = (it.get("repo") or "").replace("https://api.github.com/repos/", "")
    return {"repo": repo, "number": it.get("number"), "kind": kind,
            "created_at": it.get("created_at"), "merged_at": it.get("merged_at"),
            "closed_at": it.get("closed_at"), "updated_at": it.get("updated_at"),
            "author": it.get("user"), "state": it.get("state"), "flags": set()}


# ---------- releases ----------

def collect_releases(active_repos, known_logins, year, cfg, cache, warnings):
    """{full_name: [{published_at, login}, ...]} for identity-authored, non-draft
    releases published in the year, over repos with activity only. A 404 or empty
    repo is zero, not a warning (releases are optional per repo)."""
    account0 = cfg["gh_accounts"][0]
    out = {}
    for full in sorted(active_repos):
        def fetch(f=full):
            return gh_read.rest_get_all(f"repos/{f}/releases", account0, params={"per_page": "100"})
        try:
            items = cache.get_or(["releases", full, year], fetch)
        except gh_read.GhReadError:
            out[full] = []
            continue
        rels = []
        for rel in items or []:
            if rel.get("draft"):
                continue
            login = (rel.get("author") or {}).get("login")
            pub = rel.get("published_at") or ""
            if login in known_logins and pub[:4] == str(year):
                rels.append({"published_at": pub, "login": login})
        out[full] = rels
    return out


# ---------- dependabot context (not the person's PRs) ----------

def dependabot_context(active_repos, year, searcher, warnings, max_calls=60):
    """Dependabot PRs created/merged in the year, scoped to repos with the
    person's activity. Batches repo: qualifiers under the search query-length
    limit. Returns (dict, calls_used) or (None, calls_needed) if over budget.
    This is an org-wide aggregate; it is reported only in the combined "all"
    scope, never split per group (that would need extra API calls)."""
    y = f"{year}-01-01..{year}-12-31"
    repos = sorted(active_repos)
    budget = 175  # chars for the repo: portion, leaving room for the prefix
    batches, cur, cur_len = [], [], 0
    for full in repos:
        tok = f"repo:{full} "
        if cur and cur_len + len(tok) > budget:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(full)
        cur_len += len(tok)
    if cur:
        batches.append(cur)
    calls_needed = len(batches) * 2
    if calls_needed > max_calls:
        return None, calls_needed
    created = merged = 0
    for b in batches:
        rq = " ".join(f"repo:{x}" for x in b)
        created += searcher.count(f"is:pr author:app/dependabot created:{y} {rq}")
        merged += searcher.count(f"is:pr author:app/dependabot is:merged merged:{y} {rq}")
    return ({"note": "Dependabot's own PRs across repos with your activity; NOT your PRs.",
             "prs_created": created, "prs_merged": merged,
             "repos_scoped": len(repos), "batches": len(batches)}, calls_needed)


# ---------- grouping ----------

def group_for(owner, cfg):
    if owner in cfg["orgs"] or owner in cfg["personal_owners"]:
        return owner
    return "other"


def scope_of(full_name, cfg):
    return group_for(full_name.split("/", 1)[0], cfg)


def day_hour(ts, tz):
    d = parse_ts(ts)
    if d is None:
        return None, None, None
    local = d.astimezone(tz)
    return local.date().isoformat(), local.weekday(), local.hour


SUM_FIELDS = ["commits", "code_add", "code_del", "docs_add", "docs_del", "excl_add", "excl_del",
              "code_files_add", "code_files_del", "docs_files_add", "docs_files_del",
              "prs_opened", "prs_merged", "prs_closed_unmerged",
              "issues_opened", "issues_closed", "reviews", "releases"]
MONTH_FIELDS = ["commits", "code_add", "code_del", "docs_add", "docs_del",
                "code_files_add", "code_files_del", "docs_files_add", "docs_files_del",
                "prs_opened", "prs_merged", "prs_closed_unmerged",
                "issues_opened", "issues_closed", "reviews", "releases"]
COMMIT_LINE_FIELDS = ["code_add", "code_del", "docs_add", "docs_del", "excl_add", "excl_del",
                      "code_files_add", "code_files_del", "docs_files_add", "docs_files_del"]
LOGIN_MEASURES = ("prs_opened", "prs_merged", "prs_closed_unmerged", "issues_opened", "issues_closed", "reviews")


def compute_by_org(rows, cfg):
    """Cross-scope comparison table: one row per group (each configured org/owner
    with activity, plus 'other'), summed from the given repo rows."""
    orgs = collections.OrderedDict()
    for r in rows:
        g = group_for(r["owner"], cfg)
        o = orgs.get(g)
        if o is None:
            o = dict(group=g, repos=0, **{k: 0 for k in SUM_FIELDS})
            orgs[g] = o
        o["repos"] += 1
        for k in SUM_FIELDS:
            o[k] += r[k]
    for o in orgs.values():
        o["code_net"] = o["code_add"] - o["code_del"]
        o["docs_net"] = o["docs_add"] - o["docs_del"]
    return list(orgs.values())


def aggregate(cfg, year, through, tz, commits_by_repo, items, reviews, releases_by_repo,
              login_presence, keep, is_all, default_persona, email_persona, login_persona,
              known_logins, prior_by_month, prior_year):
    """Build one scope's full block from the data already collected, restricted
    to the repos for which keep(full_name) is true. Everything is recomputed for
    that scope: totals, ratios, cycle time, streaks, active days, insights.

    Returns a dict with totals, insights, by_persona, by_repo, by_month,
    by_identity, calendar, by_weekday_hour, streaks, and (only when is_all)
    by_org. Context (Dependabot) is attached by the caller for the "all" scope."""

    def epersona(e):
        return email_persona.get(e, default_persona)

    def lpersona(lg):
        return login_persona.get(lg, default_persona)

    # login-attributed commit totals for this scope (per-login display rows)
    login_commit_totals = collections.Counter()
    for full in commits_by_repo:
        if not keep(full):
            continue
        for lg, c in login_presence.get(full, {}).items():
            login_commit_totals[lg] += c

    pers = {}  # persona name -> accumulator

    def pbucket(name):
        b = pers.get(name)
        if b is None:
            b = {"persona": name, "repos_set": set(), "logins": set(),
                 "emails": collections.Counter(), **{k: 0 for k in SUM_FIELDS}}
            pers[name] = b
        return b

    repo_rows = {}

    def row(full):
        if full not in repo_rows:
            owner, name = full.split("/", 1)
            base = {"owner": owner, "name": name, "full_name": full,
                    "private": None, "fork": None, "archived": None, "first": None, "last": None}
            for k in SUM_FIELDS:
                base[k] = 0
            repo_rows[full] = base
        return repo_rows[full]

    def touch(r, day):
        if day is None:
            return
        if r["first"] is None or day < r["first"]:
            r["first"] = day
        if r["last"] is None or day > r["last"]:
            r["last"] = day

    calendar = collections.defaultdict(lambda: {"commits": 0, "activity": 0})
    weekday_hour = [[0] * 24 for _ in range(7)]
    by_month = [dict(month=m + 1, **{k: 0 for k in MONTH_FIELDS}) for m in range(12)]
    by_identity_emails = collections.Counter()
    by_login = collections.defaultdict(lambda: collections.Counter())
    active_days = set()
    busiest = (None, 0)
    self_reviews = 0

    # commits / lines / files (persona by author email)
    for full, meta in commits_by_repo.items():
        if not keep(full):
            continue
        r = row(full)
        r["private"], r["fork"], r["archived"] = meta["private"], meta["fork"], meta["archived"]
        for c in meta["commits"]:
            day, wd, hr = day_hour(c["date"], tz)
            r["commits"] += 1
            pb = pbucket(epersona(c["email"]))
            pb["commits"] += 1
            pb["repos_set"].add(full)
            pb["emails"][c["email"]] += 1
            for k in COMMIT_LINE_FIELDS:
                r[k] += c[k]
                pb[k] += c[k]
            by_identity_emails[c["email"]] += 1
            touch(r, day)
            if day is not None:
                calendar[day]["commits"] += 1
                calendar[day]["activity"] += 1
                active_days.add(day)
                mi = parse_ts(c["date"]).astimezone(tz).month - 1
                by_month[mi]["commits"] += 1
                for k in ("code_add", "code_del", "docs_add", "docs_del",
                          "code_files_add", "code_files_del", "docs_files_add", "docs_files_del"):
                    by_month[mi][k] += c[k]
                weekday_hour[wd][hr] += 1

    # PRs / issues (persona by author login)
    def add_measure(r, author, key, ts):
        r[key] += 1
        by_login[author][key] += 1
        pb = pbucket(lpersona(author))
        pb[key] += 1
        pb["repos_set"].add(r["full_name"])
        _bucket(by_month, calendar, ts, tz, key)
        touch(r, day_hour(ts, tz)[0])

    for rec in items.values():
        full = rec["repo"]
        if not full or not keep(full):
            continue
        r = row(full)
        author = rec["author"]
        if "opened" in rec["flags"]:
            add_measure(r, author, "prs_opened" if rec["kind"] == "pr" else "issues_opened", rec["created_at"])
        if "merged" in rec["flags"] and rec["kind"] == "pr":
            add_measure(r, author, "prs_merged", rec["merged_at"])
        if "closed_unmerged" in rec["flags"] and rec["kind"] == "pr":
            add_measure(r, author, "prs_closed_unmerged", rec["closed_at"])
        if "closed" in rec["flags"] and rec["kind"] == "issue":
            add_measure(r, author, "issues_closed", rec["closed_at"])

    # reviews given: a PR reviewed by us and authored by someone outside our
    # logins. A review of our own accounts' PR is not a review given.
    for rec in reviews.values():
        full = rec["repo"]
        if not full or not keep(full):
            continue
        if rec.get("author") in known_logins:
            self_reviews += 1
            continue
        r = row(full)
        r["reviews"] += 1
        _bucket(by_month, calendar, rec["updated_at"], tz, "reviews")
        touch(r, day_hour(rec["updated_at"], tz)[0])
        for rl in rec.get("reviewer_logins", []) or []:
            by_login[rl]["reviews"] += 1
            pb = pbucket(lpersona(rl))
            pb["reviews"] += 1
            pb["repos_set"].add(full)

    # releases (persona by release author login)
    for full, rels in (releases_by_repo or {}).items():
        if not rels or not keep(full):
            continue
        r = row(full)
        for rel in rels:
            r["releases"] += 1
            _bucket(by_month, calendar, rel["published_at"], tz, "releases")
            touch(r, day_hour(rel["published_at"], tz)[0])
            pb = pbucket(lpersona(rel.get("login")))
            pb["releases"] += 1
            pb["repos_set"].add(full)

    # cycle time from merged PRs (scope-local)
    cyc = []
    for rec in items.values():
        if not keep(rec["repo"] or ""):
            continue
        if "merged" in rec["flags"] and rec["kind"] == "pr" and rec["merged_at"] and rec["created_at"]:
            hrs = (parse_ts(rec["merged_at"]) - parse_ts(rec["created_at"])).total_seconds() / 3600
            if hrs >= 0:
                cyc.append(hrs)
    cycle = {"median_hours": round(statistics.median(cyc), 1) if cyc else None,
             "p90_hours": round(_pct(cyc, 90), 1) if cyc else None, "population": len(cyc)}

    for day, v in calendar.items():
        if v["commits"] > busiest[1]:
            busiest = (day, v["commits"])

    rows = [r for r in repo_rows.values() if _has_activity(r)]
    for r in rows:
        r["code_net"] = r["code_add"] - r["code_del"]
        r["docs_net"] = r["docs_add"] - r["docs_del"]
    rows.sort(key=lambda r: (-r["commits"], r["full_name"]))

    org_list = compute_by_org(rows, cfg) if is_all else []

    # by_persona (combined, de-duplicated already via node ids / shas)
    for lg, name in login_persona.items():
        if name in pers:
            pers[name]["logins"].add(lg)
    for lg in by_login:
        if lg:
            pbucket(lpersona(lg))["logins"].add(lg)
    persona_list = []
    for name, b in pers.items():
        pd = {"persona": name, "repos": len(b["repos_set"])}
        for k in SUM_FIELDS:
            pd[k] = b[k]
        pd["code_net"] = pd["code_add"] - pd["code_del"]
        pd["docs_net"] = pd["docs_add"] - pd["docs_del"]
        lg_rows = []
        for lg in sorted(b["logins"]):
            lr = {"login": lg, "commits": login_commit_totals.get(lg, 0)}
            for k in LOGIN_MEASURES:
                lr[k] = by_login.get(lg, {}).get(k, 0)
            lg_rows.append(lr)
        pd["logins"] = sorted(lg_rows, key=lambda x: -(x["prs_merged"] + x["commits"]))
        pd["emails"] = [{"email": e, "commits": n} for e, n in b["emails"].most_common()]
        persona_list.append(pd)
    persona_list.sort(key=lambda x: -x["commits"])

    totals = {k: sum(r[k] for r in rows) for k in SUM_FIELDS}
    totals["code_net"] = totals["code_add"] - totals["code_del"]
    totals["docs_net"] = totals["docs_add"] - totals["docs_del"]
    totals["repos"] = len(rows)
    totals["orgs"] = len(set(group_for(r["owner"], cfg) for r in rows
                             if group_for(r["owner"], cfg) in cfg["orgs"]))
    totals["active_days"] = len(active_days)
    totals["self_reviews_excluded"] = self_reviews
    totals["cycle_time"] = cycle

    lines_all = totals["code_add"] + totals["code_del"] + totals["docs_add"] + totals["docs_del"]
    em = through.month  # elapsed months this year (12 for a full past year)
    totals["ratios"] = {
        "docs_share": round((totals["docs_add"] + totals["docs_del"]) / lines_all, 4) if lines_all else None,
        "prs_per_issue_closed": round(totals["prs_merged"] / totals["issues_closed"], 2) if totals["issues_closed"] else None,
        "merge_rate": round(totals["prs_merged"] / totals["prs_opened"], 4) if totals["prs_opened"] else None,
        "commits_per_active_day": round(totals["commits"] / len(active_days), 1) if active_days else None,
        "elapsed_months": em,
        "avg_issues_closed_per_month": round(totals["issues_closed"] / em, 1) if em else None,
        "avg_prs_merged_per_month": round(totals["prs_merged"] / em, 1) if em else None,
    }

    streaks = compute_streaks(sorted(active_days), through)
    streaks["busiest_day"] = {"date": busiest[0], "commits": busiest[1]}
    streaks["active_days"] = len(active_days)

    insights = build_insights(totals, rows, org_list, by_month, weekday_hour, streaks,
                              busiest, cycle, lines_all, through, is_all, prior_by_month, prior_year)

    id_emails = [{"email": e, "commits": n, "persona": epersona(e)} for e, n in by_identity_emails.most_common()]
    id_logins = [dict(login=lg, persona=lpersona(lg), commits=login_commit_totals.get(lg, 0),
                      **{k: by_login[lg].get(k, 0) for k in LOGIN_MEASURES})
                 for lg in by_login if lg]
    id_logins.sort(key=lambda x: -(x["prs_merged"] + x["commits"]))

    block = {
        "totals": totals,
        "insights": insights,
        "by_persona": persona_list,
        "by_repo": rows,
        "by_month": by_month,
        "by_identity": {"emails": id_emails, "logins": id_logins},
        "calendar": {d: calendar[d] for d in sorted(calendar)},
        "by_weekday_hour": weekday_hour,
        "streaks": streaks,
    }
    if is_all:
        block["by_org"] = org_list
    return block


def build_scopes(cfg, year, through, tz, commits_by_repo, items, reviews, releases_by_repo,
                 login_presence, default_persona, email_persona, login_persona, known_logins,
                 dep_ctx, prior):
    """Produce the scopes object: "all" (combined) plus one block per group that
    has activity. Every block is a full re-aggregation of the same collected data
    restricted to that group's repos; no additional GitHub calls are made."""

    def prior_by_month_for(scope):
        if not prior:
            return None, None
        py = (prior.get("meta") or {}).get("year")
        if "scopes" in prior:
            b = (prior.get("scopes") or {}).get(scope)
            return (b.get("by_month") if b else None), py
        # old-format prior file: only its top-level "all" block exists
        if scope == "all":
            return prior.get("by_month"), py
        return None, None

    scopes = {}
    pbm, py = prior_by_month_for("all")
    all_block = aggregate(cfg, year, through, tz, commits_by_repo, items, reviews, releases_by_repo,
                          login_presence, lambda f: True, True, default_persona, email_persona,
                          login_persona, known_logins, pbm, py)
    if dep_ctx is not None:
        all_block["context"] = {"dependabot": dep_ctx}
    else:
        all_block["context"] = {"dependabot": None}
    scopes["all"] = all_block

    groups = [o["group"] for o in all_block["by_org"]]
    for g in groups:
        pbm, py = prior_by_month_for(g)
        scopes[g] = aggregate(cfg, year, through, tz, commits_by_repo, items, reviews, releases_by_repo,
                              login_presence, (lambda f, g=g: scope_of(f, cfg) == g), False,
                              default_persona, email_persona, login_persona, known_logins, pbm, py)
    return scopes


WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def hours_phrase(h):
    if h is None:
        return None
    if h < 1:
        return f"{round(h * 60)} min"
    if h < 10:
        return f"{h:.1f} h"
    if h < 48:
        return f"{round(h)} h"
    return f"{h / 24:.1f} d"


def build_insights(totals, rows, org_list, by_month, weekday_hour, streaks, busiest,
                   cycle, lines_all, through, is_all, prior_by_month, prior_year):
    """Plain, reproducible sentences (computed here, not in the renderer). Zero-input
    sentences are skipped. The cross-group sentence is emitted only for the combined
    "all" scope; year-over-year compares the same scope's prior-year months."""
    def nf(n):
        return f"{n:,}"
    ins = []
    if any(m["prs_merged"] for m in by_month):
        bm = max(by_month, key=lambda m: m["prs_merged"])
        ins.append(f"Most PRs merged in a month: {MONTHS[bm['month'] - 1]} ({nf(bm['prs_merged'])}).")
    if any(m["commits"] for m in by_month):
        bc = max(by_month, key=lambda m: m["commits"])
        ins.append(f"Most commits in a month: {MONTHS[bc['month'] - 1]} ({nf(bc['commits'])}).")
    moved = [r for r in sorted(rows, key=lambda r: r["code_add"] + r["code_del"], reverse=True)
             if r["code_add"] + r["code_del"] > 0][:3]
    if moved:
        ins.append("Most code moved in " + ", ".join(
            f"{r['full_name']} ({nf(r['code_add'] + r['code_del'])} lines)" for r in moved) + ".")
    if lines_all:
        docs_lines = totals["docs_add"] + totals["docs_del"]
        ins.append(f"Docs were {round(100 * docs_lines / lines_all)}% of all lines changed "
                   f"({nf(docs_lines)} of {nf(lines_all)}).")
    if totals["issues_closed"]:
        ins.append(f"{round(totals['prs_merged'] / totals['issues_closed'], 2)} PRs merged per issue closed.")
    if busiest[0]:
        ins.append(f"Most commits in a day: {busiest[0]} ({nf(busiest[1])}).")
    wt = [sum(r) for r in weekday_hour]
    if any(wt):
        wi = max(range(7), key=lambda i: wt[i])
        ins.append(f"Most commits fall on {WEEKDAYS_LONG[wi]} ({nf(wt[wi])}).")
    if streaks.get("longest") and streaks.get("longest_start"):
        ins.append(f"Longest active-day streak: {nf(streaks['longest'])} days "
                   f"({streaks['longest_start']} to {streaks['longest_end']}).")
    if is_all and totals["commits"]:
        parts = sorted(((o["group"], o["commits"]) for o in org_list if o["commits"]),
                       key=lambda x: -x[1])[:4]
        ins.append("Commits by group: " + ", ".join(
            f"{g} {round(100 * c / totals['commits'])}%" for g, c in parts) + ".")
    if rows:
        mc = hours_phrase(cycle.get("median_hours"))
        ins.append(f"Contributed to {nf(len(rows))} repos"
                   + (f"; median PR merged in {mc}." if mc else "."))
    if prior_by_month and prior_year:
        em = through.month
        cur_c = sum(m["commits"] for m in by_month[:em])
        pri_c = sum(m["commits"] for m in prior_by_month[:em])
        if pri_c:
            pct = round(100 * (cur_c - pri_c) / pri_c)
            sign = "+" if pct >= 0 else ""
            ins.append(f"Commits {MONTHS[0]}–{MONTHS[em - 1]}: {nf(cur_c)} vs "
                       f"{nf(pri_c)} in {prior_year} ({sign}{pct}%, same months).")
    return ins


def _bucket(by_month, calendar, ts, tz, key):
    day, wd, hr = day_hour(ts, tz)
    if day is None:
        return
    calendar[day]["activity"] += 1
    try:
        mi = parse_ts(ts).astimezone(tz).month - 1
        by_month[mi][key] += 1
    except Exception:
        pass


def _has_activity(r):
    return any(r[k] for k in ("commits", "prs_opened", "prs_merged", "prs_closed_unmerged",
                              "issues_opened", "issues_closed", "reviews", "releases"))


def _pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def compute_streaks(days, through):
    if not days:
        return {"longest": 0, "current": 0, "longest_start": None, "longest_end": None}
    dates = [dt.date.fromisoformat(d) for d in days]
    best_len = cur = 1
    cur_start = best_start = best_end = dates[0]
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            cur += 1
        else:
            cur = 1
            cur_start = dates[i]
        if cur > best_len:
            best_len, best_start, best_end = cur, cur_start, dates[i]
    # current streak: consecutive days ending at `through`
    current = 0
    day = through
    dayset = set(dates)
    while day in dayset:
        current += 1
        day -= dt.timedelta(days=1)
    return {"longest": best_len, "current": current,
            "longest_start": best_start.isoformat(), "longest_end": best_end.isoformat()}


# ---------- main ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=None,
                    help="year to collect (default: current year in the configured timezone)")
    ap.add_argument("--config", default="./config.json",
                    help="path to the config file (default: ./config.json)")
    ap.add_argument("--repos", default=None,
                    help="comma-separated owner/name subset to scan, skipping discovery (for testing)")
    ap.add_argument("--no-lines", action="store_true",
                    help="skip git clones and line stats; commit counts come from the presence check")
    ap.add_argument("--jobs", type=int, default=6, help="git-scan concurrency (default: 6)")
    ap.add_argument("--data-dir", default=None,
                    help="directory for data/<year>.json output (default: ./data)")
    ap.add_argument("--out", default=None,
                    help="explicit output path, overriding --data-dir/<year>.json")
    ap.add_argument("--cache-dir", default=None,
                    help="clone/API cache dir (default: $VELOCITY_CACHE_DIR or ~/.cache/velocity-report)")
    ap.add_argument("--refresh", action="store_true",
                    help="bypass the API response cache and refetch")
    args = ap.parse_args(argv)

    for tool in ("git", "gh"):
        if not shutil.which(tool):
            log(f"error: required tool {tool!r} is not on PATH. Install it and ensure `gh` is logged in.")
            return 2

    if not pathlib.Path(args.config).exists():
        log(f"error: config not found at {args.config}. Run `python3 init_config.py` to create one, "
            f"or copy config.example.json to config.json and edit it.")
        return 2

    cfg = load_config(args.config)
    default_persona = default_persona_name(cfg)
    cache_root = resolve_cache_root(args.cache_dir)
    tz = get_tz(cfg["timezone"])
    now_local = dt.datetime.now(dt.timezone.utc).astimezone(tz)
    year = args.year or now_local.year
    through = min(dt.date(year, 12, 31), now_local.date())
    account0 = cfg["gh_accounts"][0]
    classify = make_classifier(cfg["classification"])
    warnings = []
    cache = Cache(cache_root, year, args.refresh)
    t0 = time.monotonic()
    log(f"year={year} through={through} tz={cfg['timezone']} account0={account0} "
        f"cache={cache_root} refresh={args.refresh}")

    emails, login_ids, email_persona = expand_identities(cfg, account0, cache, default_persona)
    login_persona, _ = persona_maps(cfg)
    known_logins = set(login_ids) | set(cfg["identities"]["logins"])
    log(f"identity emails: {len(emails)}; logins: {sorted(known_logins)}; default persona: {default_persona!r}")

    # discovery
    if args.repos:
        wanted = [r.strip() for r in args.repos.split(",") if r.strip()]
        repos = {}
        for full in wanted:
            try:
                o = gh_read.rest_get(f"repos/{full}", account0)
                repos[full] = {"owner": o["owner"]["login"], "name": o["name"], "full_name": o["full_name"],
                               "private": o["private"], "fork": o["fork"], "archived": o.get("archived", False),
                               "default_branch": o.get("default_branch") or "main",
                               "pushed_at": o.get("pushed_at") or "", "sources": {"cli"}}
            except gh_read.GhReadError as e:
                warnings.append(f"repo {full} unreadable: {e}")
        commit_totals = collections.defaultdict(dict)
    else:
        repos, add = discover_repos(cfg, warnings, cache)
        cmeta, commit_totals = contributed_repos(cfg, year, cache, warnings)
        for full, obj in cmeta.items():
            add(obj, "contrib")
    exclude = set(cfg.get("exclude_repos", []))
    for full in list(repos):
        if full in exclude:
            del repos[full]
    year_start = f"{year}-01-01"
    log(f"discovered {len(repos)} repos (before pruning)")

    # prune clearly-stale repos with no signal
    contrib_set = set(commit_totals)
    pruned = 0
    candidates = {}
    for full, r in repos.items():
        stale = r.get("pushed_at", "") and r["pushed_at"][:10] < year_start
        if stale and full not in contrib_set:
            pruned += 1
            continue
        candidates[full] = r
    log(f"pruned {pruned} stale repos; {len(candidates)} candidates for presence check")

    # login-based presence (GraphQL history by user node id)
    login_presence = login_presence_counts(candidates, login_ids, year, account0, cache, warnings)
    log(f"login-presence pass: {len(candidates)} repos x {len(login_ids)} logins")

    # Presence + login->email harvest, iterated so a newly discovered email can
    # reveal more repos (via email presence). Capped at 3 rounds.
    discovered = collections.Counter()
    discovered_persona = {}
    presence, to_scan = {}, {}
    for rnd in range(3):
        presence = presence_counts(candidates, emails, year, account0, cache, warnings)
        to_scan = {}
        for full, r in candidates.items():
            email_ct = presence.get(full, 0)
            lp = login_presence.get(full, {})
            login_ct = sum(lp.values())
            if email_ct > 0 or login_ct > 0:
                to_scan[full] = {"repo": r, "email_ct": email_ct, "login_ct": login_ct,
                                 "logins": [lg for lg, c in lp.items() if c]}
        new = 0
        for full, info in to_scan.items():
            logins_here = info["logins"] or list(commit_totals.get(full, {}).keys())
            if not logins_here:
                continue
            acct = readable_account(info["repo"], cfg, cache) or account0
            found = discover_emails_for_repo(info["repo"], logins_here, known_logins, year, acct, cache)
            for em, rec in found.items():
                if em not in emails:
                    persona = login_persona.get(rec["login"], default_persona)
                    emails.add(em)
                    email_persona.setdefault(em, persona)
                    discovered[em] += rec["count"]
                    discovered_persona[em] = persona
                    new += 1
        log(f"  harvest round {rnd + 1}: {len(to_scan)} to scan, {new} new emails")
        if new == 0:
            break
    log(f"repos with commits to scan: {len(to_scan)}")

    # git scan
    commits_by_repo = {}
    accounts_used = set([account0])
    if not args.no_lines:
        def do_scan(full, info):
            r = info["repo"]
            acct = readable_account(r, cfg, cache) or account0
            accounts_used.add(acct)
            token = gh_read.token_for(acct)
            try:
                commits = scan_repo_git(r, token, year, emails, classify, args.no_lines, cache_root)
                return full, {"private": r["private"], "fork": r["fork"], "archived": r["archived"],
                              "commits": commits}
            except Exception as e:
                warnings.append(f"git scan failed for {full}: {str(e)[:200]}")
                return full, None
        with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futs = [ex.submit(do_scan, full, info) for full, info in to_scan.items()]
            for fut in cf.as_completed(futs):
                full, res = fut.result()
                if res is not None:
                    commits_by_repo[full] = res
                    log(f"  git {full}: {len(res['commits'])} commits")
    else:
        for full, info in to_scan.items():
            r = info["repo"]
            n = max(info["email_ct"], info["login_ct"])
            commits_by_repo[full] = {"private": r["private"], "fork": r["fork"], "archived": r["archived"],
                                     "commits": [dict({"sha": f"noline{i}", "email": "", "date": None},
                                                      **{k: 0 for k in COMMIT_LINE_FIELDS})
                                                 for i in range(n)]}

    # search
    searcher = gh_read.Search(account0)
    items, reviews, windows = search_phase(cfg, year, through, searcher, warnings)
    log(f"search: {len(items)} pr/issue items, {len(reviews)} reviews, {searcher.calls} calls")

    # repos with the person's activity (commits, PRs/issues, or reviews)
    active_full = set(commits_by_repo)
    for rec in list(items.values()) + list(reviews.values()):
        if rec["repo"]:
            active_full.add(rec["repo"])

    # releases, over active repos only (identity-authored, non-draft, in year)
    releases_by_repo = collect_releases(active_full, known_logins, year, cfg, cache, warnings)
    rel_total = sum(len(v) for v in releases_by_repo.values())
    log(f"releases: {rel_total} identity-authored across {len(active_full)} active repos")

    # Dependabot context (NOT the person's PRs), scoped to active repos
    dep_ctx, dep_calls = dependabot_context(active_full, year, searcher, warnings)
    if dep_ctx is None:
        log(f"dependabot context skipped: would cost {dep_calls} search calls (> budget)")
    else:
        log(f"dependabot context: {dep_ctx['prs_created']} created / {dep_ctx['prs_merged']} merged "
            f"({dep_calls} search calls)")

    # output paths + prior year (same data dir) for year-over-year insight
    data_dir = pathlib.Path(args.data_dir) if args.data_dir else HERE / "data"
    out = pathlib.Path(args.out) if args.out else data_dir / f"{year}.json"
    prior_path = out.parent / f"{year - 1}.json"
    prior = None
    if prior_path.exists():
        try:
            prior = json.loads(prior_path.read_text())
        except Exception as e:
            warnings.append(f"prior-year {prior_path.name} unreadable for YoY: {e}")

    scopes = build_scopes(cfg, year, through, tz, commits_by_repo, items, reviews, releases_by_repo,
                          login_presence, default_persona, email_persona, login_persona, known_logins,
                          dep_ctx if dep_ctx is not None else {"skipped": True,
                          "would_cost_search_calls": dep_calls,
                          "note": "Dependabot context skipped to stay under the search-call budget."},
                          prior)

    all_totals = scopes["all"]["totals"]
    not_logged_in = sorted(lg for lg in cfg["identities"]["logins"] if lg not in set(cfg["gh_accounts"]))
    persona_names = {p["persona"] for p in scopes["all"]["by_persona"]} | set(login_persona.values())
    if not persona_names:
        persona_names = {default_persona}

    group_colors = validate_group_colors(cfg, warnings)

    meta = {
        "year": year,
        "display_name": cfg.get("display_name") or None,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "through": through.isoformat(),
        "timezone": cfg["timezone"],
        "tool_version": TOOL_VERSION,
        "identities": {"logins": sorted(known_logins), "emails": sorted(emails),
                       "personas": sorted(persona_names)},
        "accounts_used": sorted(accounts_used),
        "coverage": {"logins_not_logged_in": not_logged_in},
        "repos_discovered": len(repos),
        "repos_candidates": len(candidates),
        "repos_scanned_for_commits": len(to_scan),
        "repos_scanned": len(to_scan),
        "repos_with_activity": len(scopes["all"]["by_repo"]),
        "prior_year_present": prior is not None,
        "no_lines": args.no_lines,
        "runtime_seconds": round(time.monotonic() - t0, 1),
        "search_calls": searcher.calls,
        "self_reviews_excluded": all_totals.get("self_reviews_excluded", 0),
        "discovered_emails": [{"email": e, "commits": n, "persona": discovered_persona.get(e)}
                              for e, n in discovered.most_common()],
        "group_colors": group_colors,
        "warnings": warnings,
    }
    result = {"meta": meta, "scopes": scopes}

    # size guard: if a year's embedded blocks get large, drop per-scope email
    # detail from the group scopes first (the combined "all" scope keeps it).
    payload = json.dumps(result, indent=1, default=str)
    if len(payload) > 3_000_000:
        for g, block in scopes.items():
            if g != "all" and "by_identity" in block:
                block["by_identity"]["emails"] = []
        meta["notes"] = "Per-scope email detail dropped from group scopes to keep the page small."
        payload = json.dumps(result, indent=1, default=str)
        log(f"note: dropped per-scope email detail; year JSON is {len(payload):,} bytes")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(payload + "\n")
    tmp.replace(out)

    # write discovered emails back into the config actually in use
    if discovered and not args.repos:
        added = merge_discovered_into_config(args.config, discovered, discovered_persona)
        if added:
            log(f"  wrote {len(added)} discovered emails into {args.config}: {', '.join(added)}")

    _print_summary(year, through, scopes, discovered, warnings, out)
    return 0


def _print_summary(year, through, scopes, discovered, warnings, out):
    t = scopes["all"]["totals"]
    log("")
    log(f"=== {year} personal velocity (through {through}) ===")
    log(f"  commits {t['commits']:,}  code +{t['code_add']:,}/-{t['code_del']:,} (net {t['code_net']:+,})  "
        f"docs +{t['docs_add']:,}/-{t['docs_del']:,} (net {t['docs_net']:+,})")
    log(f"  PRs {t['prs_opened']:,} opened / {t['prs_merged']:,} merged / {t['prs_closed_unmerged']:,} closed-unmerged   "
        f"issues {t['issues_opened']:,} opened / {t['issues_closed']:,} closed   reviews {t['reviews']:,}   releases {t['releases']:,}")
    log(f"  repos {t['repos']:,}  orgs {t['orgs']}  active days {t['active_days']}  streak {scopes['all']['streaks']['longest']}")
    log("  by scope (commits):")
    for g in [k for k in scopes if k != "all"]:
        log(f"    {g}: {scopes[g]['totals']['commits']:,}")
    if discovered:
        log(f"  discovered emails: {dict(discovered)}")
    if warnings:
        log(f"  warnings: {len(warnings)}")
    log(f"wrote {out}")


def merge_discovered_into_config(config_path, discovered, discovered_persona):
    """Add newly discovered emails to config.identities.emails and to the matching
    persona's email list. Returns the list of emails actually added."""
    path = pathlib.Path(config_path)
    try:
        cfg = json.loads(path.read_text())
    except Exception:
        return []
    ids = cfg.setdefault("identities", {})
    existing = {normalise_email(e) for e in ids.get("emails", [])}
    personas = ids.get("personas") or {}
    added = []
    for em in discovered:
        if em in existing:
            continue
        ids.setdefault("emails", []).append(em)
        existing.add(em)
        added.append(em)
        pname = discovered_persona.get(em)
        if pname and pname in personas:
            personas[pname].setdefault("emails", []).append(em)
    if added:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        tmp.replace(path)
    return added


if __name__ == "__main__":
    sys.exit(main())
