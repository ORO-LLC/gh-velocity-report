#!/usr/bin/env python3
"""Render every data/<year>.json into TWO self-contained files from one template:

  out/velocity.html           - a full standalone document for file:// use.
  out/velocity.artifact.html  - a FRAGMENT (no <!DOCTYPE>/<html>/<head>/<body>)
                                for publishing as a claude.ai Artifact, where the
                                publish skeleton supplies those tags. Both render
                                identically: same <style>, markup and JS.

Vanilla JS, no external scripts; stylesheets only from Google Fonts. A header
control switches years and a second control switches scope (the combined "all"
scorecard or one org/owner group); every section re-renders from the selected
scope's block. Everything is embedded inline so it works offline / from file://.

    python3 build.py
    python3 build.py --data-dir data --out out/velocity.html
    python3 build.py --redact-private        # share a page without private repo names
"""
import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent

# ---- configurable group colours (issue #6) -------------------------------
# A group (org/owner) can be pinned to a palette slot (g1..g8) or a hex colour,
# via config `group_colors` (copied into meta.group_colors by the collector) or
# the repeatable `--group-color NAME=VALUE` build flag. Explicit choices resolve
# first; the remaining groups keep the automatic newest-year commit-order slots,
# skipping slots an explicit choice already took. A hex value has its full token
# set (base/fill/on for light and dark) derived here in Python so it is unit
# testable, then emitted as --guN custom properties the page consumes exactly
# like the built-in --gN slots. The neutral "other" group cannot be recoloured.
GROUP_SLOT_RE = re.compile(r"^g[1-8]$")
GROUP_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Built-in palette light-theme marks (mirror the --g1..--g8 in :root), used only
# to measure how close an automatic/explicit colour sits to another for the
# distinguishability warning.
_PALETTE_LIGHT = {1: "#2a78d6", 2: "#eb6834", 3: "#1baf7a", 4: "#eda100",
                  5: "#e87ba4", 6: "#008300", 7: "#4a3aa7", 8: "#e34948"}
# Surface + near-black text tokens per theme (mirror --surface / the -on tokens).
_LIGHT_SURFACE = (255, 255, 255)
_DARK_SURFACE = (0x14, 0x1b, 0x23)
_LIGHT_NEAR_BLACK = (0x12, 0x18, 0x1f)
_DARK_NEAR_BLACK = (0x0c, 0x10, 0x15)
_WHITE = (255, 255, 255)


def _hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _quantize(rgb):
    return tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % _quantize(rgb)


def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _rel_lum(rgb):
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _rgb_to_hsl(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return (0.0, 0.0, l)
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = (g - b) / d + (6 if g < b else 0)
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return (h * 60.0, s, l)


def _hsl_to_rgb(h, s, l):
    hh = (h % 360) / 360.0
    if s == 0:
        v = l * 255
        return (v, v, v)
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q

    def hue(t):
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p
    return (hue(hh + 1 / 3) * 255, hue(hh) * 255, hue(hh - 1 / 3) * 255)


def _reach_contrast(rgb, surface, target, steps=120):
    """Adjust rgb's lightness (away from the surface) until it clears `target`
    contrast against `surface`. Contrast is measured on the 8-bit-quantized colour
    that will actually be emitted (never on the pre-rounding float), so a token
    returned here is guaranteed to meet the threshold once written as hex. Returns
    the quantized rgb, or None if no lightness reaches it."""
    q = _quantize(rgb)
    if _contrast(q, surface) >= target:
        return q
    h, s, l = _rgb_to_hsl(rgb)
    darken = _rel_lum(surface) > 0.5
    step = 1.0 / steps
    for _ in range(steps):
        l += -step if darken else step
        clamped = max(0.0, min(1.0, l))
        cand = _quantize(_hsl_to_rgb(h, s, clamped))
        if _contrast(cand, surface) >= target:
            return cand
        if l <= 0.0 or l >= 1.0:
            return None
    return None


def _reach_text(fill0, surface, near_black, ttext=4.5, tsurf=3.0, steps=120):
    """From `fill0`, find a fill lightness whose best text colour (near-black or
    white) clears `ttext` while the fill itself keeps `tsurf` against the surface.
    Measured on the quantized fill so the emitted token holds. Returns
    (fill_rgb, on_rgb) or (None, None)."""
    h, s, l = _rgb_to_hsl(fill0)
    darken = _rel_lum(surface) > 0.5
    step = 1.0 / steps
    for _ in range(steps + 1):
        clamped = max(0.0, min(1.0, l))
        fill = _quantize(_hsl_to_rgb(h, s, clamped))
        cw, cb = _contrast(_WHITE, fill), _contrast(near_black, fill)
        on = _WHITE if cw >= cb else near_black
        if max(cw, cb) >= ttext and _contrast(fill, surface) >= tsurf:
            return fill, on
        l += -step if darken else step
        if l < 0.0 or l > 1.0:
            break
    return None, None


def _derive_theme_tokens(rgb, surface, near_black):
    mark = _reach_contrast(rgb, surface, 3.0)
    if mark is None:
        return None
    fill, on = _reach_text(mark, surface, near_black)
    if fill is None:
        return None
    return (_rgb_to_hex(mark), _rgb_to_hex(fill), _rgb_to_hex(on))


def derive_hex_tokens(hexstr):
    """Full token set for a hex group in both themes, or None if either theme
    cannot reach readable contrast. Each theme yields (mark, fill, on) where mark
    holds >= 3:1 against that theme's surface and `on` holds >= 4.5:1 on `fill`."""
    if not isinstance(hexstr, str) or not GROUP_HEX_RE.match(hexstr.strip()):
        return None
    rgb = _hex_to_rgb(hexstr.strip())
    light = _derive_theme_tokens(rgb, _LIGHT_SURFACE, _LIGHT_NEAR_BLACK)
    dark = _derive_theme_tokens(rgb, _DARK_SURFACE, _DARK_NEAR_BLACK)
    if light is None or dark is None:
        return None
    return {"light": light, "dark": dark}


def _scopes_groups_for_year(d):
    """Groups of one year, in stable colour order, mirroring the page's
    groupsForScopes(scopesOf(d)): new-format files sort scope keys by commits
    desc (name tiebreak); legacy files fall back to the top-level by_org order."""
    sc = d.get("scopes")
    if sc:
        groups = [k for k in sc if k != "all"]
        groups.sort(key=lambda g: (-((sc[g].get("totals") or {}).get("commits") or 0), g))
        return groups
    by_org = sorted(d.get("by_org") or [], key=lambda r: -(r.get("commits") or 0))
    return [r.get("group") for r in by_org if r.get("group")]


def _union_groups(years):
    """Union of groups across every loaded year, newest year first (matching the
    page's computeGroupColors), so a group quiet this year but active earlier
    keeps its own hue."""
    seen, order = set(), []
    for y in sorted(years, reverse=True):
        for g in _scopes_groups_for_year(years[y]):
            if g and g not in seen:
                seen.add(g)
                order.append(g)
    return order


def _iter_overrides(cli_overrides, warn):
    for item in cli_overrides or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            yield item[0], item[1]
            continue
        if "=" not in item:
            warn(f"--group-color {item!r} is not NAME=VALUE; ignoring it")
            continue
        name, val = item.split("=", 1)
        name = name.strip()
        if not name:
            warn(f"--group-color {item!r} has an empty group name; ignoring it")
            continue
        yield name, val


def resolve_group_colors(years, cli_overrides=None, warn=None):
    """Resolve every group to a colour token. Returns (group_color_map, hex_tokens).

    group_color_map: group name -> palette slot int (1..8), "cyN" (cycled slot),
    "other" (neutral), or "uN" (a derived hex colour). hex_tokens: "uN" ->
    {"light": (mark, fill, on), "dark": (mark, fill, on)}.

    Explicit choices (the newest year's meta.group_colors, then CLI overrides on
    top) win; remaining groups take automatic slots in newest-year commit order,
    skipping slots an explicit slot choice already took. `other` stays neutral."""
    if warn is None:
        warn = lambda m: None
    explicit_raw = {}
    if years:
        gc = ((years[max(years)].get("meta") or {}).get("group_colors")) or {}
        if isinstance(gc, dict):
            explicit_raw.update(gc)
    for name, val in _iter_overrides(cli_overrides, warn):
        explicit_raw[name] = val

    resolved = {}       # group -> slot int | "uN"
    hex_tokens = {}     # "uN" -> derived token set
    taken = set()       # palette slots pinned by an explicit slot choice
    ucount = 0
    for name, val in explicit_raw.items():
        if name == "other":
            warn("group_colors: 'other' is neutral and cannot be recoloured; ignoring it")
            continue
        if not isinstance(val, str):
            warn(f"group_colors: ignoring non-string value for group {name!r}")
            continue
        v = val.strip()
        if GROUP_SLOT_RE.match(v):
            n = int(v[1:])
            resolved[name] = n
            taken.add(n)
        elif GROUP_HEX_RE.match(v):
            tok = derive_hex_tokens(v)
            if tok is None:
                warn(f"group_colors: group {name!r} colour {v!r} cannot reach readable "
                     f"contrast in both themes; falling back to the automatic slot")
                continue
            ucount += 1
            key = "u%d" % ucount
            hex_tokens[key] = tok
            resolved[name] = key
        else:
            warn(f"group_colors: group {name!r} value {val!r} is not a palette slot "
                 f"(g1-g8) or hex colour; falling back to the automatic slot")

    # An explicit choice for a group that is not present in any loaded year (a
    # typo, a stale org, a case mismatch) must not silently consume a palette
    # slot and shift every automatic group. Drop it with a warning, and release
    # any slot it reserved. Build is the only place that knows the group union.
    order = _union_groups(years)
    union = set(order)
    for name in list(resolved):
        if name not in union:
            warn(f"group_colors: group {name!r} is not present in the loaded data; ignoring it")
            resolved.pop(name)
    # Rebuild the taken-slot set from the pins that survive, so dropping an absent
    # group that shared a slot with a live pin cannot free that still-used slot
    # and let an automatic group steal the hue.
    taken = {v for v in resolved.values() if isinstance(v, int)}

    gmap = {}
    slot = 0
    for g in order:
        if g in resolved:
            gmap[g] = resolved[g]
            continue
        if g == "other":
            gmap[g] = "other"
            continue
        slot += 1
        while slot <= 8 and slot in taken:
            slot += 1
        gmap[g] = slot if slot <= 8 else "cy" + str(((slot - 1) % 8) + 1)

    # Keep only hex tokens a rendered group actually references.
    used = {v for v in gmap.values() if isinstance(v, str) and v.startswith("u")}
    hex_tokens = {k: t for k, t in hex_tokens.items() if k in used}
    _warn_close_colors(gmap, hex_tokens, warn)
    return gmap, hex_tokens


def _rep_light_rgb(v, hex_tokens):
    if v == "other":
        return None
    if isinstance(v, int):
        return _hex_to_rgb(_PALETTE_LIGHT[v])
    s = str(v)
    if s.startswith("u"):
        return _hex_to_rgb(hex_tokens[s]["light"][0])
    if s.startswith("cy"):
        base = _hex_to_rgb(_PALETTE_LIGHT[int(s[2:])])
        return tuple(0.55 * c + 0.45 * 255 for c in base)   # color-mix 55% base + white
    return None


def _warn_close_colors(gmap, hex_tokens, warn):
    """Warn (once per pair) when two groups resolve to colours too close to tell
    apart: a small hue distance plus a small lightness distance (near-grey colours
    compared by lightness alone, since their hue is unstable)."""
    reps = []
    for g, v in gmap.items():
        rgb = _rep_light_rgb(v, hex_tokens)
        if rgb is not None:
            reps.append((g, _rgb_to_hsl(rgb)))
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            ga, (ha, sa, la) = reps[i]
            gb, (hb, sb, lb) = reps[j]
            dl = abs(la - lb)
            if sa < 0.12 and sb < 0.12:
                close = dl < 0.10
            else:
                dh = abs(ha - hb)
                dh = min(dh, 360 - dh)
                close = dh < 18 and dl < 0.12
            if close:
                warn(f"group colours for {ga!r} and {gb!r} are hard to tell apart; "
                     f"consider giving one a different group_colors value")


def _hex_token_css(hex_tokens):
    """(light_css, dark_css) custom-property blocks for the hex groups, to splice
    into the light :root and both dark theme blocks. Empty when no hex groups."""
    light, dark = [], []
    for key, tok in hex_tokens.items():
        lm, lf, lo = tok["light"]
        dm, df, do = tok["dark"]
        light.append(f"--g{key}: {lm}; --g{key}-fill: {lf}; --g{key}-on: {lo};")
        dark.append(f"--g{key}: {dm}; --g{key}-fill: {df}; --g{key}-on: {do};")
    return (" ".join(light), " ".join(dark))


def load_years(data_dir):
    years = {}
    for p in sorted(pathlib.Path(data_dir).glob("*.json")):
        try:
            d = json.loads(p.read_text())
            y = int(d["meta"]["year"])
            years[y] = d
        except Exception as e:
            print(f"skip {p}: {e}", file=sys.stderr)
    return years


def _blocks_of(d):
    """Every scope block of a year, new-format (d['scopes']) or old-format
    (the top level is the sole 'all' block)."""
    if d.get("scopes"):
        return list(d["scopes"].values())
    return [d]


def _redact_str(s, mapping):
    for full in sorted(mapping, key=len, reverse=True):
        s = s.replace(full, mapping[full])
    return s


def redact_private(years):
    """Replace private repo names with `<owner>/private-repo-N` (stable per build,
    owner preserved) in every scope block. Private OWNER names are NOT changed —
    only repo names. Names are rewritten in by_repo rows, in the precomputed
    insight sentences, and in meta.warnings, which are the only places a repo name
    appears in the JSON."""
    for d in years.values():
        blocks = _blocks_of(d)
        private = set()
        for b in blocks:
            for r in b.get("by_repo", []):
                if r.get("private"):
                    private.add(r["full_name"])
        if not private:
            continue
        mapping = {}
        for i, full in enumerate(sorted(private), 1):
            owner = full.split("/", 1)[0]
            mapping[full] = f"{owner}/private-repo-{i}"
        for b in blocks:
            for r in b.get("by_repo", []):
                if r["full_name"] in mapping:
                    red = mapping[r["full_name"]]
                    r["full_name"] = red
                    r["name"] = red.split("/", 1)[1]
            if b.get("insights"):
                b["insights"] = [_redact_str(s, mapping) for s in b["insights"]]
        m = d.get("meta", {})
        if m.get("warnings"):
            m["warnings"] = [_redact_str(w, mapping) for w in m["warnings"]]
    return years


TITLE = "<title>Personal Velocity</title>"
DESC = ('<meta name="description" content="A year of one person\'s GitHub contribution '
        'across every org and identity: commits, PRs, issues, reviews, releases, code, '
        'rhythm and streaks.">')
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">'
)

