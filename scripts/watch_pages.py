"""Bounded, GET-only verification of this site's exact Pages revision."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
REPO = "smalldeskguides/smalldeskguides.github.io"
API = "https://api.github.com/repos/" + REPO + "/"
SITE = "https://smalldeskguides.github.io/"
PAGES_WORKFLOW = 380146248
LIMIT = 3 * 1024 * 1024


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def assess(expected, remote, build, runs, now, expired=False, not_before=None):
    """Classify native evidence without confusing source CI with a Pages run."""
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ValueError("expected SHA must be a full lowercase Git SHA")
    result = {"expectedSha": expected, "state": "pending", "codes": [],
              "verified": False, "automaticRetry": False}
    if remote != expected:
        return {**result, "state": "superseded", "codes": ["REMOTE_HEAD_CHANGED"]}
    if (build is not None and not isinstance(build, dict)) or not isinstance(runs, list):
        raise ValueError("invalid native response shape")
    created = stamp(build["created_at"]) if build else None
    if created and (now - created).total_seconds() < -30:
        raise ValueError("build occurrence time is in the future")
    if build and build.get("status") not in ("queued", "building", "built", "errored"):
        raise ValueError("unknown Pages build status")
    if build and build.get("commit") != expected:
        return {**result, "state": "stale_revision" if expired else "pending",
                "codes": ["NATIVE_REVISION_DIFFERS"]}
    if created and not_before and created < not_before:
        return {**result, "state": "stale_attempt" if expired else "pending",
                "codes": ["NATIVE_ATTEMPT_PRECEDES_REQUEST"]}
    matched = []
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("invalid Pages run shape")
        if run.get("head_sha") != expected or run.get("workflow_id") != PAGES_WORKFLOW:
            continue
        occurred = stamp(run.get("run_started_at") or run["created_at"])
        if (occurred - now).total_seconds() > 30:
            raise ValueError("run occurrence time is in the future")
        if (type(run.get("id")) is not int or run["id"] < 1
                or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1):
            raise ValueError("invalid Pages run identity")
        # A success from an earlier rebuild of the same SHA cannot certify this attempt.
        if (created is None or occurred >= created) and (not_before is None or occurred >= not_before):
            matched.append(run)
    run = max(matched, key=lambda row: (stamp(row.get("run_started_at") or row["created_at"]),
                                      row["run_attempt"], row["id"]),
              default=None)
    occurrence = created or (stamp(run.get("run_started_at") or run["created_at"]) if run else now)
    age = (now - occurrence).total_seconds()
    result.update(buildStatus=build["status"] if build else None,
                  buildCreatedAt=build["created_at"] if build else None,
                  nativeEvidenceKind="pages_build_api" if build else "public_pages_workflow_and_deployment",
                  buildAgeSeconds=max(0, int(age)), pagesRunId=run["id"] if run else None)
    error = build.get("error", {}).get("message") if build else None
    if (build and build["status"] == "errored") or error or (run and run["status"] == "completed"
                                                and run["conclusion"] != "success"):
        return {**result, "state": "deployment_failed", "codes": ["PAGES_FAILED"],
                "nativeError": str(error)[:500] if error else None,
                "runConclusion": run["conclusion"] if run else None}
    overdue = expired or age >= 600
    if not run:
        return {**result, "state": "missing_run" if overdue else "pending",
                "codes": ["PAGES_RUN_MISSING"] + (["BUILD_WINDOW_EXCEEDED"] if overdue else [])}
    if run["status"] == "completed" and run["conclusion"] == "success":
        if build and build["status"] == "built":
            return {**result, "state": "awaiting_served_bytes"}
        if not build:
            deployment = run.get("deployment")
            if deployment:
                status = deployment["status"]
                started = stamp(run.get("run_started_at") or run["created_at"])
                prefix = f"https://github.com/{REPO}/actions/runs/{run['id']}/job/"
                bound = (deployment["sha"] == expected and deployment["environment"] == "github-pages"
                         and started <= stamp(deployment["created_at"]) <= stamp(status["created_at"])
                         and stamp(status["created_at"]) <= now + timedelta(seconds=30)
                         and status["environment"] == "github-pages"
                         and status["log_url"].startswith(prefix)
                         and status["log_url"][len(prefix):].isdigit())
                if bound and status["state"] in ("failure", "error"):
                    return {**result, "state": "deployment_failed", "codes": ["PAGES_FAILED"]}
                job = run.get("deployJob", {})
                steps = job.get("steps", [])
                if (bound and status["state"] == "success" and status["environment_url"] == SITE
                        and job.get("head_sha") == expected and job.get("name") == "deploy"
                        and job.get("status") == "completed" and job.get("conclusion") == "success"
                        and str(job.get("id")) == status["log_url"][len(prefix):]
                        and any(s.get("name") == "Deploy to GitHub Pages" and s.get("status") == "completed"
                                and s.get("conclusion") == "success" for s in steps)):
                    return {**result, "state": "awaiting_served_bytes", "deploymentId": deployment["id"],
                            "deploymentStatusId": status["id"], "deploymentJobId": job["id"]}
            return {**result, "state": "missing_deployment_receipt" if overdue else "pending",
                    "codes": ["NATIVE_DEPLOYMENT_PROOF_MISSING"]}
    return {**result, "state": "stuck_build" if overdue else "pending",
            "codes": ["BUILD_WINDOW_EXCEEDED"] if overdue else []}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_bytes(url, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("watch deadline reached")
    request = Request(url, method="GET", headers={
        "User-Agent": "SmallDeskGuides-Pages-watch/1", "Accept-Encoding": "identity",
        "Accept": "application/vnd.github+json" if url.startswith(API) else "*/*",
        "Cache-Control": "no-cache"})
    with build_opener(NoRedirect()).open(request, timeout=min(10, remaining)) as response:
        if response.status != 200 or response.url != url:
            raise ValueError("unexpected HTTP response or redirect")
        chunks = []
        size = 0
        while size <= LIMIT:
            if time.monotonic() >= deadline:
                raise TimeoutError("absolute watch deadline reached")
            chunk = response.read1(min(65536, LIMIT + 1 - size))
            if time.monotonic() >= deadline:
                raise TimeoutError("response completed after absolute deadline")
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        body = b"".join(chunks)
    if len(body) > LIMIT:
        raise ValueError("response exceeds size limit")
    return body


def observe(expected, deadline, not_before=None):
    def read(path):
        return json.loads(get_bytes(API + path, deadline))
    remote = read("git/ref/heads/main")["object"]["sha"]
    runs = read(f"actions/workflows/{PAGES_WORKFLOW}/runs?head_sha={expected}&per_page=10")["workflow_runs"]
    matched = [r for r in runs if r["head_sha"] == expected and r["workflow_id"] == PAGES_WORKFLOW
               and (not_before is None or stamp(r.get("run_started_at") or r["created_at"]) >= not_before)]
    latest = max(matched, key=lambda r: (stamp(r.get("run_started_at") or r["created_at"]),
                                        r["run_attempt"], r["id"]), default=None)
    if latest and latest["status"] == "completed" and latest["conclusion"] == "success":
        started = stamp(latest.get("run_started_at") or latest["created_at"])
        deployments = read(f"deployments?sha={expected}&per_page=10")
        candidates = [d for d in deployments if d["sha"] == expected and d["environment"] == "github-pages"
                      and stamp(d["created_at"]) >= started]
        if candidates:
            deployment = max(candidates, key=lambda d: (stamp(d["created_at"]), d["id"]))
            statuses = read(f"deployments/{deployment['id']}/statuses?per_page=10")
            if statuses:
                deployment["status"] = max(statuses, key=lambda s: (stamp(s["created_at"]), s["id"]))
                latest["deployment"] = deployment
                jobs = read(f"actions/runs/{latest['id']}/attempts/{latest['run_attempt']}/jobs?per_page=20")["jobs"]
                latest["deployJob"] = next((j for j in jobs if j["name"] == "deploy"), {})
    return remote, None, runs


def public_files(root):
    root = root.resolve()
    paths = [p for p in root.rglob("*.html") if ".git" not in p.relative_to(root).parts]
    paths += [p for p in (root / "assets").rglob("*") if p.is_file() and p.suffix != ".md"]
    paths += [root / "robots.txt", root / "sitemap.xml"]
    files = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("public file escapes checked source")
        name = path.relative_to(root).as_posix()
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files or "index.html" not in files:
        raise ValueError("public file inventory is empty or missing home page")
    return files


def serving(files, deadline):
    def check(item):
        name, expected = item
        route = name.removesuffix("index.html") if name.endswith("index.html") else name
        url = SITE + quote(route, safe="/")
        try:
            body = get_bytes(url, deadline)
            digest = hashlib.sha256(body).hexdigest()
            return {"path": name, "sha256": digest, "exactBytes": digest == expected}
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            return {"path": name, "exactBytes": False,
                    "httpStatus": exc.code if isinstance(exc, HTTPError) else None}
    with ThreadPoolExecutor(max_workers=2) as pool:
        checks = list(pool.map(check, sorted(files.items())))
    return checks


def watch(expected, files, timeout=600, interval=60, once=False, *, not_before=None,
          native=observe, readback=serving, clock=time.monotonic, sleep=time.sleep,
          utc=lambda: datetime.now(timezone.utc), emit=lambda value: print(json.dumps(value), flush=True)):
    deadline = clock() + timeout
    previous = None
    snapshot = None
    native_observed_at = None
    while True:
        expired = clock() >= deadline
        try:
            if expired:
                if snapshot is None:
                    raise TimeoutError("no native observation before deadline")
                remote, build, runs = snapshot
            else:
                remote, build, runs = native(expected, deadline, not_before)
                snapshot = remote, build, runs
                native_observed_at = utc().isoformat()
            report = assess(expected, remote, build, runs, utc(), expired, not_before)
            if report["state"] == "awaiting_served_bytes":
                if expired:
                    raise TimeoutError("fresh ownership recheck unavailable at deadline")
                checks = readback(files, deadline)
                # Recheck native identities after byte readback; later pushes supersede this watch.
                if clock() >= deadline:
                    raise TimeoutError("public readback exhausted ownership recheck deadline")
                remote, build, runs = native(expected, deadline, not_before)
                if clock() >= deadline:
                    raise TimeoutError("ownership recheck exceeded deadline")
                snapshot = remote, build, runs
                native_observed_at = utc().isoformat()
                current = assess(expected, remote, build, runs, utc(), clock() >= deadline, not_before)
                if current["state"] != "awaiting_served_bytes":
                    report = current
                else:
                    report = current
                    good = (bool(files) and len(checks) == len(files) and {c["path"] for c in checks} == set(files)
                            and all(c["exactBytes"] for c in checks))
                    report.update(state="verified" if good else "serving_stale",
                                  verified=good, publicFileMatches=sum(c["exactBytes"] for c in checks),
                                  publicFilesChecked=len(checks), checks=checks,
                                  codes=[] if good else ["SERVED_BYTES_DIFFER"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report = {"expectedSha": expected, "state": "observation_error", "verified": False,
                      "codes": ["READBACK_UNAVAILABLE"], "errorType": type(exc).__name__,
                      "automaticRetry": False}
        terminal = report["state"] in ("verified", "superseded", "deployment_failed", "missing_run", "missing_deployment_receipt",
                                       "stuck_build", "stale_revision", "stale_attempt", "observation_error")
        finished = terminal or once or clock() >= deadline
        report.update(observedAt=utc().isoformat(), finished=finished,
                      nativeObservedAt=native_observed_at,
                      browserRenderingVerified=False, nativeMutations=0)
        if finished and not report["verified"]:
            report["recovery"] = {"eligibleForReview": report["state"] in
                ("deployment_failed", "missing_run", "stuck_build", "stale_revision", "serving_stale"),
                "requiresApproval": True, "maxAttempts": 1,
                "preconditions": "Verify unchanged expected main and passing source checks; preserve prior site.",
                "command": f"gh api --method POST repos/{REPO}/pages/builds"}
        key = (report["state"], tuple(report.get("codes", [])), report.get("pagesRunId"))
        if key != previous or finished:
            emit(report)
            previous = key
        if finished:
            return report
        sleep(min(interval, max(0, deadline - clock())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true", help="Report once without waiting or triggering recovery")
    parser.add_argument("--not-before", help="UTC timestamp captured before an approved same-SHA retry")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha):
        parser.error("expected SHA must be a full lowercase Git SHA")
    if not 60 <= args.timeout_seconds <= 600 or not 60 <= args.interval_seconds <= 120:
        parser.error("timeout must be 60..600 seconds and interval 60..120 seconds")
    try:
        not_before = stamp(args.not_before) if args.not_before else None
    except ValueError:
        parser.error("not-before must be a timezone-aware ISO timestamp")
    if not_before and not_before > datetime.now(timezone.utc) + timedelta(seconds=30):
        parser.error("not-before cannot be in the future")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if head != args.expected_sha or subprocess.check_output(
            ["git", "status", "--porcelain=v1"], cwd=ROOT, text=True).strip():
        parser.error("expected SHA must match a clean checked-out source")
    report = watch(args.expected_sha, public_files(ROOT), args.timeout_seconds,
                   args.interval_seconds, args.once, not_before=not_before)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
