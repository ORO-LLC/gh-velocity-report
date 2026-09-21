#!/usr/bin/env python3
"""Offline tests for configurable group colours (issue #6): the Python colour
resolver and hex-token derivation, plus the emitted page. No network, stdlib only.

Fixtures use fake group names only (octocat / example-org / widgets-inc / acme-labs
/ globex). The resolver reads only meta.group_colors and each scope's commit
total, so the year dicts here are deliberately minimal."""
import datetime as dt
import json
import os
import pathlib
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


def year(y, groups, gc=None):
    """A minimal year dict: `groups` is a list of (name, commits)."""
    scopes = {"all": {"totals": {"commits": sum(c for _, c in groups)}}}
    for name, c in groups:
        scopes[name] = {"totals": {"commits": c}}
    meta = {"year": y}
    if gc is not None:
        meta["group_colors"] = gc
    return {"meta": meta, "scopes": scopes}


def resolve(years, cli=None):
    warns = []
    gmap, hexes = build.resolve_group_colors(years, cli, warns.append)
    return gmap, hexes, warns


class Resolver(unittest.TestCase):
    def test_explicit_slot(self):
        years = {2025: year(2025, [("example-org", 50), ("acme-labs", 40)],
                             {"example-org": "g5"})}
        gmap, hexes, warns = resolve(years)
        self.assertEqual(gmap["example-org"], 5)
        self.assertNotEqual(gmap["acme-labs"], 5)   # auto skips the taken slot
        self.assertEqual(hexes, {})
        self.assertEqual(warns, [])

    def test_collision_avoidance_for_unlisted_groups(self):
        # Pin two groups to slots 1 and 3; the three unlisted groups must never
        # reuse either, and must not collide with each other.
        groups = [("a", 90), ("b", 80), ("c", 70), ("d", 60), ("e", 50)]
        years = {2025: year(2025, groups, {"a": "g1", "b": "g3"})}
        gmap, _, warns = resolve(years)
        self.assertEqual(gmap["a"], 1)
        self.assertEqual(gmap["b"], 3)
        auto = [gmap["c"], gmap["d"], gmap["e"]]
        self.assertNotIn(1, auto)
        self.assertNotIn(3, auto)
        self.assertEqual(len(set(auto)), 3)         # all distinct
        self.assertEqual(warns, [])

    def test_malformed_value_falls_back_with_warning(self):
        years = {2025: year(2025, [("example-org", 10), ("acme-labs", 5)],
                            {"example-org": "chartreuse"})}
        gmap, hexes, warns = resolve(years)
        self.assertIsInstance(gmap["example-org"], int)   # auto slot, not the bad value
        self.assertEqual(hexes, {})
        self.assertTrue(any("chartreuse" in w for w in warns))

    def test_other_rejected_with_warning(self):
        years = {2025: year(2025, [("example-org", 10), ("other", 4)],
                            {"other": "g2"})}
        gmap, _, warns = resolve(years)
        self.assertEqual(gmap["other"], "other")          # stays neutral
        self.assertTrue(any("other" in w for w in warns))

    def test_stability_across_two_years(self):
        # A group present in both years keeps its slot; order is set by the newest
        # year's commit ranking and held across years.
        y24 = year(2024, [("example-org", 30), ("acme-labs", 20), ("globex", 10)])
        y25 = year(2025, [("acme-labs", 90), ("example-org", 40)])   # globex quiet in 2025
        gmap, _, _ = resolve({2024: y24, 2025: y25})
        self.assertEqual(gmap["acme-labs"], 1)            # top of newest year
        self.assertEqual(gmap["example-org"], 2)
        self.assertEqual(gmap["globex"], 3)               # kept from the older year
        # single-year resolve of the same newest data gives the same two slots
        g25, _, _ = resolve({2025: y25})
        self.assertEqual(g25["acme-labs"], gmap["acme-labs"])
        self.assertEqual(g25["example-org"], gmap["example-org"])

    def test_newest_year_wins(self):
        y24 = year(2024, [("example-org", 30)], {"example-org": "g2"})
        y25 = year(2025, [("example-org", 40)], {"example-org": "g7"})
        gmap, _, _ = resolve({2024: y24, 2025: y25})
        self.assertEqual(gmap["example-org"], 7)          # newest map wins

    def test_cli_override_beats_json(self):
        years = {2025: year(2025, [("example-org", 40)], {"example-org": "g2"})}
        gmap, _, _ = resolve(years, ["example-org=g6"])
        self.assertEqual(gmap["example-org"], 6)

    def test_close_colours_warn(self):
        # Two hexes that resolve to nearly the same colour trigger the
        # distinguishability warning (non-fatal).
        years = {2025: year(2025, [("a", 10), ("b", 8)],
                            {"a": "#2a78d6", "b": "#2b79d7"})}
        _, _, warns = resolve(years)
        self.assertTrue(any("hard to tell apart" in w for w in warns))


