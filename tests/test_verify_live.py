"""Public verification must reject incorrect pages and tolerate transient HTTP errors."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import wrw_verify_live as live


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.base='https://lukeyjohnson.github.io/bg-wrw-site/'
        self.asset=b'public asset'
        self.manifest={'release_id':'a'*64,'path_prefix':'/bg-wrw-site','mode':'preview',
            'posts':[{'path':'/original-slug/','id':123}],
            'files':[{'path':'assets/test.css','sha256':hashlib.sha256(self.asset).hexdigest()}]}
        self.style=b'<link rel="stylesheet" href="/bg-wrw-site/assets/test.css">'
        self.article=self.style+b'<link rel="canonical" href="https://wrw.blackgrid.com.au/original-slug/"><article data-post-id="123"></article>'
        self.unknown_200=False
        self.css_type='text/css'

    def request(self,url,method='GET'):
        if url.endswith('__wrw_release.json'):return json.dumps(self.manifest).encode(),{}
        if 'deliberately_missing' in url:
            if self.unknown_200:return b'wrong fallback',{}
            raise HTTPError(url,404,'Missing',{},None)
        if url.endswith('/original-slug/'):return self.article,{}
        if url.endswith('test.css'):return self.asset,{'Content-Type':self.css_type}
        return self.style+b'page',{}

    def check(self):
        with patch.object(live,'request',self.request):return live.verify_live(self.manifest,self.base,full=True,wait=0)

    def test_original_article_and_assets_pass(self):
        result=self.check();self.assertTrue(result['ok']);self.assertEqual(result['articles_checked'],1)

    def test_http_200_with_wrong_article_id_fails_and_names_the_path(self):
        self.article=self.article.replace(b'123',b'999')
        result=self.check();self.assertFalse(result['ok']);self.assertIn('/original-slug/',result['failures'][0])

    def test_wrong_canonical_fails(self):
        self.article=self.article.replace(b'/original-slug/',b'/different-slug/')
        self.assertFalse(self.check()['ok'])

    def test_wrong_asset_bytes_and_soft_404_fail(self):
        self.asset=b'changed';self.unknown_200=True
        result=self.check();self.assertFalse(result['ok']);self.assertEqual(len(result['failures']),2)

    def test_custom_domain_rejects_leftover_project_stylesheet_path(self):
        self.base='https://wrw.blackgrid.com.au/'
        self.manifest['path_prefix']=''
        result=self.check()
        self.assertFalse(result['ok'])
        self.assertTrue(any('stylesheet/script missing' in issue for issue in result['failures']))

    def test_custom_domain_accepts_root_stylesheet_path(self):
        self.base='https://wrw.blackgrid.com.au/'
        self.manifest['path_prefix']=''
        self.style=self.style.replace(b'/bg-wrw-site/',b'/')
        self.article=self.article.replace(b'/bg-wrw-site/',b'/')
        self.assertTrue(self.check()['ok'])

    def test_stylesheet_returning_html_is_a_rendering_failure(self):
        self.css_type='text/html'
        result=self.check()
        self.assertFalse(result['ok'])
        self.assertTrue(any('not served as CSS' in issue for issue in result['failures']))

    def test_optional_https_service_does_not_need_to_be_in_the_site_archive(self):
        self.article+=b'<script src="https://ads.example.org/ad.js"></script>'
        self.assertTrue(self.check()['ok'])

    def test_transient_503_retries_but_404_does_not(self):
        response=MagicMock();response.__enter__.return_value=response;response.status=200;response.read.return_value=b'ok';response.headers={}
        with patch.object(live,'urlopen',side_effect=[HTTPError(self.base,503,'temporary',{},None),response]) as opened,patch.object(live.time,'sleep'):
            self.assertEqual(live.request(self.base)[0],b'ok');self.assertEqual(opened.call_count,2)
        with patch.object(live,'urlopen',side_effect=HTTPError(self.base,404,'missing',{},None)) as opened:
            with self.assertRaises(HTTPError):live.request(self.base)
            self.assertEqual(opened.call_count,1)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        live.CONNECTIONS.values={}
        self.addCleanup(lambda: setattr(live.CONNECTIONS,'values',{}))

    def response(self,status=200):
        result=MagicMock();result.status=status;result.reason='response';result.headers={};result.length=0;result.isclosed.return_value=True
        return result

    def test_completed_requests_reuse_one_certificate_checked_connection(self):
        connection=MagicMock();connection.getresponse.return_value=self.response()
        with patch.object(live.http.client,'HTTPSConnection',return_value=connection) as factory:
            for _ in range(2):
                with live.urlopen(live.Request('https://wrw.blackgrid.com.au/'),context=live.TLS_CONTEXT):pass
            self.assertEqual(factory.call_count,1)
            self.assertIs(factory.call_args.kwargs['context'],live.TLS_CONTEXT)
            self.assertEqual(connection.request.call_count,2)

    def test_incomplete_response_discards_connection(self):
        connection=MagicMock();response=self.response();response.length=20;response.isclosed.return_value=False;connection.getresponse.return_value=response
        with patch.object(live.http.client,'HTTPSConnection',return_value=connection):
            with live.urlopen(live.Request('https://wrw.blackgrid.com.au/')):pass
            connection.close.assert_called_once();self.assertFalse(live.CONNECTIONS.values)

    def test_404_keeps_its_status_and_is_not_retried(self):
        connection=MagicMock();connection.getresponse.return_value=self.response(404)
        with patch.object(live.http.client,'HTTPSConnection',return_value=connection),patch.object(live.time,'sleep') as sleep:
            with self.assertRaises(HTTPError) as caught:live.request('https://wrw.blackgrid.com.au/missing/')
            self.assertEqual(caught.exception.code,404);sleep.assert_not_called();connection.close.assert_called_once()

    def test_https_redirect_preserves_host_header_and_refuses_downgrade(self):
        first=MagicMock();redirect=self.response(301);redirect.getheader.return_value='/next/';first.getresponse.return_value=redirect
        second=MagicMock();second.getresponse.return_value=self.response()
        with patch.object(live.http.client,'HTTPSConnection',side_effect=[first,second]):
            with live.urlopen(live.Request('https://wrw.blackgrid.com.au/')):pass
            self.assertEqual(second.request.call_args.args[1],'/next/')
        live.CONNECTIONS.values={};redirect.getheader.return_value='http://wrw.blackgrid.com.au/'
        with patch.object(live.http.client,'HTTPSConnection',return_value=first):
            with self.assertRaisesRegex(ValueError,'URL'):live.urlopen(live.Request('https://wrw.blackgrid.com.au/'))


if __name__=='__main__':unittest.main()
