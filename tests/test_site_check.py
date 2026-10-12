from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from check_site import audit


class SiteRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name)/'site'
        shutil.copytree(ROOT,self.site,ignore=shutil.ignore_patterns('.git','.github','scripts','tests','__pycache__'))

    def change(self,relative,old,new):
        path = self.site/relative
        text = path.read_text()
        self.assertIn(old,text)
        path.write_text(text.replace(old,new,1))

    def assertRejected(self,phrase):
        self.assertTrue(any(phrase in e for e in audit(self.site)['errors']))

    def test_current_site(self):
        result = audit(self.site)
        self.assertEqual(result['errors'],[])
        self.assertEqual((result['pages'],result['guideArticles']),(10,6))

    def test_missing_local_asset(self):
        (self.site/'assets/pins/five-minute-desk-reset.png').unlink()
        self.assertRejected('missing local target')

    def test_missing_fragment(self):
        self.change('index.html','href="#main"','href="#missing"')
        self.assertRejected('missing fragment target')

    def test_wrong_canonical_and_social_description(self):
        self.change('about/index.html','https://smalldeskguides.github.io/about/','https://example.com/about/')
        self.assertRejected('canonical must equal')
        self.change('index.html','property="og:description" content="Short','property="og:description" content="Changed')
        self.assertRejected('social description differs')

    def test_relative_social_image(self):
        self.change('index.html','https://smalldeskguides.github.io/assets/pins/three-desk-zones.jpg','assets/pins/three-desk-zones.jpg')
        self.assertRejected('expected an absolute HTTPS site URL')

    def test_malformed_article_and_wrong_binding(self):
        path = 'guides/five-minute-desk-reset/index.html'
        self.change(path,'{"@context":','{bad "@context":')
        self.assertRejected('invalid JSON-LD')
        self.change(path,'{bad "@context":','{"@context":')
        self.change(path,'"headline": "The five-minute desk reset"','"headline": "Different article"')
        self.assertRejected('JSON-LD headline differs')

    def test_sitemap_does_not_silently_omit_page(self):
        self.change('sitemap.xml','https://smalldeskguides.github.io/privacy/','https://smalldeskguides.github.io/missing/')
        self.assertRejected('sitemap differs')

    def test_missing_or_changed_license_notice(self):
        path = self.site/'assets/fonts/licenses/Fraunces-Fontsource-LICENSE.txt'
        path.write_text('Not the distributed notice')
        self.assertRejected('font/notice checksum differs')

    def test_encoded_escape_and_malformed_url(self):
        self.change('index.html','href="#main"','href="/%2e%2e/outside.html"')
        self.assertRejected('target escapes site root')
        self.change('index.html','href="/%2e%2e/outside.html"','href="https://[invalid"')
        self.assertRejected('malformed URL')


if __name__ == '__main__': unittest.main()
