#!/usr/bin/env python3
"""The ONE read-only gateway to GitHub for the velocity report.

Read-only contract (enforced by assert_read_only, proven in tests/test_gh_read.py):
  * The only shell command this module ever runs is `gh api ...`, plus two
    allowlisted auth subcommands that cannot change anything remote:
    `gh auth status` (lists logged-in accounts) and
    `gh auth token --user <login>` (reads a keyring token; no request body).
  * REST calls must be GET: an explicit `-X GET` / `--method GET`. Any other
    method (POST/PUT/PATCH/DELETE), any request-body flag (`--input`), or a
    field flag (`-f`/`-F`/...) without an explicit GET is refused.
  * GraphQL calls (`gh api graphql`) must carry a `query=` whose text starts
    with the word `query` and contains no `mutation`. Anything else is refused.

No other module in this tool shells out to `gh`. git is used elsewhere only for
read-only shallow clone/fetch/log. Every GitHub access goes through the helpers
here.

Multi-account: a call names a gh account login; its token is read once via
`gh auth token --user <login>` and passed as GH_TOKEN, so the tool never runs
`gh auth switch`. GH_PAGER=cat and NO_COLOR keep gh non-interactive.
"""
import json
import os
import subprocess
import threading
import time

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field"}
# gh flags that consume the following token as their value (so the value is not
# mistaken for the endpoint or another flag during validation).
_VALUE_FLAGS = _FIELD_FLAGS | {
    "-X", "--method", "-H", "--header", "-q", "--jq", "--input",
    "--cache", "--hostname", "-t", "--template", "-p", "--preview",
}
_CALL_TIMEOUT = 180  # seconds, per gh invocation (subprocess-level; macOS has no timeout(1))


class GhReadError(RuntimeError):
    """A gh call failed after retries, or returned unparseable output."""


class GhReadRefused(GhReadError):
    """A call was refused by the read-only guard before ever running."""


# ---------- the guard ----------

def _split_flag(tok):
    """('-X', 'POST') from '-X=POST' or '--method=POST'; else (tok, None)."""
    if tok.startswith("--") and "=" in tok:
        k, v = tok.split("=", 1)
        return k, v
    return tok, None


def assert_read_only(args):
    """Raise GhReadRefused unless `args` (the tokens after `gh`) is a permitted
    read: `api` GET on REST, or `api graphql` with a mutation-free query."""
    args = list(args)
    if not args or args[0] != "api":
        raise GhReadRefused(f"only `gh api` is permitted, got: {' '.join(args[:2]) or '(empty)'}")

    methods = []
    field_values = []          # values of -f/-F/... flags
    has_input = False
    is_graphql = False
    i = 1
    while i < len(args):
        raw = args[i]
        key, inline = _split_flag(raw)
        if key in ("-X", "--method"):
            val = inline if inline is not None else (args[i + 1] if i + 1 < len(args) else "")
            methods.append(val)
            i += 1 if inline is not None else 2
            continue
        if key == "--input":
            has_input = True
            i += 1 if inline is not None else 2
            continue
        if key in _FIELD_FLAGS:
            val = inline if inline is not None else (args[i + 1] if i + 1 < len(args) else "")
            field_values.append(val)
            i += 1 if inline is not None else 2
            continue
        if key in _VALUE_FLAGS:
            i += 1 if inline is not None else 2
            continue
        if not raw.startswith("-") and raw == "graphql":
            is_graphql = True
        i += 1

    if has_input:
        raise GhReadRefused("--input sends a request body; refused")
    for m in methods:
        if m.upper() != "GET":
            raise GhReadRefused(f"method {m!r} is not GET; refused")

    if is_graphql:
        query = None
        for fv in field_values:
            if fv.startswith("query="):
                query = fv[len("query="):]
                break
        if query is None:
            raise GhReadRefused("graphql call without a query= field; refused")
        stripped = query.lstrip()
        if not stripped.startswith("query"):
            raise GhReadRefused("graphql text must start with `query`; refused")
        if "mutation" in query.lower():
            raise GhReadRefused("graphql text contains `mutation`; refused")
        return

    # REST: a field flag turns `gh api` into a POST unless GET is explicit.
    if field_values and not any(m.upper() == "GET" for m in methods):
        raise GhReadRefused("REST field flag without an explicit -X GET; refused")


# ---------- token / env ----------

_token_lock = threading.Lock()
_tokens = {}


def token_for(login):
    """Read-only keyring token for a logged-in gh account. Cached."""
    with _token_lock:
        if login not in _tokens:
            r = subprocess.run(["gh", "auth", "token", "--user", login],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0 or not r.stdout.strip():
                raise GhReadError(f"no token for account {login!r}: {(r.stderr or '').strip()[:200]}")
            _tokens[login] = r.stdout.strip()
        return _tokens[login]


def auth_status_text():
    """Raw text of `gh auth status`. Read-only: this subcommand only reports the
    accounts logged in to gh and touches nothing remote. gh writes it to stdout
    on newer versions and stderr on older ones, so both are returned joined."""
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "GH_PAGER": "cat", "NO_COLOR": "1", "CLICOLOR": "0"})
    except (OSError, subprocess.SubprocessError) as e:
        raise GhReadError(f"gh auth status failed: {e}")
    return ((r.stdout or "") + "\n" + (r.stderr or "")).strip()


