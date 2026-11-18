#!/usr/bin/env python3
"""Immutable delta transport; every deployment still assembles a complete site."""
import argparse
import gzip
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from wrw_pages_bundle import MAX_SITE_BYTES, SHA, PageInfo, file_sha, safe_name, sha, stable

MAX_MANIFEST = 20_000_000
MAX_LAYERS = 128


def publication(value, release_id=None):
    identity = dict(value)
    package_id = identity.pop('package_id', None)
    if sha(stable(identity)) != package_id or not SHA.fullmatch(value.get('release_id', '')):
        raise ValueError('Publication identity mismatch')
    if release_id and value['release_id'] != release_id:
        raise ValueError('Wrong publication release')
    files = value['files']
    if len(files) > 100_000 or len({safe_name(r['path']) for r in files}) != len(files):
        raise ValueError('Duplicate/excess publication files')
    if any(not SHA.fullmatch(r['sha256']) or not isinstance(r['bytes'], int) or r['bytes'] < 0 for r in files):
        raise ValueError('Invalid publication checksum/size')
    if sum(r['bytes'] for r in files) >= MAX_SITE_BYTES:
        raise ValueError('GitHub Pages site exceeds 1 GB')
    return value


def legacy_manifest(archive):
    # publication.json is the first member of all supported legacy archives.
    with tarfile.open(archive, 'r|gz') as tar:
        member = next(iter(tar), None)
        if not member or member.name != 'publication.json' or not member.isfile() or member.size > MAX_MANIFEST:
            raise ValueError('Missing legacy publication manifest')
        return publication(json.loads(tar.extractfile(member).read()))


def load_delivery(file, expected_sha=None, release_id=None):
    file = Path(file)
    if file.stat().st_size > MAX_MANIFEST or expected_sha and file_sha(file) != expected_sha:
        raise ValueError('Delivery manifest checksum/size mismatch')
    value = json.loads(file.read_text())
    if value.get('schema') != 1 or value.get('transport') != 'layers':
        raise ValueError('Unknown layered delivery format')
    pub = publication(value['publication'], release_id)
    layers = value['layers']
    if not layers or len(layers) > MAX_LAYERS or len({r['sha256'] for r in layers}) != len(layers):
        raise ValueError('Duplicate/excess/missing layers')
    for layer in layers:
        if set(layer) != {'sha256', 'bytes', 'tag', 'asset', 'kind'}:
            raise ValueError('Unexpected layer metadata')
        if (not SHA.fullmatch(layer['sha256']) or not re.fullmatch(r'wrw-(?:layer-)?[a-f0-9]{64}', layer['tag'])
                or layer['asset'] not in ('wrw-site.tar.gz', 'layer.tar.gz')
                or layer['kind'] not in ('legacy', 'delta')
                or not isinstance(layer['bytes'], int) or not 0 < layer['bytes'] <= MAX_SITE_BYTES + MAX_MANIFEST):
            raise ValueError('Invalid layer reference')
    records = {r['path']: r for r in pub['files']}
    if set(value['owners']) != set(records) or not set(value['owners'].values()) <= {r['sha256'] for r in layers}:
        raise ValueError('Layer ownership differs from publication')
    changed = value.get('changed', [])
    if len(set(changed)) != len(changed) or not set(changed) <= records.keys():
        raise ValueError('Invalid changed-path manifest')
    for key in ('baseline', 'baseline_release_id'):
        baseline = value.get(key)
        if baseline is not None and not SHA.fullmatch(baseline): raise ValueError('Invalid baseline identity')
    removed=value.get('removed', [])
    if len(set(removed)) != len(removed) or set(removed) & records.keys(): raise ValueError('Invalid removed paths')
    for name in removed: safe_name(name)
    return value


def baseline(receipt):
    if receipt.get('transport') == 'layers':
        value = load_delivery(receipt['delivery'], receipt['sha256'], receipt['release_id'])
        if value['publication']['package_id'] != receipt['package_id']:raise ValueError('Layered baseline receipt differs')
        return value['publication'], value['layers'], value['owners']
    archive = Path(receipt['archive'])
    pub = legacy_manifest(archive)
    if pub['package_id'] != receipt['package_id'] or pub['release_id'] != receipt['release_id']:
        raise ValueError('Legacy baseline receipt differs')
    digest = file_sha(archive)
    layer = {'sha256': digest, 'bytes': archive.stat().st_size, 'tag': receipt['release_tag'],
             'asset': 'wrw-site.tar.gz', 'kind': 'legacy'}
    return pub, [layer], {r['path']: digest for r in pub['files']}


