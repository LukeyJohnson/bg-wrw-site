#!/usr/bin/env python3
"""Verify deployed release identity, article IDs and public asset availability."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from wrw_pages_bundle import PageInfo


def request(url, method='GET', limit=10_000_000):
    with urlopen(Request(url, method=method, headers={'User-Agent':'WRW-publication-verification/1.0'}), timeout=30) as response:
        body=response.read(limit+1) if method=='GET' else b''
        if len(body)>limit: raise ValueError('Unexpectedly large public response')
        if response.status!=200: raise ValueError('HTTP '+str(response.status))
        return body,dict(response.headers)


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
            if json.loads(body)['release_id']!=manifest['release_id']: raise ValueError('Old/unexpected release marker')
            break
        except Exception:
            if time.monotonic()>=deadline:raise
            time.sleep(3)
    posts=manifest['posts'] if full else manifest['posts'][:5]
    def post_check(post):
        body,_=request(base_url+post['path'].lstrip('/'));info=PageInfo();info.feed(body.decode('utf-8'))
        if info.ids!=[post['id']]:raise ValueError('Wrong public article: '+post['path'])
        if info.canonical!='https://wrw.blackgrid.com.au'+post['path']:raise ValueError('Wrong public canonical: '+post['path'])
        return post['path']
    failures=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(post_check,post) for post in posts]):
            try:future.result()
            except Exception as exc:failures.append(str(exc))
    assets=[row for row in manifest['files'] if row['path'].startswith(('media/','assets/','static/'))]
    selected=assets if full else assets[:10]
    def asset_check(row):
        # HEAD checks availability; immutable artifact checksums were verified in
        # CI. Body hashes of selected images/indexes provide independent samples.
        method='GET' if not full or row['path'].startswith('assets/') else 'HEAD'
        body,headers=request(base_url+row['path'],method=method)
        if method=='GET' and hashlib.sha256(body).hexdigest()!=row['sha256']:raise ValueError('Wrong public asset: '+row['path'])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(asset_check,row) for row in selected]):
            try:future.result()
            except Exception as exc:failures.append(str(exc))
    for path in ('','search/','archive/'):
        try:request(base_url+path)
        except Exception as exc:failures.append(path+': '+str(exc))
    try:
        request(base_url+'__wrw_deliberately_missing_route_49b9fa/')
        failures.append('Unknown route returned HTTP 200')
    except HTTPError as exc:
        if exc.code!=404:failures.append('Unknown route returned '+str(exc.code))
    except Exception as exc:failures.append('404 check: '+str(exc))
    result={'release_id':manifest['release_id'],'url':base_url,'full':full,'articles_checked':len(posts),'assets_checked':len(selected),
        'asset_method':'all assets HEAD; shared assets sampled by byte hash' if full else 'sampled assets GET and byte hash',
        'failures':failures,'ok':not failures}
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True);p.add_argument('--url',required=True);p.add_argument('--full',action='store_true')
    args=p.parse_args()
    with tarfile.open(args.archive,'r:gz') as tar:manifest=json.loads(tar.extractfile('publication.json').read())
    result=verify_live(manifest,args.url,args.full);print(json.dumps(result,indent=2));raise SystemExit(0 if result['ok'] else 1)
