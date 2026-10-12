# Deployment and recovery

The public repository is `smalldeskguides/smalldeskguides.github.io` and the site is
<https://smalldeskguides.github.io/>. Its existing GitHub Pages configuration uses
the `main` branch and repository root, with the managed `pages-build-deployment`
workflow. `.nojekyll` keeps this a plain static site. No local build, package
installation or secret is needed for the checks.

## Publish an authorized source change

Run from the repository root:

```sh
git fetch origin
python3 -B scripts/check_site.py
python3 -B -m unittest discover -s tests -v
git diff --check
git log --oneline origin/main..HEAD
```

Review the exact diff and reconcile any newer `origin/main` commits. Commit only
the intended source, then make a normal push after publication is authorized:

```sh
git push origin HEAD:main
git rev-parse HEAD
```

The `Site checks` workflow runs on pushes and pull requests. It checks all HTML,
local references (including fragments and CSS fonts), canonical/social metadata,
sitemap coverage, six current Article records, and font/notice checksums. It
does not fetch external links or observe a browser. Its checkout action is
bound to the [official v7.0.1 commit](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1)
with read-only contents permission and no persisted credentials.

`Site checks` and the existing managed Pages build run independently. Adding
this workflow does not change Pages settings or make it a deployment gate. A
passing local check is required before the push; separately verify the hosted
check and native Pages build for that same commit:

```sh
gh run list --repo smalldeskguides/smalldeskguides.github.io --commit COMMIT_SHA --json databaseId,headSha,name,status,conclusion,url
gh api repos/smalldeskguides/smalldeskguides.github.io/pages/builds/latest
```

Require `Site checks` to finish successfully with `headSha == COMMIT_SHA`, and
require the Pages build's `commit == COMMIT_SHA` and `status == built`. If either
is queued or running, completion is pending. After Pages finishes, use a public
GET for changed served files and compare their bytes with the checked source.
Those are source, CI and served-byte receipts; they do not establish live-browser
layout, accessibility, external-link availability or customer acceptance.

## Bound Pages waits and recover once

On a main push, `pages-watch` follows the source check and makes anonymous GETs
using the existing public APIs. It waits at most ten minutes, with spaced
observations; the job also has a twelve-minute hard limit. Its token permissions
remain `contents: read`. It neither deploys nor retries a build. A newer main push
cancels the old watcher, and a changed remote revision supersedes its evidence.

The watcher uses the public managed Pages workflow, its successful deploy job,
and a native `github-pages` deployment/status receipt bound to that run, SHA,
attempt time and site URL. The anonymous legacy Pages build endpoint returns
404 for this site, so CI does not request it or infer a raw build status from
workflow state. No new permission is needed for these observed public endpoints.
It also requires matching public HTML/assets/robots/sitemap bytes. It reports
`missing_run`, `stuck_build`, `deployment_failed`, `stale_revision`,
`serving_stale`, `missing_deployment_receipt`, or `observation_error` instead of treating an unchanged pending
status or source-CI success as deployment success. Updating a capture time cannot
refresh an old build occurrence. Earlier success for the same SHA cannot certify
a new build attempt. Stale serving receives the remaining bounded window for
propagation; no wait or retry loop continues indefinitely. HTTP reads use socket
timeouts and absolute-deadline checks between bounded body chunks; late bytes or
an unavailable final ownership recheck cannot produce success. The twelve-minute
hosted job limit supplies the separate process-level bound on blocking I/O.
At the default cadence, an unsuccessful ten-minute serving wait makes at most
50 GitHub API GETs; success needs one additional fresh ownership/proof readback
(at most five GETs). Mismatched bytes never cause a second full observation in
the same interval. API rate-limit or read errors stop as unverified observations,
without permission escalation or a retry loop.

For a read-only local observation from a clean checkout of the target commit:

```sh
python3 -B scripts/watch_pages.py --expected-sha COMMIT_SHA --once
```

Without `--once`, the same ten-minute maximum applies. Nonzero exit means that
the required deployment evidence was not verified; inspect the named condition
and the existing managed workflow before choosing recovery. Preserve the last
successful site. If a single retry is explicitly authorized, first verify that
main still equals the approved SHA and source checks passed, then use the
[supported build request](https://docs.github.com/en/rest/pages/pages#request-a-github-pages-build):

```sh
gh api repos/smalldeskguides/smalldeskguides.github.io/git/ref/heads/main
gh api --method POST repos/smalldeskguides/smalldeskguides.github.io/pages/builds
```

Capture the UTC request-start time immediately before the approved POST, then
pass it when watching that retry:

```sh
python3 -B scripts/watch_pages.py --expected-sha COMMIT_SHA --not-before REQUEST_START_UTC
```

This fence excludes a prior successful run/deployment of the same SHA, including
the time before a new retry appears in public run history. The request builds
the latest default-branch revision; it cannot bind a supplied
SHA, so stop on any remote drift and verify the response/run revision. GitHub
queues requests behind an in-progress build. Request at most one approved retry,
then inspect its terminal status and run the watcher. A second request, settings
change or rollback needs its own scope; never loop POSTs. Enabling automated
POST recovery would require broader Pages write authority and separate approval;
this change adds no such permission or credential.

The October 12 incident produced a `building` record without an exposed Pages
run or deployment record. One approved build request created a managed run and
published the same bytes successfully. This identifies the missing execution
evidence and the successful recovery route; GitHub did not expose the underlying
dispatch cause. The watcher detects recurrence and fails visibly in existing CI;
it cannot guarantee that GitHub's service never stalls.

## Roll back with a forward change

Keep the suspect commit and last verified production commit in the release
receipt. Before changing production, confirm the current remote head, inspect
intervening commits and get authorization for the recovery scope. Never reset
the remote or force-push over later work.

```sh
git fetch origin
git switch -c recover-site-CHANGE_ID origin/main
git revert --no-commit SUSPECT_COMMIT_SHA
git diff --stat
git diff
```

Resolve conflicts deliberately and preserve unrelated later changes. Keep
complete font notices with any font still distributed. If reverting a change
also removes the checking tools or notices, restore their known-good versions
or prepare a focused forward fix; do not waive verification to push the revert.
Then run the two canonical checks and `git diff --check`, review the final diff,
commit the recovery and reconcile the remote tip again before a normal push.
Verify hosted checks and Pages against the new recovery commit, followed by
served-byte readback. Rollback instructions grant no authority to execute one.
