"""Explicit cross-modal exploration of independently confirmed reference pairs."""
from __future__ import annotations

import json
import sys
import time
import subprocess
from pathlib import Path

import numpy as np
from fpv_matcher.pipeline import atomic_json
from fpv_matcher.matching import activity
from fpv_matcher.matching import window_rhythm
from fpv_matcher.matching.fingerprint_review import signals,fpv_motion,document,trace
from fpv_matcher.motion.telemetry import cached_telemetry
from fpv_matcher.audio.motor import cached_motor,prepare as motor_signal
from fpv_matcher.radio.scene_activity import cached_scene

VERSION="cross-modal-reference-rhythms-v5-window-consensus"


def consensus(radio_bank,fpv_bank):
    """Let independent modalities corroborate timing; never average away a cue."""
    targets=[k for k in ("angular_rate_changes","motor_spectral_changes") if k in fpv_bank]
    if not targets:
        raise ValueError("No FPV motion/audio rhythm is available")
    comparisons=[]
    for rname,r in radio_bank.items():
        for fname in targets:
            f=fpv_bank[fname]
            best=activity.compare(r,f)
            if best["valid_seconds"]>=8 and best["score"]>0:
                comparisons.append((rname,fname,r,f,best))
    if not comparisons:
        raise ValueError("No source has enough jointly observed rhythm")
    lags=[]
    for *_,best in sorted(comparisons,key=lambda c:-c[-1]["score"]):
        lag=best["offset_seconds"]
        if not any(abs(lag-p)<.25 for p in lags):lags.append(lag)
    for rname,fname,r,f,best in comparisons:
        try:
            local_best=window_rhythm.compare(r,f)
            if not any(abs(local_best["offset_seconds"]-p)<.05 for p in lags):lags.append(local_best["offset_seconds"])
        except ValueError:pass
    candidates=[]
    for lag in lags:
        families={}
        for rname,fname,r,f,best in comparisons:
            local=activity.sections(r,f,lag)
            times=r["time"];q=r["quality"]*np.interp(times-lag,f["time"],f["quality"],left=0,right=0)
            good=q>.01
            if good.sum()<80 or local["usable"]<2:continue
            x=r["energy"][good];y=np.interp(times[good]-lag,f["time"],f["energy"])
            weights=q[good];x=x-np.average(x,weights=weights);y=y-np.average(y,weights=weights)
            denom=float(np.sqrt(np.average(x*x,weights=weights)*np.average(y*y,weights=weights)))
            corr=max(0.,float(np.average(x*y,weights=weights)/denom)) if denom>1e-8 else 0.
            coverage=min(1.,float(good.sum())/300)
            score=(.55*corr+.45*local["agreement"]*min(1.,local["matched"]/4))*np.sqrt(coverage)
            strength=local["matched"]*np.sqrt(local["agreement"])*(.5+.5*local["spread"])
            family="visual" if rname in {"scene_residual","gimbal_activity","rhythm"} else "audio"
            item={"radio_signal":rname,"fpv_signal":fname,"family":family,"score":score,"window_strength":strength,"correlation":corr,
                  "sections":local,"valid_seconds":float(good.sum()/10),"source_peak_offset":best["offset_seconds"],
                  "source_peak_margin":best["peak_margin"]}
            if family not in families or strength>families[family]["window_strength"]:families[family]=item
        if not families:continue
        ordered=sorted(families.values(),key=lambda x:-x["window_strength"]);primary=ordered[0]
        supporting=[v for v in ordered if v["sections"]["matched"]>=3 and v["sections"]["agreement"]>=.4
                    and v["correlation"]>=.2 and v["valid_seconds"]>=12]
        score=min(1.,primary["score"]+.1*max(0,len(supporting)-1))
        strength=primary["window_strength"]+.25*sum(v["window_strength"] for v in ordered[1:])
        candidates.append({"offset_seconds":lag,"score":score,"primary":primary,"support":ordered,
                           "independent_modalities":len(supporting),"window_strength":strength})
    if not candidates:raise ValueError("No locally supported rhythm timing is available")
    candidates.sort(key=lambda x:-x["window_strength"])
    winner=candidates[0];primary=winner["primary"]
    rival=max((c["window_strength"] for c in candidates[1:] if abs(c["offset_seconds"]-winner["offset_seconds"])>2),default=0)
    margin=max(0.,(winner["window_strength"]-rival)/max(winner["window_strength"],1e-8))
    eligible=bool(margin>=.2 and primary["sections"]["matched"]>=6
                  and primary["sections"]["agreement"]>=.3 and primary["sections"]["spread"]>=.3
                  and primary["valid_seconds"]>=20)
    family_peaks={}
    for candidate in candidates:
        for support in candidate["support"]:
            family=support["family"]
            if family not in family_peaks or support["window_strength"]>family_peaks[family]["window_strength"]:
                family_peaks[family]={**support,"offset_seconds":candidate["offset_seconds"]}
    conflicts=[v for v in family_peaks.values() if abs(v["offset_seconds"]-winner["offset_seconds"])>2
               and v["window_strength"]>=.6*primary["window_strength"]]
    if conflicts:eligible=False
    result={**activity.compare(radio_bank[primary["radio_signal"]],fpv_bank[primary["fpv_signal"]]),
            "score":winner["score"],"offset_seconds":winner["offset_seconds"],"peak_margin":margin,
            "window_strength":winner["window_strength"],
            "sections":primary["sections"],"valid_seconds":primary["valid_seconds"],"eligible":eligible,
            "support":winner["support"],"independent_modalities":winner["independent_modalities"],
            "primary_signals":{"radio":primary["radio_signal"],"fpv":primary["fpv_signal"]},
            "modality_conflicts":conflicts,
            "reason":"Exploratory consensus of independent source modalities; visible-event review required."}
    return result,candidates[:10]


