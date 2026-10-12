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
