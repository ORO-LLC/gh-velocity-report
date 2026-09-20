#!/usr/bin/env python3
"""Build smoke tests: render synthetic data and check the output shape, the
Artifact fragment contract, the scope control, --redact-private, and that an
old-format JSON (no `scopes` key) still renders. Offline."""
import datetime as dt
import json
import os
import random
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples"))

import build
import collect
import make_demo


def write_demo(data_dir, years=(2024, 2025)):
    tz = collect.get_tz("UTC")
    login_p, email_p = collect.persona_maps(make_demo.CFG)
    known = set(make_demo.CFG["identities"]["logins"])
    prior = None
    for year in years:
        rng = random.Random(1000 + year)
        cbr, lp, items, reviews, rel = make_demo.gen_year(rng, year)
        dep = {"note": "x", "prs_created": 5, "prs_merged": 4, "repos_scoped": len(cbr), "batches": 1}
        scopes = collect.build_scopes(make_demo.CFG, year, dt.date(year, 12, 31), tz, cbr, items, reviews,
                                      rel, lp, "Octocat", email_p, login_p, known, dep, prior)
        meta = make_demo.build_meta(make_demo.CFG, year, dt.date(year, 12, 31), scopes, "Octocat", login_p)
        result = {"meta": meta, "scopes": scopes}
        (data_dir / f"{year}.json").write_text(json.dumps(result, default=str))
        prior = result


class BuildSmoke(unittest.TestCase):
    def test_outputs_and_fragment_contract(self):
        import pathlib
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            data_dir = td / "data"
            data_dir.mkdir()
            write_demo(data_dir)
            out = td / "out" / "velocity.html"
            self.assertEqual(build.main(["--data-dir", str(data_dir), "--out", str(out)]), 0)
            frag = out.with_name("velocity.artifact.html")
            self.assertTrue(out.exists())
            self.assertTrue(frag.exists())

            fragment = frag.read_text()
            self.assertTrue(fragment.startswith("<title>"))
            low = fragment.lower()
            # no document-structure tags (a <header> element is fine)
            for tag in ("<!doctype", "<html", "<head>", "<body>", "<body ", "<head "):
                self.assertNotIn(tag, low)
            self.assertNotIn("<script src=", low)

            doc = out.read_text()
            self.assertIn('id="scope"', doc)   # scope control renders
            self.assertIn('id="tabs"', doc)

            # new visual-pass controls are present in both outputs: the group
            # legend strip is static markup; the V4 metric toggle and V7 treemap
            # size toggle get their stable ids in the inline script.
            for marker in ('id="grouplegend"', 'id:"metric-toggle"', 'id:"treemap-metric"'):
                self.assertIn(marker, doc)
                self.assertIn(marker, fragment)

            # group colour tokens (V1) defined in the light block and BOTH dark
            # blocks (media query + data-theme override)
            self.assertIn("--g1: #2a78d6", doc)          # light slot 1
            self.assertEqual(doc.count("--g1: #3987e5"), 2)  # dark slot 1 in both dark blocks
            self.assertIn("--g8: #e34948", doc)          # full 8-hue palette present
            self.assertEqual(doc.count("--gother: #8b95a1"), 2)

    def test_redact_private_removes_private_names(self):
        import pathlib
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            data_dir = td / "data"
            data_dir.mkdir()
            write_demo(data_dir)
            out = td / "out" / "velocity.html"
            self.assertEqual(build.main(["--data-dir", str(data_dir), "--out", str(out),
                                         "--redact-private"]), 0)
            doc = out.read_text()
            frag = out.with_name("velocity.artifact.html").read_text()
            self.assertNotIn("example-org/api", doc)         # a private repo in the demo
            self.assertNotIn("example-org/api", frag)
            self.assertIn("private-repo-", doc)
            self.assertIn("example-org", doc)                # owner is NOT redacted

    def test_old_format_without_scopes_renders(self):
        import pathlib
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            data_dir = td / "data"
            data_dir.mkdir()
            # build a legacy file: the 'all' block flattened to top level, no `scopes`
            tz = collect.get_tz("UTC")
            login_p, email_p = collect.persona_maps(make_demo.CFG)
            rng = random.Random(4242)
            cbr, lp, items, reviews, rel = make_demo.gen_year(rng, 2025)
            dep = {"note": "x", "prs_created": 1, "prs_merged": 1, "repos_scoped": len(cbr), "batches": 1}
            scopes = collect.build_scopes(make_demo.CFG, 2025, dt.date(2025, 12, 31), tz, cbr, items,
                                          reviews, rel, lp, "Octocat", email_p, login_p,
                                          set(make_demo.CFG["identities"]["logins"]), dep, None)
            meta = make_demo.build_meta(make_demo.CFG, 2025, dt.date(2025, 12, 31), scopes, "Octocat", login_p)
            meta["year"] = 2099
            legacy = {"meta": meta}
            legacy.update(scopes["all"])   # top-level block, no `scopes` key
            (data_dir / "2099.json").write_text(json.dumps(legacy, default=str))
            out = td / "out" / "velocity.html"
            self.assertEqual(build.main(["--data-dir", str(data_dir), "--out", str(out)]), 0)
            doc = out.read_text()
            self.assertTrue(out.exists())
            self.assertIn('id="scope"', doc)
            self.assertIn("2099", doc)


if __name__ == "__main__":
    unittest.main()
