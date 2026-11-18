"""Round-trip layered delivery, deletions, corruption and extraction boundaries."""
import gzip
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import wrw_layers as layers
from wrw_pages_bundle import file_sha, sha, stable


class LayerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.article=b'<html><head><link rel="canonical" href="https://wrw.blackgrid.com.au/post/"></head><body><article data-post-id="1">Original</article></body></html>'
        self.base=self.package('base',{'post/index.html':self.article,'media/old.webp':b'preserved-image','old.html':b'old path'},'a'*64)
        archive=self.root/'base.tar.gz'
        self.tar(archive,{'publication.json':Path(self.base['publication']).read_bytes(),**{'site/'+p:f.read_bytes() for p,f in self.files(self.base).items()}})
        self.receipt={**self.base,'archive':str(archive),'release_tag':'wrw-'+'c'*64}

    def package(self,name,files,rid):
        root=self.root/name;site=root/'site';site.mkdir(parents=True)
        marker={'schema':1,'release_id':rid,'mode':'production','path_prefix':'','adapter_sha256':'d'*64}
        files={**files,'__wrw_release.json':stable(marker)+b'\n'}
        for p,b in files.items():
            f=site/p;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b)
        pub={**marker,'files':[{'path':p,'sha256':sha(b),'bytes':len(b)} for p,b in sorted(files.items())],
             'posts':[{'path':'/post/','file':'post/index.html','id':1}]}
        pub['package_id']=sha(stable(pub));manifest=root/'publication.json';manifest.write_bytes(stable(pub)+b'\n')
        return {'publication':str(manifest),'site':str(site),'release_id':rid,'package_id':pub['package_id']}

    def files(self,p):
        return {str(f.relative_to(p['site'])):f for f in Path(p['site']).rglob('*') if f.is_file()}

    def tar(self,file,files,extra=None):
        with tarfile.open(file,'w:gz') as tar:
            for name,body in files.items():
                info=tarfile.TarInfo(name);info.size=len(body);tar.addfile(info,io.BytesIO(body))
            if extra:tar.addfile(extra)

    def archives(self,transport):
        values={file_sha(Path(self.receipt['archive'])):Path(self.receipt['archive'])}
        values.update({r['sha256']:Path(r['file']) for r in transport['upload_layers']})
        return values

    def test_delta_roundtrip_matches_manifest_without_reuploading_media(self):
        candidate=self.package('next',{'post/index.html':self.article.replace(b'Original',b'Updated'),
            'media/old.webp':b'preserved-image','media/new.webp':b'new-image'},'b'*64)
        delivery=layers.prepare(candidate,self.receipt,self.root/'delta')
        self.assertNotIn('media/old.webp',delivery['changed_paths'])
        self.assertEqual(delivery['removed_paths'],['old.html'])
        pub=layers.assemble(delivery['delivery'],self.archives(delivery),self.root/'assembled',delivery['sha256'],'b'*64)
        actual={str(p.relative_to(self.root/'assembled')):file_sha(p) for p in (self.root/'assembled').rglob('*') if p.is_file()}
        self.assertEqual(actual,{r['path']:r['sha256'] for r in pub['files']})
        self.assertFalse((self.root/'assembled/old.html').exists())
        with tarfile.open(delivery['upload_layers'][0]['file']) as tar:
            self.assertNotIn('site/media/old.webp',tar.getnames())

    def test_second_layer_and_nochange_do_not_require_new_base_or_upload(self):
        one=layers.prepare(self.base,self.receipt,self.root/'one')
        self.assertEqual(one['upload_layers'],[])
        two=layers.prepare(self.base,one,self.root/'two')
        self.assertEqual(two['upload_layers'],[])
        self.assertEqual(one['package_id'],two['package_id'])
        layers.assemble(two['delivery'],self.archives(two),self.root/'out')

    def test_feed_promotion_only_uploads_changed_feed_and_marker(self):
        held=self.package('held',{'post/index.html':self.article,'feed.xml':b'<rss>old</rss>','media/old.webp':b'preserved-image'},'b'*64)
        a=layers.prepare(held,self.receipt,self.root/'held-delta')
        ready=self.package('ready',{'post/index.html':self.article,'feed.xml':b'<rss>new</rss>','media/old.webp':b'preserved-image'},'e'*64)
        b=layers.prepare(ready,a,self.root/'ready-delta')
        self.assertEqual(set(b['changed_paths']),{'feed.xml','__wrw_release.json'})
        archives={**self.archives(a),**self.archives(b)}
        layers.assemble(b['delivery'],archives,self.root/'ready-out')
        self.assertEqual((self.root/'ready-out/feed.xml').read_bytes(),b'<rss>new</rss>')
        self.assertEqual(layers.read_files(b,lambda p:p.endswith('.xml')),{'/feed.xml':b'<rss>new</rss>'})

    def test_corrupted_layer_and_manifest_are_refused(self):
        d=layers.prepare(self.base,self.receipt,self.root/'d')
        with self.assertRaisesRegex(ValueError,'manifest checksum'):
            layers.assemble(d['delivery'],self.archives(d),self.root/'out','0'*64)
        archives=self.archives(d)
        Path(self.receipt['archive']).write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError,'checksum/size'):
            layers.assemble(d['delivery'],archives,self.root/'out2')

    def test_missing_owner_cannot_silently_drop_a_file(self):
        d=layers.prepare(self.base,self.receipt,self.root/'d');file=Path(d['delivery']);value=json.loads(file.read_text())
        del value['owners']['post/index.html'];file.write_bytes(stable(value))
        with self.assertRaisesRegex(ValueError,'ownership'):layers.load_delivery(file)

    def test_stale_dispatch_is_fenced_before_deployment(self):
        d=layers.prepare(self.base,self.receipt,self.root/'d')
        request=self.root/'current-request.json'
        current={'package_id':d['package_id'],'release_id':d['release_id'],'archive_sha256':d['sha256'],
                 'transport':'layers','baseline':d['baseline']}
        request.write_bytes(stable(current))
        with patch('wrw_verify_live.request',return_value=(stable({'release_id':'a'*64}),{})):
            layers.check_dispatch(d['delivery'],d['sha256'],request,'https://wrw.blackgrid.com.au/')
        current['package_id']='f'*64;request.write_bytes(stable(current))
        with self.assertRaisesRegex(ValueError,'superseded'):
            layers.check_dispatch(d['delivery'],d['sha256'],request,'https://wrw.blackgrid.com.au/')

    def test_tar_symlinks_and_traversal_are_refused_even_when_not_selected(self):
        for name,kind in [('site/../escape',tarfile.REGTYPE),('site/link',tarfile.SYMTYPE)]:
            with self.subTest(name=name):
                folder=self.root/str(kind);folder.mkdir(exist_ok=True)
                info=tarfile.TarInfo(name);info.type=kind;info.linkname='/tmp/escape'
                bad=folder/'bad.tar.gz';self.tar(bad,{},info)
                d=layers.prepare(self.base,self.receipt,folder/'delta')
                v=json.loads(Path(d['delivery']).read_text());old=v['layers'][0]['sha256'];digest=file_sha(bad)
                v['layers'][0].update(sha256=digest,bytes=bad.stat().st_size)
                v['owners']={p:digest for p in v['owners']};Path(d['delivery']).write_bytes(stable(v))
                with self.assertRaises(ValueError):layers.assemble(d['delivery'],{digest:bad},folder/'out')


if __name__=='__main__':unittest.main()
