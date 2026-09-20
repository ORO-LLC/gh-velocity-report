# gh-velocity-report

A read-only, year-selectable report of one person's GitHub contribution across
every org, account and commit identity they use, folded into one combined
scorecard. For a given year it answers: how many commits, PRs, issues, reviews
and releases; how much code and docs moved (lines and files added/removed);
where, when (rhythm and streaks), and under which identity, counting work
GitHub's own contribution graph undercounts when it is spread across several
logins and emails. Everything runs locally against your own `gh` login; nothing
is written to GitHub and nothing leaves your machine.

There is a rendered example page (fully synthetic data) at
[`examples/demo.html`](examples/demo.html) — open it in a browser to see what the
report looks like.

Two scripts, Python 3.9+ standard library only, plus `git` and an authenticated
`gh`:

- `collect.py --year YYYY` writes `data/<year>.json` (one snapshot per year).
- `build.py` writes `out/velocity.html` (standalone, works from `file://`) and
  `out/velocity.artifact.html` (a fragment for publishing as a claude.ai
  Artifact). Both render identically.

## Read-only guarantee (and how it is enforced)

This tool reads repositories you have access to and never mutates anything
remote. That is enforced in code, not by convention:

- Every GitHub call goes through one module, `gh_read.py`. Its guard,
  `assert_read_only`, permits only `gh api -X GET …` (REST GET) and
  `gh api graphql` whose query text starts with `query` and contains no
  `mutation`. Any write method (`-X POST/PUT/PATCH/DELETE`), any request body
  (`--input`), or a field flag without an explicit `-X GET` is refused before
  the process runs. The only other `gh` commands the module will run are
  `gh auth status` and `gh auth token --user <login>`, neither of which can
  change anything remote. `tests/test_gh_read.py` proves each rejection.
- No other module shells out to `gh`. Multi-account reads pass a token from
  `gh auth token --user <login>` as `GH_TOKEN`; the tool never runs
  `gh auth switch`.
- `git` is used only for read-only shallow clones into a cache outside any repo,
  plus `git fetch` and `git log`. Each cache clone has its push URL set to
  `DISABLED` as a second guard.

## Requirements

