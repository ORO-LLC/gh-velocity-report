#!/usr/bin/env python3
"""Generate deterministic, synthetic velocity data and render examples/demo.html.

No network, no gh, no git: this builds fake inputs with a seeded RNG and runs
them through the exact same aggregation the real collector uses
(collect.build_scopes), so the demo JSON validates against the real schema. The
data is obviously fake (octocat / example-org) and exercises every section:
multiple personas, discovered-email callout, a warning, Dependabot context,
releases, unmerged PRs, and three+ scope groups across two years (with one group
present in only one year, to exercise the scope fallback).

    python3 examples/make_demo.py
"""
import datetime as dt
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402
import build    # noqa: E402

DATA_DIR = HERE / "data"
DEMO_OUT = HERE / "demo.html"

CFG = {
    "display_name": "Octocat",
    "identities": {
        "logins": ["octocat", "octocat-work", "robocat"],
        "emails": ["mona@example.com", "octocat@users.noreply.github.com", "robo@example.com"],
        "personas": {
            "Octocat": {"logins": ["octocat", "octocat-work"],
                        "emails": ["mona@example.com", "octocat@users.noreply.github.com"]},
            "Robocat": {"logins": ["robocat"], "emails": ["robo@example.com"]},
        },
    },
    "orgs": ["example-org", "widgets-inc", "acme-labs", "globex"],
    "personal_owners": ["octocat"],
    "gh_accounts": ["octocat", "octocat-work"],
    "exclude_repos": [],
    "bot_suffixes": ["[bot]"],
    "timezone": "UTC",
    "classification": {
        "doc_ext": [".md", ".rst"], "doc_dirs": ["docs"],
        "excluded_dirs": ["node_modules", "dist"], "excluded_ext": [".map", ".lock"],
        "lockfiles": ["package-lock.json"],
    },
}

# repos per year -> (full_name, private, fork, archived). One group (widgets-inc)
# appears only in 2025.
REPOS = {
    2024: [
        ("example-org/web", False, False, False),
        ("example-org/api", True, False, False),
        ("acme-labs/pipeline", False, False, False),
        ("acme-labs/cli", False, False, False),
        ("globex/platform", True, False, False),
        ("octocat/dotfiles", False, False, False),
        ("opensource/tool", False, True, False),
    ],
    2025: [
        ("example-org/web", False, False, False),
        ("example-org/api", True, False, False),
        ("widgets-inc/widget", False, False, False),
        ("acme-labs/pipeline", False, False, False),
        ("acme-labs/cli", False, False, False),
        ("globex/platform", True, False, False),
        ("octocat/dotfiles", False, False, False),
        ("opensource/tool", False, True, False),
        ("opensource/archived-lib", False, False, True),
    ],
}

EMAILS = [("mona@example.com", "octocat"),
          ("octocat@users.noreply.github.com", "octocat"),
          ("robo@example.com", "robocat")]


def iso(year, doy, hour):
    d = dt.date(year, 1, 1) + dt.timedelta(days=doy)
    return f"{d.isoformat()}T{hour:02d}:30:00Z"


def gen_year(rng, year):
    commits_by_repo, login_presence = {}, {}
    items, reviews, releases = {}, {}, {}
    node = year * 100000
    for full, private, fork, archived in REPOS[year]:
        n = rng.randint(8, 40)
        commits, lp = [], {}
        for i in range(n):
            email, login = rng.choice(EMAILS)
            doy = rng.randint(0, 360)
            rec = {"sha": f"{full}-{year}-{i}", "email": email, "date": iso(year, doy, rng.randint(8, 20))}
            for k in collect.COMMIT_LINE_FIELDS:
                rec[k] = 0
            rec["code_add"] = rng.randint(0, 400)
            rec["code_del"] = rng.randint(0, 200)
            rec["docs_add"] = rng.randint(0, 120)
            rec["docs_del"] = rng.randint(0, 60)
            rec["code_files_add"] = rng.randint(0, 5)
            rec["docs_files_add"] = rng.randint(0, 3)
            commits.append(rec)
            lp[login] = lp.get(login, 0) + 1
        commits_by_repo[full] = {"private": private, "fork": fork, "archived": archived, "commits": commits}
        login_presence[full] = lp

        # PRs: opened + merged, plus a few closed-unmerged
        for j in range(rng.randint(3, 14)):
            node += 1
            doy = rng.randint(0, 360)
            created = iso(year, doy, 10)
            merged_doy = min(360, doy + rng.randint(0, 6))
            merged = iso(year, merged_doy, 15)
            items[str(node)] = {"repo": full, "number": node, "kind": "pr", "created_at": created,
                                "merged_at": merged, "closed_at": merged, "updated_at": merged,
                                "author": "octocat", "state": "closed", "flags": {"opened", "merged"}}
        for j in range(rng.randint(0, 3)):
            node += 1
            doy = rng.randint(0, 360)
            items[str(node)] = {"repo": full, "number": node, "kind": "pr",
                                "created_at": iso(year, doy, 9), "merged_at": None,
                                "closed_at": iso(year, doy, 12), "updated_at": iso(year, doy, 12),
                                "author": "octocat", "state": "closed", "flags": {"opened", "closed_unmerged"}}
        # issues: opened + closed
        for j in range(rng.randint(2, 10)):
            node += 1
            doy = rng.randint(0, 360)
            items[str(node)] = {"repo": full, "number": node, "kind": "issue",
                                "created_at": iso(year, doy, 8), "merged_at": None,
                                "closed_at": iso(year, min(360, doy + 2), 8), "updated_at": iso(year, doy, 8),
                                "author": "octocat", "state": "closed", "flags": {"opened", "closed"}}
        # reviews given (author is someone else) + one self-review (excluded)
        for j in range(rng.randint(1, 6)):
            node += 1
            doy = rng.randint(0, 360)
            reviews[str(node)] = {"repo": full, "number": node, "kind": "pr",
                                  "created_at": iso(year, doy, 11), "merged_at": None,
                                  "closed_at": None, "updated_at": iso(year, doy, 11),
                                  "author": "some-contributor", "state": "closed",
                                  "reviewer_logins": {"octocat"}}
        node += 1
        reviews[str(node)] = {"repo": full, "number": node, "kind": "pr",
                              "created_at": iso(year, 100, 11), "merged_at": None, "closed_at": None,
                              "updated_at": iso(year, 100, 11), "author": "octocat", "state": "closed",
                              "reviewer_logins": {"octocat-work"}}
        # releases on a couple of repos
        if rng.random() < 0.5:
            releases[full] = [{"published_at": iso(year, rng.randint(0, 360), 16), "login": "octocat"}
                              for _ in range(rng.randint(1, 3))]

    # Deterministic streak + busiest day in the newest year, so calendar
    # annotations (V6), YoY pills (V3) and the treemap (V7) always have a shape
    # to draw. A 10-day active-day run in June with one heavy day on top.
    if year == max(REPOS):
        streak_repo = "acme-labs/pipeline"
        bucket = commits_by_repo[streak_repo]["commits"]
        lp = login_presence[streak_repo]
        seq = 0
        for doy in range(160, 170):            # ten consecutive active days
            burst = 12 if doy == 164 else 1    # doy 164 -> busiest day of the year
            for _ in range(burst):
                seq += 1
                rec = {"sha": f"{streak_repo}-{year}-streak-{seq}",
                       "email": "mona@example.com", "date": iso(year, doy, 14)}
                for k in collect.COMMIT_LINE_FIELDS:
                    rec[k] = 0
                rec["code_add"] = 180
                rec["code_del"] = 60
                rec["docs_add"] = 24
                bucket.append(rec)
                lp["octocat"] = lp.get("octocat", 0) + 1
    return commits_by_repo, login_presence, items, reviews, releases