def _env(account):
    env = {**os.environ, "GH_PAGER": "cat", "NO_COLOR": "1", "CLICOLOR": "0"}
    if account:
        env["GH_TOKEN"] = token_for(account)
    return env


# ---------- runner ----------

def _run(args, account=None, retries=6):
    """Validate, run `gh <args>`, return stdout. Backoff on secondary rate
    limit and 5xx; raise GhReadError on anything else."""
    assert_read_only(args)
    for attempt in range(retries):
        try:
            r = subprocess.run(["gh", *args], capture_output=True, text=True,
                               env=_env(account), timeout=_CALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            if attempt == retries - 1:
                raise GhReadError(f"gh timed out after {_CALL_TIMEOUT}s: {' '.join(args[:3])}")
            time.sleep(5 * (attempt + 1))
            continue
        if r.returncode == 0:
            return r.stdout
        err = (r.stderr or r.stdout or "").strip()
        low = err.lower()
        if "secondary rate limit" in low or "abuse" in low:
            time.sleep(60 * (attempt + 1))
            continue
        if "rate limit" in low or "429" in err or "502" in err or "503" in err or "504" in err:
            time.sleep(20 * (attempt + 1))
            continue
        raise GhReadError(err[:400])
    raise GhReadError("gh failed after retries: " + " ".join(args[:4]))


def _decode_concatenated(raw):
    """`gh api --paginate` prints one JSON value per page, concatenated. Join
    arrays into one list; if pages are objects, return the list of objects."""
    dec = json.JSONDecoder()
    pos, out = 0, []
    n = len(raw)
    while pos < n:
        while pos < n and raw[pos] in " \n\r\t":
            pos += 1
        if pos >= n:
            break
        val, pos = dec.raw_decode(raw, pos)
        if isinstance(val, list):
            out.extend(val)
        else:
            out.append(val)
    return out


# ---------- public REST ----------

def rest_get(path, account=None, params=None, jq=None):
    """One GET to a REST endpoint. `params` become query-string fields (safe on
    GET). Returns parsed JSON, or raw text lines when `jq` is given."""
    args = ["api", "-X", "GET", path]
    for k, v in (params or {}).items():
        args += ["-f", f"{k}={v}"]
    if jq is not None:
        args += ["--jq", jq]
        return _run(args, account)
    return json.loads(_run(args, account) or "null")


def rest_get_all(path, account=None, params=None):
    """Every item of a paginated list endpoint (follows Link headers)."""
    args = ["api", "-X", "GET", path, "--paginate"]
    for k, v in (params or {}).items():
        args += ["-f", f"{k}={v}"]
    return _decode_concatenated(_run(args, account))


def graphql(query, variables=None, account=None):
    """A GraphQL query (never a mutation). `variables` are passed as -F fields.
    Returns the parsed `data` object; raises if the response carries errors."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in (variables or {}).items():
        if isinstance(v, (list, dict)):
            v = json.dumps(v)
        args += ["-F", f"{k}={v}"]
    payload = json.loads(_run(args, account) or "null")
    if payload is None:
        raise GhReadError("empty graphql response")
    if payload.get("errors"):
        raise GhReadError("graphql errors: " + json.dumps(payload["errors"])[:400])
    return payload.get("data")


class Search:
    """Paced GitHub search. The search API allows ~30 requests/min, so calls are
    spaced `interval` seconds apart across threads."""

    def __init__(self, account=None, interval=2.1):
        self.account = account
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0
        self.calls = 0

    def _pace(self):
        wait = self.last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def count(self, q, kind="issues"):
        """total_count for a search/{kind} query."""
        with self.lock:
            self._pace()
            out = rest_get(f"search/{kind}", self.account,
                           params={"q": q, "per_page": "1"}, jq=".total_count")
            self.last = time.monotonic()
            self.calls += 1
            return int((out or "0").strip() or 0)

    def items(self, q, kind="issues", per_page=100, max_pages=10, fields=None, jq=None):
        """All items of a search query, paged. GitHub caps search at 1000
        results (10 pages of 100); the caller splits by month to stay under it.
        `jq` overrides `fields` with an explicit projection over `.items`."""
        collected = []
        if jq is None and fields:
            jq = ".items | map({" + ",".join(f"{f}:.{f}" for f in fields) + "})"
        for page in range(1, max_pages + 1):
            with self.lock:
                self._pace()
                params = {"q": q, "per_page": str(per_page), "page": str(page)}
                if jq:
                    raw = rest_get(f"search/{kind}", self.account, params=params, jq=jq)
                    chunk = json.loads(raw or "[]")
                else:
                    resp = rest_get(f"search/{kind}", self.account, params=params)
                    chunk = (resp or {}).get("items", [])
                self.last = time.monotonic()
                self.calls += 1
            collected.extend(chunk)
            if len(chunk) < per_page:
                break
        return collected
