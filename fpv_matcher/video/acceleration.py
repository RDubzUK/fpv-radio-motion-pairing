"""Optional OpenCV CUDA tracking with explicit, observable CPU fallback.

One reduced frame upload is shared by forward/reverse flow and crop warps.
Geometry, SIFT reacquisition and visibility checks intentionally remain on CPU.
"""
from __future__ import annotations

import os
import subprocess
from collections import OrderedDict
from functools import lru_cache

import cv2
import numpy as np


@lru_cache(maxsize=1)
def capabilities():
    try:
        count = cv2.cuda.getCudaEnabledDeviceCount()
        if count <= 0:
            return {"available": False, "reason": "OpenCV CUDA tracking is unavailable in this runtime"}
        factory = getattr(cv2.cuda, "SparsePyrLKOpticalFlow_create", None)
        if factory is None or not hasattr(cv2.cuda, "createTemplateMatching") or not hasattr(cv2.cuda, "warpPerspective"):
            return {"available": False, "reason": "OpenCV is missing the CUDA tracking modules"}
        info = cv2.cuda.DeviceInfo(0)
        if not info.isCompatible():
            return {"available": False, "reason": "OpenCV CUDA build does not support this GPU"}
        try:
            result = subprocess.run(["nvidia-smi", "-i", "0", "--query-gpu=name", "--format=csv,noheader"],
                                    capture_output=True, text=True, timeout=3)
            device = result.stdout.strip() if result.returncode == 0 else "NVIDIA CUDA device 0"
        except (OSError, subprocess.TimeoutExpired):
            device = "NVIDIA CUDA device 0"
        return {"available": True, "device": device, "devices": count,
                "operations": ["optical flow", "perspective crops", "stick template matching"],
                "cpu_operations": ["SIFT reacquisition", "geometry and confidence checks"]}
    except (cv2.error, AttributeError, RuntimeError) as exc:
        return {"available": False, "reason": str(exc).splitlines()[0]}


class TrackingBackend:
    def __init__(self, mode=None):
        self.mode = mode or os.environ.get("FPV_MATCHER_TRACKING", "auto")
        if self.mode not in {"auto", "cpu", "cuda"}:
            raise ValueError("Choose automatic, CPU or CUDA tracking")
        info = capabilities() if self.mode != "cpu" else {}
        self.cuda = bool(info.get("available"))
        self.reason = "CPU selected" if self.mode == "cpu" else info.get("reason", "")
        self.device = info.get("device", "")
        self.calls = 0
        self.frames = OrderedDict()
        self.lk = self.matcher = None
        if self.mode == "cuda" and not self.cuda:
            raise RuntimeError(self.reason)
        if self.cuda:
            try:
                cv2.cuda.setDevice(0)
                self.lk = cv2.cuda.SparsePyrLKOpticalFlow_create((25, 25), 3, 30, False)
                self.matcher = cv2.cuda.createTemplateMatching(cv2.CV_8UC1, cv2.TM_CCOEFF_NORMED)
            except (cv2.error, AttributeError) as exc:
                self._fallback(exc)

    def _fallback(self, exc):
        self.reason = f"CUDA tracking failed: {str(exc).splitlines()[0]}"
        self.cuda = False
        self.frames.clear()
        if self.mode == "cuda":
            raise RuntimeError(self.reason) from exc

    def _remember(self, frame, gpu):
        self.frames[id(frame)] = (frame, gpu)
        self.frames.move_to_end(id(frame))
        while len(self.frames) > 10:
            self.frames.popitem(last=False)
        return gpu

    def _gpu(self, frame):
        pair = self.frames.get(id(frame))
        if pair is not None and pair[0] is frame:
            self.frames.move_to_end(id(frame))
            return pair[1]
        gpu = cv2.cuda_GpuMat()
        gpu.upload(np.ascontiguousarray(frame))
        return self._remember(frame, gpu)

    def flow(self, previous, current, points, _unused=None, winSize=(25, 25), maxLevel=3):
        if self.cuda:
            try:
                p = cv2.cuda_GpuMat()
                p.upload(np.asarray(points, np.float32).reshape(1, -1, 2))
                moved, status, error = self.lk.calc(self._gpu(previous), self._gpu(current), p, None,
                                                   status=cv2.cuda_GpuMat(), err=cv2.cuda_GpuMat())
                self.calls += 1
                return moved.download().reshape(-1, 1, 2), status.download().reshape(-1, 1), error.download().reshape(-1, 1)
            except cv2.error as exc:
                self._fallback(exc)
        return cv2.calcOpticalFlowPyrLK(previous, current, points, None, winSize=winSize, maxLevel=maxLevel)

    def warp(self, frame, transform, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=127):
        if self.cuda:
            try:
                gpu = cv2.cuda.warpPerspective(self._gpu(frame), transform, size, flags=flags,
                                               borderMode=cv2.BORDER_CONSTANT, borderValue=borderValue)
                crop = gpu.download()
                self._remember(crop, gpu)
                self.calls += 1
                return crop
            except cv2.error as exc:
                self._fallback(exc)
        return cv2.warpPerspective(frame, transform, size, flags=flags, borderValue=borderValue)

    def match(self, crop, template):
        if self.cuda:
            try:
                result = self.matcher.match(self._gpu(crop), self._gpu(template)).download()
                self.calls += 1
                return result
            except cv2.error as exc:
                self._fallback(exc)
        return cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)

    def status(self):
        backend = "cuda" if self.cuda else "cpu"
        label = f"CUDA tracking · {self.device}" if self.cuda else f"CPU tracking · {self.reason}"
        if not self.cuda and self.calls:
            backend = "cuda_then_cpu"
        return {"tracking_backend": backend, "tracking_label": label,
                "tracking_gpu_operations": self.calls, "tracking_fallback": self.reason}

    def metadata(self):
        return {key: np.asarray(value) for key, value in self.status().items()}
