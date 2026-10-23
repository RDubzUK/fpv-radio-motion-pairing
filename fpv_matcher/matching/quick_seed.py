"""Sequential embedded-motion seeds with provisional camera-clock suggestions.

Automatic radio regions and movement matches remain reviewable observations,
never human confirmations. No audio, visual FPV extraction or exact tips run.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from fpv_matcher.config import validate_calibration
from fpv_matcher.matching import activity, clocks
from fpv_matcher.motion.telemetry import body_rates, cached_telemetry
from fpv_matcher.radio.activity import cached_activity, cache_status, specification
from fpv_matcher.radio.calibration_scan import _frame, _proposal, _scale_region
from fpv_matcher.storage import atomic_json
from fpv_matcher.video.metadata import probe_video, filename_timestamp

VERSION = "sequential-embedded-movement-seed-v1"


def enabled(settings, records):
    sources = settings.get("evidence_sources", {})
    if settings.get("matching_strategy") == "telemetry_seed":
        return True
    names = settings.get("match_target_names", [])
    fpv = [r for r in records["fpv"] if settings.get("match_target_kind") != "fpv" or r["name"] in names]
    return (settings.get("matching_strategy") == "automatic" and settings.get("embedded_seed_enabled", True)
            and sources.get("fpv_gyro") and sources.get("gimbal_activity") and not sources.get("stick_inputs")
            and bool(fpv) and all(r.get("metadata", {}).get("has_audio") is False for r in fpv))


def signature(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def regions(path, metadata, gallery, radio_model, notify):
    duration = metadata["duration"]
    begun = time.monotonic()
    # An in-flight view often avoids setup/landing occlusion. Also inspect the
    # opening in five-second steps when the midpoint is not a clear radio view.
    times = [duration/2, min(5., duration/2), *np.arange(10., min(duration-.05, 45.1), 5.)]
    attempts = []
    for at in dict.fromkeys(round(float(t), 3) for t in times if 0 <= t < duration):
        if time.monotonic()-begun > 60:
            break
        notify(at)
        try:
            image = _frame(path, at)
            proposal = _proposal(image, gallery, radio_model, include_tips=False)
            if not proposal.get("radio_outline"):
                raise ValueError("No observed radio outline")
            if proposal.get("reference_inliers", 0) < 10 and (
                    proposal.get("geometry_score", 0) < .48 or proposal.get("shell_edge_support", 0) < .52):
                raise ValueError("Radio housing/body evidence is too weak")
            outline = np.float32(proposal["radio_outline"])
            fraction = abs(cv2.contourArea(outline))/(image.shape[0]*image.shape[1])
            if not .015 <= fraction <= .9:
                raise ValueError("Radio outline has an implausible image area")
            for region in proposal["regions"].values():
                center = (float(region["x"]+region["center_x"]), float(region["y"]+region["center_y"]))
                if cv2.pointPolygonTest(outline, center, False) < 0:
                    raise ValueError("Gimbal centre is outside the observed radio")
            sx, sy = metadata["width"]/image.shape[1], metadata["height"]/image.shape[0]
            config = {"stickcam_mode": "wonky", "reference_time": at, "auto_generated": True,
                      "radio_orientation_confirmed": False, "radio_rotation_deg": proposal.get("radio_rotation_deg") or 0,
                      "radio_outline": [[x*sx, y*sy] for x,y in outline.tolist()],
                      **{side: _scale_region(region, sx, sy) for side, region in proposal["regions"].items()}}
            config = validate_calibration(config, (metadata["width"], metadata["height"]))
            specification(config)
            return config, attempts + [{"at": at, "status": "radio_regions_observed"}]
        except subprocess.TimeoutExpired:
            break
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, cv2.error) as exc:
            attempts.append({"at": at, "status": "no_clear_radio", "reason": str(exc)})
    raise ValueError("No clear radio outline/gimbal regions found in the sampled frames")


def seed_eligible(check):
    return (check.get("eligible") and check["score"] >= .45 and check["peak_margin"] >= .08
            and check["valid_seconds"] >= 12 and check["sections"]["matched"] >= 3
            and check["sections"]["agreement"] >= .6)


def content_row(radio, fpv, check, suggested=False):
    offset = check["offset_seconds"]
    overlap = max(0., min(radio["duration"]-max(0.,offset), fpv["duration"]-max(0.,-offset)))
    row = clocks.date_row({"radio_video":radio["name"], "fpv_video":fpv["name"], "offset_seconds":offset,
                           "overlap_seconds":overlap, "clock_source":"Tip-free movement seed", "support":[]})
    check = {**check, "suggested":bool(suggested), "candidate_margin":check.get("candidate_margin", 0.),
             "reason":"Embedded-motion/gimbal activity candidate. Other StickCam clips may remain unscanned; independently review visible events."}
    row.update(timing_only=False, evidence="Tip-free gimbal movement · embedded telemetry · review required",
               fpv_motion_source=fpv.get("motion_source", "embedded"), gimbal_activity=check,
               review_reasons=[check["reason"]], seed_search=True)
    return row


def clock_model(radio, fpv, check, settings, index):
    rc, fc = clocks.start(radio, "modified", settings), clocks.start(fpv, "modified", settings)
    if rc is None or fc is None:
        return None
    return {"id":f"seed-clock-{index}", "label":f"Provisional movement clock {index}", "method":"modified",
            "delta_seconds":rc-fc+check["offset_seconds"], "raw_modified_difference_seconds":radio["modified_time"]-fpv["modified_time"],
            "tolerance":settings["modified_tolerance"], "modified_kind":settings["modified_kind"],
            "recorder":fpv.get("clock_group", ""), "tentative":True, "confirmed":False, "spot_status":"not_checked",
            "anchors":[{"radio_video":radio["name"], "fpv_video":fpv["name"], "offset_seconds":check["offset_seconds"],
                        "source_signatures":{"radio":[radio["size"],radio["mtime_ns"]], "fpv":[fpv["size"],fpv["mtime_ns"]]}}]}


def projections(records, models, settings, minimum, matched_fpv, rejections=(), clock_rejections=()):
    counts = Counter((kind, r.get("clock_group", ""), r.get("modified_time")) for (kind,_),r in records.items())
    output = []
    for model in models:
        if model.get("spot_status") == "conflict":
            continue
        for (kind, fname), fpv in records.items():
            if kind != "fpv" or fname in matched_fpv or (model["recorder"] and fpv.get("clock_group") not in {"",model["recorder"]}):
                continue
            for (rkind,rname),radio in records.items():
                if rkind != "radio":
                    continue
                if any(d.get("radio_video")==rname and d.get("fpv_video")==fname for d in rejections):
                    continue
                if any(counts[(k,r.get("clock_group", ""),r.get("modified_time"))] > 1 for k,r in (("fpv",fpv),("radio",radio))):
                    continue
                fc, rc = clocks.start(fpv,"modified",settings), clocks.start(radio,"modified",settings)
                if fc is None or rc is None:
                    continue
                offset = model["delta_seconds"]+fc-rc
                overlap = max(0.,min(radio["duration"]-max(0.,offset),fpv["duration"]-max(0.,-offset)))
                if overlap < minimum:
                    continue
                row = clocks.date_row({"radio_video":rname,"fpv_video":fname,"offset_seconds":offset,"overlap_seconds":overlap,
                      "clock_source":model["label"]+" · modified dates · content not checked", "support":[],
                      "tentative":True,"recorder":model["recorder"],"clock_label":model["label"]})
                row.update(provisional_clock=True,seed_clock_id=model["id"],seed_anchor=model["anchors"][0],
                           raw_modified_difference_seconds=radio["modified_time"]-fpv["modified_time"],
                           review_reasons=["Provisional dates from an unconfirmed movement seed. Independently check content and sync before confirming."],
                           spot_check_status="not_checked")
                if not clocks.rejected_suggestion(row,records,clock_rejections,settings):
                    output.append(row)
    return sorted(output,key=lambda r:(-r["overlap_seconds"],r["fpv_video"],r["radio_video"]))


def run_seed(fpv_paths, radio_paths, config, output, cache, settings, progress=None, *, fallback=False):
    started = time.monotonic()
    output, cache = Path(output), Path(cache)
    output.mkdir(parents=True, exist_ok=True)
    os.environ["FPV_MATCHER_DECODE"] = settings.get("decode_mode", "auto")
    os.environ["FPV_MATCHER_TRACKING"] = settings.get("tracking_backend", "auto")
    cv2.setNumThreads(1)
    names = settings.get("match_target_names") or [settings.get("match_target_name")]
    kind = settings.get("match_target_kind", "all")
    fpv_paths = [Path(p) for p in fpv_paths if kind != "fpv" or Path(p).name in names]
    radio_paths = [Path(p) for p in radio_paths if (kind != "radio" or Path(p).name in names)
                   and not config.get("clips", {}).get(Path(p).name, {}).get("exclude")
                   and not config.get("clips", {}).get(Path(p).name, {}).get("skip_motion")]
    clock_settings = clocks.options(config.get("clock_settings"))
    minimum = settings.get("min_overlap", 2.)
    attempts, errors, motions, gallery, models, checked = [], [], {}, [], [], []
    rows, records, clips, paths = {}, {}, {"fpv":[], "radio":[]}, {}
    previous_path = output / "seed-preparation.json"
    try:
        prior = json.loads(previous_path.read_text())
        prior = {r["name"]:r for r in prior.get("clips", [])} if prior.get("version") == VERSION else {}
    except (OSError, ValueError):
        prior = {}
    maximum_progress = [0.]
    def report(stage, message, percent, **extra):
        maximum_progress[0] = max(maximum_progress[0], percent)
        event = {"stage":stage, "message":message, "percent":round(maximum_progress[0],1),
                 "elapsed_seconds":round(time.monotonic()-started,1), "seed_attempts":len(attempts),
                 "total_stickcam":len(radio_paths), "spot_checks":checked, **extra}
        atomic_json(output / "progress.json", event)
        if progress:
            progress(event)
    def save_preparation():
        atomic_json(previous_path, {"version":VERSION, "clips":attempts, "radio_model":config.get("radio_model", "auto")})
    def identity(kind, name):
        return config.get("identities", {}).get(kind, {}).get(name)
    total = len(fpv_paths)+len(radio_paths)
    for index, (source_kind, path) in enumerate([*(('fpv',p) for p in fpv_paths), *(('radio',p) for p in radio_paths)]):
        try:
            sig = signature(path)
            info = probe_video(path)
            if signature(path) != sig:
                raise ValueError("Source changed during metadata scan")
            modified = config.get("modified_times", {}).get(source_kind, {}).get(path.name, {}).get("timestamp", sig[1]/1e9)
            info = {**info, "name":path.name, "size":sig[0], "mtime_ns":sig[1], "source_usable":True,
                    "modified_time":modified, "filename_time":filename_timestamp(path.name),
                    "clock_group":config.get("fpv_recorders", {}).get(path.name, "") if source_kind == "fpv" else "",
                    "motion_used":False, "audio_used":False, "gimbal_activity_used":False,
                    "motion_source":"not_scanned", "tracking_quality":0.}
            records[source_kind,path.name] = info
            paths[source_kind,path.name] = path
            clips[source_kind].append(info)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append({"kind":source_kind,"name":path.name,"error":str(exc)})
        report("seed_metadata",f"Scanning source metadata {index+1}/{total}",(index+1)/max(1,total)*5)
    for index, fpv in enumerate(clips["fpv"]):
        report("seed_telemetry",f"Caching embedded gyro/attitude {index+1}/{len(clips['fpv'])} · {fpv['name']}",
               5+15*index/max(1,len(clips["fpv"])))
        try:
            telemetry = cached_telemetry(paths["fpv",fpv["name"]], cache, identity("fpv",fpv["name"]))
            fpv['embedded']={"status":"available","kind":str(telemetry['kind'].item())}
            data = body_rates(telemetry, settings.get("camera_uptilt_deg",0),settings.get("imu_orientation","zXy"),settings.get("gyro_video_offset",0))
            signal = activity.prepare_fpv(data)
            if np.count_nonzero(signal["quality"] > .1) < 80:
                raise ValueError("Embedded movement has fewer than eight usable seconds")
            motions[fpv["name"]] = signal
            fpv.update(motion_used=True,motion_source=str(telemetry["kind"].item()),motion_label=str(telemetry["label"].item()),
                       embedded={"status":"available","kind":str(telemetry["kind"].item())})
        except (ValueError, RuntimeError, OSError, ImportError) as exc:
            fpv['motion_label']=str(exc)
            fpv.setdefault('embedded',{"status":"unavailable","message":str(exc)})
    if not motions and fallback and not any(r.get('embedded',{}).get('status')=='available' for r in clips['fpv']):
        return None  # The normal automatic path can still use selected audio/visual evidence.

    def add_reference(name, calibration):
        if any(r["name"] == name for r in gallery) or len(gallery) >= 3:
            return
        try:
            meta = records["radio",name]
            image = _frame(paths["radio",name], calibration["reference_time"])
            sx, sy = image.shape[1]/meta["width"], image.shape[0]/meta["height"]
            gallery.append({"name":name,"image":image,"orientation_confirmed":False,
                            "outline":[[x*sx,y*sy] for x,y in calibration["radio_outline"]],
                            "regions":{s:_scale_region(calibration[s],sx,sy) for s in ("left_gimbal","right_gimbal")}})
        except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError, cv2.error):
            pass
    for name, calibration in config.get("clips", {}).items():
        if ("radio",name) in records and calibration.get("radio_outline"):
            add_reference(name,calibration)
    signals = {}
    def prepare_radio(name, index, spot=False):
        if name in signals:
            return signals[name]
        radio, path = records["radio",name], paths["radio",name]
        begun = time.monotonic()
        sig = [radio["size"],radio["mtime_ns"]]
        entry = {"name":name,"signature":sig,"status":"preparing","automatic":False}
        attempts.append(entry)
        calibration = config.get("clips", {}).get(name)
        if not calibration:
            saved = prior.get(name, {})
            if saved.get("signature") == sig and saved.get("calibration") and saved.get("radio_model") == config.get("radio_model", "auto"):
                calibration = saved["calibration"]
        try:
            try:
                specification(calibration)
            except (ValueError, TypeError, KeyError):
                calibration, frame_attempts = regions(path,radio,[r for r in gallery if r["name"] != name],config.get("radio_model","auto"),
                    lambda at: report("seed_calibration",f"Finding radio regions · {name} · frame {at:.1f}s",
                                      76 if spot else 20+50*index/max(1,len(radio_paths))))
                entry["frame_attempts"] = frame_attempts
            entry.update(calibration=calibration,automatic=bool(calibration.get("auto_generated")),radio_model=config.get("radio_model","auto"))
            save_preparation()
            reused = cache_status(path,cache,calibration,identity("radio",name))["status"] == "cached"
            def update(event):
                percent = float(event.get("percent",0))
                report("seed_derivation",f"{'Spot-check' if spot else 'Seed search'} · StickCam {index+1}/{len(radio_paths)} · {name} · {percent:.0f}%",
                       78+percent*.12 if spot else 20+50*(index+percent/100)/max(1,len(radio_paths)),
                       active_clips=[{"name":name,**event}],clip_percent=percent)
            raw = cached_activity(path,cache,calibration,identity("radio",name),update)
            signal = activity.prepare_signal(raw,"activity")
            observed = float(np.sum(signal["quality"] > .1)/activity.FPS)
            entry.update(observed_seconds=observed,observed_percent=float(np.mean(signal["quality"] > .1)*100),
                         cache_reused=reused,extraction_seconds=float(raw["extraction_seconds"]),elapsed_seconds=round(time.monotonic()-begun,2))
            radio.update(gimbal_activity_used=True,gimbal_activity=cache_status(path,cache,calibration,identity("radio",name)),
                         motion_source="gimbal_activity",motion_label="Tip-free gimbal activity",needs_calibration=False)
            if signature(path) != sig:
                raise ValueError("StickCam source changed during preparation")
            if observed < 12 or np.std(signal["energy"][signal["quality"] > .1]) < 1e-5:
                raise ValueError(f"Only {observed:.1f}s usable, varying gimbal movement; at least 12s is required for a seed")
            entry["status"] = "ready"
            signals[name] = signal
            add_reference(name,calibration)
            return signal
        except (ValueError, RuntimeError, OSError, TypeError, KeyError, subprocess.SubprocessError, cv2.error) as exc:
            entry.update(status="needs_review",message=str(exc),elapsed_seconds=round(time.monotonic()-begun,2))
            radio.update(needs_calibration=True,motion_skipped_reason=str(exc))
            return None
        finally:
            save_preparation()

    def compare_radio(name, signal):
        results = []
        for fname, fpv_signal in motions.items():
            check = activity.compare(signal,fpv_signal,minimum)
            row = content_row(records["radio",name],records["fpv",fname],check)
            rows[name,fname] = row
            if any(d.get("radio_video")==name and d.get("fpv_video")==fname for d in config.get("rejected_pairs",[])):
                check["suggested"] = False
            else:
                results.append(row)
        results.sort(key=lambda r:r["gimbal_activity"]["score"],reverse=True)
        if not results:
            return None
        best = results[0]
        check = best["gimbal_activity"]
        margin = check["score"]-(results[1]["gimbal_activity"]["score"] if len(results)>1 else 0.)
        check["fpv_candidate_margin"] = margin
        consistent_parts = False
        if seed_eligible(check) and len(results)>1:
            # Several O4 file parts may be true matches to this same StickCam.
            # Independently strong, non-overlapping parts with one consistent
            # camera-clock delta corroborate a flight rather than compete for it.
            first = clock_model(records['radio',name],records['fpv',best['fpv_video']],check,clock_settings,0)
            for rival in results[1:]:
                other = rival['gimbal_activity']
                if not seed_eligible(other):
                    continue
                second = clock_model(records['radio',name],records['fpv',rival['fpv_video']],other,clock_settings,0)
                a,b = records['fpv',best['fpv_video']],records['fpv',rival['fpv_video']]
                start1,start2 = max(0.,check['offset_seconds']),max(0.,other['offset_seconds'])
                end1,end2 = start1+best['overlap_seconds'],start2+rival['overlap_seconds']
                if (first and second and a.get('clock_group','') == b.get('clock_group','')
                        and a.get('modified_time') != b.get('modified_time')
                        and abs(first['delta_seconds']-second['delta_seconds']) <= clock_settings['modified_tolerance']
                        and min(end1,end2)-max(start1,start2) <= .2):
                    consistent_parts = True
                    other['suggested'] = True
                    check['corroborated_fpv_parts'] = [best['fpv_video'],rival['fpv_video']]
                    break
        if seed_eligible(check) and (margin >= .06 or consistent_parts):
            check["suggested"] = True
            return best
        return None

    seed = None
    if motions:
        for index, path in enumerate(radio_paths):
            if ("radio",path.name) not in records:
                continue
            signal = prepare_radio(path.name,index)
            if signal is None:
                continue
            report("seed_compare",f"Comparing {path.name} against {len(motions)} embedded FPV fingerprints",maximum_progress[0])
            seed = compare_radio(path.name,signal)
            if seed:
                break
    if seed:
        model = clock_model(records["radio",seed["radio_video"]],records["fpv",seed["fpv_video"]],seed["gimbal_activity"],clock_settings,1)
        if model:
            models.append(model)
        report("seed_found",f"Movement seed found · {seed['radio_video']} ↔ {seed['fpv_video']} · offset {seed['offset_seconds']:+.1f}s · building provisional modified-date suggestions",75)
        matched = {row['fpv_video'] for row in rows.values() if row['gimbal_activity']['suggested']}
        initial = projections(records,models,clock_settings,minimum,matched,config.get("rejected_pairs",[]),config.get("clock_rejections",[]))
        candidates = []
        for row in initial:
            if row['fpv_video'] in motions and row["radio_video"] not in signals and row["radio_video"] not in [r["radio_video"] for r in candidates]:
                candidates.append(row)
        count = min(settings.get("seed_spot_checks",2),len(candidates))
        chosen = [candidates[i] for i in np.linspace(0,len(candidates)-1,count).round().astype(int)] if count else []
        for index, prediction in enumerate(chosen):
            name = prediction["radio_video"]
            signal = prepare_radio(name,radio_paths.index(paths["radio",name]),spot=True)
            result = {"radio_video":name,"fpv_video":prediction["fpv_video"],"expected_offset":prediction["offset_seconds"],"status":"inconclusive"}
            if signal is not None:
                found = compare_radio(name,signal)
                actual = rows[name,prediction["fpv_video"]]["gimbal_activity"]
                if found and found["fpv_video"] == prediction["fpv_video"]:
                    result.update(offset_seconds=actual["offset_seconds"],status="agrees" if abs(actual["offset_seconds"]-prediction["offset_seconds"]) <= clock_settings["modified_tolerance"] else "disagrees")
                    matched.add(found["fpv_video"])
                    if result["status"] == "disagrees":
                        alternative = clock_model(records["radio",name],records["fpv",found["fpv_video"]],actual,clock_settings,len(models)+1)
                        if alternative and not any(abs(m["delta_seconds"]-alternative["delta_seconds"]) <= m["tolerance"] and m["recorder"] == alternative["recorder"] for m in models):
                            if any(m["recorder"]==alternative["recorder"] and abs(m["delta_seconds"]-alternative["delta_seconds"]) <= clocks.GROUP_SEPARATION_SECONDS for m in models):
                                models[0]["spot_status"] = "conflict"
                            else:
                                models.append(alternative)
                elif found:
                    result.update(status="different_content_match",matched_fpv=found["fpv_video"],offset_seconds=found["offset_seconds"])
                    matched.add(found["fpv_video"])
                    alternative=clock_model(records['radio',name],records['fpv',found['fpv_video']],found['gimbal_activity'],clock_settings,len(models)+1)
                    if alternative and not any(abs(m['delta_seconds']-alternative['delta_seconds']) <= m['tolerance'] and m['recorder']==alternative['recorder'] for m in models):
                        models.append(alternative)
            checked.append(result)
            report("seed_spot_check",f"Spot check {index+1}/{count} · {name} · {result['status']}",90+index/max(1,count)*6)
        if models and models[0]["spot_status"] != "conflict":
            models[0]["spot_status"] = "corroborated" if any(r["status"] == "agrees" for r in checked) else "inconclusive" if checked else "not_checked"
        matched.update(row['fpv_video'] for row in rows.values() if row['gimbal_activity']['suggested'])
        projected = projections(records,models,clock_settings,minimum,matched,config.get("rejected_pairs",[]),config.get("clock_rejections",[]))
        for row in projected:
            key = row["radio_video"],row["fpv_video"]
            if key not in rows or not (rows[key].get("gimbal_activity") or {}).get("suggested"):
                if key in rows and rows[key].get('gimbal_activity'):
                    row['gimbal_activity']=rows[key]['gimbal_activity']
                    row['spot_check_status']='inconclusive'
                    row['evidence']=row['evidence'].replace('content not checked','content not verified')
                rows[key] = row
    else:
        projected = []
    invalid = set()
    for key,path in paths.items():
        try:
            if signature(path) != [records[key]['size'],records[key]['mtime_ns']]:
                raise ValueError('Source changed during seed matching')
        except (ValueError,OSError) as exc:
            invalid.add(key);records[key]['source_usable']=False
            errors.append({'kind':key[0],'name':key[1],'error':str(exc)})
    def current(row):
        anchor=row.get('seed_anchor',{})
        return all((kind,row[kind+'_video']) not in invalid and (kind,anchor.get(kind+'_video')) not in invalid for kind in ('radio','fpv'))
    rows={key:row for key,row in rows.items() if current(row)}
    projected=[row for row in projected if current(row)]
    if seed and not current(seed):
        seed=None;models=[];projected=[]
        rows={key:row for key,row in rows.items() if not row.get('provisional_clock')}
    attempted = {r["name"] for r in attempts}
    remaining = [p.name for p in radio_paths if p.name not in attempted]
    diagnostics = {"version":VERSION,"seed":seed,"clocks":models,"spot_checks":checked,"attempts":attempts,
                   "stopped_early":bool(seed and remaining),"automatic_confirmation":False}
    result = {"version":6,"pairing_model":"multiple_fpv_segments_per_stickcam","rows":list(rows.values()),
        "errors":errors,"clips":clips,"rate_profile":None,"settings":settings,"excluded_radio":[],
        "considered_fpv":[p.name for p in fpv_paths],"considered_radio":[p.name for p in radio_paths if p.name in attempted],
        "unmatched_fpv":[p.name for p in fpv_paths],"unmatched_radio":[p.name for p in radio_paths],
        "clock":{"delta_seconds":None,"confidence":0,"support":0},"elapsed_seconds":round(time.monotonic()-started,1),
        "match_scope":{"kind":kind,"targets":names if kind!='all' else [],"sample_percent":100,
            "sampled_fpv":list(motions),"sampled_radio":list(attempted),"total_fpv":len(fpv_paths),"total_radio":len(radio_paths),
            "deferred_fpv":[],"deferred_radio":remaining,"timing_suggestions":projected,"timing_suggestions_truncated":0},
        "workflow":{"strategy":"telemetry_seed","seed_search":diagnostics,"sample_percent":100,
            "unscanned_fpv":[],"unscanned_radio":remaining,"timing_suggestion_count":len(projected),
            "needs_calibration":[r['name'] for r in attempts if r['status']!='ready'],"unavailable_sources":[],
            "skipped_motion":[{"kind":"fpv","name":r['name'],"reason":r.get('motion_label','No usable telemetry')} for r in clips['fpv'] if not r['motion_used']],
            "motion_loaded":{"fpv":len(motions),"radio":len(signals)},"gimbal_activity":{"enabled":True,"sample_fps":10,
                "proposals":sum(bool(r.get('gimbal_activity',{}).get('suggested')) for r in rows.values()),"automatic_confirmation":False},
            "audio_screening":{"enabled":False},"movement_rhythm":{"enabled":False},"automatic_confirmation":False}}
    atomic_json(output / "seed-results.json",diagnostics)
    atomic_json(output / "results.json",result)
    with (output / "matches.csv.tmp").open('w',newline='') as stream:
        fields=sorted(set().union(*(r.keys() for r in rows.values()))) if rows else ['radio_video','fpv_video','offset_seconds','evidence']
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows.values())
    (output / 'matches.csv.tmp').replace(output / 'matches.csv')
    report("complete",f"Embedded-motion Quick Match: {'seed found' if seed else 'no distinct movement seed'} · {len(attempts)}/{len(radio_paths)} StickCam prepared · {len(projected)} provisional date suggestions · {len(checked)} spot checks. Review content and sync before confirming.",100)
    return result