STYLE = r"""<style>
:root {
  --plane: #f4f6f9;
  --surface: #ffffff;
  --surface-2: #f8fafc;
  --ink: #12181f;
  --ink-2: #47515e;
  --muted: #6b7684;
  --rule: #dde3ea;
  --rule-soft: #eaeef3;
  --link: #2166bf;
  --brand: #2a78d6;
  --brand-fill: #2771c9;
  --brand-on: #ffffff;
  /* --accent is the live page accent: brand blue in All, the group hue in a
     single scope (set inline on the root element as a var() reference so it
     stays theme-aware). Rules, swatches, the heat ramp and selected fills
     follow it; links use --link so they stay readable whatever the hue. */
  --accent: var(--brand);
  --accent-fill: var(--brand-fill);
  --accent-on: var(--brand-on);
  --accent-soft: color-mix(in srgb, var(--accent) 12%, var(--surface));
  --good: #0ca30c;
  --bad: #d03b3b;
  --s1: #2a78d6;
  --s2: #eb6834;
  --s3: #1baf7a;
  /* Categorical group palette (dataviz reference theme, validated light+dark).
     hue = marks/dots/bars; -fill/-on = accessible selected-chip fill + text. */
  --g1: #2a78d6; --g1-fill: #2771c9; --g1-on: #ffffff;
  --g2: #eb6834; --g2-fill: #eb6834; --g2-on: #12181f;
  --g3: #1baf7a; --g3-fill: #1baf7a; --g3-on: #12181f;
  --g4: #eda100; --g4-fill: #eda100; --g4-on: #12181f;
  --g5: #e87ba4; --g5-fill: #e87ba4; --g5-on: #12181f;
  --g6: #008300; --g6-fill: #008300; --g6-on: #ffffff;
  --g7: #4a3aa7; --g7-fill: #4a3aa7; --g7-on: #ffffff;
  --g8: #e34948; --g8-fill: #e55453; --g8-on: #12181f;
  --gother: #78838f; --gother-fill: #59626d; --gother-on: #ffffff; /*__GC_LIGHT__*/
  /* Heat ramp derived from --accent: base = surface, top = a neutral dark so
     "more" darkens (light) / brightens (dark). Recomputes per theme + scope. */
  --heat-base: #eef2f6;
  --heat-top: #0e1a2b;
  --heat0: #eef2f6;
  --heat1: color-mix(in srgb, var(--accent) 20%, var(--heat-base));
  --heat2: color-mix(in srgb, var(--accent) 42%, var(--heat-base));
  --heat3: color-mix(in srgb, var(--accent) 68%, var(--heat-base));
  --heat4: color-mix(in srgb, var(--accent) 84%, var(--heat-top));
  --heat5: color-mix(in srgb, var(--accent) 58%, var(--heat-top));
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --plane: #0c1015;
    --surface: #141b23;
    --surface-2: #10161d;
    --ink: #e7ecf2;
    --ink-2: #aab6c4;
    --muted: #8592a1;
    --rule: #263140;
    --rule-soft: #1b232e;
    --link: #6aa8ee;
    --brand: #4d94e8;
    --brand-fill: #2f6fc4;
    --brand-on: #ffffff;
    --good: #0ca30c;
    --bad: #e66767;
    --s1: #3987e5;
    --s2: #d95926;
    --s3: #199e70;
    --g1: #3987e5; --g1-fill: #3987e5; --g1-on: #0c1015;
    --g2: #d95926; --g2-fill: #d95926; --g2-on: #0c1015;
    --g3: #199e70; --g3-fill: #199e70; --g3-on: #0c1015;
    --g4: #c98500; --g4-fill: #c98500; --g4-on: #0c1015;
    --g5: #d55181; --g5-fill: #d55181; --g5-on: #0c1015;
    --g6: #008300; --g6-fill: #008300; --g6-on: #ffffff;
    --g7: #9085e9; --g7-fill: #9085e9; --g7-on: #0c1015;
    --g8: #e66767; --g8-fill: #e66767; --g8-on: #0c1015;
    --gother: #8b95a1; --gother-fill: #8b95a1; --gother-on: #0c1015; /*__GC_DARK__*/
    --heat-base: #1b232e;
    --heat-top: #eaf1fb;
    --heat0: #1b232e;
  }
}
:root[data-theme="dark"] {
  --plane: #0c1015;
  --surface: #141b23;
  --surface-2: #10161d;
  --ink: #e7ecf2;
  --ink-2: #aab6c4;
  --muted: #8592a1;
  --rule: #263140;
  --rule-soft: #1b232e;
  --link: #6aa8ee;
  --brand: #4d94e8;
  --brand-fill: #2f6fc4;
  --brand-on: #ffffff;
  --good: #0ca30c;
  --bad: #e66767;
  --s1: #3987e5;
  --s2: #d95926;
  --s3: #199e70;
  --g1: #3987e5; --g1-fill: #3987e5; --g1-on: #0c1015;
  --g2: #d95926; --g2-fill: #d95926; --g2-on: #0c1015;
  --g3: #199e70; --g3-fill: #199e70; --g3-on: #0c1015;
  --g4: #c98500; --g4-fill: #c98500; --g4-on: #0c1015;
  --g5: #d55181; --g5-fill: #d55181; --g5-on: #0c1015;
  --g6: #008300; --g6-fill: #008300; --g6-on: #ffffff;
  --g7: #9085e9; --g7-fill: #9085e9; --g7-on: #0c1015;
  --g8: #e66767; --g8-fill: #e66767; --g8-on: #0c1015;
  --gother: #8b95a1; --gother-fill: #8b95a1; --gother-on: #0c1015; /*__GC_DARK__*/
  --heat-base: #1b232e;
  --heat-top: #eaf1fb;
  --heat0: #1b232e;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background: var(--plane);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1140px; margin: 0 auto; display: grid; grid-template-columns: minmax(0, 1fr); gap: 26px; padding-inline: 16px; padding-block: 28px 56px; }
main, section.card { min-width: 0; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
h1 { font-size: 30px; line-height: 1.1; font-weight: 700; margin: 0; letter-spacing: -.02em; text-wrap: balance; }
h2 { font-size: 15px; font-weight: 600; margin: 0; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-2); padding-left: 10px; border-left: 3px solid var(--accent); line-height: 1.15; }
.eyebrow { font-family: var(--mono); font-size: 12px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
header { display: grid; gap: 12px; }
.controls {
  position: sticky; top: env(safe-area-inset-top, 0px); z-index: 10;
  background: var(--plane); display: flex; flex-wrap: wrap; gap: 10px 14px;
  align-items: center; padding: 8px 0; max-width: 100%; min-width: 0;
  border-bottom: 1px solid var(--rule);
}
.facts { display: flex; flex-wrap: wrap; gap: 4px 18px; font-family: var(--mono); font-size: 12.5px; color: var(--muted); }
.facts b { color: var(--ink); font-weight: 500; }
.head-note { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.tabs { display: inline-flex; gap: 2px; padding: 3px; background: var(--surface-2); border: 1px solid var(--rule); border-radius: 999px; flex: none; }
.tabs button {
  font: inherit; font-family: var(--mono); font-size: 13px; font-weight: 500; cursor: pointer;
  border: 0; background: transparent; color: var(--ink-2); padding: 6px 16px; border-radius: 999px; line-height: 1.4;
}
.tabs button[aria-selected="true"] { background: var(--accent-fill); color: var(--accent-on); }
.scope {
  display: flex; gap: 2px; padding: 3px; background: var(--surface-2); border: 1px solid var(--rule);
  border-radius: 999px; overflow-x: auto; max-width: 100%; min-width: 0; scrollbar-width: thin; -webkit-overflow-scrolling: touch;
}
.scope button {
  font: inherit; font-family: var(--mono); font-size: 12px; font-weight: 500; cursor: pointer; border: 0;
  background: transparent; color: var(--ink-2); padding: 5px 12px; border-radius: 999px; line-height: 1.3;
  white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; flex: none;
}
.scope button[aria-selected="true"] { background: var(--accent-fill); color: var(--accent-on); }
.scope .scope-n { font-size: 10.5px; opacity: .65; font-variant-numeric: tabular-nums; }
.scope button[aria-selected="true"] .scope-n { opacity: .85; }
.scope .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; box-shadow: 0 0 0 1px rgba(0,0,0,.08) inset; }
.scope button[aria-selected="true"] .dot { box-shadow: 0 0 0 1px rgba(255,255,255,.35) inset; }
section.card { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 20px 22px; display: grid; gap: 16px; scroll-margin-top: calc(env(safe-area-inset-top, 0px) + 56px); }
.sec-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; justify-content: space-between; }
.sec-head .hint { font-size: 12.5px; color: var(--muted); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); gap: 10px; }
.tile { display: grid; gap: 3px; align-content: start; padding: 13px 14px; border: 1px solid var(--rule-soft); border-radius: 8px; background: var(--surface-2); }
.tile .k { font-family: var(--mono); font-size: 11px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); }
.tile .v { font-size: 22px; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.tile .s { font-size: 12px; color: var(--muted); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12.5px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.chart { width: 100%; overflow-x: auto; }
svg { display: block; max-width: 100%; }
svg text { fill: var(--ink-2); font-family: var(--mono); }
.two-col { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,2fr); gap: 18px 30px; align-items: start; }
.plain { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; font-size: 13.5px; }
.plain .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink-2); }
.insights { margin: 0; padding-left: 18px; display: grid; gap: 7px; font-size: 14px; }
.tablewrap { overflow-x: auto; border: 1px solid var(--rule-soft); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; min-width: 720px; }
table.wide { min-width: 980px; }
thead th { position: sticky; top: 0; background: var(--surface); text-align: right; font-family: var(--mono); font-weight: 500; font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); padding: 9px 11px; border-bottom: 1px solid var(--rule); white-space: nowrap; cursor: pointer; user-select: none; }
thead th:first-child, thead th.l { text-align: left; }
thead th.sorted::after { content: " \25be"; color: var(--accent); }
thead th.asc::after { content: " \25b4"; color: var(--accent); }
tbody td { padding: 8px 11px; border-bottom: 1px solid var(--rule-soft); text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tbody td.l { text-align: left; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--accent-soft); }
tbody tr.here td { background: var(--accent-soft); font-weight: 600; }
tfoot td { padding: 9px 11px; border-top: 1px solid var(--rule); font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tfoot td.l { text-align: left; }
.badge { font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 4px; border: 1px solid var(--rule); color: var(--muted); margin-left: 6px; }
.bar-mini { height: 6px; border-radius: 3px; background: var(--accent); display: inline-block; vertical-align: middle; min-width: 1px; }
.filter-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: flex-end; }
input[type="search"] { font: inherit; font-size: 13px; padding: 6px 11px; border: 1px solid var(--rule); border-radius: 8px; background: var(--surface-2); color: var(--ink); min-width: 180px; }
.callout { border: 1px solid var(--rule); border-left: 3px solid var(--accent); border-radius: 8px; padding: 12px 14px; background: var(--surface-2); font-size: 13px; display: grid; gap: 6px; }
.callout.warn { border-left-color: var(--s2); }
.callout.context { border-left-color: var(--muted); }
.callout code { font-family: var(--mono); font-size: 12px; background: var(--rule-soft); padding: 1px 4px; border-radius: 3px; }
.ctx-nums { display: flex; flex-wrap: wrap; gap: 6px 22px; font-family: var(--mono); font-size: 13px; }
.ctx-nums b { font-size: 17px; }
.notes-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 26px; font-size: 13px; color: var(--ink-2); }
.notes-grid dt { font-weight: 600; color: var(--ink); }
.notes-grid dd { margin: 2px 0 0; color: var(--muted); }
footer { font-size: 12.5px; color: var(--muted); text-align: center; }
.tip { position: fixed; pointer-events: none; z-index: 20; background: var(--ink); color: var(--surface); font-family: var(--mono); font-size: 11.5px; padding: 5px 8px; border-radius: 6px; opacity: 0; transition: opacity .08s; max-width: 260px; }
/* group legend strip under the header */
.grouplegend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-family: var(--mono); font-size: 12px; color: var(--ink-2); align-items: center; }
.grouplegend .gl { display: inline-flex; align-items: center; gap: 6px; }
.grouplegend .gl-c { font-variant-numeric: tabular-nums; color: var(--muted); }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
/* lead tile row */
.leads { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.lead { padding: 15px 16px; border: 1px solid var(--rule); border-radius: 10px; background: var(--surface-2); display: grid; gap: 7px; align-content: start; }
.lead .k { font-family: var(--mono); font-size: 11px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); }
.lead .r { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.lead .v { font-size: 30px; font-weight: 700; letter-spacing: -.02em; font-variant-numeric: tabular-nums; line-height: 1; }
.lead .s { font-size: 12px; color: var(--muted); }
.pill { font-family: var(--mono); font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 999px; display: inline-flex; align-items: center; gap: 3px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.pill.up { color: var(--good); background: color-mix(in srgb, var(--good) 15%, var(--surface)); }
.pill.down { color: var(--bad); background: color-mix(in srgb, var(--bad) 15%, var(--surface)); }
.pill.flat { color: var(--muted); background: var(--rule-soft); }
.spark-wrap { position: relative; display: block; margin-top: 3px; line-height: 0; }
.spark { display: block; width: 100%; height: 30px; overflow: visible; }
/* End-of-series marker: a CSS-positioned dot overlaid on the sparkline, placed
   from the last point's normalized x/y. Drawn outside the SVG because the SVG
   stretches non-uniformly (preserveAspectRatio="none") to fill the tile width,
   which squashes an in-SVG <circle> into an ellipse; a CSS dot stays round at
   every tile width and in both themes. */
.spark-dot { position: absolute; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); transform: translate(-50%, -50%); pointer-events: none; }
/* segmented control (metric / size toggles) */
.seg { display: inline-flex; gap: 2px; padding: 3px; background: var(--surface-2); border: 1px solid var(--rule); border-radius: 8px; flex: none; }
.seg button { font: inherit; font-family: var(--mono); font-size: 12px; font-weight: 500; cursor: pointer; border: 0; background: transparent; color: var(--ink-2); padding: 4px 11px; border-radius: 6px; line-height: 1.3; white-space: nowrap; }
.seg button[aria-selected="true"] { background: var(--accent-fill); color: var(--accent-on); }
.obadge { font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 4px; margin-left: 6px; }
.rowdot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-right: 7px; vertical-align: middle; }
/* treemap + narrow fallback bar list */
.treemap { width: 100%; }
.tm-bars { display: grid; gap: 5px; }
.tm-bar { display: grid; grid-template-columns: minmax(84px, 32%) 1fr auto; gap: 9px; align-items: center; font-size: 12.5px; }
.tm-bar .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tm-bar .track { height: 12px; border-radius: 3px; background: var(--rule-soft); overflow: hidden; }
.tm-bar .fill { height: 100%; border-radius: 3px; }
.tm-bar .num { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink-2); }
.cap { font-size: 12.5px; color: var(--muted); }
@media (max-width: 720px) {
  .wrap { padding-block: 20px 40px; }
  h1 { font-size: 25px; }
  section.card { padding: 16px 15px; }
  .two-col, .notes-grid { grid-template-columns: 1fr; }
  .leads { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .lead .v { font-size: 26px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
</style>"""

