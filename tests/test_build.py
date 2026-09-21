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

            # count-axis step is clamped to >= 1 so a low peak (max 1/2) cannot
            # produce duplicate integer tick labels at distinct gridlines
            self.assertIn("Math.max(1, niceNum(range/4,true))", doc)
            self.assertIn("Math.max(1, niceNum(range/4,true))", fragment)

    def test_control_row_pins_with_env_top_and_opaque_bg(self):
        # The compact control row (year tabs + scope chips) pins to the top while
        # scrolling: sticky, offset by the safe-area inset (env()), with an opaque
        # token background and a bottom hairline, so it stays legible over content
        # scrolling under it. Regression guard for the "controls vanish on scroll"
        # cause: the row must live outside <header> (a short containing block).
        import pathlib
        import re
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            data_dir = td / "data"
            data_dir.mkdir()
            write_demo(data_dir)
            out = td / "out" / "velocity.html"
            self.assertEqual(build.main(["--data-dir", str(data_dir), "--out", str(out)]), 0)
            doc = out.read_text()
            frag = out.with_name("velocity.artifact.html").read_text()
            for text in (doc, frag):
                # the .controls rule is sticky, offset by env(safe-area-inset-top),
                # painted on an opaque token background with a bottom hairline
                m = re.search(r"\.controls\s*\{([^}]*)\}", text)
                self.assertIsNotNone(m, "no .controls rule in emitted CSS")
                rule = m.group(1)
                self.assertIn("position: sticky", rule)
                self.assertIn("top: env(safe-area-inset-top, 0px)", rule)
                self.assertIn("background: var(--plane)", rule)   # opaque token
                self.assertIn("border-bottom: 1px solid var(--rule)", rule)
                # the control row is a sibling of <header>, not nested inside it,
                # so its containing block is the full-height page wrap and it pins
                # for the whole scroll rather than only within the header box
                header = re.search(r"<header>.*?</header>", text, re.S)
                self.assertIsNotNone(header)
                self.assertNotIn('class="controls"', header.group(0))
                self.assertIn('<div class="controls" id="controls">', text)
                # anchored sections clear the pinned bar on deep-link scroll
                self.assertIn("scroll-margin-top: calc(env(safe-area-inset-top, 0px)", text)

    def test_sparkline_end_dot_is_css_overlay_and_dead_rule_gone(self):
        # #2: the sparkline end dot is a CSS-positioned overlay (round at any tile
        # width under preserveAspectRatio="none"), not an in-SVG <circle> that the
        # non-uniform x-scale would stretch. #4: the dead `.tile .v.lead-num` rule
        # is removed.
        import pathlib
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            data_dir = td / "data"
            data_dir.mkdir()
            write_demo(data_dir)
            out = td / "out" / "velocity.html"
            self.assertEqual(build.main(["--data-dir", str(data_dir), "--out", str(out)]), 0)
            doc = out.read_text()
            frag = out.with_name("velocity.artifact.html").read_text()
            for text in (doc, frag):
                # #2 — CSS overlay dot + relative wrapper in the stylesheet,
                # emitted by the sparkline builder in the inline script
                self.assertIn(".spark-dot {", text)
                self.assertIn(".spark-wrap {", text)
                self.assertIn('class:"spark-dot"', text)
                self.assertIn('el("span",{class:"spark-wrap"}', text)
                # #4 — dead rule gone
                self.assertNotIn("lead-num", text)

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
            # legacy files derive repo groups from the "all" block; unconfigured
            # owners collapse to "other" instead of the owner-fallback that
            # mislabels them (repoGroupMap legacy branch; group-of falls back to
            # "other", never r.owner).
            self.assertIn("recognised.has(r.owner)", doc)
            self.assertNotIn("||r.owner", doc.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