def tracking_audit(record,calibration,cache,settings,bank):
    result={"prepared_coverage_percent":{k:round(float(np.mean(v["quality"]>.1))*100,1) for k,v in bank.items()},
            "outline_observed_percent":None,"tip_observed_percent":{},"recommendations":[]}
    if not calibration:
        result["recommendations"].append("No radio calibration is saved; overall scene and audio activity remain independently available.")
        return result
    try:
        from fpv_matcher.radio.derivation import cache_path
        from fpv_matcher.radio.sampling import tracking_fps
        from fpv_matcher.matching.fingerprint_review import read_cache
        fps=tracking_fps(settings.get("sample_fps",30),settings.get("stick_sampling_ratio"),record["metadata"].get("fps"))
        data=read_cache(cache_path(record["path"],cache,calibration,fps,record.get("identity")))
        if "radio_orientation_quality" in data:
            result["outline_observed_percent"]=round(float(np.mean(data["radio_orientation_quality"]>=.35))*100,1)
        for key in ("roll","pitch","yaw","throttle"):
            result["tip_observed_percent"][key]=round(float(np.mean(data.get(key+"_quality",data["quality"])>.1))*100,1)
    except (OSError,ValueError,KeyError,TypeError):
        result["recommendations"].append("Precise inputs are not cached at the current settings; the study does not force a full tip derivation.")
    pose=result["outline_observed_percent"]
    tips=list(result["tip_observed_percent"].values())
    if pose is not None and pose<50:
        result["recommendations"].append("Radio pose is often missing. Confirm a clear face outline and add full-frame calibration landmarks for different views; a tip-only mark cannot recover a missing radio pose.")
    if pose is not None and pose>=70 and tips and min(tips)<30:
        result["recommendations"].append("The radio remains observed while tips are often missing. Review visible tip examples and hand occlusions; keep covered-tip intervals as gaps and use overall activity for matching.")
    if calibration.get("auto_generated"):
        result["recommendations"].append("Radio regions were generated automatically; review the outline and both housings before precise tip tracking.")
    return result


