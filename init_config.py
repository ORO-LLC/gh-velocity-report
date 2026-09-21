#!/usr/bin/env python3
"""Bootstrap a config.json for gh-velocity-report from the accounts logged in to
`gh`. Strictly read-only: it lists the logged-in accounts (`gh auth status`),
then for each account reads its login/id (`GET user`), its orgs (`GET user/orgs`)
and its verified emails (`GET user/emails`, which may 404/403 without the
`user:email` scope — handled gracefully, and no new scope is ever requested).
It never writes anything to GitHub.

It writes ./config.json (override with --config), refusing to overwrite an
existing file unless --force, and prints the next commands to run.

    python3 init_config.py
    python3 init_config.py --config config.json --force
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys

import gh_read

try:
    from zoneinfo import ZoneInfo, available_timezones
except ImportError:  # pragma: no cover
    ZoneInfo = None
    available_timezones = None

DEFAULT_CLASSIFICATION = {
    "doc_ext": [".md", ".mdx", ".rst", ".txt", ".adoc"],
    "doc_dirs": ["docs", "doc"],
    "excluded_dirs": ["node_modules", "dist", "build", "vendor", ".claude"],
    "excluded_ext": [".map", ".snap", ".lock"],
    "lockfiles": ["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock",
                  "poetry.lock", "composer.lock", "Gemfile.lock"],
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def logged_in_accounts():
    """Logins that `gh auth status` reports for github.com, in the order shown."""
    text = gh_read.auth_status_text()
    logins = []
    for m in re.finditer(r"Logged in to \S+ (?:account|as) (\S+)", text):
        lg = m.group(1).strip().strip("()")
        if lg and lg not in logins:
            logins.append(lg)
    return logins


def account_facts(login):
    """(user_id, verified_emails, orgs) for one logged-in account, read-only.
    Missing pieces (e.g. no user:email scope) come back empty, not fatal."""
    user = gh_read.rest_get("user", login)
    uid = user.get("id")
    orgs = []
    try:
        for o in gh_read.rest_get_all("user/orgs", login, params={"per_page": "100"}):
            if o.get("login"):
                orgs.append(o["login"])
    except gh_read.GhReadError as e:
        log(f"  note: could not read orgs for {login}: {e}")
    emails = []
    try:
        for e in gh_read.rest_get("user/emails", login) or []:
            addr = e.get("email")
            if addr and e.get("verified") and not addr.endswith("@users.noreply.github.com"):
                emails.append(addr.lower())
    except gh_read.GhReadError:
        log(f"  note: {login} email list unreadable (needs the user:email scope); "
            f"using noreply forms only. No new scope requested.")
    return uid, emails, orgs


def noreply_forms(login, uid):
    forms = [f"{login}@users.noreply.github.com".lower()]
    if uid is not None:
        forms.append(f"{uid}+{login}@users.noreply.github.com".lower())
    return forms


def detect_timezone():
    """Best-effort IANA timezone; falls back to UTC."""
    def valid(name):
        if not name:
            return False
        if available_timezones is not None:
            try:
                return name in available_timezones()
            except Exception:
                return True
        return "/" in name
    tz = os.environ.get("TZ")
    if valid(tz):
        return tz
    try:
        p = "/etc/localtime"
        if os.path.islink(p):
            target = os.readlink(p)
            if "zoneinfo/" in target:
                cand = target.split("zoneinfo/", 1)[1].replace("posix/", "").replace("right/", "")
                if valid(cand):
                    return cand
    except OSError:
        pass
    return "UTC"


def build_config(accounts):
    """Assemble a config dict from per-account facts. accounts is a list of
    (login, uid, emails, orgs)."""
    logins = [a[0] for a in accounts]
    emails, seen = [], set()
    for login, uid, acct_emails, _ in accounts:
        for e in acct_emails + noreply_forms(login, uid):
            if e not in seen:
                seen.add(e)
                emails.append(e)
    orgs, seen_o = [], set()
    for _, _, _, acct_orgs in accounts:
        for o in acct_orgs:
            if o not in seen_o:
                seen_o.add(o)
                orgs.append(o)
    return {
        "display_name": "Me",
        "identities": {"logins": logins, "emails": emails},
        "orgs": orgs,
        "group_colors": {},
        "personal_owners": list(logins),
        "gh_accounts": list(logins),
        "exclude_repos": [],
        "bot_suffixes": ["[bot]"],
        "timezone": detect_timezone(),
        "classification": DEFAULT_CLASSIFICATION,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="./config.json",
                    help="path to write the config to (default: ./config.json)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing config file")
    args = ap.parse_args(argv)

    path = pathlib.Path(args.config)
    if path.exists() and not args.force:
        log(f"error: {path} already exists. Re-run with --force to overwrite it.")
        return 2

    try:
        logins = logged_in_accounts()
    except gh_read.GhReadError as e:
        log(f"error: could not read `gh auth status` ({e}). Install gh and run `gh auth login` first.")
        return 2
    if not logins:
        log("error: no accounts are logged in to gh. Run `gh auth login` first.")
        return 2

    log(f"Found {len(logins)} logged-in account(s): {', '.join(logins)}")
    accounts = []
    for login in logins:
        try:
            uid, emails, orgs = account_facts(login)
        except gh_read.GhReadError as e:
            log(f"  warning: skipping {login}: {e}")
            continue
        log(f"  {login}: id={uid}, {len(emails)} verified email(s), {len(orgs)} org(s)"
            + (": " + ", ".join(orgs) if orgs else ""))
        accounts.append((login, uid, emails, orgs))
    if not accounts:
        log("error: none of the logged-in accounts were readable.")
        return 2

    cfg = build_config(accounts)
    log("")
    log(f"Config summary:")
    log(f"  display_name: {cfg['display_name']} (edit this to your name)")
    log(f"  logins:       {', '.join(cfg['identities']['logins'])}")
    log(f"  emails:       {len(cfg['identities']['emails'])} (verified + GitHub noreply forms)")
    log(f"  orgs:         {', '.join(cfg['orgs']) or '(none)'}")
    log(f"  timezone:     {cfg['timezone']}")

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)
    log("")
    log(f"Wrote {path}. Review it (especially display_name, orgs and personas), then:")
    log(f"  python3 collect.py --year {dt.date.today().year}")
    log(f"  python3 build.py")
    log(f"  open out/velocity.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
