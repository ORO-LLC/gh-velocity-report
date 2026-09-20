#!/usr/bin/env python3
"""Assert the demo JSON (built via the real aggregation path) and a tiny hand-made
collector-shaped dict carry every key build.py reads. Offline."""
import datetime as dt
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples"))

import collect
import make_demo

BLOCK_KEYS = {"totals", "insights", "by_persona", "by_repo", "by_month",
              "by_identity", "calendar", "by_weekday_hour", "streaks"}
TOTALS_KEYS = {"commits", "prs_opened", "prs_merged", "prs_closed_unmerged", "issues_opened",
               "issues_closed", "reviews", "self_reviews_excluded", "releases", "repos", "orgs",
               "active_days", "code_add", "code_del", "code_net", "docs_add", "docs_del", "docs_net",
               "docs_files_add", "docs_files_del", "cycle_time", "ratios"}
RATIO_KEYS = {"merge_rate", "commits_per_active_day", "avg_issues_closed_per_month",
              "avg_prs_merged_per_month", "elapsed_months"}
MONTH_KEYS = {"month", "commits", "prs_opened", "prs_merged", "prs_closed_unmerged", "issues_opened",
              "issues_closed", "reviews", "releases", "code_add", "code_del", "docs_add", "docs_del",
              "docs_files_add", "docs_files_del"}
REPO_KEYS = {"full_name", "owner", "name", "private", "fork", "archived", "commits", "code_add",
             "code_del", "docs_add", "docs_del", "code_files_add", "code_files_del", "docs_files_add",
             "docs_files_del", "prs_opened", "prs_merged", "prs_closed_unmerged", "issues_opened",
             "issues_closed", "reviews", "releases", "first", "last"}
META_KEYS = {"year", "through", "timezone", "tool_version", "generated_at", "identities",
             "accounts_used", "repos_discovered", "repos_candidates", "repos_with_activity",
             "search_calls", "runtime_seconds", "discovered_emails", "warnings"}


def demo_result(year=2025):
    rng = random.Random(1000 + year)
    tz = collect.get_tz("UTC")
    login_p, email_p = collect.persona_maps(make_demo.CFG)
    cbr, lp, items, reviews, rel = make_demo.gen_year(rng, year)
    dep = {"note": "x", "prs_created": 1, "prs_merged": 1, "repos_scoped": len(cbr), "batches": 1}
    scopes = collect.build_scopes(make_demo.CFG, year, dt.date(year, 12, 31), tz, cbr, items, reviews,
                                  rel, lp, "Octocat", email_p, login_p,
                                  set(make_demo.CFG["identities"]["logins"]), dep, None)
    meta = make_demo.build_meta(make_demo.CFG, year, dt.date(year, 12, 31), scopes, "Octocat", login_p)
    return {"meta": meta, "scopes": scopes}


class DemoSchema(unittest.TestCase):
    def test_demo_blocks_have_keys(self):
        res = demo_result()
        self.assertIn("all", res["scopes"])
        self.assertTrue(META_KEYS.issubset(res["meta"]), META_KEYS - set(res["meta"]))
        for name, block in res["scopes"].items():
            self.assertTrue(BLOCK_KEYS.issubset(block), (name, BLOCK_KEYS - set(block)))
            self.assertTrue(TOTALS_KEYS.issubset(block["totals"]), (name, TOTALS_KEYS - set(block["totals"])))
            self.assertTrue(RATIO_KEYS.issubset(block["totals"]["ratios"]))
            self.assertTrue(MONTH_KEYS.issubset(block["by_month"][0]))
            if block["by_repo"]:
                self.assertTrue(REPO_KEYS.issubset(block["by_repo"][0]),
                                (name, REPO_KEYS - set(block["by_repo"][0])))
        self.assertIn("by_org", res["scopes"]["all"])
        self.assertIn("context", res["scopes"]["all"])

    def test_group_present_in_only_one_year(self):
        # widgets-inc exists in 2025 but not 2024 (scope-fallback fixture)
        self.assertIn("widgets-inc", demo_result(2025)["scopes"])
        self.assertNotIn("widgets-inc", demo_result(2024)["scopes"])


class TinySyntheticSchema(unittest.TestCase):
    def test_hand_dict_has_keys(self):
        tiny = {
            "meta": {k: None for k in META_KEYS},
            "scopes": {"all": {
                "totals": {**{k: 0 for k in TOTALS_KEYS},
                           "cycle_time": {"median_hours": None, "p90_hours": None, "population": 0},
                           "ratios": {k: 0 for k in RATIO_KEYS}},
                "insights": [],
                "by_persona": [],
                "by_org": [],
                "by_repo": [],
                "by_month": [{k: 0 for k in MONTH_KEYS} for _ in range(12)],
                "by_identity": {"emails": [], "logins": []},
                "calendar": {},
                "by_weekday_hour": [[0] * 24 for _ in range(7)],
                "streaks": {"longest": 0, "current": 0, "longest_start": None, "longest_end": None,
                            "busiest_day": {"date": None, "commits": 0}},
                "context": {"dependabot": None},
            }},
        }
        block = tiny["scopes"]["all"]
        self.assertTrue(BLOCK_KEYS.issubset(block))
        self.assertTrue(TOTALS_KEYS.issubset(block["totals"]))
        self.assertTrue(META_KEYS.issubset(tiny["meta"]))


if __name__ == "__main__":
    unittest.main()
