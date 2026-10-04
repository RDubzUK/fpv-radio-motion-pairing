"""Approximate radio face geometry, used as editable priors rather than identity proof.

Normalized shell coordinates exclude antennas, switches and removable sticks.
Layouts are estimates from manufacturer illustrations, not engineering dimensions.
A session's confirmed photographic reference always takes precedence.
"""
from __future__ import annotations

VERSION = "radio-shell-priors-v1"

# Rounded box and gamepad shell families. Per-model landmarks/aspects remain
# separate: these are deliberately broad priors for perspective and hand occlusion.
BOX = [[.12, 0], [.88, 0], [1, .16], [1, .8], [.88, 1], [.12, 1], [0, .8], [0, .16]]
GAMEPAD = [[.18, 0], [.82, 0], [1, .22], [.97, .78], [.82, 1], [.66, .94], [.59, .7], [.41, .7], [.34, .94], [.18, 1], [.03, .78], [0, .22]]
COMPACT = [[.1, 0], [.9, 0], [1, .16], [.95, .82], [.83, 1], [.17, 1], [.05, .82], [0, .16]]


def _model(id, label, family, aspect, centers, radius, screen, source, variants=()):
    return {"id": id, "label": label, "family": family, "body_aspect": aspect,
            "shell": GAMEPAD if family == "gamepad" else COMPACT if family == "compact" else BOX,
            "gimbals": [[centers[0], centers[2]], [centers[1], centers[2]]],
            "gimbal_radius": radius, "screen": screen, "source": source, "variants": list(variants),
            "approximate": True}


MODELS = [
    _model("rm-tx16s", "RadioMaster TX16S", "full size", 1.0, (.25, .75, .37), .17, [.35, .66, .65, .88],
           "https://radiomasterrc.com/products/tx16s-mark-ii-radio-controller", ("MKII", "MAX")),
    _model("rm-boxer", "RadioMaster Boxer", "box", 1.27, (.25, .75, .4), .19, [.32, .75, .68, .89],
           "https://radiomasterrc.com/products/boxer-radio-controller-m2", ("MAX", "Crush")),
    _model("rm-gx12", "RadioMaster GX12", "compact", 1.24, (.25, .75, .4), .18, [.35, .76, .65, .88],
           "https://radiomasterrc.com/products/gx12-dual-band-gemini-x-radio-controller", ("Crush",)),
    _model("rm-tx15", "RadioMaster TX15", "full size", 1.08, (.25, .75, .36), .18, [.34, .64, .66, .9],
           "https://radiomasterrc.com/products/tx15-radio-controller-elrs-m2", ("MAX",)),
    _model("rm-tx12", "RadioMaster TX12", "compact", 1.15, (.25, .75, .4), .16, [.31, .73, .69, .9],
           "https://radiomasterrc.com/products/tx12-mark-ii-radio-controller", ("Mark II",)),
    _model("rm-pocket", "RadioMaster Pocket", "compact", 1.4, (.25, .75, .4), .16, [.34, .73, .66, .91],
           "https://radiomasterrc.com/products/pocket-radio-controller-m2", ("Crush",)),
    _model("rm-zorro", "RadioMaster Zorro", "gamepad", 1.65, (.25, .75, .47), .17, [.38, .02, .62, .3],
           "https://radiomasterrc.com/products/zorro-radio-controller"),
    _model("tbs-tango2", "TBS Tango 2", "gamepad", 1.65, (.25, .75, .38), .18, [.39, .6, .61, .74],
           "https://www.team-blacksheep.com/products/product:4067", ("Pro",)),
    _model("tbs-mambo", "TBS Mambo", "box", 1.35, (.25, .75, .39), .18, [.33, .75, .67, .9],
           "https://www.team-blacksheep.com/products/prod:tbs_mambo", ("Ethix",)),
    _model("beta-literadio3", "BETAFPV LiteRadio 3", "gamepad", 1.7, (.25, .75, .4), .16, None,
           "https://betafpv.com/products/literadio-3-radio-transmitter", ("Pro",)),
]


def model_for(value):
    if not isinstance(value, str):
        return None
    key = value.strip().casefold()
    return next((m for m in MODELS if key in {m["id"].casefold(), m["label"].casefold()}), None)


def model_id(value):
    model = model_for(value)
    return model["id"] if model else "other"