def build_meta(cfg, year, through, scopes, default_persona, login_persona):
    persona_names = {p["persona"] for p in scopes["all"]["by_persona"]} | set(login_persona.values())
    return {
        "year": year,
        "display_name": cfg.get("display_name"),
        "generated_at": f"{year}-12-31T12:00:00Z",
        "through": through.isoformat(),
        "timezone": cfg["timezone"],
        "tool_version": collect.TOOL_VERSION,
        "identities": {"logins": sorted(cfg["identities"]["logins"]),
                       "emails": sorted(e for e, _ in EMAILS),
                       "personas": sorted(persona_names)},
        "accounts_used": sorted(cfg["gh_accounts"]),
        "coverage": {"logins_not_logged_in": sorted(
            lg for lg in cfg["identities"]["logins"] if lg not in set(cfg["gh_accounts"]))},
        "repos_discovered": len(REPOS[year]) + 3,
        "repos_candidates": len(REPOS[year]) + 1,
        "repos_scanned_for_commits": len(REPOS[year]),
        "repos_scanned": len(REPOS[year]),
        "repos_with_activity": len(scopes["all"]["by_repo"]),
        "prior_year_present": year > min(REPOS),
        "no_lines": False,
        "runtime_seconds": 12.3,
        "search_calls": 128,
        "self_reviews_excluded": scopes["all"]["totals"].get("self_reviews_excluded", 0),
        # One explicit hex and one explicit palette slot so configured group
        # colours are visible in the demo (widgets-inc appears in 2025 only).
        "group_colors": {"widgets-inc": "#b5179e", "globex": "g6"},
        "discovered_emails": [{"email": "octocat@users.noreply.github.com", "commits": 7, "persona": "Octocat"}],
        "warnings": ["search failed [is:pr author:robocat created:%d-05-01..%d-05-31]: transient 502; re-run to fill it in"
                     % (year, year)],
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tz = collect.get_tz(CFG["timezone"])
    default_persona = collect.default_persona_name(CFG)
    login_persona, email_persona = collect.persona_maps(CFG)
    known_logins = set(CFG["identities"]["logins"])
    prior = None
    for year in sorted(REPOS):
        rng = random.Random(1000 + year)
        through = dt.date(year, 12, 31)
        commits_by_repo, login_presence, items, reviews, releases = gen_year(rng, year)
        n_active = len(set(commits_by_repo))
        dep_ctx = {"note": "Dependabot's own PRs across repos with your activity; NOT your PRs.",
                   "prs_created": 40 + year % 7, "prs_merged": 33 + year % 5,
                   "repos_scoped": n_active, "batches": 1}
        scopes = collect.build_scopes(CFG, year, through, tz, commits_by_repo, items, reviews, releases,
                                      login_presence, default_persona, email_persona, login_persona,
                                      known_logins, dep_ctx, prior)
        meta = build_meta(CFG, year, through, scopes, default_persona, login_persona)
        result = {"meta": meta, "scopes": scopes}
        (DATA_DIR / f"{year}.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
        prior = result
        groups = ", ".join(f"{g}={scopes[g]['totals']['commits']}" for g in scopes if g != "all")
        print(f"{year}: all commits={scopes['all']['totals']['commits']}, scopes: {groups}")

    rc = build.main(["--data-dir", str(DATA_DIR), "--out", str(DEMO_OUT)])
    if rc == 0:
        print(f"rendered {DEMO_OUT}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
