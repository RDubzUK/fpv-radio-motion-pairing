"""Camera clocks learned only from independently reviewed content alignments.

Offset is StickCam source time minus FPV source time. Durations intersect
timelines; they never establish flight identity. No media is decoded here.
"""
from __future__ import annotations

import math
import hashlib
from collections import Counter
from statistics import median

VERSION = "confirmed-camera-clocks-v2"
GROUP_SEPARATION_SECONDS = 300.


def options(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("Camera-clock settings must be an object")
    kind = value.get("modified_kind", "end")
    tolerance = float(value.get("modified_tolerance", 5))
    filenames = value.get("use_filenames", True)
    if kind not in {"start", "end"} or not math.isfinite(tolerance) or not 1 <= tolerance <= 10 or not isinstance(filenames, bool):
        raise ValueError("Choose recording start/end, a modified-date tolerance from 1–10 seconds, and a filename toggle")
    return {"modified_kind": kind, "modified_tolerance": tolerance, "use_filenames": filenames}


def start(record, method, settings):
    stamp = record.get("filename_time" if method == "filename" else "modified_time")
    duration = record.get("duration")
    if not isinstance(stamp, (int, float)) or not isinstance(duration, (int, float)) or not math.isfinite(stamp) or not math.isfinite(duration) or duration <= 0:
        return None
    return stamp - (duration if method == "modified" and settings["modified_kind"] == "end" else 0)


def models(records, decisions, settings):
    output = []
    modified_groups = {}
    for method in ("modified", *(["filename"] if settings["use_filenames"] else [])):
        field = "filename_time" if method == "filename" else "modified_time"
        counts = Counter((kind, rec.get("clock_group", ""), rec.get(field)) for (kind, _), rec in records.items())
        anchors = []
        for decision in decisions:
            if decision.get("verification", "date_only" if decision.get("row", {}).get("timing_only") else "independent") != "independent":
                continue
            f = records.get(("fpv", decision["fpv_video"]))
            r = records.get(("radio", decision["radio_video"]))
            if not f or not r:
                continue
            if counts[("fpv", f.get("clock_group", ""), f.get(field))] > 1 or counts[("radio", r.get("clock_group", ""), r.get(field))] > 1:
                continue  # Constant/reset/copy timestamps cannot teach this clock.
            fc, rc = start(f, method, settings), start(r, method, settings)
            offset = decision.get("offset_seconds")
            if fc is None or rc is None or not isinstance(offset, (int, float)) or not math.isfinite(offset):
                continue
            if min(r["duration"] - max(0., offset), f["duration"] - max(0., -offset)) <= 0:
                continue
            raw = (r["modified_time"] - f["modified_time"]
                   if r.get("modified_time") is not None and f.get("modified_time") is not None else None)
            anchors.append({"radio_video": r["name"], "fpv_video": f["name"], "offset_seconds": offset,
                            "delta_seconds": rc - fc + offset, "raw_modified_difference_seconds": raw,
                            "recorder": f.get("clock_group", ""), "confirmed_at": decision.get("updated_at", 0)})
        if not anchors:
            continue
        tolerance = settings["modified_tolerance"] if method == "modified" else 2.
        groups = {}
        for anchor in anchors:
            # Filename corroboration follows the modified-date group learned
            # from the same confirmed footage, even if camera epochs differ.
            key = (anchor["recorder"], modified_groups.get(anchor["fpv_video"], "") if method == "filename" else "")
            groups.setdefault(key, []).append(anchor)
        for (recorder, inherited_group), group in sorted(groups.items()):
            clusters = []
            for anchor in sorted(group, key=lambda a: a["delta_seconds"]):
                if not clusters or anchor["delta_seconds"] - clusters[-1][0]["delta_seconds"] > GROUP_SEPARATION_SECONDS:
                    clusters.append([])
                clusters[-1].append(anchor)
            for index, values in enumerate(clusters):
                first = min(values, key=lambda a: (a["confirmed_at"], a["fpv_video"], a["radio_video"]))
                identity = f"{recorder}:{first['radio_video']}:{first['fpv_video']}"
                group_id = inherited_group or "recorder-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
                inferred = not recorder or len(clusters) > 1
                label = (f"{recorder} · clock group {index+1}" if recorder and len(clusters)>1 else recorder or
                         f"Detected recorder group {len({m['group_id'] for m in output if m['method']==method})+1}")
                if inherited_group:
                    label = next((m["label"] for m in output if m["group_id"] == inherited_group), label)
                delta = median(a["delta_seconds"] for a in values)
                flights = len({a["radio_video"] for a in values})
                model = {"id":group_id+":"+method,"group_id":group_id,"method":method,
                         "delta_seconds":delta,"tolerance":tolerance,
                         "consistent":all(abs(a["delta_seconds"]-delta)<=tolerance for a in values),
                         "independent_flights":flights,"tentative":flights<2 or inferred,
                         "recorder":recorder,"label":label,"inferred_recorder":inferred,"anchors":values,
                         "group_separation_seconds":GROUP_SEPARATION_SECONDS,
                         "confirmed_members":sorted({a["fpv_video"] for a in values})}
                output.append(model)
                if method == "modified":
                    modified_groups.update({a["fpv_video"]:group_id for a in values})
    return output


def enrich(results, records, decisions, settings=None, min_overlap=2, rejections=(), clock_rejections=()):
    settings = options(settings)
    learned = models(records, decisions, settings)
    results.update(learned_clocks=learned, clock_settings=settings, clock_warnings=[])
    raw_counts = Counter((kind, rec.get("clock_group", ""), rec.get("modified_time")) for (kind, _), rec in records.items())
    filename_counts = Counter((kind, rec.get("clock_group", ""), rec.get("filename_time")) for (kind, _), rec in records.items())
    warnings = results["clock_warnings"]
    memberships = {name:m["group_id"] for m in learned if m["method"]=="modified" for name in m["confirmed_members"]}
    for model in learned:
        if not model["consistent"]:
            warnings.append(f"{model['label']}: confirmed {model['method']} clock differences disagree; suggestions from this recorder clock are disabled.")

    inferred_counts = Counter(m["method"] for m in learned if m["inferred_recorder"])
    if len({m["group_id"] for m in learned if m["method"]=="modified"}) > 1:
        warnings.append("Confirmed offsets more than five minutes apart form separate possible recorder groups. Unassigned FPV files are considered against each group; a rejected prediction in one group does not rule out another. A camera clock reset can also create a new group.")

    def applicable(f, model):
        return ((not f.get("clock_group") or f["clock_group"] == model["recorder"])
                and (f["name"] not in memberships or memberships[f["name"]] == model["group_id"]))

    def group_key(model):
        return model["group_id"]

    def evidence(f, r, offset, model):
        if not applicable(f, model):
            return None
        method = model["method"]
        fc, rc = start(f, method, settings), start(r, method, settings)
        if fc is None or rc is None:
            return None
        details = {**{k: model[k] for k in ("method", "tolerance", "independent_flights", "tentative", "anchors", "recorder", "label", "inferred_recorder")},
                   "group_id":model["group_id"],
                   "clock_delta_seconds": model["delta_seconds"], "modified_kind": settings["modified_kind"],
                   "raw_modified_difference_seconds": r["modified_time"] - f["modified_time"]
                   if r.get("modified_time") is not None and f.get("modified_time") is not None else None}
        if not model["consistent"]:
            return {**details, "status": "conflicting", "agrees": False}
        key = "modified_time" if method == "modified" else "filename_time"
        counts = raw_counts if method == "modified" else filename_counts
        if counts[("fpv", f.get("clock_group", ""), f[key])] > 1 or counts[("radio", r.get("clock_group", ""), r[key])] > 1:
            return {**details, "status": "repeated_dates", "agrees": False}
        expected = model["delta_seconds"] + fc - rc
        difference = offset - expected
        return {**details, "status": "available", "expected_offset": expected,
                "alignment_difference": difference, "agrees": abs(difference) <= model["tolerance"]}

    rows = results["rows"]
    confirmed_seeds={(a['radio_video'],a['fpv_video']) for model in learned for a in model['anchors']}
    def superseded(row):
        anchor=row.get('seed_anchor',{})
        return row.get('provisional_clock') and (anchor.get('radio_video'),anchor.get('fpv_video')) in confirmed_seeds
    rows[:]=[row for row in rows if not superseded(row)]
    rows[:] = [row for row in rows if not row.get("timing_only") or row.get("reviewed") or row.get("rejected")
               or not rejected_suggestion(row,records,clock_rejections,settings)]
    scope=results.get('match_scope',{})
    scope['timing_suggestions']=[row for row in scope.get('timing_suggestions',[]) if not superseded(row)]
    # Date-only rows from old searches are regenerated whenever reviewed clocks
    # exist, including when those clocks conflict. Never move content offsets.
    labelled = any(kind == "fpv" and rec.get("clock_group") for (kind, _), rec in records.items())
    if learned or labelled:
        rows[:] = [row for row in rows if not row.get("timing_only") or row.get("provisional_clock") or row.get("reviewed") or row.get("rejected") or row.get("review_state") == "later"]
    known = {(row["radio_video"], row["fpv_video"]): row for row in rows}
    confirmed_fpv = {row["fpv_video"] for row in rows if row.get("reviewed")}
    for row in rows:
        row.pop("clock_support", None)
        f, r = records.get(("fpv", row["fpv_video"])), records.get(("radio", row["radio_video"]))
        if not f or not r or row.get("reviewed"):
            continue
        row["clock_support"] = [item for m in learned if (item := evidence(f, r, row["offset_seconds"], m))]
    proposals = []
    negative = {}
    for model in learned:
        for rejection in clock_rejections:
            if (rejection["clock_method"]==model["method"]
                    and (model["method"]!="modified" or rejection.get("modified_kind")==settings["modified_kind"])
                    and abs(rejection["clock_delta_seconds"]-model["delta_seconds"])<=max(model["tolerance"],rejection.get("tolerance",5))):
                negative.setdefault((rejection["fpv_video"],model["group_id"]),set()).add(rejection["radio_video"])
        for rejection in rejections:
            f = records.get(("fpv",rejection.get("fpv_video")))
            r = records.get(("radio",rejection.get("radio_video")))
            item = evidence(f,r,0,model) if f and r else None
            if item and item["status"]=="available":
                overlap = min(r["duration"]-max(0.,item["expected_offset"]), f["duration"]-max(0.,-item["expected_offset"]))
                if overlap >= min_overlap:
                    negative.setdefault((f["name"],model["group_id"]),set()).add(r["name"])
    for (kind, fname), f in records.items():
        if kind != "fpv":
            continue
        for (rkind, rname), r in records.items():
            if rkind != "radio":
                continue
            key = (rname, fname)
            if fname in confirmed_fpv or (key in known and (known[key].get("rejected") or known[key].get("review_state") == "later")):
                continue
            supported = [(m, item) for m in learned if (item := evidence(f, r, 0, m)) and item["status"] == "available"]
            for model, first in supported:
                same_recorder = [(m, item) for m, item in supported if group_key(m) == group_key(model)]
                # Modified dates are the primary clock; filename dates corroborate
                # only that same recorder, never a different drone's clock.
                if model["method"] == "filename" and any(m["method"] == "modified" for m, _ in same_recorder):
                    continue
                offset = first["expected_offset"]
                overlap = max(0., min(r["duration"] - max(0., offset), f["duration"] - max(0., -offset)))
                if overlap < min_overlap:
                    continue
                if any(abs(item["expected_offset"] - offset) > max(first["tolerance"], item["tolerance"]) for _, item in same_recorder):
                    continue
                proposal = {"radio_video": rname, "fpv_video": fname, "offset_seconds": offset,
                                  "overlap_seconds": overlap, "clock_source": f"{model['label']} · confirmed {first['method']} dates",
                                  "clock_support": first["independent_flights"], "tentative": first["tentative"] or not f.get("clock_group"),
                                  "recorder": model["recorder"], "clock_label": model["label"],
                                  "group_id":model["group_id"],"clock_method":model["method"],"clock_delta_seconds":model["delta_seconds"],
                                  "negative_support":sorted(negative.get((fname,model["group_id"]),set())),
                                  "recorder_unassigned": not bool(f.get("clock_group")),
                                  "support": [evidence(f, r, offset, m) for m, _ in same_recorder]}
                if not rejected_suggestion(proposal,records,clock_rejections,settings):
                    proposals.append(proposal)
    proposals.sort(key=lambda p: (len(p["negative_support"]),-p["overlap_seconds"],p["fpv_video"],p["radio_video"]))
    for proposal in proposals:
        key = (proposal["radio_video"], proposal["fpv_video"])
        if key not in known:
            row = date_row(proposal)
            rows.append(row)
            known[key] = row
    # Make unscanned recordings available to the regular preview and review API.
    for (kind, name), record in records.items():
        if not any(c["name"] == name for c in results["clips"][kind]):
            results["clips"][kind].append({k: v for k, v in record.items() if k != "filename_time"})
    if learned or labelled:
        scope = results.setdefault("match_scope", {})
        unique = {}
        for row in scope.get('timing_suggestions',[])+rows:
            existing=known.get((row['radio_video'],row['fpv_video']),{})
            if (row.get('provisional_clock') and row['fpv_video'] not in confirmed_fpv and not existing.get('rejected')
                    and not existing.get('reviewed') and existing.get('review_state') != 'later'):
                if not rejected_suggestion(row,records,clock_rejections,settings):
                    unique[row['radio_video'],row['fpv_video'],row.get('seed_clock_id'),row['offset_seconds']]=row
        provisional = list(unique.values())
        scope.update(timing_suggestions=proposals+provisional, timing_suggestions_truncated=0)
    if len({m["label"] for m in learned if m["method"] == "modified"}) > 1 or len({rec.get("clock_group", "") for (kind, _), rec in records.items() if kind == "fpv"}) > 1:
        results["clock"] = {}  # No legacy pooled clock represents this session.
    results["confirmed_clock_suggestions"] = proposals
    # Old date-only rows are also filtered, including provisional seed clocks.
    rows[:] = [r for r in rows if not r.get("timing_only") or r.get("reviewed") or r.get("rejected")
               or not rejected_suggestion(r,records,clock_rejections,settings)]
    scope["timing_suggestions"] = [r for r in scope.get("timing_suggestions",[]) if not rejected_suggestion(r,records,clock_rejections,settings)]
    results["clock_rejections"] = list(clock_rejections)
    results["recorder_groups"] = [{"id":m["group_id"],"label":m["label"],"confirmed_members":m["confirmed_members"],
        "delta_seconds":m["delta_seconds"],"consistent":m["consistent"],"independent_flights":m["independent_flights"]}
        for m in learned if m["method"]=="modified"]
    if negative:
        warnings.append("Rejected FPV/StickCam pairs suppress those pairs and count against the corresponding recorder-group predictions for that FPV. Other groups stay available; rejection alone does not establish a drone identity.")
    if any(item["status"] == "repeated_dates" for row in rows for item in row.get("clock_support", []) if item):
        warnings.append("Repeated dates within a feed were excluded from clock evidence; copying or a reset may have destroyed ordering.")
    return results


def rejected_suggestion(proposal, records, rejections, settings):
    """Reject a predicted alignment, keeping other offsets/groups reviewable."""
    f = records.get(("fpv",proposal.get("fpv_video")))
    r = records.get(("radio",proposal.get("radio_video")))
    if not f or not r:
        return False
    for rejection in rejections:
        if (rejection.get("radio_video"),rejection.get("fpv_video")) != (r["name"],f["name"]):
            continue
        method = rejection["clock_method"]
        if method == "modified" and rejection.get("modified_kind") != settings["modified_kind"]:
            continue
        rc,fc = start(r,method,settings),start(f,method,settings)
        if rc is not None and fc is not None and abs(rc-fc+proposal["offset_seconds"]-rejection["clock_delta_seconds"]) <= rejection.get("tolerance",5):
            return True
    return False


def date_row(proposal):
    offset = proposal["offset_seconds"]
    zeros = {k: 0. for k in ("score", "combined_score", "candidate_margin", "peak_margin", "section_agreement", "section_spread",
             "tracking_coverage", "valid_motion_seconds", "event_score", "audio_best_score", "audio_score_at_motion_offset",
             "audio_fingerprint_strength", "audio_fingerprint_span_seconds", "audio_overlap_seconds", "audio_section_agreement", "audio_owner_margin")}
    counts = {k: 0 for k in ("reliable_axes", "correlated_sections", "usable_sections", "audio_correlated_sections", "audio_usable_sections",
              "audio_independent_sections", "audio_trend_sections", "audio_joint_sections", "audio_fingerprint_shared_hashes")}
    return {**proposal, **zeros, **counts, "clock_support": [s for s in proposal["support"] if s],
            "confidence": "review", "evidence": proposal["clock_source"] + " · content not checked",
            "global_assignment": False, "reviewed": False, "timing_only": True, "motion_included": False, "audio_included": False,
            "fpv_motion_source": "not_assessed", "axis_details": {}, "alternatives": [],
            "audio_best_offset_seconds": None, "audio_fingerprint_offset_seconds": None, "timestamp_agreement": None,
            "radio_common_start_seconds": max(0., offset), "fpv_common_start_seconds": max(0., -offset),
            "review_reasons": ["Date suggestion only; independently check shared content and refine sync"]}
