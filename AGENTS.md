# Small Desk Guides source maintenance

This is the remote-backed public site repository. Keep one source writer per
branch. Its sole publication branch is `main`; source publication to that branch
also starts the already configured main/root GitHub Pages build. Push only when
the user has authorized publication, after reconciling origin/main and verifying
the final source. Never force-push or change Pages settings as part of a source fix.

Canonical verification from the repository root:

```sh
python3 -B scripts/check_site.py
python3 -B -m unittest discover -s tests -v
```

Both commands use the Python standard library and make no network calls.
The check discovers HTML pages and validates local targets, canonical/social
metadata, sitemap coverage, guide JSON-LD and bundled font/notice hashes.
Hosted CI and native Pages completion must be checked for the exact pushed SHA.
Static checks do not establish browser rendering or customer acceptance.

Preserve third-party font bytes and full notices. Update provenance from actual
source evidence when an asset changes; do not infer its original download version
from a current matching distribution. Font licenses do not license other site
content. Keep private content/video assets, tracking and promotional actions out
of this repository maintenance work.
