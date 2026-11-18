#!/usr/bin/env python3
"""Verify deployed release identity, article IDs and public asset availability."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
import ssl
import http.client
import threading
from html.parser import HTMLParser
from pathlib import Path
import tarfile
import time
import xml.etree.ElementTree as ET
from wrw_pages_bundle import safe_name
from urllib.parse import urljoin, urlsplit, unquote
from urllib.request import Request
from urllib.error import HTTPError, URLError
from wrw_pages_bundle import PageInfo


TLS_CONTEXT = ssl.create_default_context()


CONNECTIONS = threading.local()


def discard_connection(key):
    connection=getattr(CONNECTIONS,'values',{}).pop(key,None)
    if connection: connection.close()


class PooledResponse:
    def __init__(self, response, key): self.response=response; self.key=key
    def __enter__(self): return self.response
    def __exit__(self, kind, value, traceback):
        complete=self.response.isclosed() or self.response.length==0
        self.response.close()
        if kind or not complete: discard_connection(self.key)


def urlopen(request, timeout=30, context=TLS_CONTEXT):
    # urllib closes each connection. The large WRW check instead keeps one TLS
    # connection per worker/origin, preserving certificate and hostname checks.
    url=request.full_url
    for _ in range(6):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.username or not parsed.hostname:
            raise ValueError('Unexpected verification request URL')
        key=(parsed.hostname,parsed.port or 443)
        if not hasattr(CONNECTIONS,'values'): CONNECTIONS.values={}
        connection=CONNECTIONS.values.get(key)
        if connection is None:
            connection=http.client.HTTPSConnection(key[0],key[1],timeout=timeout,context=context)
            CONNECTIONS.values[key]=connection
        path=(parsed.path or '/')+('?' + parsed.query if parsed.query else '')
        try:
            connection.request(request.get_method(),path,headers=dict(request.header_items()))
            response=connection.getresponse()
        except (OSError,http.client.HTTPException) as exc:
            discard_connection(key)
            raise URLError(exc) from exc
        if response.status in (301,302,303,307,308):
            location=response.getheader('Location')
            response.close(); discard_connection(key)
            if not location: raise ValueError('Redirect has no Location')
            url=urljoin(url,location)
            continue
        if response.status>=400:
            status,reason,headers=response.status,response.reason,response.headers
            response.close(); discard_connection(key)
            raise HTTPError(url,status,reason,headers,None)
        return PooledResponse(response,key)
    raise ValueError('Too many verification redirects')


def request(url, method='GET', limit=10_000_000):
    for attempt in range(3):
        try:
            with urlopen(Request(url, method=method, headers={'User-Agent':'WRW-publication-verification/1.0'}), timeout=30, context=TLS_CONTEXT) as response:
                body=response.read(limit+1) if method=='GET' else b''
                if len(body)>limit: raise ValueError('Unexpectedly large public response')
                if response.status!=200: raise ValueError('HTTP '+str(response.status))
                return body,dict(response.headers)
        except HTTPError as exc:
            if exc.code not in (408,429,500,502,503,504) or attempt==2: raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt==2: raise
        time.sleep(0.5 * (attempt+1))


class PageResources(HTMLParser):
    def __init__(self):
        super().__init__(); self.styles=[]; self.scripts=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='link' and 'stylesheet' in attrs.get('rel','').lower().split():
            self.styles.append(attrs.get('href',''))
        if tag=='script' and attrs.get('src'): self.scripts.append(attrs['src'])


def page_resources(body, page_url, base_url, files):
    info=PageResources(); info.feed(body.decode('utf-8'))
    if not info.styles: raise ValueError('Page has no stylesheet')
    base=urlsplit(base_url); result=set()
    for value in info.styles+info.scripts:
        url=urlsplit(urljoin(page_url,value))
        if url.scheme!='https':
            raise ValueError('Insecure stylesheet/script URL: '+value)
        # Optional HTTPS services such as future advertising are not files in
        # our publication archive. Validate the first-party design resources.
        if url.netloc!=base.netloc: continue
        if not url.path.startswith(base.path):
            raise ValueError('Stylesheet/script outside publication prefix: '+value)
        relative=unquote(url.path[len(base.path):])
        if relative not in files:
            raise ValueError('Referenced stylesheet/script missing from publication: '+value)
        result.add(relative)
    return result


def verify_feeds(files, base_url, full=False, workers=8):
    """Prove subscriber bytes on the public host, not just the release marker."""
    if '__wrw_feed_manifest.json' not in files: return {'checked':0,'failures':[]}
    def body(name):
        value,headers=request(urljoin(base_url,name))
        if hashlib.sha256(value).hexdigest()!=files[name]['sha256']:raise ValueError('Wrong public feed bytes: '+name)
        return value,headers
    contract=json.loads(body('__wrw_feed_manifest.json')[0])
    if contract.get('schema')!=1:raise ValueError('Unknown public feed manifest')
    selected=list(contract['endpoints']) if full else [p for p in ('/feed.xml','/atom.xml','/comments.xml','/comments-atom.xml') if p in contract['endpoints']]
    def check(path):
        name=path.lstrip('/')
        if not path.startswith('/') or name not in files:raise ValueError('Unmanifested public feed: '+path)
        value,headers=body(name);root=ET.fromstring(value)
        expected='rss' if contract['endpoints'][path]['format']=='rss' else '{http://www.w3.org/2005/Atom}feed'
        if root.tag!=expected:raise ValueError('Wrong public feed format: '+path)
        kind=next((v.split(';')[0].strip().lower() for k,v in headers.items() if k.lower()=='content-type'),'')
        if kind not in ('application/rss+xml','application/atom+xml','application/xml','text/xml'):raise ValueError('Public feed has non-XML content type: '+path)
        if path=='/feed.xml' and contract.get('phase')!='held':
            actual=[(x.findtext('guid'),x.findtext('link')) for x in root.findall('./channel/item')]
            expected=[(x['guid'],x['url']) for x in contract['main_items']]
            if actual!=expected:raise ValueError('Public subscriber identities differ from feed contract')
    failures=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending={pool.submit(check,path):path for path in selected}
        for count,future in enumerate(as_completed(pending),1):
            try:future.result()
            except Exception as exc:failures.append(pending[future]+': '+str(exc))
            if full and count%1000==0:print('Public feeds checked',count,'/',len(selected),flush=True)
    return {'checked':len(selected),'failures':failures,'phase':contract.get('phase','ready')}


def verify_live(manifest, base_url, full=False, workers=8, wait=120):
    parsed=urlsplit(base_url)
    if parsed.scheme!='https' or parsed.hostname not in ('lukeyjohnson.github.io','wrw.blackgrid.com.au') or parsed.username:
        raise ValueError('Unexpected public verification origin')
    base_url=base_url.rstrip('/')+'/'
    prefix=manifest['path_prefix']
    if parsed.path.rstrip('/')!=prefix: raise ValueError('Deployed URL prefix does not match bundle')
    deadline=time.monotonic()+wait
    while True:
        try:
            body,_=request(urljoin(base_url,'__wrw_release.json'))
            marker=json.loads(body)
            if any(marker.get(key)!=manifest[key] for key in ('release_id','path_prefix','mode','adapter_sha256') if key in manifest): raise ValueError('Old/unexpected release marker')
            break
        except Exception:
            if time.monotonic()>=deadline:raise
            time.sleep(3)
    files={row['path']:row for row in manifest['files']}
    resources=set()
    posts=manifest['posts'] if full else manifest['posts'][:5]
    def post_check(post):
        body,_=request(base_url+post['path'].lstrip('/'));info=PageInfo();info.feed(body.decode('utf-8'))
        if info.ids!=[post['id']]:raise ValueError('Wrong public article: '+post['path'])
        if info.canonical!='https://wrw.blackgrid.com.au'+post['path']:raise ValueError('Wrong public canonical: '+post['path'])
        return page_resources(body,base_url+post['path'].lstrip('/'),base_url,files)
    failures=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending={pool.submit(post_check,post):post['path'] for post in posts}
        for count,future in enumerate(as_completed(pending),1):
            try:resources.update(future.result())
            except Exception as exc:failures.append(pending[future]+': '+str(exc))
            if full and count%500==0:print('Public articles checked',count,'/',len(posts),flush=True)
    for path in ('','search/','archive/'):
        try:
            body,_=request(base_url+path)
            resources.update(page_resources(body,base_url+path,base_url,files))
        except Exception as exc:failures.append(path+': '+str(exc))
    assets=[row for row in manifest['files'] if row['path'].startswith(('media/','assets/','static/'))]
    selected=assets if full else assets[:10]
    selected_paths={row['path'] for row in selected}
    selected += [files[path] for path in sorted(resources-selected_paths)]
    def asset_check(row):
        # HEAD checks availability; immutable artifact checksums were verified in
        # CI. Body hashes of selected images/indexes provide independent samples.
        method='GET' if not full or row['path'].startswith('assets/') else 'HEAD'
        body,headers=request(base_url+row['path'],method=method)
        if row['path'].endswith('.css'):
            content_type=next((v for k,v in headers.items() if k.lower()=='content-type'),'')
            if content_type.split(';')[0].strip().lower()!='text/css':
                raise ValueError('Stylesheet is not served as CSS: '+row['path'])
        if method=='GET' and hashlib.sha256(body).hexdigest()!=row['sha256']:raise ValueError('Wrong public asset: '+row['path'])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending={pool.submit(asset_check,row):row['path'] for row in selected}
        for count,future in enumerate(as_completed(pending),1):
            try:future.result()
            except Exception as exc:failures.append(pending[future]+': '+str(exc))
            if full and count%500==0:print('Public assets checked',count,'/',len(selected),flush=True)
    feed_report={'checked':0,'failures':[]}
    try:
        feed_report=verify_feeds(files,base_url,full,workers);failures.extend(feed_report['failures'])
    except Exception as exc:failures.append('Subscriber feed check: '+str(exc))
    try:
        request(base_url+'__wrw_deliberately_missing_route_49b9fa/')
        failures.append('Unknown route returned HTTP 200')
    except HTTPError as exc:
        if exc.code!=404:failures.append('Unknown route returned '+str(exc.code))
    except Exception as exc:failures.append('404 check: '+str(exc))
    result={'release_id':manifest['release_id'],'url':base_url,'full':full,'articles_checked':len(posts),'assets_checked':len(selected),
        'asset_method':'all assets HEAD; shared assets sampled by byte hash' if full else 'sampled assets GET and byte hash',
        'referenced_stylesheets_scripts_checked':len(resources),'feeds_checked':feed_report['checked'],'feed_phase':feed_report.get('phase'),'failures':failures,'ok':not failures}
    return result


def verify_changed(manifest, base_url, changed, removed=(), sample_size=12, workers=12):
    """Exact bytes for every changed path plus reproducible unchanged samples."""
    result = verify_live(manifest, base_url, full=False, workers=workers)
    files = {r['path']: r for r in manifest['files']}
    changed = set(changed)
    if not changed <= files.keys(): raise ValueError('Unmanifested changed path')
    for name in changed | set(removed): safe_name(name)
    seed = manifest['release_id']
    sample = set()
    for candidates in ([p['file'] for p in manifest['posts']],
                       [p for p in files if p.startswith(('assets/', 'media/', 'static/'))]):
        ordered = sorted(set(candidates) - changed, key=lambda p: hashlib.sha256((seed + p).encode()).hexdigest())
        sample.update(ordered[:sample_size])
    posts = {p['file']: p for p in manifest['posts']}
    def check(name):
        reason = ''
        for attempt in range(4):
            try:
                body, headers = request(urljoin(base_url.rstrip('/') + '/', name))
                if len(body) != files[name]['bytes'] or hashlib.sha256(body).hexdigest() != files[name]['sha256']:
                    raise ValueError('Published bytes differ from exact publication manifest')
                if name in posts:
                    info=PageInfo(); info.feed(body.decode('utf-8')); post=posts[name]
                    if info.ids != [post['id']] or info.canonical != 'https://wrw.blackgrid.com.au'+post['path']:
                        raise ValueError('Changed article identity/canonical differs')
                if name.endswith('.xml'):
                    ET.fromstring(body)
                    kind=next((v.split(';')[0].strip().lower() for k,v in headers.items() if k.lower()=='content-type'),'')
                    if kind not in ('application/rss+xml','application/atom+xml','application/xml','text/xml'):
                        raise ValueError('Changed XML has wrong public content type')
                return None
            except Exception as exc: reason=str(exc)
            if attempt < 3: time.sleep(2 ** attempt)
        return name+': '+reason
    failures=[]
    selected=sorted(changed | sample)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for count, future in enumerate(as_completed([pool.submit(check,name) for name in selected]),1):
            error=future.result()
            if error: failures.append(error)
            if count % 250 == 0: print('Changed/sample paths checked',count,'/',len(selected),flush=True)
    for name in removed:
        try:
            request(urljoin(base_url.rstrip('/')+'/',name));failures.append(name+': removed path still returns 200')
        except HTTPError as exc:
            if exc.code != 404:failures.append(name+': removed path returns '+str(exc.code))
        except Exception as exc:failures.append(name+': '+str(exc))
    result['failures'].extend(failures)
    result.update(ok=not result['failures'],verification='changed-and-sampled',
        changed_paths_checked=len(changed),unchanged_sample=sorted(sample),
        exact_paths_checked=len(selected),removed_paths_checked=len(removed))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive',type=Path);source.add_argument('--delivery',type=Path)
    p.add_argument('--url',required=True);p.add_argument('--full',action='store_true')
    args=p.parse_args()
    if args.delivery:
        from wrw_layers import load_delivery
        value=load_delivery(args.delivery);manifest=value['publication']
        result=verify_live(manifest,args.url,True) if args.full else verify_changed(manifest,args.url,value['changed'],value['removed'])
    else:
        with tarfile.open(args.archive,'r:gz') as tar:manifest=json.loads(tar.extractfile('publication.json').read())
        result=verify_live(manifest,args.url,args.full)
    print(json.dumps(result,indent=2));raise SystemExit(0 if result['ok'] else 1)
