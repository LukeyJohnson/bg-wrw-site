#!/usr/bin/env python3
"""Verify deployed release identity, article IDs and public asset availability."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
import ssl
from html.parser import HTMLParser
from pathlib import Path
import tarfile
import time
from urllib.parse import urljoin, urlsplit, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from wrw_pages_bundle import PageInfo


TLS_CONTEXT = ssl.create_default_context()


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
        if url.scheme!='https' or url.netloc!=base.netloc:
            raise ValueError('Unexpected stylesheet/script origin: '+value)
        if not url.path.startswith(base.path):
            raise ValueError('Stylesheet/script outside publication prefix: '+value)
        relative=unquote(url.path[len(base.path):])
        if relative not in files:
            raise ValueError('Referenced stylesheet/script missing from publication: '+value)
        result.add(relative)
    return result


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
    try:
        request(base_url+'__wrw_deliberately_missing_route_49b9fa/')
        failures.append('Unknown route returned HTTP 200')
    except HTTPError as exc:
        if exc.code!=404:failures.append('Unknown route returned '+str(exc.code))
    except Exception as exc:failures.append('404 check: '+str(exc))
    result={'release_id':manifest['release_id'],'url':base_url,'full':full,'articles_checked':len(posts),'assets_checked':len(selected),
        'asset_method':'all assets HEAD; shared assets sampled by byte hash' if full else 'sampled assets GET and byte hash',
        'referenced_stylesheets_scripts_checked':len(resources),'failures':failures,'ok':not failures}
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True);p.add_argument('--url',required=True);p.add_argument('--full',action='store_true')
    args=p.parse_args()
    with tarfile.open(args.archive,'r:gz') as tar:manifest=json.loads(tar.extractfile('publication.json').read())
    result=verify_live(manifest,args.url,args.full);print(json.dumps(result,indent=2));raise SystemExit(0 if result['ok'] else 1)
