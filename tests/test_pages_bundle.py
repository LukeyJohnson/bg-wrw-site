"""Public artifact boundary: integrity, path safety and article identity."""
import gzip
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import wrw_pages_bundle as bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.release='a'*64
        self.files={'index.html':b'<html><article data-post-id="12">A public post</article></html>',
            '__wrw_release.json':bundle.stable({'release_id':self.release,'path_prefix':'/bg-wrw-site'})}

    def archive(self, files=None, extra=None, posts=None):
        files=files or self.files
        manifest={'schema':1,'release_id':self.release,'mode':'preview','path_prefix':'/bg-wrw-site',
            'files':[{'path':p,'bytes':len(b),'sha256':bundle.sha(b)} for p,b in sorted(files.items())],
            'posts':posts if posts is not None else [{'path':'/','id':12}]}
        manifest['package_id']=bundle.sha(bundle.stable(manifest))
        data={**{'site/'+p:b for p,b in files.items()},'publication.json':bundle.stable(manifest)}
        archive=self.root/'bundle.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            for name,body in data.items():
                member=tarfile.TarInfo(name);member.size=len(body);tar.addfile(member,io.BytesIO(body))
            if extra:extra(tar)
        return archive

    def verify(self, archive):return bundle.verify(archive,self.root/'out',self.release,bundle.file_sha(archive))

    def test_verified_public_payload_and_article_id(self):
        self.assertEqual(self.verify(self.archive())['release_id'],self.release)
        self.assertEqual((self.root/'out/index.html').read_bytes(),self.files['index.html'])

    def test_transfer_corruption_is_rejected_before_extraction(self):
        archive=self.archive()
        with self.assertRaisesRegex(ValueError,'checksum'):bundle.verify(archive,self.root/'out',self.release,'b'*64)
        self.assertFalse((self.root/'out').exists())

    def test_private_files_are_rejected_even_if_their_checksums_match(self):
        for name in ['.env','wp-config.php','backups/db.sql','secrets/password.txt','../escape','/absolute']:
            with self.subTest(name=name),self.assertRaises(ValueError):
                self.verify(self.archive({**self.files,name:b'private'}))

    def test_unmanifested_file_is_rejected(self):
        def add(tar):
            member=tarfile.TarInfo('site/extra.html');member.size=1;tar.addfile(member,io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError,'Unmanifested'):self.verify(self.archive(extra=add))

    def test_archive_links_are_rejected(self):
        def add(tar):
            member=tarfile.TarInfo('site/link');member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';tar.addfile(member)
        with self.assertRaisesRegex(ValueError,'links'):self.verify(self.archive(extra=add))

    def test_wrong_article_is_rejected_despite_http_style_html(self):
        with self.assertRaisesRegex(ValueError,'article identity'):
            self.verify(self.archive(posts=[{'path':'/','id':99}]))

    def test_project_prefix_preserves_article_text_and_final_canonical(self):
        try:from lxml import html
        except ImportError:self.skipTest('Local rebase uses lxml; verifier remains stdlib-only')
        source=self.root/'source';(source/'assets').mkdir(parents=True)
        (source/'assets/index.json').write_bytes(bundle.stable([{'path':'/article/','title':'A','text':'Original text'}]))
        (source/'index.html').write_text('<html><head><link rel="canonical" href="https://wrw.blackgrid.com.au/"></head><body><p>Original /article/ text.</p><a href="/article/">Read</a><form action="/search/"></form><script src="/assets/search.js" data-index="/assets/index.json"></script></body></html>')
        out=self.root/'rebased';bundle.rebase(source,out,'/bg-wrw-site');doc=html.parse(str(out/'index.html'))
        self.assertEqual(doc.xpath('//p/text()'),['Original /article/ text.'])
        self.assertEqual(doc.xpath('//a/@href'),['/bg-wrw-site/article/'])
        self.assertEqual(doc.xpath('//link/@href'),['https://wrw.blackgrid.com.au/'])
        index=doc.xpath('//script/@data-index')[0].removeprefix('/bg-wrw-site/')
        self.assertEqual(json.loads((out/index).read_text())[0]['path'],'/bg-wrw-site/article/')


if __name__=='__main__':unittest.main()