BODY = r"""<div class="wrap">
  <header>
    <div class="eyebrow" id="eyebrow">Personal velocity</div>
    <h1 id="title">Personal velocity</h1>
    <div class="facts" id="facts"></div>
    <div class="grouplegend" id="grouplegend" aria-label="Group colours"></div>
    <div class="head-note" id="head-note"></div>
  </header>
  <div class="controls" id="controls">
    <div class="tabs" id="tabs" role="tablist" aria-label="Year"></div>
    <div class="scope" id="scope" role="tablist" aria-label="Scope (org or owner)"></div>
  </div>
  <main id="main"></main>
  <footer>
    Counts are read-only from GitHub. Line and commit counts come from git on each repo's default branch, merges excluded, bot commits excluded by identity matching. See "How these numbers are counted".
  </footer>
  <div class="tip" id="tip"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
""" + r"""<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const YEARS = Object.keys(DATA).map(Number).sort((a,b)=>a-b);
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const WD = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const N = n => (n==null? "—" : Number(n).toLocaleString("en-US"));
const P = x => (x==null? "—" : Math.round(x*100)+"%");
const el = (t, a, ...kids) => { const e=document.createElement(t); if(a) for(const k in a){ if(k==="class") e.className=a[k]; else if(k==="html") e.innerHTML=a[k]; else e.setAttribute(k,a[k]); } for(const c of kids){ if(c==null) continue; e.append(c.nodeType? c : document.createTextNode(c)); } return e; };
const NS = "http://www.w3.org/2000/svg";
const svg = (t,a)=>{ const e=document.createElementNS(NS,t); for(const k in a) e.setAttribute(k,a[k]); return e; };

// ---- group colour identity (V1) -----------------------------------------
// Deterministic hue per org/owner group, assigned by the all-scope by_org order
// (commits descending) of the NEWEST year, then held stable across years and
// scopes. The catch-all "other" group always takes the neutral colour; a group
// past slot 8 cycles with a lighter tint of the reused slot.
let GROUP_COLOR = {};
// Resolved at build time (config group_colors + --group-color, with explicit
// choices, hex-derived --guN tokens and the automatic newest-year slots already
// merged). Empty {} for legacy single-group builds → fall back to computing here.
const GROUP_COLOR_IN = __GROUPCOLORS__;
let CUR_RGM = {};                    // full_name -> group, for the current year
// Groups present for a year's scopes, in stable colour order. Falls back to the
// all-block `by_org` ordering for legacy (no-`scopes`) files, which carry their
// org breakdown at the top level rather than as scope keys.
function groupsForScopes(scopes){
  const gs = groupsOrdered(scopes);
  if(gs.length) return gs;
  return [...(((scopes.all||{}).by_org)||[])].sort((a,b)=>(b.commits||0)-(a.commits||0)).map(r=>r.group).filter(Boolean);
}
function computeGroupColors(){
  // Prefer the build-time resolution (honours configured/CLI colours); only the
  // legacy single-group case leaves it empty and falls through to computing here.
  if(GROUP_COLOR_IN && Object.keys(GROUP_COLOR_IN).length){ GROUP_COLOR = GROUP_COLOR_IN; return; }
  // Union of groups across every loaded year, newest-year commit order first,
  // then groups seen only in older years — so a group that is quiet this year
  // but active in a prior one keeps its own hue instead of collapsing to gray.
  const map = {};
  let slot = 0;
  const seen = new Set();
  for(const y of [...YEARS].sort((a,b)=>b-a)){
    for(const g of groupsForScopes(scopesOf(DATA[y]))){
      if(seen.has(g)) continue;
      seen.add(g);
      if(g === "other"){ map[g] = "other"; continue; }
      slot++;
      map[g] = slot <= 8 ? slot : "cy" + (((slot - 1) % 8) + 1);
    }
  }
  GROUP_COLOR = map;
}
function groupVar(name){
  const a = GROUP_COLOR[name];
  if(a == null || a === "other") return {mark:"var(--gother)", fill:"var(--gother-fill)", on:"var(--gother-on)"};
  if(typeof a === "number") return {mark:"var(--g"+a+")", fill:"var(--g"+a+"-fill)", on:"var(--g"+a+"-on)"};
  if(a.charAt(0) === "u")                     // configured hex: derived --guN token set
    return {mark:"var(--g"+a+")", fill:"var(--g"+a+"-fill)", on:"var(--g"+a+"-on)"};
  const k = a.slice(2);                       // cycled slot: lighter tint of --gK
  return {mark:"color-mix(in srgb, var(--g"+k+") 55%, var(--surface))", fill:"var(--g"+k+"-fill)", on:"var(--g"+k+"-on)"};
}
function groupDot(name, cls){ return el("span",{class:cls||"dot",style:"background:"+groupVar(name).mark}); }
// Switch the live page accent to the selected group's hue (theme-aware because
// the value is a var() reference); All clears back to the brand default.
function setAccent(scope){
  const st = document.documentElement.style;
  if(scope === "all"){ st.removeProperty("--accent"); st.removeProperty("--accent-fill"); st.removeProperty("--accent-on"); return; }
  const g = groupVar(scope);
  st.setProperty("--accent", g.mark);
  st.setProperty("--accent-fill", g.fill);
  st.setProperty("--accent-on", g.on);
}
// Map each repo full_name -> its group, from the per-group scope blocks.
function repoGroupMap(scopes){
  const m = {};
  const groups = Object.keys(scopes).filter(g=>g!=="all");
  if(groups.length){
    for(const g of groups) for(const r of (scopes[g].by_repo || [])) m[r.full_name] = g;
    return m;
  }
  // Legacy files (no per-group `scopes`) carry only the flat "all" block, so
  // derive each repo's group the way the collector's group_for() does: a repo
  // whose owner is a recognised org/owner group (it appears in by_org) keeps
  // that owner as its group; every other owner collapses to "other". Without
  // this the owner-fallback below would mislabel unconfigured repos by owner,
  // contradicting the legend and the org table.
  const all = scopes.all || {};
  const recognised = new Set(((all.by_org)||[]).map(o=>o.group).filter(g=>g && g!=="other"));
  for(const r of (all.by_repo||[])) m[r.full_name] = recognised.has(r.owner) ? r.owner : "other";
  return m;
}
// ---- sparkline (V2): 12-pt line + soft area + emphasised end dot ----------
function sparkline(vals, label){
  const W = 150, H = 30, pad = 3;
  const s = svg("svg",{class:"spark",viewBox:"0 0 "+W+" "+H,preserveAspectRatio:"none",role:"img"});
  const ttl = document.createElementNS(NS,"title");
  ttl.textContent = label + ": " + vals.map((v,i)=>MON[i]+" "+N(v)).join(", ");
  s.append(ttl);
  const max = Math.max(1, ...vals), n = vals.length;
  const xx = i => n<2? W/2 : pad + i*(W-2*pad)/(n-1);
  const yy = v => (H-pad) - v/max*(H-2*pad);
  const pts = vals.map((v,i)=>[xx(i),yy(v)]);
  const line = pts.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1)).join(" ");
  const area = "M"+pts[0][0].toFixed(1)+" "+(H-pad)+" " + pts.map(p=>"L"+p[0].toFixed(1)+" "+p[1].toFixed(1)).join(" ") + " L"+pts[n-1][0].toFixed(1)+" "+(H-pad)+" Z";
  s.append(svg("path",{d:area,fill:"color-mix(in srgb, var(--accent) 16%, transparent)",stroke:"none"}));
  s.append(svg("path",{d:line,fill:"none",stroke:"var(--accent)","stroke-width":1.5,"stroke-linejoin":"round","stroke-linecap":"round","vector-effect":"non-scaling-stroke"}));
  // Emphasised end dot as a CSS overlay positioned from the last point's
  // normalized x/y, so it stays round under the sparkline's non-uniform x-scale
  // (an in-SVG circle would render as a stretched ellipse). See .spark-dot.
  const last = pts[n-1];
  const dot = el("span",{class:"spark-dot",
    style:"left:"+(last[0]/W*100).toFixed(2)+"%;top:"+(last[1]/H*100).toFixed(2)+"%"});
  return el("span",{class:"spark-wrap"}, s, dot);
}
// same-elapsed-period prior-year sum for YoY deltas (V3)
function priorSum(scope, year, field, uptoMonth){
  const py = DATA[year-1]; if(!py) return null;
  const sc = scopesOf(py); const blk = sc[scope]; if(!blk) return null;
  let t = 0; for(const x of blk.by_month){ if(x.month <= uptoMonth) t += (x[field]||0); }
  return t;
}
function priorActiveDays(scope, year, through){
  const py = DATA[year-1]; if(!py) return null;
  const sc = scopesOf(py); const blk = sc[scope]; if(!blk || !blk.calendar) return null;
  const md = (through||"").slice(5);            // "MM-DD"
  if(!md) return null;
  let c = 0;
  for(const iso in blk.calendar){ if(iso.slice(5) <= md && (blk.calendar[iso].commits||0) > 0) c++; }
  return c;
}
// whole months elapsed this year: the current month counts only once it is over,
// so same-period deltas never weigh a partial month against a full prior one.
function completedMonths(m){
  const d=new Date((((m&&m.through))||"")+"T00:00:00");
  if(!Number.isFinite(d.getTime())) return 0;          // unparseable through → no comparable window
  const mo=d.getMonth()+1;
  // The collector sets through = min(Dec 31, today), so a year is fully elapsed
  // only when through reaches Dec 31; any earlier date is an as-of-today
  // snapshot whose through-month is still in progress and does not count.
  // A Dec-31 `through` is treated as a completed year by definition: the emitted
  // JSON cannot distinguish a completed historical year from a live snapshot
  // taken on Dec 31 (make_demo sets generated_at to Dec 31 of the data year), so
  // the once-a-year Dec-31 live case carries at most a sub-day bias.
  return (mo===12 && d.getDate()===31) ? 12 : mo-1;
}
// current-year sum of a field over months up to (and including) uptoMonth
function curSum(d, field, uptoMonth){ let t=0; for(const x of (d.by_month||[])){ if(x.month<=uptoMonth) t+=(x[field]||0); } return t; }
// completed months the prior-year snapshot covers (a full past year → 12; null = no prior year)
function priorCompletedMonths(year){ const py=DATA[year-1]; return py? completedMonths(py.meta||{}) : null; }
// does the prior-year snapshot reach at least the same calendar day (MM-DD)?
function priorCoversMD(year, through){ const py=DATA[year-1]; if(!py||!py.meta||!py.meta.through) return false; return py.meta.through.slice(5) >= (through||"").slice(5); }
function deltaPill(cur, prior){
  if(prior == null) return null;
  const diff = cur - prior;
  if(prior === 0){
    if(cur === 0) return null;                       // nothing either year — no pill
    return el("span",{class:"pill up", title:"prior year same period: 0 (new activity)"}, "▲ ", "new");
  }
  const pct = diff/prior;
  const cls = Math.abs(pct) <= 0.02 ? "flat" : (diff > 0 ? "up" : "down");
  const arrow = cls==="up" ? "▲" : cls==="down" ? "▼" : "–";
  const txt = (diff>0?"+":diff<0?"−":"±") + Math.round(Math.abs(pct)*100) + "%";
  return el("span",{class:"pill "+cls, title:"prior year same period: "+N(prior)+" ("+signed(diff)+")"}, arrow+" ", txt);
}

const tip = document.getElementById("tip");
// Escape repo/owner/group names before they enter the innerHTML tooltip sink.
// GitHub names can't hold markup, but a hand-crafted third-party JSON could.
const esc = s => String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function showTip(ev, html){ tip.innerHTML=html; tip.style.opacity=1; moveTip(ev); }
function moveTip(ev){ const x=ev.clientX, y=ev.clientY; tip.style.left=Math.min(x+12, innerWidth-tip.offsetWidth-8)+"px"; tip.style.top=(y+16)+"px"; }
function hideTip(){ tip.style.opacity=0; }

function hoursText(h){ if(h==null) return "—"; if(h<1) return Math.round(h*60)+" min"; if(h<10) return h.toFixed(1)+" h"; if(h<48) return Math.round(h)+" h"; return (h/24).toFixed(1)+" d"; }
function fmtDate(iso){ if(!iso) return "—"; const d=new Date(iso+"T00:00:00"); return MON[d.getMonth()]+" "+d.getDate(); }
function pm(a,b){ return "+"+N(a)+" / −"+N(b); }
function signed(n){ return (n<0? "−":"+")+N(Math.abs(n)); }
function elapsedMonths(m){ try{ return new Date(m.through+"T00:00:00").getMonth()+1; }catch(e){ return 12; } }

// ---- scope handling (with backward compatibility for pre-scopes files) ----
function legacyBlock(d){ return {totals:d.totals, insights:d.insights, by_persona:d.by_persona, by_org:d.by_org, by_repo:d.by_repo, by_month:d.by_month, by_identity:d.by_identity, calendar:d.calendar, by_weekday_hour:d.by_weekday_hour, streaks:d.streaks, context:d.context}; }
function scopesOf(d){ return (d.scopes && Object.keys(d.scopes).length)? d.scopes : {all: legacyBlock(d)}; }
function groupsOrdered(scopes){ return Object.keys(scopes).filter(k=>k!=="all").sort((a,b)=> (((scopes[b].totals||{}).commits||0)-((scopes[a].totals||{}).commits||0)) || a.localeCompare(b)); }

let current = {year:null, scope:"all"};

function buildScopeControl(scopes, active){
  const cont=document.getElementById("scope");
  cont.innerHTML="";
  const order=["all", ...groupsOrdered(scopes)];
  for(const s of order){
    const b=el("button",{type:"button",role:"tab","data-s":s,"aria-selected":String(s===active),"aria-label":(s==="all"?"All":s)+" scope"});
    if(s!=="all") b.append(groupDot(s,"dot"));
    b.append(el("span",{class:"scope-l"}, s==="all"?"All":s));
    const c=((scopes[s]||{}).totals||{}).commits;
    b.append(el("span",{class:"scope-n"}, N(c==null?0:c)));
    b.addEventListener("click",()=>{ writeHash(current.year, s); render(current.year, s); });
    cont.append(b);
  }
}

function render(year, scope){
  const d = DATA[year];
  const scopes = scopesOf(d);
  if(!scope || !scopes[scope]) scope="all";
  current = {year, scope};
  setAccent(scope);
  CUR_RGM = repoGroupMap(scopes);
  const m = d.meta;
  const block = scopes[scope];
  const view = Object.assign({meta:m}, block);
  view.by_org = (scopes.all||{}).by_org || block.by_org || [];
  view._scope = scope;
  view._scopes = scopes;

  document.getElementById("eyebrow").textContent = m.display_name || "Personal velocity";
  document.getElementById("title").textContent = "Personal velocity · " + year + (scope!=="all"? " · " + scope : "");
  for(const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(Number(b.dataset.y)===year));
  buildScopeControl(scopes, scope);
  buildGroupLegend(scopes, scope);

  const facts = document.getElementById("facts");
  facts.innerHTML = "";
  facts.append(
    el("span",{html:"through <b>"+fmtDate(m.through)+", "+year+"</b>"}),
    el("span",{html:"identities <b>"+m.identities.emails.length+" emails · "+m.identities.logins.length+" logins</b>"}),
    el("span",{html:"accounts <b>"+m.accounts_used.join(", ")+"</b>"}),
    el("span",{html:"generated <b>"+(m.generated_at||"").slice(0,10)+"</b>"})
  );
  document.getElementById("head-note").textContent =
    "as of "+m.through+" · "+(scope==="all"? "all groups combined" : "scope: "+scope)+" · default branches · merges excluded · bot commits excluded by identity matching";

  const main = document.getElementById("main");
  main.innerHTML = "";
  main.append(leadsSection(view), tilesSection(view), insightsSection(view), monthlySection(view), calendarSection(view),
              orgSection(view), treemapSection(view), repoSection(view), rhythmSection(view), accountsSection(view),
              contextSection(view), notesSection(view));
}

// Group legend strip under the header (V1): one swatch + name + commits per
// group present this year, in stable colour order.
function buildGroupLegend(scopes, active){
  const cont = document.getElementById("grouplegend");
  cont.innerHTML = "";
  const groups = groupsForScopes(scopes);
  if(!groups.length){ cont.append(el("span",{class:"gl-c"},"one group")); return; }
  const byOrg = ((scopes.all||{}).by_org)||[];
  const commitsOf = g => { const s=scopes[g]; if(s&&s.totals) return s.totals.commits||0; const r=byOrg.find(x=>x.group===g); return r? (r.commits||0):0; };
  cont.append(el("span",{class:"gl-c",style:"letter-spacing:.08em"},"GROUPS"));
  for(const g of groups){
    const c = commitsOf(g);
    cont.append(el("span",{class:"gl"+(g===active?" ":""),style:g===active?"color:var(--ink);font-weight:600":""},
      groupDot(g), g, el("span",{class:"gl-c"}, N(c))));
  }
}

function card(title, hint, ...body){
  const head = el("div",{class:"sec-head"}, el("h2",null,title), hint? el("span",{class:"hint"},hint): null);
  return el("section",{class:"card"}, head, ...body.filter(Boolean));
}

// Lead tile row (V3): four large headline tiles with same-period YoY delta pills.
function leadsSection(d){
  const t=d.totals, m=d.meta, scope=d._scope, year=m.year;
  const cm=completedMonths(m);
  const pcm=priorCompletedMonths(year);              // null = no prior year loaded
  const hasPrior = pcm!=null && pcm>0 && cm>0;
  // Count tiles compare the same whole-month window on both years (no prior
  // day-grain exists for PRs/issues) — clamped to what the prior snapshot
  // actually covers, so a partial prior file can't inflate the delta. When that
  // scope has a real prior series the headline numeral IS the windowed value
  // (so number, pill and caption name one period) and the YTD figure moves to
  // the subtitle; with no comparison (no prior year, or a scope absent from the
  // prior year) the numeral stays the full YTD and no pill or comparison
  // wording is shown. Active days keeps its day-level prior slice, except when
  // the scope exists in the prior year but no month has completed yet — then it
  // drops the pill too so the card does not compare and deny in the same breath.
  const win = hasPrior ? Math.min(cm, pcm) : cm;
  const grid=el("div",{class:"leads"});
  const counts=[
    ["Commits","commits", t.commits, "default branch, merges excluded"],
    ["PRs merged","prs_merged", t.prs_merged, N(t.prs_opened)+" opened in scope"],
    ["Issues closed","issues_closed", t.issues_closed, N(t.issues_opened)+" opened"]
  ];
  let compared=false;   // did any count tile show a real prior comparison for this scope?
  for(const [k,f,ytd,extra] of counts){
    const prior = (hasPrior && win>0) ? priorSum(scope,year,f,win) : null;
    const num = prior!=null ? curSum(d,f,win) : ytd;
    const pill = prior!=null ? deltaPill(curSum(d,f,win), prior) : null;
    if(prior!=null) compared=true;
    const sub = ((prior!=null && win<12)? N(ytd)+" YTD through "+m.through+" · " : "") + extra;
    grid.append(el("div",{class:"lead"}, el("div",{class:"k"},k),
      el("div",{class:"r"}, el("div",{class:"v"}, N(num)), pill),
      el("div",{class:"s"}, sub)));
  }
  const havePriorYear = DATA[year-1]!=null;
  const priorBlk = havePriorYear ? (scopesOf(DATA[year-1])[scope] || null) : null;
  // Prior year AND this scope both exist, but no completed-month window has
  // elapsed yet (current year still in its first month, cm===0): the count
  // tiles are YTD with no pill, so the active-days tile drops its day-slice pill
  // too and the card names the single YTD window instead of claiming "new scope".
  const noCompYet = priorBlk!=null && !compared;
  const adPrior = (!noCompYet && priorCoversMD(year,m.through)) ? priorActiveDays(scope,year,m.through) : null;
  grid.append(el("div",{class:"lead"}, el("div",{class:"k"},"Active days"),
    el("div",{class:"r"}, el("div",{class:"v"}, N(t.active_days)), deltaPill(t.active_days, adPrior)),
    el("div",{class:"s"}, (t.ratios&&t.ratios.commits_per_active_day!=null? N(t.ratios.commits_per_active_day)+" commits/day":"with ≥1 commit")+" · through "+m.through)));
  const wlabel = win>0 ? MON[0]+"–"+MON[win-1] : null;   // completed-month window the counts cover
  // Three no-comparison states get three distinct captions (never "new scope"
  // for a scope that exists in the prior year): no prior year loaded; prior year
  // without this scope; or prior year with this scope but no completed month yet.
  // Numeral, pill and caption name one window in every case.
  const basis = (scope!=="all"? "scope: "+scope+" · " : "")
    + (compared && wlabel
         // "vs prior year" only covers the clauses actually compared: when the
         // prior snapshot reaches the current day (adPrior set) both counts and
         // active days are compared; a partial prior snapshot compares counts
         // only, so active days is stated without the comparison claim.
         ? (adPrior!=null ? "counts "+wlabel+" · active days through "+m.through+" · vs prior year"
                          : "counts "+wlabel+" vs prior year · active days through "+m.through)
       : !havePriorYear ? "no prior year loaded"
       : !priorBlk ? "new scope · no prior-year comparison"
       : "YTD through "+m.through+" · no completed months to compare yet");
  return card("Headline", basis, grid);
}

function tilesSection(d){
  const t=d.totals, s=d.streaks, m=d.meta, r=t.ratios||{};
  const em=elapsedMonths(m);
  const scanned = m.repos_scanned_for_commits!=null? m.repos_scanned_for_commits : m.repos_scanned;
  const CODE=x=>(x.code_add||0)+(x.code_del||0), DOCS=x=>(x.docs_add||0)+(x.docs_del||0);
  const tiles=[
    ["Commits", N(t.commits), "default branch · merges excluded", x=>x.commits],
    ["Pull requests", N(t.prs_opened)+" / "+N(t.prs_merged), "opened / merged · "+N(t.prs_closed_unmerged)+" closed unmerged · "+P(r.merge_rate)+" merge rate", x=>x.prs_merged],
    ["Issues", N(t.issues_opened)+" / "+N(t.issues_closed), "opened / closed, authored", x=>x.issues_closed],
    ["Reviews given", N(t.reviews), N(t.self_reviews_excluded||0)+" self-reviews excluded", x=>x.reviews],
    ["Releases", N(t.releases), "published this year", x=>x.releases],
    ["Active repos", N(t.repos), d._scope==="all"? "of "+N(scanned)+" scanned for commits" : "in this scope"],
    ["Orgs", N(t.orgs), "configured orgs touched"],
    ["Active days", N(t.active_days), (r.commits_per_active_day!=null? N(r.commits_per_active_day)+" commits/day":"with ≥1 commit")],
    ["Longest streak", N(s.longest)+" d", (s.longest_start? s.longest_start+" → "+s.longest_end : "current "+N(s.current)+" d")],
    ["Code lines", pm(t.code_add,t.code_del), "net "+signed(t.code_net), CODE],
    ["Docs lines", pm(t.docs_add,t.docs_del), "net "+signed(t.docs_net)+" · "+N(t.docs_files_add)+" files added, "+N(t.docs_files_del)+" removed", DOCS],
    ["Identities", N((m.identities.personas||[]).length)+" people", N(m.identities.emails.length)+" author emails and "+N(m.identities.logins.length)+" logins folded to one scorecard"],
    ["Median PR cycle", hoursText(t.cycle_time.median_hours), "p90 "+hoursText(t.cycle_time.p90_hours)+" · n="+N(t.cycle_time.population)],
    ["Monthly pace", N(r.avg_issues_closed_per_month)+" · "+N(r.avg_prs_merged_per_month), "avg issues closed · PRs merged / month ("+N(r.elapsed_months)+" mo)"]
  ];
  const grid=el("div",{class:"tiles"});
  for(const [k,v,s2,spk] of tiles){
    const tile=el("div",{class:"tile"}, el("div",{class:"k"},k), el("div",{class:"v"},v), el("div",{class:"s"},s2));
    if(spk){ const vals=d.by_month.filter(x=>x.month<=em).map(spk); if(vals.some(x=>x>0)) tile.append(sparkline(vals,k)); }
    grid.append(tile);
  }
  return card("At a glance", d._scope==="all"? null : "scope: "+d._scope, grid);
}

function insightsSection(d){
  const ins=d.insights||[];
  const em=elapsedMonths(d.meta);
  const recent=d.by_month.filter(x=>x.month<=em && (x.commits||x.issues_closed||x.prs_merged)).slice(-4);
  const left=el("div",null, el("h3",{style:"font-size:13px;margin:0 0 8px;color:var(--ink-2)"},"Last four months"),
    el("ul",{class:"plain"}, ...(recent.length? recent.map(x=>el("li",null,
      el("span",{style:"font-weight:600"}, MON[x.month-1]+" "), el("span",{class:"mono"}, N(x.issues_closed)+" iss · "+N(x.prs_merged)+" PRs · "+N(x.commits)+" commits")))
      : [el("li",{class:"mono"},"no month with activity yet")])));
  const right=el("div",null, el("h3",{style:"font-size:13px;margin:0 0 8px;color:var(--ink-2)"},"Insights"),
    el("ul",{class:"insights"}, ...ins.map(s=>el("li",null,s))));
  if(!ins.length) return null;
  return card("Insights", null, el("div",{class:"two-col"}, left, right));
}

function legend(items){
  const w=el("div",{class:"legend"});
  for(const [c,l] of items) w.append(el("span",null, el("span",{class:"swatch",style:"background:"+c}), l));
  return w;
}

const MONTH_COLS=[
  ["month","Month","l",x=>MON[x.month-1]],
  ["commits","Commits","",x=>N(x.commits)],
  ["prs_opened","PRs open","",x=>N(x.prs_opened)],
  ["prs_merged","PRs mg","",x=>N(x.prs_merged)],
  ["prs_closed_unmerged","PRs unmg","",x=>N(x.prs_closed_unmerged)],
  ["issues_opened","Iss open","",x=>N(x.issues_opened)],
  ["issues_closed","Iss closed","",x=>N(x.issues_closed)],
  ["reviews","Reviews","",x=>N(x.reviews)],
  ["releases","Rel","",x=>N(x.releases)],
  ["code","Code +/−","",x=>pm(x.code_add,x.code_del)],
  ["docs","Docs +/−","",x=>pm(x.docs_add,x.docs_del)],
  ["dfiles","Docs files +/−","",x=>"+"+N(x.docs_files_add)+" / −"+N(x.docs_files_del)]
];

function niceNum(x, round){ if(x<=0) return 1; const exp=Math.floor(Math.log10(x)); const f=x/Math.pow(10,exp);
  const nf = round ? (f<1.5?1: f<3?2: f<7?5:10) : (f<=1?1: f<=2?2: f<=5?5:10); return nf*Math.pow(10,exp); }
function axisTicks(max){ if(max<=0) return {max:1,ticks:[0,1]}; const range=niceNum(max,false);
  // These axes count whole events (commits / PRs / issues), so the step is at
  // least 1 — otherwise a low peak (max 1 or 2) yields a sub-unit step whose
  // ticks round to repeated integers (e.g. [0,0,0,1,1,1]) at distinct gridlines.
  const step=Math.max(1, niceNum(range/4,true));
  const nmax=Math.ceil(max/step)*step; const t=[]; for(let v=0; v<=nmax+1e-9; v+=step) t.push(Math.round(v)); return {max:nmax,ticks:t}; }

function monthlySection(d){
  const bm=d.by_month;
  const scope=d._scope, scopes=d._scopes;
  const METRICS=[["commits","Commits"],["prs_merged","PRs merged"],["issues_closed","Issues closed"]];
  let metric="commits";
  const groups = scope==="all" ? groupsOrdered(scopes) : [scope];
  const stacked = scope==="all" && groups.length>0;   // legacy data has no group scopes → one series
  function seriesFor(field){
    if(stacked) return groups.map(g=>({g, vals:(scopes[g].by_month||[]).map(x=>x[field]||0)}));
    return [{g:scope, vals: bm.map(x=>x[field]||0)}];
  }
  const chartWrap=el("div",{class:"chart"});
  const legendWrap=el("div");
  const W=760,H=240, padL=44, padB=28, padT=12, padR=8;
  const iw=W-padL-padR, ih=H-padT-padB;
  function drawChart(){
    const field=metric, ser=seriesFor(field);
    const totals=MON.map((_,mi)=> ser.reduce((a,se)=>a+(se.vals[mi]||0),0));
    const ax=axisTicks(Math.max(...totals));
    const s=svg("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Monthly "+field.replace("_"," ")+(stacked?" by group":"")});
    for(const val of ax.ticks){ const y=padT+ih*(1-val/ax.max);
      s.append(svg("line",{x1:padL,y1:y,x2:W-padR,y2:y,stroke:"var(--rule-soft)","stroke-width":1}));
      const tx=svg("text",{x:padL-6,y:y+3,"text-anchor":"end","font-size":10}); tx.textContent=N(val); s.append(tx); }
    const bw=iw/12, gw=bw*0.62, x0off=(bw-gw)/2;
    MON.forEach((_,mi)=>{
      let acc=0;
      const perGroup=ser.map(se=>[se.g, se.vals[mi]||0]).filter(z=>z[1]>0);
      ser.forEach(se=>{
        const v=se.vals[mi]||0; if(v<=0) return;
        const h=ih*v/ax.max; const yTop=padT+ih*(1-(acc+v)/ax.max);
        const gap = (stacked && h>2.5) ? 1.5 : 0;   // 2px surface gap between stacked segments
        const rr=svg("rect",{x:padL+bw*mi+x0off, y:yTop+gap, width:gw, height:Math.max(0,h-gap), fill:groupVar(se.g).mark, rx:1.5});
        const rows=perGroup.map(z=>`${esc(z[0])}: ${N(z[1])}`).join("<br>");
        rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${MON[mi]} ${d.meta.year}</b><br>${rows}<br>total: ${N(totals[mi])}`));
        rr.addEventListener("mouseleave",hideTip);
        s.append(rr); acc+=v;
      });
      const tx=svg("text",{x:padL+bw*mi+bw/2,y:H-padB+14,"text-anchor":"middle","font-size":10}); tx.textContent=MON[mi]; s.append(tx);
    });
    chartWrap.innerHTML=""; chartWrap.append(s);
    legendWrap.innerHTML="";
    if(stacked) legendWrap.append(legend(groups.map(g=>[groupVar(g).mark, g])));
    else legendWrap.append(legend([[groupVar(scope).mark, scope]]));
  }
  const seg=el("div",{class:"seg",id:"metric-toggle",role:"tablist","aria-label":"Monthly metric"});
  METRICS.forEach(([key,label])=>{
    const b=el("button",{type:"button",role:"tab","data-m":key,"aria-selected":String(key===metric)}, label);
    b.addEventListener("click",()=>{ metric=key; for(const x of seg.children) x.setAttribute("aria-selected",String(x.dataset.m===metric)); drawChart(); });
    seg.append(b);
  });
  drawChart();
  const em=elapsedMonths(d.meta);
  const shown=bm.filter(x=>x.month<=em);
  const tbl=el("table",{class:"wide"}); const hr=el("tr");
  for(const [key,label,cls] of MONTH_COLS) hr.append(el("th",{class:cls||""},label));
  tbl.append(el("thead",null,hr));
  const tb=el("tbody");
  for(const x of shown){ const tr=el("tr"); for(const [key,label,cls,fn] of MONTH_COLS) tr.append(el("td",{class:cls||""},fn(x))); tb.append(tr); }
  tbl.append(tb);
  const tot={month:0,commits:0,prs_opened:0,prs_merged:0,prs_closed_unmerged:0,issues_opened:0,issues_closed:0,reviews:0,releases:0,code_add:0,code_del:0,docs_add:0,docs_del:0,docs_files_add:0,docs_files_del:0};
  for(const x of shown) for(const k in tot) if(k!=="month") tot[k]+=x[k]||0;
  const fr=el("tr");
  fr.append(el("td",{class:"l"},"Total"));
  for(const [key,label,cls,fn] of MONTH_COLS.slice(1)) fr.append(el("td",{class:cls||""}, fn(tot)));
  tbl.append(el("tfoot",null,fr));
  const hint = stacked ? "stacked by group · toggle the metric; table has every counter"
                       : (scope==="all" ? "toggle the metric; table has every counter"
                                        : "bars in this group's colour; table has every counter");
  const head=el("div",{class:"sec-head"}, el("h2",null,"Monthly activity"),
    el("div",{style:"display:flex;align-items:center;gap:12px;flex-wrap:wrap"}, el("span",{class:"hint"},hint), seg));
  return el("section",{class:"card"}, head, legendWrap, chartWrap, el("div",{class:"tablewrap"}, tbl));
}

function calendarSection(d){
  const cal=d.calendar||{}; const year=d.meta.year;
  const start=new Date(year,0,1); const end=new Date(year,11,31);
  const gridStart=new Date(start); gridStart.setDate(start.getDate()-(start.getDay()));
  const days=[]; for(let dtv=new Date(gridStart); dtv<=end; dtv.setDate(dtv.getDate()+1)) days.push(new Date(dtv));
  const weeks=Math.ceil(days.length/7);
  let max=1; for(const k in cal) max=Math.max(max,cal[k].commits);
  const cell=13, gap=3, padL=30, padT=18;
  const W=padL+weeks*(cell+gap), H=padT+7*(cell+gap)+4;
  const s=svg("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Contribution calendar heatmap"});
  const heat=v=>{ if(!v) return "var(--heat0)"; const q=v/max; if(q<=0.15) return "var(--heat1)"; if(q<=0.35) return "var(--heat2)"; if(q<=0.6) return "var(--heat3)"; if(q<=0.85) return "var(--heat4)"; return "var(--heat5)"; };
  // annotations (V6): outline the longest-streak days, ring the busiest day
  const st=d.streaks||{};
  const streakSet=new Set();
  if(st.longest_start && st.longest_end){
    for(let dv=new Date(st.longest_start+"T00:00:00"); dv<=new Date(st.longest_end+"T00:00:00"); dv.setDate(dv.getDate()+1))
      streakSet.add(dv.getFullYear()+"-"+String(dv.getMonth()+1).padStart(2,"0")+"-"+String(dv.getDate()).padStart(2,"0"));
  }
  const busiest=(st.busiest_day||{}).date; let bxy=null;
  let lastMonth=-1;
  days.forEach((dtv,i)=>{
    const wk=Math.floor(i/7), wd=i%7;
    const iso=dtv.getFullYear()+"-"+String(dtv.getMonth()+1).padStart(2,"0")+"-"+String(dtv.getDate()).padStart(2,"0");
    const inYear=dtv.getFullYear()===year;
    const rec=cal[iso]; const cm=rec?rec.commits:0; const act=rec?rec.activity:0;
    const x=padL+wk*(cell+gap), y=padT+wd*(cell+gap);
    const onStreak=inYear && streakSet.has(iso);
    const rr=svg("rect",{x,y,width:cell,height:cell,rx:3,fill:inYear?heat(cm):"transparent",
      "stroke":onStreak?"var(--ink)":"var(--rule-soft)","stroke-width":inYear?(onStreak?1.4:0.5):0});
    if(inYear){ rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${iso}</b><br>${N(cm)} commits · ${N(act)} activity${onStreak?"<br>longest streak":""}${iso===busiest?"<br>busiest day":""}`)); rr.addEventListener("mouseleave",hideTip); }
    s.append(rr);
    if(inYear && iso===busiest) bxy=[x+cell/2, y+cell/2];
    if(inYear && dtv.getMonth()!==lastMonth && wd===0){ lastMonth=dtv.getMonth(); const tx=svg("text",{x,y:padT-6,"font-size":9}); tx.textContent=MON[dtv.getMonth()]; s.append(tx); }
  });
  if(bxy) s.append(svg("circle",{cx:bxy[0],cy:bxy[1],r:cell*0.72,fill:"none",stroke:"var(--ink)","stroke-width":1.6}));
  ["Mon","Wed","Fri"].forEach(l=>{ const wd=WD.indexOf(l)+1; const y=padT+wd*(cell+gap)+cell-3; const tx=svg("text",{x:0,y,"font-size":9}); tx.textContent=l; s.append(tx); });
  const capParts=[];
  if(st.longest_start && st.longest_end) capParts.push("outlined: longest streak "+fmtDate(st.longest_start)+" – "+fmtDate(st.longest_end)+" ("+N(st.longest)+" d)");
  if(busiest) capParts.push("ringed: busiest day "+fmtDate(busiest)+" ("+N((st.busiest_day||{}).commits)+" commits)");
  return card("Contribution calendar", null,
    el("div",{class:"chart"}, s),
    legend([["var(--heat1)","less"],["var(--heat3)","more"],["var(--heat5)","most"]]),
    capParts.length? el("div",{class:"cap"}, capParts.join(" · ")) : null);
}

function barCell(v,max,color){ const pct=max? Math.round(100*v/max):0; return el("span",{class:"bar-mini",style:"width:"+Math.max(pct*0.7,v?2:0)+"px"+(color?";background:"+color:"")}); }

function orgSection(d){
  const rows=[...d.by_org].sort((a,b)=>b.commits-a.commits);
  const max=Math.max(1,...rows.map(r=>r.commits));
  const cols=[
    ["Group","l",r=>el("span",null, groupDot(r.group,"rowdot"), r.group)],
    ["Repos","",r=>N(r.repos)],
    ["Commits","",r=>N(r.commits)],
    ["","l",r=>barCell(r.commits,max,groupVar(r.group).mark)],
    ["Code +/−","",r=>pm(r.code_add,r.code_del)],
    ["Docs +/−","",r=>pm(r.docs_add,r.docs_del)],
    ["Docs files +/−","",r=>"+"+N(r.docs_files_add)+" / −"+N(r.docs_files_del)],
    ["PRs mg","",r=>N(r.prs_merged)],
    ["PRs unmg","",r=>N(r.prs_closed_unmerged)],
    ["Iss closed","",r=>N(r.issues_closed)],
    ["Reviews","",r=>N(r.reviews)],
    ["Rel","",r=>N(r.releases)]
  ];
  const tbl=el("table",{class:"wide"}); const hr=el("tr");
  for(const [label,cls] of cols) hr.append(el("th",{class:cls||""},label));
  tbl.append(el("thead",null,hr));
  const tb=el("tbody");
  for(const r of rows){ const tr=el("tr", r.group===d._scope? {class:"here"}: null); for(const [label,cls,fn] of cols) tr.append(el("td",{class:cls||""}, fn(r))); tb.append(tr); }
  tbl.append(tb);
  const hint = d._scope==="all"? "click a scope above to focus one group" : "the selected scope is highlighted";
  return card("By org / owner", hint, el("div",{class:"tablewrap"}, tbl));
}

// ---- treemap (V7): squarified, sized by commits or code lines, group-coloured
function clip(str, n){ return str.length<=n ? str : str.slice(0, Math.max(1,n-1))+"…"; }
function squarify(data, X, Y, W, H){
  const total=data.reduce((a,d)=>a+d.v,0) || 1;
  const nodes=data.map(d=>({d, v:d.v, area:d.v/total*(W*H)}));
  const out=[]; const rect={x:X,y:Y,w:W,h:H}; let row=[];
  const worst=(row, side)=>{ let s=0,mn=Infinity,mx=0; for(const r of row){ s+=r.area; if(r.area<mn)mn=r.area; if(r.area>mx)mx=r.area; }
    return Math.max((side*side*mx)/(s*s), (s*s)/(side*side*mn)); };
  const place=(row)=>{ const s=row.reduce((a,r)=>a+r.area,0); const vertical=rect.w>=rect.h;
    if(vertical){ const rw=s/rect.h; let yy=rect.y; for(const r of row){ const rh=r.area/rw; out.push(Object.assign(r,{x:rect.x,y:yy,w:rw,h:rh})); yy+=rh; } rect.x+=rw; rect.w-=rw; }
    else { const rh=s/rect.w; let xx=rect.x; for(const r of row){ const rw=r.area/rh; out.push(Object.assign(r,{x:xx,y:rect.y,w:rw,h:rh})); xx+=rw; } rect.y+=rh; rect.h-=rh; } };
  let i=0;
  while(i<nodes.length){ const n=nodes[i]; const side=Math.min(rect.w,rect.h);
    if(row.length===0){ row.push(n); i++; continue; }
    if(worst(row.concat(n), side) <= worst(row, side)){ row.push(n); i++; }
    else { place(row); row=[]; } }
  if(row.length) place(row);
  return out;
}
let TREEMAP_REDRAW=null;
function treemapSection(d){
  const METRICS=[["commits","Commits",r=>r.commits||0],["lines","Code lines Δ",r=>(r.code_add||0)+(r.code_del||0)]];
  let metric=METRICS[0];
  const box=el("div",{class:"treemap"});
  const scope=d._scope;
  const groupOf=r=> r._agg!=null? "other" : (CUR_RGM[r.full_name]||"other");
  function rows(){
    let rs=d.by_repo.map(r=>({r, v:metric[2](r)})).filter(z=>z.v>0).sort((a,b)=>b.v-a.v);
    if(rs.length>40){ const top=rs.slice(0,40), rest=rs.slice(40), sum=rest.reduce((a,z)=>a+z.v,0);
      if(sum>0) top.push({r:{full_name:"other repos",name:"other repos",owner:"other",_agg:rest.length}, v:sum}); rs=top; }
    return rs;
  }
  function drawTreemap(rs){
    const W=760,H=330;
    const laid=squarify(rs,0,0,W,H);
    const s=svg("svg",{viewBox:"0 0 "+W+" "+H,role:"img","aria-label":"Repositories sized by "+metric[1]});
    laid.forEach((nd,idx)=>{
      const z=nd.d, grp=groupOf(z.r), pct=100-(idx%4)*7;
      // The >40-repo overflow bucket: in a single group scope every collapsed
      // repo belongs to that group, so tint the block with the scope's hue (a
      // light mix so it still reads as an aggregate, not one repo); the combined
      // "all" view keeps the neutral "other" colour. Label/tooltip stay on grp.
      const scopeAgg = z.r._agg!=null && scope!=="all";
      const fill = scopeAgg
        ? "color-mix(in srgb, "+groupVar(scope).mark+" 42%, var(--surface))"
        : "color-mix(in srgb, "+groupVar(grp).mark+" "+pct+"%, var(--surface))";
      const rr=svg("rect",{x:nd.x+0.75,y:nd.y+0.75,width:Math.max(0,nd.w-1.5),height:Math.max(0,nd.h-1.5),fill,rx:2});
      rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${esc(z.r.full_name)}</b><br>${esc(grp)}<br>${metric[1]}: ${N(z.v)}${z.r._agg!=null?" ("+z.r._agg+" repos)":""}`));
      rr.addEventListener("mouseleave",hideTip);
      s.append(rr);
      if(nd.w>58 && nd.h>24){
        // Labels are decorative: pointer-events:none lets the pointer fall
        // through to the <rect> beneath (a sibling painted below), so hovering a
        // block's name/value keeps the rect's tooltip up instead of firing its
        // mouseleave.
        const lstyle="fill:#fff;stroke:rgba(0,0,0,.55);stroke-width:2.4px;paint-order:stroke;pointer-events:none";
        const t1=svg("text",{x:nd.x+6,y:nd.y+16,"font-size":11,style:lstyle}); t1.textContent=clip(z.r.name||z.r.full_name, Math.floor((nd.w-10)/6.4)); s.append(t1);
        if(nd.h>40){ const t2=svg("text",{x:nd.x+6,y:nd.y+30,"font-size":10,style:lstyle}); t2.textContent=N(z.v); s.append(t2); }
      }
    });
    box.innerHTML=""; box.append(el("div",{class:"chart"}, s));
  }
  function drawBars(rs){
    const top=rs.slice(0,10), max=top.length?top[0].v:1;
    const wrap=el("div",{class:"tm-bars"});
    for(const z of top){ const grp=groupOf(z.r);
      wrap.append(el("div",{class:"tm-bar"},
        el("div",{class:"lbl"}, groupDot(grp,"rowdot"), z.r.name||z.r.full_name),
        el("div",{class:"track"}, el("div",{class:"fill",style:"width:"+Math.max(2,Math.round(100*z.v/max))+"%;background:"+groupVar(grp).mark})),
        el("div",{class:"num"}, N(z.v)))); }
    box.innerHTML=""; box.append(wrap);
  }
  const hintEl=el("span",{class:"hint"},"");
  function draw(){
    hintEl.textContent="blocks sized by "+metric[1].toLowerCase()+", coloured by group";
    const rs=rows();
    if(!rs.length){ box.innerHTML=""; box.append(el("div",{class:"cap"},"no repositories with "+metric[1].toLowerCase()+" in this scope")); return; }
    const wpx=box.clientWidth || (box.parentElement? box.parentElement.clientWidth:0);
    const narrow = wpx>0 ? wpx<520 : innerWidth<560;
    narrow ? drawBars(rs) : drawTreemap(rs);
  }
  TREEMAP_REDRAW=draw;
  requestAnimationFrame(draw);
  const seg=el("div",{class:"seg",id:"treemap-metric",role:"tablist","aria-label":"Treemap size metric"});
  METRICS.forEach(m=>{ const b=el("button",{type:"button",role:"tab","data-m":m[0],"aria-selected":String(m===metric)}, m[1]);
    b.addEventListener("click",()=>{ metric=m; for(const x of seg.children) x.setAttribute("aria-selected",String(x.dataset.m===m[0])); draw(); }); seg.append(b); });
  hintEl.textContent="blocks sized by "+metric[1].toLowerCase()+", coloured by group";
  const head=el("div",{class:"sec-head"}, el("h2",null,"Where the year went"),
    el("div",{style:"display:flex;align-items:center;gap:12px;flex-wrap:wrap"}, hintEl, seg));
  return el("section",{class:"card"}, head, box);
}

const REPO_COLS=[
  ["full_name","Repo","l",r=>repoLink(r)],
  ["commits","Commits","",r=>N(r.commits)],
  ["code_add","+code","",r=>N(r.code_add)],
  ["code_del","−code","",r=>N(r.code_del)],
  ["docs_add","+docs","",r=>N(r.docs_add)],
  ["docs_del","−docs","",r=>N(r.docs_del)],
  ["files_add","+files","",r=>N((r.code_files_add||0)+(r.docs_files_add||0))],
  ["files_del","−files","",r=>N((r.code_files_del||0)+(r.docs_files_del||0))],
  ["prs_opened","PRs open","",r=>N(r.prs_opened)],
  ["prs_merged","PRs mg","",r=>N(r.prs_merged)],
  ["prs_closed_unmerged","PRs unmg","",r=>N(r.prs_closed_unmerged)],
  ["issues_opened","Iss open","",r=>N(r.issues_opened)],
  ["issues_closed","Iss closed","",r=>N(r.issues_closed)],
  ["reviews","Reviews","",r=>N(r.reviews)],
  ["releases","Rel","",r=>N(r.releases)],
  ["first","First","",r=>fmtDate(r.first)],
  ["last","Last","",r=>fmtDate(r.last)]
];
function sortKey(r,k){
  if(k==="files_add") return (r.code_files_add||0)+(r.docs_files_add||0);
  if(k==="files_del") return (r.code_files_del||0)+(r.docs_files_del||0);
  return r[k];
}
function repoLink(r){
  const grp=CUR_RGM[r.full_name] || "other";
  const a=el("a",{href:"https://github.com/"+r.full_name,target:"_blank",rel:"noopener"}, r.full_name);
  const gv=groupVar(grp);
  const wrap=el("span",null, groupDot(grp,"rowdot"), a,
    el("span",{class:"obadge",style:"background:"+gv.fill+";color:"+gv.on}, grp));
  if(r.private) wrap.append(el("span",{class:"badge"},"private"));
  if(r.fork) wrap.append(el("span",{class:"badge"},"fork"));
  if(r.archived) wrap.append(el("span",{class:"badge"},"archived"));
  return wrap;
}
let repoState={sort:"commits",asc:false,q:""};
function repoSection(d){
  repoState={sort:"commits",asc:false,q:""};
  const search=el("input",{type:"search",id:"repo-filter",placeholder:"filter repos…","aria-label":"Filter repos"});
  search.addEventListener("input",()=>{ repoState.q=search.value.toLowerCase(); draw(); });
  const tblwrap=el("div",{class:"tablewrap"});
  const sec=card("By repo", d.by_repo.length+" repos with activity · click a header to sort, scroll for more columns",
    el("div",{class:"filter-row"}, search), tblwrap);
  const maxC=Math.max(1,...d.by_repo.map(r=>r.commits));
  function draw(){
    let rows=d.by_repo.filter(r=> (!repoState.q||r.full_name.toLowerCase().includes(repoState.q)));
    rows.sort((a,b)=>{ const k=repoState.sort; let av=sortKey(a,k),bv=sortKey(b,k); if(typeof av==="string"){ av=av||""; bv=bv||""; return repoState.asc? av.localeCompare(bv):bv.localeCompare(av);} av=av||0; bv=bv||0; return repoState.asc? av-bv:bv-av; });
    const tbl=el("table",{class:"wide"}); const hr=el("tr");
    for(const [key,label,cls] of REPO_COLS){ const th=el("th",{class:cls||"",role:"button",tabindex:"0"},label); if(repoState.sort===key) th.classList.add(repoState.asc?"asc":"sorted");
      const doSort=()=>{ if(repoState.sort===key) repoState.asc=!repoState.asc; else {repoState.sort=key; repoState.asc=false;} draw(); };
      th.addEventListener("click",doSort); th.addEventListener("keydown",e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); doSort(); } }); hr.append(th); }
    hr.append(el("th",{class:"l"},"share"));
    tbl.append(el("thead",null,hr));
    const tb=el("tbody");
    for(const r of rows){ const tr=el("tr");
      for(const [key,label,cls,fn] of REPO_COLS) tr.append(el("td",{class:cls||""}, fn(r)));
      tr.append(el("td",{class:"l"}, barCell(r.commits,maxC, groupVar(CUR_RGM[r.full_name]||"other").mark)));
      tb.append(tr);
    }
    tbl.append(tb); tblwrap.innerHTML=""; tblwrap.append(tbl);
  }
  draw();
  return sec;
}

function rhythmSection(d){
  const wh=d.by_weekday_hour;
  let max=1; for(const row of wh) for(const v of row) max=Math.max(max,v);
  const cell=15, gap=2, padL=34, padT=16;
  const W=padL+24*(cell+gap), H=padT+7*(cell+gap)+4;
  const s=svg("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Commits by weekday and hour"});
  const heat=v=>{ if(!v) return "var(--heat0)"; const q=v/max; if(q<=0.15) return "var(--heat1)"; if(q<=0.35) return "var(--heat2)"; if(q<=0.6) return "var(--heat3)"; if(q<=0.85) return "var(--heat4)"; return "var(--heat5)"; };
  // working-hours band (V9): Mon–Fri 09:00–17:00, drawn behind the cells
  const bx=padL+9*(cell+gap)-1, bw2=8*(cell+gap)-gap+2, by=padT-2, bh=5*(cell+gap)-gap+4;
  s.append(svg("rect",{x:bx,y:by,width:bw2,height:bh,rx:4,fill:"var(--accent)","fill-opacity":0.08}));
  s.append(svg("rect",{x:bx,y:by,width:bw2,height:bh,rx:4,fill:"none",stroke:"var(--accent)","stroke-opacity":0.45,"stroke-width":1}));
  for(let wd=0;wd<7;wd++){ const tx=svg("text",{x:0,y:padT+wd*(cell+gap)+cell-3,"font-size":9}); tx.textContent=WD[wd]; s.append(tx);
    for(let h=0;h<24;h++){ const v=wh[wd][h]||0; const x=padL+h*(cell+gap), y=padT+wd*(cell+gap);
      const rr=svg("rect",{x,y,width:cell,height:cell,rx:3,fill:heat(v)});
      rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${WD[wd]} ${String(h).padStart(2,"0")}:00</b><br>${N(v)} commits`)); rr.addEventListener("mouseleave",hideTip);
      s.append(rr);
    }
  }
  for(let h=0;h<24;h+=3){ const tx=svg("text",{x:padL+h*(cell+gap),y:padT-5,"font-size":8}); tx.textContent=String(h); s.append(tx); }
  const totals=wh.map(row=>row.reduce((a,b)=>a+b,0)); const wmax=Math.max(1,...totals);
  const bs=svg("svg",{viewBox:"0 0 760 150",role:"img","aria-label":"Commits per weekday"});
  const bw=760/7;
  totals.forEach((v,wd)=>{ const h=120*v/wmax; const x=bw*wd+bw*0.2; const rr=svg("rect",{x,y:130-h,width:bw*0.6,height:h,fill:"var(--accent)",rx:2});
    rr.addEventListener("mousemove",ev=>showTip(ev,`<b>${WD[wd]}</b><br>${N(v)} commits`)); rr.addEventListener("mouseleave",hideTip); bs.append(rr);
    const tx=svg("text",{x:bw*wd+bw/2,y:146,"text-anchor":"middle","font-size":10}); tx.textContent=WD[wd]; bs.append(tx);
    const vt=svg("text",{x:bw*wd+bw/2,y:126-h,"text-anchor":"middle","font-size":9}); vt.textContent=v?N(v):""; bs.append(vt);
  });
  let tot=0, work=0, best=[0,0], bestv=-1;
  for(let wd=0;wd<7;wd++) for(let h=0;h<24;h++){ const v=wh[wd][h]||0; tot+=v; if(wd<=4 && h>=9 && h<=16) work+=v; if(v>bestv){ bestv=v; best=[wd,h]; } }
  const pct = tot? Math.round(100*work/tot) : 0;
  const cap = tot ? (pct+"% of commits land Mon–Fri 09:00–17:00; busiest hour "+WD[best[0]]+" "+String(best[1]).padStart(2,"0")+":00")
                  : "no commit-time data in this scope";
  return card("Rhythm", "commit timing in "+d.meta.timezone,
    el("div",{class:"chart"}, s),
    el("div",{class:"cap"}, cap),
    el("div",{class:"chart"}, bs));
}

function accountsSection(d){
  const personas=(d.by_persona||[]).slice().sort((a,b)=>b.commits-a.commits);
  const max=Math.max(1,...personas.map(p=>p.commits),1);
  const cols=[["Account / login","l"],["Commits",""],["","l"],["PRs op",""],["PRs mg",""],["Iss op",""],["Iss cl",""],["Reviews",""],["Code +/−",""]];
  const tbl=el("table",{class:"wide"}); const hr=el("tr");
  for(const [label,cls] of cols) hr.append(el("th",{class:cls||""},label));
  tbl.append(el("thead",null,hr));
  const tb=el("tbody");
  for(const p of personas){
    const tr=el("tr",{style:"font-weight:600"});
    tr.append(el("td",{class:"l"},p.persona), el("td",null,N(p.commits)), el("td",{class:"l"},barCell(p.commits,max)),
      el("td",null,N(p.prs_opened)), el("td",null,N(p.prs_merged)), el("td",null,N(p.issues_opened)),
      el("td",null,N(p.issues_closed)), el("td",null,N(p.reviews)), el("td",null,pm(p.code_add,p.code_del)));
    tb.append(tr);
    for(const lg of (p.logins||[])){
      const lr=el("tr",{style:"color:var(--muted)"});
      lr.append(el("td",{class:"l"}, el("span",{style:"opacity:.55"},"↳ "), lg.login),
        el("td",null,N(lg.commits)), el("td",{class:"l"},""),
        el("td",null,N(lg.prs_opened)), el("td",null,N(lg.prs_merged)), el("td",null,N(lg.issues_opened)),
        el("td",null,N(lg.issues_closed)), el("td",null,N(lg.reviews)), el("td",null,""));
      tb.append(lr);
    }
  }
  tbl.append(tb);
  const erows=(d.by_identity&&d.by_identity.emails)||[];
  const emax=Math.max(1,...erows.map(x=>x.commits),1);
  const etbl=el("table",{class:"wide"}); const ehr=el("tr");
  for(const [label,cls] of [["Commit email","l"],["Persona","l"],["Commits",""],["","l"]]) ehr.append(el("th",{class:cls||""},label));
  etbl.append(el("thead",null,ehr));
  const etb=el("tbody");
  for(const e of erows) etb.append(el("tr",null, el("td",{class:"l"},e.email), el("td",{class:"l"},e.persona||"—"), el("td",null,N(e.commits)), el("td",{class:"l"},barCell(e.commits,emax))));
  etbl.append(etb);
  const body=[el("div",{class:"tablewrap"},tbl)];
  if(erows.length) body.push(el("h3",{style:"font-size:13px;margin:0;color:var(--ink-2)"},"Per commit email"), el("div",{class:"tablewrap"},etbl));
  const disc=d.meta.discovered_emails||[]; const warns=d.meta.warnings||[];
  if(disc.length){ const c=el("div",{class:"callout"}); c.append(el("strong",null,"Discovered emails ("+disc.length+")"), el("div",null,"Commits by a known login under an email not in config, folded in and written back to config.identities.emails:"));
    const ul=el("div",null); for(const e of disc) ul.append(el("div",null, el("code",null,e.email), " — "+N(e.commits)+" commits"+(e.persona? " ("+e.persona+")":""))); c.append(ul); body.push(c); }
  if(warns.length){ const c=el("div",{class:"callout warn"}); c.append(el("strong",null,"Warnings ("+warns.length+")"));
    const ul=el("ul",{style:"margin:4px 0 0;padding-left:18px"}); for(const w of warns.slice(0,40)) ul.append(el("li",null,w)); if(warns.length>40) ul.append(el("li",null,"…and "+(warns.length-40)+" more")); c.append(ul); body.push(c); }
  return card("Accounts", "one combined scorecard · commits per email, PRs/issues/reviews per login (GitHub-attributed)", ...body);
}

function contextSection(d){
  const dep=(d.context||{}).dependabot;
  if(!dep) return null;
  const c=el("div",{class:"callout context"});
  c.append(el("strong",null,"Dependabot — not your PRs"));
  if(dep.skipped){
    c.append(el("div",null,"Skipped to stay under the search-call budget (would cost "+N(dep.would_cost_search_calls)+" calls)."));
  } else {
    c.append(el("div",{class:"ctx-nums"},
      el("span",null, el("b",null,N(dep.prs_created)), " created"),
      el("span",null, el("b",null,N(dep.prs_merged)), " merged")),
      el("div",{style:"color:var(--muted);font-size:12px"}, "Dependabot's own PRs across "+N(dep.repos_scoped)+" repos with your activity (all groups). Shown for context; excluded from every other number on this page."));
  }
  return card("Context", "dependency-bot activity, for reference · combined across groups", c);
}

function notesSection(d){
  const m=d.meta;
  const scanned = m.repos_scanned_for_commits!=null? m.repos_scanned_for_commits : m.repos_scanned;
  const cov=(m.coverage&&m.coverage.logins_not_logged_in)||[];
  const covText = cov.length
    ? "Logins in your identity list that are not logged in to gh: "+cov.join(", ")+". Their private repos are invisible to this tool. "
    : "Every identity login is logged in to gh. ";
  const items=[
    ["Default branch only","Commits and lines come from git on each repo's default branch. Work on unmerged branches is not counted."],
    ["Merges excluded","git log runs with --no-merges; a squash-merged PR counts as one commit."],
    ["Combined accounts","One scorecard across every account and commit identity you control ("+N((m.identities.personas||[]).length)+" personas, "+m.identities.logins.length+" logins), de-duplicated by commit sha and by PR/issue node id. Per-persona and per-login splits are in Accounts."],
    ["Scope","The Scope control above recomputes every section from one org/owner group (or all groups combined). Streaks, active days, cycle time and ratios are true for the selected scope alone."],
    ["Self-reviews","A review of a PR authored by any of your own logins is not a review given; "+N(d.totals.self_reviews_excluded||0)+" were excluded in this scope."],
    ["Bot commits","Excluded by construction: a bot's email/login is not in the identity list, so a bot commit never matches."],
    ["Identity matching","A commit is “mine” when its author email is in the identity list (case-insensitive, GitHub numeric noreply prefix stripped) or GitHub attributes it to a known login. A login-based presence pass plus a login→email harvest catch commits under a known login whose email was not yet in config."],
    ["Files added / removed","From git log --name-status --diff-filter=AD on the default branch: counts of files added (A) and removed (D), classified code or docs. Paths are never stored."],
    ["PRs closed unmerged","PRs you authored that were closed without merging, counted by close date."],
    ["Releases","Counted by published_at in the year where the release author is one of your logins; drafts excluded. Only repos with your activity are queried; a repo with none is zero, not an error."],
    ["Monthly averages","Divide by elapsed months: January through the current month ("+N((m.through||"").slice(5,7))+" for this year), 12 for a full past year."],
    ["Dependabot context","Dependabot figures are that bot's own PRs across repos with your activity (combined across groups), shown for reference and excluded from all other counts."],
    ["Timezone","Weekday, hour, calendar and streak bucketing use "+m.timezone+". Search date windows are UTC."],
    ["Search API","PRs, issues and reviews come from the GitHub search API per login, split by month to stay under the 1000-result cap, de-duplicated by node id. A PR/issue/review is assigned to a scope by the repo it belongs to."],
    ["Forks","A fork is counted only when an identity has commits in it; forks with no commits are dropped. Fork/private/archived are badged in the repo table."],
    ["Coverage",covText+"Repos discovered: "+N(m.repos_discovered)+"; candidates after pruning: "+N(m.repos_candidates)+"; scanned for commits: "+N(scanned)+"; with activity (all groups): "+N(m.repos_with_activity)+"."]
  ];
  const dl=el("dl",{class:"notes-grid"});
  for(const [k,v] of items) dl.append(el("div",null, el("dt",null,k), el("dd",null,v)));
  return card("How these numbers are counted", "tool v"+m.tool_version+" · "+N(m.search_calls)+" search calls · "+m.runtime_seconds+"s", dl);
}

// ---- boot (hash access guarded for sandboxed iframes) ----
function safeHash(){ try{ return (location.hash||"").replace(/^#/,""); }catch(e){ return ""; } }
function writeHash(y,scope){ try{ location.hash = (scope && scope!=="all")? y+"/"+encodeURIComponent(scope) : String(y); return true; }catch(e){ return false; } }
function fromHash(){
  const h=safeHash(); const parts=h.split("/");
  const y=Number(parts[0]); const year=YEARS.includes(y)? y : Math.max(...YEARS);
  let scope="all"; try{ scope=parts[1]? decodeURIComponent(parts[1]) : "all"; }catch(e){ scope="all"; }
  return {year, scope};
}

addEventListener("mousemove",e=>{ if(tip.style.opacity==="1") moveTip(e); });
const tabs=document.getElementById("tabs");
for(const y of [...YEARS].sort((a,b)=>b-a)){
  const b=el("button",{type:"button",role:"tab","data-y":y,"aria-selected":"false"},String(y));
  b.addEventListener("click",()=>{ writeHash(y, current.scope); render(y, current.scope); });
  tabs.append(b);
}
try{ addEventListener("hashchange",()=>{ const h=fromHash(); if(h.year!==current.year || h.scope!==current.scope) render(h.year, h.scope); }); }catch(e){}
let _rsz;
addEventListener("resize",()=>{ clearTimeout(_rsz); _rsz=setTimeout(()=>{ if(TREEMAP_REDRAW) TREEMAP_REDRAW(); }, 150); });
computeGroupColors();
const boot=fromHash();
render(boot.year, boot.scope);
</script>"""


