"""Readable output folders and an export index independent of session data."""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path

from . import jobs
from .storage import atomic_json


def folder_name(value, limit=60):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', str(value))
    name = ' '.join(name.split())[:limit].strip(' .') or 'Session'
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    return '_' + name if name.split('.')[0].upper() in reserved else name


def destination(value):
    root = Path(value).expanduser().resolve() if value else (jobs.DATA / 'exports').resolve()
    sessions = (jobs.DATA / 'jobs').resolve()
    if root == sessions or sessions in root.parents:
        raise ValueError('Choose an output folder outside session data so deleting a session preserves exports')
    if root.exists() and not root.is_dir():
        raise ValueError('The export destination must be a folder')
    return root


def create_directory(root, session_name):
    """Reserve a new folder without overwriting any earlier output."""
    root.mkdir(parents=True, exist_ok=True)
    label = f'{folder_name(session_name, 40)}_aligned_{datetime.now():%Y-%m-%d_%H-%M-%S}'
    for index in range(10000):
        path = root / (label if index == 0 else f'{label}_{index + 1}')
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise ValueError('Too many exports share this folder name; choose another destination')


def _index_path(sid, eid):
    return jobs.DATA / 'export-index' / identifier(sid, 12) / f'{identifier(eid, 10)}.json'


def _write_json(path, result):
    atomic_json(path, result)


def identifier(value, length):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{' + str(length) + r'}', value):
        raise ValueError('Invalid export identifier')
    return value


def save(result):
    with jobs.LOCK:
        _write_json(Path(result['directory']) / 'export.json', result)
        _write_json(_index_path(result['session'], result['id']), result)


def register_pending(result):
    # Keep track of generated folders even if a job fails or is cancelled.
    # They must not become new source recordings during a recursive rescan.
    with jobs.LOCK:
        _write_json(_index_path(result['session'], result['id']), result | {'pending': True})


def _read(path):
    result = json.loads(path.read_text(encoding='utf-8'))
    identifier(result['session'], 12)
    identifier(result['id'], 10)
    if not isinstance(result.get('directory'), str):
        raise ValueError('Invalid export directory')
    if (not math.isfinite(result['created']) or not isinstance(result['session_name'], str)
        or not isinstance(result['pairs'], int) or result['pairs'] < 1
        or result['profile'] not in {'copy', 'h264', 'dnxhr'}
        or result['fps'] not in {'original', 24, 25, 30, 50, 60}):
        raise ValueError('Invalid export metadata')
    return result


def records(include_pending=False):
    found = {}
    with jobs.LOCK:
        # Read index metadata locally: do not touch external/network video
        # folders on every frontend poll. Retain pre-index export discovery.
        paths = [*(jobs.DATA / 'export-index').glob('*/*.json'),
                 *(jobs.DATA / 'exports').glob('*/*/export.json'),
                 *(jobs.DATA / 'exports').glob('*/export.json')]
        for path in paths:
            try:
                result = _read(path)
                key = result['session'], result['id']
                if key not in found or found[key].get('pending') and not result.get('pending'):
                    found[key] = result
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return sorted((r for r in found.values() if include_pending or not r.get('pending')),
                  key=lambda r: r['created'], reverse=True)


def load(sid, eid):
    with jobs.LOCK:
        index = _index_path(sid, eid)
        if index.is_file():
            result = _read(index)
        else:
            result = next((r for r in records() if r['session'] == sid and r['id'] == eid), None)
            if result is None:
                raise FileNotFoundError('Export not found')
    directory = Path(result['directory'])
    marker = _read(directory / 'export.json')
    if marker['session'] != sid or marker['id'] != eid:
        raise ValueError('The output folder no longer contains this export')
    return (marker if result.get('pending') else result), directory
