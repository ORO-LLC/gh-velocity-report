#!/usr/bin/env python3
"""Offline unit tests for the pure functions in collect.py and for per-scope
re-aggregation. No network, no gh, no git."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect

RULES = {
    "doc_ext": [".md", ".rst"], "doc_dirs": ["docs", "doc"],
    "excluded_dirs": ["node_modules", "dist", "vendor"], "excluded_ext": [".map", ".lock"],
    "lockfiles": ["package-lock.json", "yarn.lock"],
}

CFG = {
    "display_name": "Me",
    "identities": {"logins": ["octocat"], "emails": ["mona@example.com"]},
    "orgs": ["example-org"], "personal_owners": ["octocat"], "gh_accounts": ["octocat"],
    "exclude_repos": [], "bot_suffixes": ["[bot]"], "timezone": "UTC", "classification": RULES,
}


def commit(email, date_iso, code_add=0, code_del=0, docs_add=0, docs_del=0):
    rec = {"sha": email + date_iso, "email": email, "date": date_iso}
    for k in collect.COMMIT_LINE_FIELDS:
        rec[k] = 0
    rec["code_add"], rec["code_del"], rec["docs_add"], rec["docs_del"] = code_add, code_del, docs_add, docs_del
    return rec


class PathClassification(unittest.TestCase):
    def setUp(self):
        self.classify = collect.make_classifier(RULES)

    def test_code_docs_excluded(self):
        self.assertEqual(self.classify("src/app.js"), "code")
        self.assertEqual(self.classify("docs/guide.txt"), "docs")     # doc dir
        self.assertEqual(self.classify("README.md"), "docs")          # doc ext
        self.assertEqual(self.classify("node_modules/x/index.js"), "excluded")
        self.assertEqual(self.classify("package-lock.json"), "excluded")
        self.assertEqual(self.classify("bundle.min.js"), "excluded")
        self.assertEqual(self.classify("assets/app.map"), "excluded")

    def test_rename_normalisation(self):
        # a git rename "{old => new}/file.js" classifies by the new path
        self.assertEqual(self.classify("src/{a => b}/thing.js"), "code")
        self.assertEqual(self.classify("old/name.js => docs/name.md"), "docs")


class EmailMatching(unittest.TestCase):
    def test_noreply_prefix_stripped(self):
        self.assertEqual(collect.normalise_email("123456+Octocat@users.noreply.github.com"),
                         "octocat@users.noreply.github.com")

    def test_lowercase_and_trim(self):
        self.assertEqual(collect.normalise_email("  Mona@Example.COM "), "mona@example.com")

    def test_plain_noreply_unchanged(self):
        self.assertEqual(collect.normalise_email("octocat@users.noreply.github.com"),
                         "octocat@users.noreply.github.com")


class MonthWindows(unittest.TestCase):
    def test_full_year(self):
        w = collect.month_windows(2025, dt.date(2025, 12, 31))
        self.assertEqual(len(w), 12)
        self.assertEqual(w[0][1], "2025-01-01")
        self.assertEqual(w[11][2], "2025-12-31")

    def test_partial_year_clipped(self):
        w = collect.month_windows(2025, dt.date(2025, 3, 15))
        self.assertEqual(len(w), 3)
        self.assertEqual(w[-1][2], "2025-03-15")


class Streaks(unittest.TestCase):
    def test_longest_and_current(self):
        days = ["2025-03-01", "2025-03-02", "2025-03-03", "2025-06-10"]
        s = collect.compute_streaks(days, dt.date(2025, 6, 10))
        self.assertEqual(s["longest"], 3)
        self.assertEqual(s["longest_start"], "2025-03-01")
        self.assertEqual(s["longest_end"], "2025-03-03")
        self.assertEqual(s["current"], 1)

    def test_empty(self):
        s = collect.compute_streaks([], dt.date(2025, 1, 1))
        self.assertEqual(s["longest"], 0)
        self.assertIsNone(s["longest_start"])


class InsightsZeroInput(unittest.TestCase):
    def test_all_sentences_skipped(self):
        totals = {"docs_add": 0, "docs_del": 0, "issues_closed": 0, "prs_merged": 0, "commits": 0}
        by_month = [dict(month=m + 1, commits=0, prs_merged=0, issues_closed=0) for m in range(12)]
        wh = [[0] * 24 for _ in range(7)]
        streaks = {"longest": 0}
        cycle = {"median_hours": None}
        out = collect.build_insights(totals, [], [], by_month, wh, streaks, (None, 0),
                                     cycle, 0, dt.date(2025, 12, 31), True, None, None)
        self.assertEqual(out, [])


class PerScopeAggregation(unittest.TestCase):
    def _scopes(self):
        tz = collect.get_tz("UTC")
        login_p, email_p = collect.persona_maps(CFG)
        commits_by_repo = {
            "example-org/a": {"private": False, "fork": False, "archived": False, "commits": [
                commit("mona@example.com", "2025-03-01T10:00:00Z", code_add=100, code_del=10),
                commit("mona@example.com", "2025-03-02T10:00:00Z", code_add=50, docs_add=20),
                commit("mona@example.com", "2025-03-03T10:00:00Z", code_add=30),
            ]},
            "octocat/b": {"private": False, "fork": False, "archived": False, "commits": [
                commit("mona@example.com", "2025-06-10T10:00:00Z", code_add=5, docs_add=200),
            ]},
        }
        login_presence = {"example-org/a": {"octocat": 3}, "octocat/b": {"octocat": 1}}
        items = {
            "1": {"repo": "example-org/a", "number": 1, "kind": "pr", "created_at": "2025-03-01T09:00:00Z",
                  "merged_at": "2025-03-01T12:00:00Z", "closed_at": "2025-03-01T12:00:00Z",
                  "updated_at": "2025-03-01T12:00:00Z", "author": "octocat", "state": "closed",
                  "flags": {"opened", "merged"}},
            "2": {"repo": "octocat/b", "number": 2, "kind": "issue", "created_at": "2025-06-10T08:00:00Z",
                  "merged_at": None, "closed_at": "2025-06-11T08:00:00Z", "updated_at": "2025-06-10T08:00:00Z",
                  "author": "octocat", "state": "closed", "flags": {"opened", "closed"}},
        }
        releases = {"example-org/a": [{"published_at": "2025-03-02T15:00:00Z", "login": "octocat"}]}
        return collect.build_scopes(CFG, 2025, dt.date(2025, 12, 31), tz, commits_by_repo, items, {},
                                    releases, login_presence, "Me", email_p, login_p, {"octocat"}, None, None)

    def test_streaks_and_active_days_per_scope(self):
        s = self._scopes()
        self.assertEqual(s["all"]["streaks"]["longest"], 3)
        self.assertEqual(s["all"]["totals"]["active_days"], 4)
        self.assertEqual(s["example-org"]["streaks"]["longest"], 3)
        self.assertEqual(s["example-org"]["totals"]["active_days"], 3)
        self.assertEqual(s["octocat"]["streaks"]["longest"], 1)
        self.assertEqual(s["octocat"]["totals"]["active_days"], 1)

    def test_all_equals_sum_of_groups(self):
        s = self._scopes()
        groups = [g for g in s if g != "all"]
        for k in ("commits", "prs_merged", "issues_closed", "releases",
                  "code_add", "code_del", "docs_add", "docs_del", "repos"):
            self.assertEqual(s["all"]["totals"][k], sum(s[g]["totals"][k] for g in groups), k)

    def test_group_scope_has_no_cross_group_sentence(self):
        s = self._scopes()
        joined = " ".join(s["example-org"]["insights"])
        self.assertNotIn("Commits by group", joined)
        self.assertTrue(any("Commits by group" in x for x in s["all"]["insights"]))


if __name__ == "__main__":
    unittest.main()