def _body(payload, gc_json):
    # Substitute the colour-map marker BEFORE inserting the data payload: a
    # serialized field (e.g. a display_name) could itself contain the literal
    # "__GROUPCOLORS__", and replacing markers after the payload is in place would
    # corrupt it. gc_json holds only group names + short tokens and can never
    # contain "__DATA__", so this order is safe both ways.
    return BODY.replace("__GROUPCOLORS__", gc_json).replace("__DATA__", payload)


def full_document(payload, gc_json, style):
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        + TITLE + "\n" + DESC + "\n" + FONTS + "\n" + style + "\n"
        "</head>\n<body>\n" + _body(payload, gc_json) + "\n</body>\n</html>\n"
    )


def fragment(payload, gc_json, style):
    # No <!DOCTYPE>/<html>/<head>/<body>: the Artifact publish skeleton supplies
    # those. Order: <title>, fonts, <style>, one outer wrapper, inline <script>.
    return TITLE + "\n" + FONTS + "\n" + style + "\n" + _body(payload, gc_json) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(HERE / "data"),
                    help="directory of data/<year>.json files to render (default: ./data)")
    ap.add_argument("--out", default=str(HERE / "out" / "velocity.html"),
                    help="standalone HTML output path; the fragment is written alongside as *.artifact.html")
    ap.add_argument("--redact-private", action="store_true",
                    help="replace private repo names with <owner>/private-repo-N so the page can be shared publicly")
    ap.add_argument("--group-color", action="append", default=[], metavar="NAME=VALUE",
                    help="pin a group's colour to a palette slot (g1-g8) or a hex colour, "
                         "e.g. --group-color example-org=g3 --group-color 'widgets-inc=#7a4fd0' "
                         "(quote a #hex so the shell does not treat it as a comment); "
                         "repeatable, and overrides config group_colors at build time")
    args = ap.parse_args(argv)

    years = load_years(args.data_dir)
    if not years:
        print(f"error: no data/*.json under {args.data_dir}; run collect.py first", file=sys.stderr)
        return 2
    if args.redact_private:
        years = redact_private(years)

    def _warn(m):
        print(f"group-colours: {m}", file=sys.stderr)
    group_color_map, hex_tokens = resolve_group_colors(years, args.group_color, _warn)
    gc_json = json.dumps(group_color_map, separators=(",", ":")).replace("</", "<\\/")
    light_css, dark_css = _hex_token_css(hex_tokens)
    style = STYLE.replace("/*__GC_LIGHT__*/", light_css).replace("/*__GC_DARK__*/", dark_css)

    payload = json.dumps(years, separators=(",", ":"), default=str).replace("</", "<\\/")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = full_document(payload, gc_json, style)
    out.write_text(doc)
    frag_path = out.with_name(out.stem + ".artifact.html")
    frag = fragment(payload, gc_json, style)
    frag_path.write_text(frag)
    print(f"wrote {out} ({len(doc):,} bytes) and {frag_path} ({len(frag):,} bytes) with years {sorted(years)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