- Python 3.9+ (standard library only — no `pip install`).
- `git` on your PATH.
- [`gh`](https://cli.github.com/) installed and logged in (`gh auth login`).
  Add more accounts with `gh auth login` again; the tool reads them all.

## Quick start

```
git clone <this repo> && cd gh-velocity-report
python3 init_config.py                 # writes config.json from your gh logins
python3 collect.py --year 2026         # writes data/2026.json (may take a while; see Runtime)
python3 build.py                       # writes out/velocity.html and out/velocity.artifact.html
open out/velocity.html                 # or just double-click it
```

`init_config.py` inspects the accounts you are logged in to (read-only), lists
their orgs and verified emails, detects your timezone, and writes a starting
`config.json`. Review it — especially `display_name`, `orgs` and, if you want
them, `personas` — before collecting. It will not overwrite an existing config
unless you pass `--force`.

If you would rather write the config by hand, copy `config.example.json` to
`config.json` and edit it.

### Adding another year

```
python3 collect.py --year 2025
python3 build.py
```

`build.py` renders every `data/*.json` it finds into one page with a year
switcher. A prior year's file, if present, is used for a same-months
year-over-year commit line.

### Multiple accounts and personas

List every login and email that is "you" under `identities`, and every
logged-in `gh` account under `gh_accounts` (used in order to read repos only
some accounts can see). By default all of them fold into a single persona named
from `display_name`. If you also commit under other people's identities you
control (for example separate work identities), group them with an optional
`personas` map:

```json
"identities": {
  "logins": ["octocat", "octocat-work", "robocat"],
  "emails": ["mona@example.com", "robo@example.com"],
  "personas": {
    "Octocat": {"logins": ["octocat", "octocat-work"], "emails": ["mona@example.com"]},
    "Robocat": {"logins": ["robocat"], "emails": ["robo@example.com"]}
  }
}
```

The headline stays the de-duplicated combined total; the Accounts section breaks
it down per persona and per login.

### Scope (per org / owner)

The page header has a **Scope** control next to the year switcher. `All` is the
combined scorecard; each other button focuses one org or owner group and
recomputes every section — totals, streaks, active days, cycle time, ratios and
insights — from only that group's repositories. The selection deep-links as
`#2026/example-org` (a bare `#2026` selects `All`) and persists across year
switches when that group exists in the target year.

## What is counted, and how

- **Combined accounts.** One scorecard across every login and commit identity you
  list, de-duplicated by commit sha (git) and by PR/issue node id (search).
- **Default branch only.** Commits and lines come from `git log` on each repo's
  default branch. Work on unmerged branches is not counted.
- **Merges excluded.** `git log --no-merges`; a squash-merged PR is one commit.
- **Identity matching.** A commit is yours when its author email is in your
  identity set (case-insensitive, GitHub's numeric noreply prefix stripped) or
  GitHub attributes it to one of your logins. A login-based presence pass plus a
  login→email harvest catch commits made under a known login whose email was not
  yet in your config; such emails are reported and written back to the config in
  use.
- **Self-reviews excluded.** Reviewing a PR authored by any of your own logins is
  not a review given; the count is reported as `self_reviews_excluded`.
- **Releases.** Counted by `published_at` in the year where the release author is
  one of your logins; drafts excluded.
- **Forks.** Counted only when one of your identities has commits in them.
- **Search API caveats.** PRs, issues and reviews come from the GitHub search API
  per login, split by month to stay under the 1000-result cap, de-duplicated by
  node id. A PR/issue/review is assigned to a scope by the repo it belongs to.
- **Timezone.** Weekday, hour, calendar and streak bucketing use the configured
  timezone; search date windows are UTC.
- **Dependabot context** is that bot's own PRs across repos with your activity,
  shown for reference in the combined scope and excluded from every other number.

## Runtime expectations

The first run of a year is dominated by the paced search API (roughly 15 to 50
minutes depending on how many logins you have and how active they are). Raw API
responses are cached per year under the cache dir, and git clones are fetched,
not re-cloned, so re-runs are much faster. A completed past year is served from
cache. Pass `--refresh` to bypass the API cache and refetch. Non-fatal failures
are collected into `meta.warnings` (and shown on the page) instead of aborting;
re-run to fill them in.

`collect.py` flags:

- `--year YYYY` — year to collect (default: current year in your timezone).
- `--config PATH` — config file (default: `./config.json`).
- `--repos owner/name,…` — scan just these repos, skipping discovery (for testing).
- `--no-lines` — skip git clones and line stats (commit counts come from the
  presence check instead).
- `--jobs N` — git-scan concurrency (default 6).
- `--data-dir DIR` — where to write `<year>.json` (default `./data`).
- `--out PATH` — explicit output path, overriding `--data-dir`.
- `--cache-dir DIR` — clone/API cache location. Also settable via
  `$VELOCITY_CACHE_DIR`; default `~/.cache/velocity-report`.
- `--refresh` — bypass the API response cache.

`build.py` flags: `--data-dir`, `--out`, and `--redact-private` (see Privacy).

## Privacy

`data/` and `out/` are git-ignored and contain private information:

- The JSON in `data/` stores **counts and repository names only** — never commit
  messages, never file paths. Private repo names do appear in it and in the
  rendered pages.
- The clone cache (default `~/.cache/velocity-report/<owner>/<repo>.git`) holds
  actual repository source code. Delete it any time with
  `rm -rf ~/.cache/velocity-report` (or your `--cache-dir`). It is outside this
  repository and never committed.

To share a page publicly, run `python3 build.py --redact-private`. It replaces
each private repo name with `<owner>/private-repo-N` (stable per build) in the
rendered pages. Private **owner** names are not changed — only repo names — so if
an owner name is itself sensitive, do not share the page.

## Config reference (`config.json`)

Nothing about a year lives in config; the year is a CLI flag. `config.json` is
git-ignored and user-local.

| Field | Meaning |
|---|---|
| `display_name` | Your name, shown in the header; also the default persona when no `personas` are set. Defaults to "Me". |
| `identities.logins` | GitHub logins that are "you". |
| `identities.emails` | Commit author emails that are "you". Auto-expanded with each login's `<id>+<login>@` and `<login>@users.noreply.github.com`. |
| `identities.personas` | Optional map grouping logins/emails into named people. Omit to fold everything into one persona. |
| `orgs` | Orgs whose repos are all in scope. |
| `personal_owners` | Logins whose owned repos are in scope. |
| `gh_accounts` | Logged-in gh accounts, in preference order for reading private repos. |
| `exclude_repos` | `owner/name` entries to skip entirely. |
| `bot_suffixes` | Author name/email suffixes treated as bots. |
| `timezone` | IANA timezone for weekday/hour/day bucketing and streaks. |
| `classification` | `doc_ext`, `doc_dirs`, `excluded_dirs`, `excluded_ext`, `lockfiles`. |

## Output JSON schema (`data/<year>.json`, tool_version 2.0.0)

```
meta      year, display_name, generated_at, through, timezone, tool_version,
          identities {logins, emails, personas}, accounts_used,
          coverage {logins_not_logged_in},
          repos_discovered / repos_candidates / repos_scanned_for_commits /
          repos_with_activity, prior_year_present, runtime_seconds,
          search_calls, self_reviews_excluded,
          discovered_emails [{email, commits, persona}], warnings []
scopes    object keyed by scope id. "all" is the combined scorecard; every
          org/owner group with activity (and "other") is its own scope,
          recomputed from only that group's repos. Each scope block has:
  totals        commits, code_add/del/net, docs_add/del/net, excl_add/del,
                code_files_add/del, docs_files_add/del, prs_opened/merged/
                closed_unmerged, issues_opened/closed, reviews, releases,
                self_reviews_excluded, repos, orgs, active_days,
                cycle_time {median_hours, p90_hours, population},
                ratios {docs_share, prs_per_issue_closed, merge_rate,
                        commits_per_active_day, elapsed_months,
                        avg_issues_closed_per_month, avg_prs_merged_per_month}
  insights      reproducible plain sentences computed for this scope
  by_persona    per persona (+ per-login rows and per-email rows)
  by_repo       one row per repo with activity (every counter + first/last)
  by_month      12 entries with every monthly counter
  by_identity   emails [{email, commits, persona}], logins [{login, …}]
  calendar      {YYYY-MM-DD: {commits, activity}} in the configured timezone
  by_weekday_hour  7x24 commit counts
  streaks       longest, current, active_days, busiest_day {date, commits}
  by_org        (only in the "all" scope) the cross-group comparison table
  context       (only in the "all" scope) dependabot {prs_created, prs_merged, …}
```

Older files without a `scopes` key still render: `build.py` treats their
top-level block as the sole `all` scope.

## Troubleshooting

- **Rate limits.** The search API is paced to about 30 requests/minute; secondary
  rate-limit and 5xx responses are retried with backoff. A long run is normal.
- **A transient search failure** appears in the Warnings callout on the page and
  in `meta.warnings`. Re-run `collect.py` for that year; cached responses make the
  re-run fast and it fills in the gap.
- **`error: required tool 'gh' is not on PATH`** — install `gh` and run
  `gh auth login`.
- **`error: config not found`** — run `python3 init_config.py`.
- **A login's private repos are missing.** Only accounts logged in to `gh` can
  read their private repos. Logins in your identity list that are not logged in
  have their public activity counted but their private repos are invisible; the
  page's Coverage note lists them.

## Tests

```
python3 -m unittest discover -s tests
```

Standard-library `unittest`, no network. Covers the read-only guard, the pure
aggregation functions, per-scope re-aggregation, the JSON schema, and a build
smoke test.

## License

MIT. See [LICENSE](LICENSE).
