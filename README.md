# smalldeskguides.github.io
Practical guides for making a small desk easier to use.

This is a plain static site published by the existing GitHub Pages main/root
build at <https://smalldeskguides.github.io/>.

Run the offline checks with Python 3.9 or later:

```sh
python3 -B scripts/check_site.py
python3 -B -m unittest discover -s tests -v
```

[Deployment and rollback](docs/deployment.md) describes source publication and
verification of the exact hosted commit. Static checks do not replace browser
acceptance.

[Bundled-font provenance and full notices](assets/fonts/README.md) cover Fraunces
and Inter. Their bytes are unchanged and match Fontsource Variable 5.3.0
distributions. The recorded font licenses apply to the fonts; no project-wide
license for other source/content is asserted here.