def run(request_path):
    request=json.loads(Path(request_path).read_text());directory=Path(request["directory"])
    cache=request["cache"];settings=request["settings"];rows=[];started=time.monotonic()
    radio_bank={};fpv_bank={};failures={}
    total=len(request["pairs"])
    def progress(index,name,event):
        percent=(index+min(1.,event.get("percent",0)/100))/max(1,total)*95
        atomic_json(directory/"progress.json",{"percent":percent,"name":name,"completed":index,"total":total,
                    "message":f"{name}: {event.get('message','reading saved signals')}","elapsed_seconds":time.monotonic()-started})
    for index,pair in enumerate(request["pairs"]):
        radio,fpv=pair["radio"],pair["fpv"];key=radio["name"],fpv["name"]
        checks=[];issues=[]
        for record in (radio,fpv):
            stat=Path(record["path"]).stat()
            if [stat.st_size,stat.st_mtime_ns]!=record["signature"]:
                raise ValueError("A reference source changed before processing")
        rkey=radio["name"]
        if rkey not in radio_bank:
            bank={};problems=[]
            callback=lambda e:progress(index,rkey,e)
            if request["kind"]!="motor-search":
                try:
                    scene=cached_scene(radio["path"],cache,radio.get("identity"),callback)
                    bank["scene_residual"]=activity.prepare_signal(scene,"activity")
                except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc: problems.append(str(exc))
            if radio.get("metadata",{}).get("has_audio"):
                try:
                    audio=cached_motor(radio["path"],cache,settings.get("motor_band"),radio.get("identity"),callback)
                    bank["motor_spectral_changes"]=motor_signal(audio)
                    if request["kind"]!="motor-search":
                        q=np.where(audio["band_energy"]>1e-8,1.,0.)
                        bank["audio_band_energy"]=activity.prepare_signal({"time":audio["time"],"activity":audio["band_energy"],"quality":q},"activity")
                        bank["audio_spectral_flux"]=activity.prepare_signal({"time":audio["time"],"activity":audio["flux"],"quality":q},"activity")
                except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc: problems.append(str(exc))
            radio_bank[rkey]=bank;failures[rkey]=problems
        fkey=fpv["name"]
        if fkey not in fpv_bank:
            bank={}
            try:
                cached_telemetry(fpv["path"],cache,fpv.get("identity"))
            except (ValueError,RuntimeError) as exc: issues.append(str(exc))
            try:
                data,label=fpv_motion(fpv,cache,settings,request["kind"]=="motor-search")
                bank["angular_rate_changes"]=activity.prepare_fpv(data)
                if request["kind"]!="motor-search":
                    speed=np.sqrt(sum(np.asarray(data[k])**2 for k in ("roll","pitch","yaw")))
                    bank["angular_speed"]=activity.prepare_signal({"time":data["time"],"activity":np.log1p(speed),"quality":data["quality"]},"activity")
                    for axis in ("roll","pitch","yaw"):
                        bank[axis+"_rate_activity"]=activity.prepare_signal({"time":data["time"],"activity":np.log1p(np.abs(data[axis])),"quality":data.get(axis+"_quality",data["quality"])},"activity")
            except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
                issues.append(str(exc));label="No usable saved FPV motion"
            if fpv.get("metadata",{}).get("has_audio") and request["kind"]!="motor-search":
                try:
                    audio=cached_motor(fpv["path"],cache,settings.get("motor_band"),fpv.get("identity"))
                    bank["motor_spectral_changes"]=motor_signal(audio)
                except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:issues.append(str(exc))
            fpv_bank[fkey]=bank,label
        rbank=dict(radio_bank[rkey]);fbank,label=fpv_bank[fkey]
        if request["kind"]!="motor-search":
            for method in ("gimbal_activity","rhythm"):
                try:
                    r,_,_,_=signals(radio,fpv,pair.get("calibration"),cache,settings,method)
                    rbank[method]=r
                except (ValueError,KeyError,TypeError,OSError):
                    pass  # Do not derive exact sticks just to inspect a reference.
        issues+=failures[rkey]
        try:
            agreed,candidates=consensus(rbank,fbank)
            r=rbank[agreed["primary_signals"]["radio"]];f=fbank[agreed["primary_signals"]["fpv"]]
            view=document(r,f,label,activity,pair.get("reference_offset"))
            view["best"]=agreed
            if "reference_offset" not in pair:
                view["offset_seconds"]=agreed["offset_seconds"];view["sections"]=agreed["sections"]
            for rname,rsignal in rbank.items():
                for fname,fsignal in fbank.items():
                    best=activity.compare(rsignal,fsignal)
                    try:window_best=window_rhythm.compare(rsignal,fsignal)
                    except ValueError:window_best=None
                    local=activity.sections(rsignal,fsignal,pair["reference_offset"]) if "reference_offset" in pair else None
                    checks.append({"stickcam_signal":rname,"fpv_signal":fname,"score":best["score"],"best_offset":best["offset_seconds"] if best["score"]>0 else None,
                        "offset_error":best["offset_seconds"]-pair["reference_offset"] if "reference_offset" in pair and best["score"]>0 else None,
                        "matched_sections_at_reference":local["matched"] if local else None,
                        "usable_sections_at_reference":local["usable"] if local else None,"eligible":best["eligible"],
                        "window_offset":window_best["offset_seconds"] if window_best else None,
                        "window_offset_error":window_best["offset_seconds"]-pair["reference_offset"] if window_best and "reference_offset" in pair else None,
                        "window_matched":window_best["sections"]["matched"] if window_best else 0,
                        "window_usable":window_best["sections"]["usable"] if window_best else 0,
                        "window_eligible":window_best["eligible"] if window_best else False,
                        "windows":local["windows"] if local else [],"display_offset":pair.get("reference_offset")})
            checks.sort(key=lambda c:(-(c["matched_sections_at_reference"] or 0),-c["score"]))
            view.update(method="combined_rhythm",radio_video=rkey,fpv_video=fkey,reference_offset=pair.get("reference_offset"),
                        consensus_candidates=candidates,
                        contributions={"radio":list(rbank),"fpv":list(fbank)},checks=checks,issues=issues,
                        radio_channels={k:trace(v) for k,v in rbank.items()},fpv_channels={k:trace(v) for k,v in fbank.items()},
                        source_signatures={"radio":radio["signature"],"fpv":fpv["signature"]},
                        tracking_audit=tracking_audit(radio,pair.get("calibration"),cache,settings,rbank),
                        note="Exploratory cross-modal timing consensus. Plots show the strongest contributing signals; other channels remain selectable. Correlated channels from one modality count as one source. The reference offset is separate from the searched offset. This is not a calibrated confidence.")
            if request["kind"]=="motor-search":
                best=view["best"]
                best["eligible"]=bool(best["eligible"] and best["score"]>=.55 and best["peak_margin"]>=.08 and best["sections"]["matched"]>=3 and best["valid_seconds"]>=20)
            atomic_json(directory/f"pair-{index}.json",view)
            rows.append({"index":index,"radio_video":rkey,"fpv_video":fkey,"reference_offset":pair.get("reference_offset"),
                         "best":view["best"],"contributions":view["contributions"],"issues":issues,"available":True})
        except ValueError as exc:
            rows.append({"index":index,"radio_video":rkey,"fpv_video":fkey,"available":False,"issues":[*issues,str(exc)]})
        progress(index+1,rkey,{"percent":0,"message":"Reference fingerprints saved"})
    rows.sort(key=lambda r:-(r.get("best") or {}).get("window_strength",0))
    if request["kind"]!="fingerprint-study":
        available=[r for r in rows if r["available"]]
        for i,row in enumerate(available):
            strength=row["best"]["window_strength"]
            rival=max((r["best"]["window_strength"] for r in available if r is not row),default=0)
            row["candidate_margin"]=max(0.,(strength-rival)/max(strength,1e-8))
            row["suggested"]=bool(row["best"]["eligible"] and row["candidate_margin"]>=.2)
    atomic_json(directory/"fingerprints.json",{"version":VERSION,"rows":rows,"kind":request["kind"],
        "elapsed_seconds":round(time.monotonic()-started,2),"automatic_confirmation":False,
        "note":"Exploratory timing evidence. Correlation cannot prove shared footage, RPM, thrust, axis direction or drone identity."})
    atomic_json(directory/"progress.json",{"percent":100,"completed":total,"total":total,"message":"Movement fingerprints ready"})


if __name__=="__main__":
    run(sys.argv[1])
