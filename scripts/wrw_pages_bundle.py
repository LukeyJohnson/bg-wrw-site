#!/usr/bin/env python3
"""Portable public-only Pages bundle creation, extraction and verification."""
import argparse
from collections import Counter
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
from urllib.parse import urlsplit

MAX_SITE_BYTES = 1_000_000_000
SHA = re.compile(r'[a-f0-9]{64}')


def stable(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
def sha(data): return hashlib.sha256(data).hexdigest()
def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def safe_name(name):
    if not name or name.startswith('/') or '\\' in name or '\0' in name or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Unsafe public path')
    lower = name.lower()
    if any(p in ('secrets', 'captures', 'context', 'runtime', 'backups', '.git') for p in lower.split('/')):
        raise ValueError('Private directory in public bundle')
    if Path(lower).name in ('.env', 'wp-config.php', 'access.json', 'local-admin.json') or lower.endswith(('.sql', '.php', '.pem', '.key')):
        raise ValueError('Private/application file in public bundle')
    return name


class PageInfo(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids = []; self.canonical = None
    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'article' and attrs.get('data-post-id'): self.ids.append(int(attrs['data-post-id']))
        if tag == 'link' and attrs.get('rel') == 'canonical': self.canonical = attrs.get('href')


def rebase(source, destination, prefix):
    from lxml import html as LH
    if prefix and not re.fullmatch(r'/[A-Za-z0-9_-]+', prefix): raise ValueError('Invalid Pages path prefix')
    shutil.copytree(source, destination)
    replacements = {}
    if prefix:
        for file in sorted((destination / 'assets').glob('*.json')):
            data = json.loads(file.read_text())
            if not isinstance(data, list): continue
            if not all(isinstance(item, dict) and isinstance(item.get('path'), str) and item['path'].startswith('/') for item in data): continue
            for item in data: item['path'] = prefix + item['path']
            body = stable(data); renamed = file.with_name(sha(body)[:24] + '.json')
            old = '/' + str(file.relative_to(destination)); new = '/' + str(renamed.relative_to(destination))
            renamed.write_bytes(body)
            if renamed != file: file.unlink()
            replacements[old] = new
    def url(value):
        if not value.startswith('/') or value.startswith('//'): return value
        return prefix + replacements.get(value, value)
    for file in sorted(destination.rglob('*.html')):
        doc = LH.document_fromstring(file.read_bytes())
        for node in doc.iter():
            for attribute in ('href', 'src', 'action', 'poster', 'data-index'):
                if node.get(attribute): node.set(attribute, url(node.get(attribute)))
            if node.get('srcset'):
                node.set('srcset', ', '.join(' '.join([url(bits[0]), *bits[1:]]) for bits in
                    (part.strip().split() for part in node.get('srcset').split(',')) if bits))
            if node.tag == 'script' and node.get('data-index'): node.set('data-base', prefix)
        file.write_bytes(LH.tostring(doc, encoding='utf-8', doctype='<!doctype html>'))
    return destination


def create(site, output, release_id, prefix='', mode='preview'):
    if not SHA.fullmatch(release_id): raise ValueError('Invalid release ID')
    if mode not in ('preview', 'production'): raise ValueError('Invalid mode')
    output.mkdir(parents=True, exist_ok=True)
    public = output / 'site'
    if public.exists(): raise ValueError('Bundle destination must be new')
    rebase(site, public, prefix)
    # Host-neutral local release headers are not interpreted by GitHub Pages.
    (public / '_headers').unlink(missing_ok=True)
    marker = {'schema': 1, 'release_id': release_id, 'path_prefix': prefix, 'mode': mode, 'adapter_sha256': file_sha(Path(__file__))}
    (public / '__wrw_release.json').write_bytes(stable(marker) + b'\n')
    files, posts = [], []
    for file in sorted(public.rglob('*')):
        if file.is_symlink(): raise ValueError('Public symlink refused')
        if not file.is_file(): continue
        name = safe_name(file.relative_to(public).as_posix())
        files.append({'path': name, 'bytes': file.stat().st_size, 'sha256': file_sha(file)})
        if file.suffix == '.html':
            parsed = PageInfo(); parsed.feed(file.read_text())
            if parsed.ids:
                if len(parsed.ids) != 1: raise ValueError('Multiple article identities')
                path = '/' + name
                if path.endswith('index.html'): path = path[:-10]
                canonical_path = urlsplit(parsed.canonical).path if parsed.canonical else path
                posts.append({'path': canonical_path, 'file': name, 'id': parsed.ids[0]})
    if sum(item['bytes'] for item in files) >= MAX_SITE_BYTES: raise ValueError('GitHub Pages site exceeds 1 GB')
    manifest = {**marker, 'files': files, 'posts': posts}
    manifest['package_id'] = sha(stable(manifest))
    (output / 'publication.json').write_bytes(stable(manifest) + b'\n')
    archive = output / 'wrw-site.tar.gz'
    with archive.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as gz, tarfile.open(mode='w', fileobj=gz) as tar:
        for file in [output / 'publication.json', *[public / item['path'] for item in files]]:
            name = file.relative_to(output).as_posix(); info = tarfile.TarInfo(name)
            info.size = file.stat().st_size; info.mode = 0o644; info.mtime = 0
            with file.open('rb') as stream: tar.addfile(info, stream)
    return {'release_id': release_id, 'package_id': manifest['package_id'], 'sha256': file_sha(archive),
        'archive': str(archive), 'bytes': archive.stat().st_size, 'site_bytes': sum(x['bytes'] for x in files), 'files': len(files), 'posts': len(posts), 'prefix': prefix}


def verify(archive, output, release_id, expected_sha):
    if not SHA.fullmatch(release_id) or not SHA.fullmatch(expected_sha): raise ValueError('Invalid expected ID/hash')
    if file_sha(archive) != expected_sha: raise ValueError('Uploaded archive checksum mismatch')
    if output.exists() and any(output.iterdir()): raise ValueError('Extraction directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) > 100_000 or len({x.name for x in members}) != len(members): raise ValueError('Duplicate/excess archive members')
        if any(not member.isfile() for member in members): raise ValueError('Archive links/directories/special files refused')
        if sum(x.size for x in members) > MAX_SITE_BYTES + 20_000_000: raise ValueError('Archive exceeds Pages size budget')
        index = {x.name: x for x in members}
        entry = index.get('publication.json')
        if entry is None or entry.size > 20_000_000: raise ValueError('Missing/oversized publication manifest')
        manifest = json.loads(tar.extractfile(entry).read())
        identity = dict(manifest); package_id = identity.pop('package_id')
        if sha(stable(identity)) != package_id or manifest['release_id'] != release_id: raise ValueError('Publication identity mismatch')
        records = manifest['files']
        expected = {'site/' + safe_name(item['path']): item for item in records}
        if len(expected) != len(records) or set(index) != set(expected) | {'publication.json'}: raise ValueError('Unmanifested or duplicate public files')
        for name, record in expected.items():
            member = index[name]
            if member.size != record['bytes']: raise ValueError('Public file size mismatch')
            data = tar.extractfile(member).read()
            if sha(data) != record['sha256']: raise ValueError('Public file checksum mismatch')
            destination = output / safe_name(record['path']); destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(data)
        for post in manifest['posts']:
            path = post.get('file') or post['path'].lstrip('/') + ('index.html' if post['path'].endswith('/') else '')
            parsed = PageInfo(); parsed.feed((output / safe_name(path)).read_text())
            if parsed.ids != [post['id']]: raise ValueError('Uploaded article identity mismatch')
        marker = json.loads((output / '__wrw_release.json').read_text())
        if marker['release_id'] != release_id or marker['path_prefix'] != manifest['path_prefix']: raise ValueError('Publication marker mismatch')
        return {key: manifest[key] for key in ('release_id', 'package_id', 'mode', 'path_prefix')}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='command', required=True)
    v = sub.add_parser('verify'); v.add_argument('archive', type=Path); v.add_argument('--output', type=Path, required=True); v.add_argument('--release-id', required=True); v.add_argument('--sha256', required=True)
    args = p.parse_args()
    if args.command == 'verify': print(json.dumps(verify(args.archive, args.output, args.release_id, args.sha256)))
