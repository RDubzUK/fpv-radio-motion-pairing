"""Find reviewable FPV segment candidates from a human-confirmed timing anchor."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fpv_matcher.video.metadata import filename_timestamp, probe_video

VERSION = 'confirmed-relative-camera-clock-segments-v1'


def run(request_path):
    from fpv_matcher.pipeline import atomic_json
    request = json.loads(Path(request_path).read_text())
    directory, anchor = Path(request['directory']), request['anchor']
    anchor_clock = filename_timestamp(anchor['fpv_video'])
    radio_clock = filename_timestamp(anchor['radio_video'])
    if anchor_clock is None:
        raise ValueError('The confirmed FPV filename has no usable timestamp; additional segments need their own audio or motion match')
    rows, failures = [], []
    for index, clip in enumerate(request['clips']):
        atomic_json(directory / 'progress.json', {'name': clip['name'], 'completed': index,
                    'total': len(request['clips']), 'percent': index / len(request['clips']) * 100})
        if clip['name'] == anchor['fpv_video']:
            continue
        clock = filename_timestamp(clip['name'])
        if clock is None:
            continue
        # Relative filename time avoids assuming either camera's date is correct.
        offset = anchor['offset_seconds'] + clock - anchor_clock
        if not -24 * 3600 < offset < anchor['radio_duration']:
            continue
        try:
            path = Path(clip['path'])
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != clip['signature']:
                raise ValueError('Source changed while the segment search was queued')
            info = clip.get('metadata') or probe_video(path)
            info = {**info, 'name': clip['name'], 'mtime_ns': stat.st_mtime_ns}
            duration = max(0., min(anchor['radio_duration'] - max(0., offset), info['duration'] - max(0., -offset)))
            if duration < request['min_overlap']:
                continue
            if [path.stat().st_size, path.stat().st_mtime_ns] != clip['signature']:
                raise ValueError('Source changed during segment search')
            rows.append({'radio_video': anchor['radio_video'], 'fpv_video': clip['name'],
                         'offset_seconds': offset, 'radio_start': max(0., offset),
                         'fpv_start': max(0., -offset), 'overlap_seconds': duration,
                         'fpv_info': info, 'timestamp_delta_seconds': clock - anchor_clock,
                         'clock_delta_seconds': None if radio_clock is None else anchor_clock - radio_clock - anchor['offset_seconds'],
                         'note': 'Filename timing predicts an overlap; confirm shared content and refine sync before exporting.'})
        except (ValueError, RuntimeError, OSError) as exc:
            failures.append({'name': clip['name'], 'message': str(exc)})
    rows.sort(key=lambda row: (row['radio_start'], row['fpv_video']))
    spans = [(anchor['fpv_video'], anchor['radio_start'], anchor['radio_start'] + anchor['overlap_duration'])]
    spans += [(row['fpv_video'], row['radio_start'], row['radio_start'] + row['overlap_seconds']) for row in rows]
    for row in rows:
        conflicts = [name for name, start, end in spans if name != row['fpv_video'] and
                     min(end, row['radio_start'] + row['overlap_seconds']) - max(start, row['radio_start']) > 2]
        if conflicts:
            row['note'] = 'Overlapping timestamp coverage: may be a duplicate, clock reset or another recording. Review individually.'
            row['conflicts'] = conflicts
    for kind in ('radio', 'fpv'):
        source = Path(anchor[kind + '_path'])
        if [source.stat().st_size, source.stat().st_mtime_ns] != anchor['source_signatures'][kind]:
            raise ValueError('The confirmed anchor footage changed during segment search')
    atomic_json(directory / 'segments.json', {'anchor': anchor, 'clips': rows, 'failures': failures,
                'message': f'{len(rows)} possible FPV segments overlap the StickCam timeline; timestamps remain unconfirmed.'})
    atomic_json(directory / 'progress.json', {'completed': len(request['clips']), 'total': len(request['clips']), 'percent': 100})


if __name__ == '__main__':
    run(sys.argv[1])
