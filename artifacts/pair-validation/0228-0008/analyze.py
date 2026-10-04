"""Reproducible study of the user-supplied known flight pair.

Reads existing session caches; missing inexpensive audio is saved locally.
Does not tune or confirm a session match from the supplied ground truth.
"""
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import yaml

from fpv_matcher.audio.fingerprint import (
    ALGORITHM_VERSION as AUDIO_VERSION, best_offset, extract_audio, landmark_match, score_at_offset,
)
from fpv_matcher.audio.review import audio_sections
from fpv_matcher.cache.fingerprints import _key, get_or_extract
from fpv_matcher.matching.correlation import compare, prepare, section_support
from fpv_matcher.motion.telemetry import VERSION, body_rates
from fpv_matcher.radio.derivation import cache_path
from fpv_matcher.radio.rates import apply_rate_profile

HERE = Path(__file__).resolve().parent
DATA = Path('/home/rdubz/.fpv-radio-motion-pairing')
JOB = DATA / 'jobs/5a76c58c0100'
FPV = 'DJI_20260406161138_0008_D.MP4'
RADIO = 'DJI_20260406162534_0228_D.MP4'

def load(path):
    with np.load(path, allow_pickle=False) as data:
        return {k: data[k] for k in data.files if k != 'cache_key'}

def cache_file(record, kind, version, config):
    return DATA / 'cache' / f'{kind}.{_key(record["path"], version, config, record.get("sha256"))}.npz'

def audio(record):
    path = cache_file(record, 'audio', AUDIO_VERSION, {'boundary_seconds': 30})
    if path.exists():
        return load(path)
    return get_or_extract(record['path'], HERE / 'cache', 'audio', AUDIO_VERSION,
                          {'boundary_seconds': 30}, lambda: extract_audio(record['path']), record.get('sha256'))

def main():
    job = json.loads((JOB / 'job.json').read_text())
    fr = next(r for r in job['inputs']['fpv'] if r['name'] == FPV)
    rr = next(r for r in job['inputs']['radio'] if r['name'] == RADIO)
    fa, ra = audio(fr), audio(rr)
    best = best_offset(ra, fa, 2)
    result = {'known_pair_supplied_by_user': True, 'fpv': fr, 'radio': rr,
              'settings': job['settings'], 'calibration': job['calibrations'][RADIO],
              'audio': {'best': dict(zip(['score','offset','overlap','peak_margin'], best)),
                        'landmarks': dict(zip(['score','offset','peak_margin','hashes','span'], landmark_match(ra, fa))),
                        'sections': audio_sections(ra, fa, best[1])},
              'filename_difference_radio_minus_fpv_seconds': 836,
              'note': 'Scores are evidence measurements, not probabilities. Filename difference is not independent evidence.'}
    negatives = []
    for r in job['inputs']['fpv']:
        p = cache_file(r, 'audio', AUDIO_VERSION, {'boundary_seconds': 30})
        if p.exists():
            a = load(p)
            negatives.append({'name': r['name'], 'best': list(best_offset(ra, a, 2)),
                              'landmarks': list(landmark_match(ra, a))})
    result['audio']['candidate_ranking'] = sorted(negatives, key=lambda r:r['best'][0], reverse=True)
    result['audio']['boundary_landmarks'] = {}
    for label, begin, end in [('start',0,30),('end',float(rr['metadata']['duration'])-30,float(rr['metadata']['duration']))]:
        keep = (ra['hash_times'] >= begin) & (ra['hash_times'] < end)
        subset = {k:v for k,v in ra.items() if not k.startswith('_')}
        subset.update(hashes=ra['hashes'][keep], hash_times=ra['hash_times'][keep])
        result['audio']['boundary_landmarks'][label] = list(landmark_match(subset,fa))
    sp = cache_path(rr['path'], DATA/'cache', job['calibrations'][RADIO], 30, rr.get('sha256'))
    tp = cache_file(fr, 'telemetry', VERSION, {})
    if sp.exists() and tp.exists():
        sticks, telemetry = load(sp), load(tp)
        result['telemetry'] = {'kind': str(telemetry['kind'].item()), 'label': str(telemetry['label'].item()),
                               'samples': len(telemetry['time']), 'range': [float(telemetry['time'][0]), float(telemetry['time'][-1])]}
        motion = body_rates(telemetry, job['settings']['camera_uptilt_deg'], job['settings']['imu_orientation'], job['settings']['gyro_video_offset'])
        rp, fp = prepare(sticks,30,True), prepare(motion,30,False)
        m = compare(rp,fp,30,2)
        result['motion'] = asdict(m)
        result['motion']['sections'] = section_support(rp,fp,30,m.offset_seconds,m.details,include_windows=True)
        result['motion']['audio_at_motion_offset'] = list(score_at_offset(ra,fa,m.offset_seconds,2))
        result['motion']['at_audio_offset'] = asdict(compare(rp,fp,30,2,(best[1]-.05,best[1]+.05)))
        fixed = compare(rp,fp,30,2,(best[1]-.05,best[1]+.05))
        result['motion']['sections_at_audio_offset'] = section_support(rp,fp,30,best[1],fixed.details,include_windows=True)
        result['motion']['tracking'] = {k:{'usable_fraction':float(np.mean(sticks.get(k+'_quality',sticks['quality'])>.1)),
                                         'std':float(np.std(sticks[k])), 'range':[float(np.min(sticks[k])),float(np.max(sticks[k]))]}
                                         for k in ['roll','pitch','yaw','throttle']}
        rates = yaml.safe_load((HERE.parents[2]/'rate_presets.yaml').read_text())
        result['rate_presets'] = rates
        profile = rates['presets'][rates['default']]
        kiss = prepare(apply_rate_profile(sticks,profile),30,True)
        result['motion']['kiss'] = asdict(compare(kiss,fp,30,2))
        result['motion']['kiss_at_audio_offset'] = asdict(compare(kiss,fp,30,2,(best[1]-.05,best[1]+.05)))
        result['motion']['tracking'].update({k:{**result['motion']['tracking'][k],
            'saturated_fraction': float(np.mean(np.abs(sticks[k])>=.99))} for k in ['roll','pitch','yaw','throttle']})
        ranking = []
        for record in job['inputs']['fpv']:
            target = cache_file(record,'telemetry',VERSION,{})
            if target.exists():
                td = load(target)
                if 'unavailable' in td:
                    continue
                body = body_rates(td,job['settings']['camera_uptilt_deg'],job['settings']['imu_orientation'],job['settings']['gyro_video_offset'])
                ranking.append({'name':record['name'],**asdict(compare(rp,prepare(body,30,False),30,2))})
        result['motion']['candidate_ranking'] = sorted(ranking,key=lambda r:r['score'],reverse=True)
    (HERE/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    print(json.dumps({k:v for k,v in result.items() if k not in ['fpv','radio','settings','calibration']},indent=2)[:14000])

if __name__ == '__main__':
    main()