def prepare(package, previous, output):
    """Package only new bytes. A manifest explicitly selects every output file."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    pub = publication(json.loads(Path(package['publication']).read_text()))
    old, layers, owners = baseline(previous) if previous else ({'files': []}, [], {})
    records = {r['path']: r for r in pub['files']}
    prior = {r['path']: r for r in old['files']}
    changed = sorted(p for p, r in records.items() if prior.get(p) != r)
    removed = sorted(prior.keys() - records.keys())
    owners = {p: owner for p, owner in owners.items() if p in records and p not in changed}
    upload = []
    if changed:
        archive = output / 'layer.tar.gz'
        with archive.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as gz, tarfile.open(mode='w', fileobj=gz) as tar:
            for name in changed:
                file = Path(package['site']) / name
                if file.is_symlink() or file.stat().st_size != records[name]['bytes'] or file_sha(file) != records[name]['sha256']:
                    raise ValueError('Changed publication file differs from manifest')
                info = tarfile.TarInfo('site/' + safe_name(name))
                info.size = records[name]['bytes']; info.mode = 0o644; info.mtime = 0
                with file.open('rb') as stream: tar.addfile(info, stream)
        digest = file_sha(archive)
        layer = {'sha256': digest, 'bytes': archive.stat().st_size,
                 'tag': 'wrw-layer-' + digest, 'asset': 'layer.tar.gz', 'kind': 'delta'}
        layers = [r for r in layers if r['sha256'] != digest] + [layer]
        owners.update({name: digest for name in changed})
        upload.append({**layer, 'file': str(archive)})
    used = set(owners.values())
    layers = [r for r in layers if r['sha256'] in used]
    if len(layers) > MAX_LAYERS:
        raise ValueError('Layer limit reached; explicit compaction is required')
    value = {'schema': 1, 'transport': 'layers', 'publication': pub, 'layers': layers,
             'owners': owners, 'changed': changed, 'removed': removed, 'baseline': old.get('package_id'),
             'baseline_release_id': old.get('release_id'),
             'code_revision': package.get('code_revision'), 'selected_post_ids': package.get('selected_post_ids', [])}
    file = output / 'delivery.json'
    file.write_bytes(stable(value) + b'\n')
    load_delivery(file)
    return {**package, 'transport': 'layers', 'delivery': str(file), 'sha256': file_sha(file),
            'upload_layers': upload, 'bytes': sum(r['bytes'] for r in upload) + file.stat().st_size,
            'changed_paths': changed, 'removed_paths': removed, 'baseline': value['baseline']}


def assemble(delivery, archives, output, expected_sha=None, release_id=None):
    value = load_delivery(delivery, expected_sha, release_id)
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Assembly directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    records = {r['path']: r for r in value['publication']['files']}
    extracted = set()
    for layer in value['layers']:
        if layer['sha256'] not in archives: raise ValueError('Missing layer archive')
        archive = Path(archives[layer['sha256']])
        if archive.stat().st_size != layer['bytes'] or file_sha(archive) != layer['sha256']:
            raise ValueError('Layer checksum/size mismatch')
        seen = set(); count = 0; size = 0
        with tarfile.open(archive, 'r|gz') as tar:
            for member in tar:
                count += 1; size += member.size
                if (count > 100_000 or size > MAX_SITE_BYTES + MAX_MANIFEST or not member.isfile()
                        or member.name in seen):
                    raise ValueError('Unsafe/duplicate/excess layer member')
                seen.add(member.name)
                if member.name == 'publication.json' and layer['kind'] == 'legacy':
                    if member.size > MAX_MANIFEST: raise ValueError('Oversized legacy manifest')
                    publication(json.loads(tar.extractfile(member).read()))
                    continue
                if not member.name.startswith('site/'):
                    raise ValueError('Unrecognised layer member')
                name = safe_name(member.name[5:])
                # A full base contains superseded pages; only explicit owners
                # can emit files. This prevents removed paths being resurrected.
                if value['owners'].get(name) != layer['sha256']: continue
                if name in extracted or name not in records:
                    raise ValueError('Duplicate/unmanifested assembled file')
                row = records[name]
                if member.size != row['bytes']: raise ValueError('Assembled file size mismatch')
                body = tar.extractfile(member).read()
                if sha(body) != row['sha256']: raise ValueError('Assembled file checksum mismatch')
                target = output / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
                extracted.add(name)
    if extracted != records.keys(): raise ValueError('Missing assembled files')
    for post in value['publication']['posts']:
        info = PageInfo(); info.feed((output / safe_name(post['file'])).read_text())
        if info.ids != [post['id']]: raise ValueError('Assembled article identity mismatch')
        if info.canonical != 'https://wrw.blackgrid.com.au' + post['path']:
            raise ValueError('Assembled canonical mismatch')
    marker = json.loads((output / '__wrw_release.json').read_text())
    if any(marker.get(k) != value['publication'].get(k) for k in ('release_id', 'path_prefix', 'mode', 'adapter_sha256')):
        raise ValueError('Assembled marker mismatch')
    return value['publication']


def download(value, cache, repo, env=None):
    if repo not in ('LukeyJohnson/bg-wrw-site', 'LukeyJohnson/bg-wrw-publish-preview'):
        raise ValueError('Unexpected layer repository')
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    archives = {}
    for layer in value['layers']:
        folder = cache / layer['sha256']; folder.mkdir(exist_ok=True)
        file = folder / layer['asset']
        if not file.exists():
            subprocess.run(['gh', 'release', 'download', layer['tag'], '--repo', repo,
                            '--pattern', layer['asset'], '--dir', str(folder)], check=True, env=env)
        if file.stat().st_size != layer['bytes'] or file_sha(file) != layer['sha256']:
            raise ValueError('Downloaded layer differs; refusing corrupted cache')
        archives[layer['sha256']] = file
    return archives


def read_files(receipt, predicate):
    """Read verified prior subscriber bytes without rebuilding/recompressing media."""
    if receipt.get('transport') != 'layers':
        raise ValueError('Expected a layered receipt')
    pub = load_delivery(receipt['delivery'], receipt['sha256'], receipt['release_id'])['publication']
    result = {}
    for row in pub['files']:
        if not predicate('/' + row['path']): continue
        file = Path(receipt['site']) / safe_name(row['path'])
        if file.is_symlink() or file.stat().st_size != row['bytes'] or file_sha(file) != row['sha256']:
            raise ValueError('Previous subscriber bytes differ from publication')
        result['/' + row['path']] = file.read_bytes()
    return result


def check_dispatch(delivery, digest, current_request, public_url):
    """Fence stale workflows immediately before publishing their artifact."""
    from urllib.parse import urlsplit
    value=load_delivery(delivery,digest)
    request=json.loads(Path(current_request).read_text())
    pub=value['publication']
    if any(request.get(k)!=v for k,v in {'package_id':pub['package_id'],'release_id':pub['release_id'],
            'archive_sha256':digest,'transport':'layers','baseline':value['baseline']}.items()):
        raise ValueError('A newer publication superseded this workflow')
    parsed=urlsplit(public_url)
    if parsed.scheme!='https' or parsed.netloc not in ('wrw.blackgrid.com.au','lukeyjohnson.github.io'):
        raise ValueError('Unexpected baseline origin')
    if value['baseline_release_id']:
        from wrw_verify_live import request as public_request
        marker=json.loads(public_request(public_url.rstrip('/')+'/__wrw_release.json')[0])
        if marker.get('release_id') not in (value['baseline_release_id'],pub['release_id']):
            raise ValueError('Live baseline changed; refusing stale artifact')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('delivery', type=Path); p.add_argument('--sha256', required=True)
    p.add_argument('--release-id', required=True); p.add_argument('--repo', required=True)
    p.add_argument('--output', type=Path, required=True); p.add_argument('--cache', type=Path, default=Path('incoming/layers'))
    args = p.parse_args()
    value = load_delivery(args.delivery, args.sha256, args.release_id)
    assemble(args.delivery, download(value, args.cache, args.repo), args.output, args.sha256, args.release_id)
    print(json.dumps({'release_id': args.release_id, 'package_id': value['publication']['package_id'],
                      'layers': len(value['layers']), 'files': len(value['publication']['files'])}))


if __name__ == '__main__': main()
