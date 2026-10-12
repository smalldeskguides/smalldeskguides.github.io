"""Offline checks for the published static site; Python standard library only."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

BASE = 'https://smalldeskguides.github.io/'
ROOT = Path(__file__).resolve().parents[1]


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.refs = []
        self.ids = []
        self.meta = {}
        self.canonicals = []
        self.title = []
        self.heading = []
        self.h1_count = 0
        self.lang = None
        self.jsonld = []
        self.capture = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'html': self.lang = a.get('lang')
        if 'id' in a: self.ids.append(a['id'])
        for key in ('href', 'src'):
            if key in a: self.refs.append(a[key])
        if tag == 'link' and 'canonical' in a.get('rel','').lower().split():
            self.canonicals.append(a.get('href',''))
        if tag == 'meta':
            key = a.get('property',a.get('name','')).lower()
            self.meta.setdefault(key,[]).append(a.get('content',''))
            if key in ('og:image','og:url'): self.refs.append(a.get('content',''))
        if tag == 'title': self.capture = 'title'
        if tag == 'h1': self.h1_count += 1; self.capture = 'heading'
        if tag == 'script' and a.get('type','').lower() == 'application/ld+json':
            self.jsonld.append(''); self.capture = 'jsonld'

    def handle_endtag(self, tag):
        if tag in ('title','h1','script'): self.capture = None

    def handle_data(self, data):
        if self.capture == 'jsonld': self.jsonld[-1] += data
        elif self.capture: getattr(self,self.capture).append(data)


def audit(root):
    root = Path(root).resolve()
    errors = []
    refs = 0
    articles = 0
    paths = sorted(p for p in root.rglob('*.html') if '.git' not in p.relative_to(root).parts)
    pages = {p:Page(p.read_text(encoding='utf-8')) for p in paths}
    canonicals = set()

    def fail(path, message):
        errors.append(f'{path.relative_to(root)}: {message}')

    def single(path, values, label):
        if len(values) != 1 or not isinstance(values[0],str) or not values[0].strip():
            fail(path,f'expected one nonempty {label}')
            return ''
        return values[0]

    def local_ref(path, value, require_local=False):
        nonlocal refs
        refs += 1
        if not isinstance(value,str) or not value.strip() or any(ord(c)<32 for c in value):
            fail(path,'empty or invalid URL/reference'); return
        try:
            full = urlsplit(urljoin(BASE+str(path.relative_to(root).as_posix()),value))
        except ValueError:
            fail(path,f'malformed URL: {value}'); return
        if require_local and (urlsplit(value).scheme != 'https' or full.netloc != urlsplit(BASE).netloc):
            fail(path,f'expected an absolute HTTPS site URL: {value}'); return
        if full.scheme not in ('https','http','mailto','tel'):
            fail(path,f'unsupported URL scheme: {value}'); return
        if full.netloc != urlsplit(BASE).netloc:
            if require_local: fail(path,f'expected a site-local URL: {value}')
            return  # External links are not fetched or claimed live.
        try:
            target = (root/unquote(full.path).lstrip('/')).resolve()
        except (OSError,ValueError):
            fail(path,f'invalid local path: {value}'); return
        if root not in target.parents and target != root:
            fail(path,f'target escapes site root: {value}'); return
        if target.is_dir(): target /= 'index.html'
        if not target.is_file():
            fail(path,f'missing local target: {value}'); return
        if full.fragment and target in pages and unquote(full.fragment) not in pages[target].ids:
            fail(path,f'missing fragment target: {value}')

    if not paths: errors.append('no HTML pages found')
    for required in ('index.html','about/index.html','privacy/index.html','disclosure/index.html'):
        if root/required not in pages: errors.append(f'missing supporting page: {required}')
    for path,page in pages.items():
        rel = path.relative_to(root).as_posix()
        expected = BASE+rel.removesuffix('index.html')
        canonical = single(path,page.canonicals,'canonical link')
        if canonical != expected: fail(path,f'canonical must equal {expected}')
        if canonical in canonicals: fail(path,'duplicate canonical URL')
        canonicals.add(canonical)
        if page.lang != 'en': fail(path,'expected html lang="en"')
        if not ''.join(page.title).strip(): fail(path,'empty title')
        if page.h1_count != 1 or not ''.join(page.heading).strip(): fail(path,'expected one nonempty H1')
        if len(page.ids) != len(set(page.ids)): fail(path,'duplicate element id')
        meta = {key:single(path,page.meta.get(key,[]),key) for key in
                ('description','og:title','og:description','og:url','og:type','og:image','og:image:alt','twitter:card')}
        if meta['og:url'] != canonical: fail(path,'og:url differs from canonical')
        if meta['og:description'] != meta['description']: fail(path,'social description differs from description')
        if meta['twitter:card'] != 'summary_large_image': fail(path,'expected summary_large_image card')
        is_guide = rel.startswith('guides/')
        if meta['og:type'] != ('article' if is_guide else 'website'): fail(path,'wrong og:type for page')
        for value in page.refs: local_ref(path,value)
        local_ref(path,meta['og:image'],require_local=True)
        structured = []
        for raw in page.jsonld:
            try: data = json.loads(raw)
            except json.JSONDecodeError as exc:
                fail(path,f'invalid JSON-LD: {exc.msg}'); continue
            nodes = data if isinstance(data,list) else data.get('@graph',[data]) if isinstance(data,dict) else []
            if not isinstance(nodes,list):
                fail(path,'invalid JSON-LD node collection'); continue
            structured.extend(node for node in nodes if isinstance(node,dict) and node.get('@type') == 'Article')
        if is_guide:
            if len(structured) != 1: fail(path,'expected one JSON-LD Article')
            for article in structured:
                articles += 1
                for key,expected_value in (('@context','https://schema.org'),('headline',meta['og:title']),
                    ('description',meta['description']),('mainEntityOfPage',canonical),('image',meta['og:image'])):
                    if article.get(key) != expected_value: fail(path,f'JSON-LD {key} differs from page metadata')
                publisher = article.get('publisher',{})
                if not isinstance(publisher,dict) or publisher.get('name') != 'Small Desk Guides':
                    fail(path,'JSON-LD publisher differs')
                local_ref(path,article.get('image',''),require_local=True)
                local_ref(path,article.get('mainEntityOfPage',''),require_local=True)

    for path in sorted((root/'assets').rglob('*.css')):
        for value in re.findall(r'url\(\s*[\"\']?([^\)\"\']+)[\"\']?\s*\)',path.read_text()):
            local_ref(path,value.strip())
    sitemap = root/'sitemap.xml'
    try:
        urls = [node.text for node in ET.parse(sitemap).getroot().findall('{*}url/{*}loc')]
        if len(urls) != len(set(urls)) or set(urls) != canonicals: fail(sitemap,'sitemap differs from HTML canonical URLs')
        for value in urls: local_ref(sitemap,value,require_local=True)
    except (OSError,ET.ParseError) as exc: fail(sitemap,f'unreadable sitemap: {exc}')
    robots = root/'robots.txt'
    if not robots.is_file() or f'Sitemap: {BASE}sitemap.xml' not in robots.read_text():
        errors.append('robots.txt: missing canonical sitemap declaration')

    manifest = root/'assets/fonts/provenance.json'
    try:
        font_data = json.loads(manifest.read_text())
        entries = font_data['fonts']
        if not isinstance(entries,list) or len(entries) != 2: raise ValueError('expected two bundled font records')
        declared = set()
        for entry in entries:
            declared.add(entry['file'])
            if entry['license'] != 'OFL-1.1': raise ValueError('unexpected recorded font license')
            family = entry['family']
            if family not in ('Fraunces','Inter') or entry['file'] != family.lower()+'-latin-wght-normal.woff2':
                raise ValueError('font family/file binding differs')
            notices = entry['notices']
            required = {f'licenses/{family}-Fontsource-LICENSE.txt',f'licenses/{family}-Google-Fonts-OFL.txt'}
            if not isinstance(notices,list) or len(notices) != 2 or {n['file'] for n in notices} != required:
                raise ValueError(f'expected complete package and Google Fonts notices for {family}')
            for item in [entry,*notices]:
                path = (root/'assets/fonts'/item['file']).resolve()
                if root/'assets/fonts' not in path.parents: raise ValueError('font/notice path escapes font directory')
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                    fail(manifest,f'font/notice checksum differs: {item["file"]}')
        if declared != {p.name for p in (root/'assets/fonts').glob('*.woff2')}:
            fail(manifest,'bundled fonts differ from provenance records')
    except (OSError,KeyError,TypeError,ValueError) as exc: fail(manifest,f'invalid font provenance: {exc}')
    return {'pages':len(pages),'guideArticles':articles,'referencesChecked':refs,'fontRecords':2,'errors':errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    result = audit(parser.parse_args().root)
    print(json.dumps(result,indent=2))
    sys.exit(1 if result['errors'] else 0)