def geometry_support(model, left, right, width, height):
    """Soft landmark prior in a marked, rectified body; never substitutes rims."""
    import numpy as np
    points = np.asarray([[left[0] / width, left[1] / height], [right[0] / width, right[1] / height]])
    distance = float(np.sqrt(np.mean((points - model["gimbals"]) ** 2)))
    return float(np.exp(-distance ** 2 / (2 * .18 ** 2)))


def suggest_shell(image, proposal, choice="auto"):
    """Fit bounded silhouette hypotheses to observed rims, requiring visible edges.

    This is supporting geometry, not a trained model classifier. Similar shells
    produce several candidates and the operator confirms the actual radio.
    """
    import cv2
    import numpy as np
    if choice == "other" or proposal.get("radio_outline"):
        return proposal
    selected = model_for(choice)
    models = [selected] if selected else MODELS if choice == "auto" else []
    regions = proposal["regions"]
    centers = np.asarray([[regions[s]["x"] + regions[s]["center_x"], regions[s]["y"] + regions[s]["center_y"]]
                          for s in ("left_gimbal", "right_gimbal")], np.float32)
    vector = centers[1] - centers[0]
    spacing = float(np.linalg.norm(vector))
    if spacing < 40:
        return proposal
    horizontal = vector / spacing
    vertical = np.asarray([-horizontal[1], horizontal[0]])
    edges = cv2.Canny(image, 70, 140)
    distance = cv2.distanceTransform(255 - edges, cv2.DIST_L2, 3)
    height, width = image.shape[:2]
    candidates = []
    for model in models:
        body_width = spacing / (model["gimbals"][1][0] - model["gimbals"][0][0])
        radius = np.mean([min(r["width"], r["height"]) / 2.5 for r in regions.values()])
        if not .55 <= radius / (body_width * model["gimbal_radius"]) <= 1.65:
            continue
        for depth in (.85, 1., 1.15):
            body_height = body_width / model["body_aspect"] * depth
            origin = centers[0] - horizontal * body_width * model["gimbals"][0][0] - vertical * body_height * model["gimbals"][0][1]
            def project(points):
                points = np.asarray(points)
                return origin + points[:, :1] * body_width * horizontal + points[:, 1:] * body_height * vertical
            shell = project(model["shell"])
            box = project([[0, 0], [1, 0], [1, 1], [0, 1]])
            if (box < 0).any() or (box[:, 0] >= width).any() or (box[:, 1] >= height).any():
                continue
            if not .015 * width * height < abs(cv2.contourArea(box.astype(np.float32))) < .8 * width * height:
                continue
            # Sample each shell edge separately, so one rim/background edge
            # cannot support the entire body. Hands may obscure some sectors.
            segments = [np.linspace(a, b, 12) for a, b in zip(shell, np.roll(shell, -1, axis=0))]
            coverage = []
            tolerance = max(2.5, min(8., body_width * .012))
            for segment in segments:
                pixels = np.rint(segment).astype(int)
                coverage.append(float((distance[pixels[:, 1], pixels[:, 0]] <= tolerance).mean()))
            support = float(np.mean(coverage))
            distributed = sum(c >= .4 for c in coverage) / len(coverage)
            if support >= .52 and distributed >= .6:
                candidates.append({"model_id": model["id"], "label": model["label"],
                                   "edge_support": support, "outline": box.tolist(), "shell": shell.tolist()})
    candidates.sort(key=lambda row: row["edge_support"], reverse=True)
    if not candidates:
        return {**proposal, "shape_message": "No clear shell fit; mark the outline or use a confirmed session reference."}
    unique = []
    for candidate in candidates:
        if not any(row["model_id"] == candidate["model_id"] for row in unique):
            unique.append(candidate)
    best = candidates[0]
    # Only offer shell corners for a selected model or a distinct auto hypothesis.
    distinct = len(unique) == 1 or best["edge_support"] - unique[1]["edge_support"] >= .08
    result = {**proposal, "shape_matches": [{k: r[k] for k in ("model_id", "label", "edge_support")} for r in unique[:3]],
              "shape_message": "Approximate shell fit; confirm the model and all corners." if selected or distinct else "Several radio shapes fit; choose the model or mark its outline."}
    if selected or distinct:
        result.update(radio_outline=best["outline"], shape_shell=best["shell"], detection_method="paired rims with catalogue shell edges")
    return result