class HexDerivation(unittest.TestCase):
    def _ratio(self, hex_a, hex_b):
        return build._contrast(build._hex_to_rgb(hex_a), build._hex_to_rgb(hex_b))

    def test_derived_tokens_meet_contrast_both_themes(self):
        for hexstr in ("#7a4fd0", "#b5179e", "#2a9d8f", "#e76f51", "#1d3557"):
            tok = build.derive_hex_tokens(hexstr)
            self.assertIsNotNone(tok, hexstr)
            lm, lf, lo = tok["light"]
            dm, df, do = tok["dark"]
            # mark holds >= 3:1 against its theme's surface
            self.assertGreaterEqual(self._ratio(lm, "#ffffff"), 3.0, (hexstr, "light mark"))
            self.assertGreaterEqual(self._ratio(dm, "#141b23"), 3.0, (hexstr, "dark mark"))
            # on-fill text holds >= 4.5:1 on the fill
            self.assertGreaterEqual(self._ratio(lo, lf), 4.5, (hexstr, "light on/fill"))
            self.assertGreaterEqual(self._ratio(do, df), 4.5, (hexstr, "dark on/fill"))
            # on-fill is one of the theme's black/white text tokens
            self.assertIn(lo, ("#ffffff", "#12181f"))
            self.assertIn(do, ("#ffffff", "#0c1015"))

    def test_short_hex_accepted(self):
        self.assertIsNotNone(build.derive_hex_tokens("#7a4"))

    def test_non_hex_rejected(self):
        self.assertIsNone(build.derive_hex_tokens("g3"))
        self.assertIsNone(build.derive_hex_tokens("not-a-colour"))


# ---- build-level: tokens/injection reach the page, old format unchanged -----

def write_years(data_dir, strip_group_colors=False):
    tz = collect.get_tz("UTC")
    login_p, email_p = collect.persona_maps(make_demo.CFG)
    known = set(make_demo.CFG["identities"]["logins"])
    prior = None
    for y in (2024, 2025):
        rng = random.Random(1000 + y)
        cbr, lp, items, reviews, rel = make_demo.gen_year(rng, y)
        dep = {"note": "x", "prs_created": 1, "prs_merged": 1, "repos_scoped": len(cbr), "batches": 1}
        scopes = collect.build_scopes(make_demo.CFG, y, dt.date(y, 12, 31), tz, cbr, items, reviews,
                                      rel, lp, "Octocat", email_p, login_p, known, dep, prior)
        meta = make_demo.build_meta(make_demo.CFG, y, dt.date(y, 12, 31), scopes, "Octocat", login_p)
        if strip_group_colors:
            meta.pop("group_colors", None)
        result = {"meta": meta, "scopes": scopes}
        (data_dir / f"{y}.json").write_text(json.dumps(result, default=str))
        prior = result


class BuildLevel(unittest.TestCase):
    def _build(self, strip=False, extra=None):
        td = pathlib.Path(tempfile.mkdtemp())
        data_dir = td / "data"
        data_dir.mkdir()
        write_years(data_dir, strip_group_colors=strip)
        out = td / "out" / "velocity.html"
        argv = ["--data-dir", str(data_dir), "--out", str(out)] + (extra or [])
        self.assertEqual(build.main(argv), 0)
        doc = out.read_text()
        frag = out.with_name("velocity.artifact.html").read_text()
        return doc, frag

    def test_hex_group_emits_gu_tokens_in_all_three_blocks(self):
        # The demo meta pins widgets-inc to a hex, so a --gu1 token set is emitted
        # once in the light :root and once in each of the two dark blocks.
        doc, frag = self._build()
        for text in (doc, frag):
            self.assertEqual(text.count("--gu1: "), 3)
            self.assertIn("--gu1-fill: ", text)
            self.assertIn("--gu1-on: ", text)
            self.assertIn('"widgets-inc":"u1"', text)   # injected resolved map

    def test_old_format_without_key_emits_no_hex_tokens(self):
        # No meta.group_colors → automatic slots only, no --gu custom properties,
        # and no visual regression path (the injected map is still present).
        doc, frag = self._build(strip=True)
        for text in (doc, frag):
            self.assertNotIn("--gu1", text)             # no derived hex custom props
            self.assertIn("GROUP_COLOR_IN = {", text)
        # fragment contract still holds with the feature wired in
        low = frag.lower()
        for tag in ("<!doctype", "<html", "<head>", "<body>", "<script src="):
            self.assertNotIn(tag, low)

    def test_cli_override_adds_hex_token_at_build(self):
        doc, _ = self._build(extra=["--group-color", "example-org=#008b8b"])
        self.assertIn("--gu", doc)                       # a hex token was derived
        self.assertIn('"example-org":"u', doc)           # example-org points at it


if __name__ == "__main__":
    unittest.main()
