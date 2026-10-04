#!/usr/bin/env bash
# Build official OpenCV sources in this project; no system packages are replaced.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$project_dir/.venv/bin/python"
build_dir="$project_dir/.gpu-build/opencv-python"
source_commit=2ba8bf62f2f1215ab620416b041c9659005fa25c
cuda_root="${CUDA_PATH:-/usr/local/cuda}"
cuda_arch="${FPV_CUDA_ARCH:-$(nvidia-smi -i 0 --query-gpu=compute_cap --format=csv,noheader)}"
if [[ ! "$cuda_arch" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo 'Set FPV_CUDA_ARCH to the target compute capability, for example 8.6.' >&2
  exit 1
fi
uv pip install --python "$runtime" pip cmake scikit-build setuptools wheel
if [[ ! -d "$build_dir" ]]; then
  git clone --depth 1 --branch 94 --recurse-submodules --shallow-submodules \
    https://github.com/opencv/opencv-python.git "$build_dir"
fi
if [[ "$(git -C "$build_dir" rev-parse HEAD)" != "$source_commit" ]]; then
  echo 'Build folder contains a different source revision; use a fresh build folder.' >&2
  exit 1
fi
export PATH="$project_dir/.venv/bin:$cuda_root/bin:$PATH"
export ENABLE_CONTRIB=1 ENABLE_HEADLESS=1
export CMAKE_BUILD_PARALLEL_LEVEL="${FPV_BUILD_JOBS:-6}"
export CMAKE_ARGS="-DWITH_CUDA=ON -DCUDA_ARCH_BIN=$cuda_arch -DCUDA_ARCH_PTX= -DWITH_CUDNN=OFF -DOPENCV_DNN_CUDA=OFF -DBUILD_LIST=core,imgproc,imgcodecs,video,videoio,calib3d,features2d,python3,cudev,cudaarithm,cudafilters,cudaimgproc,cudawarping,cudaoptflow -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF -DWITH_OPENCL=OFF"
cd "$build_dir"
"$runtime" setup.py bdist_wheel
uv pip uninstall --python "$runtime" opencv-python-headless opencv-python opencv-contrib-python opencv-contrib-python-headless
uv pip install --python "$runtime" --no-deps "$build_dir"/dist/opencv_contrib_python_headless-4.14.0.94-*.whl
cd "$project_dir"
"$runtime" -c 'from fpv_matcher.video.acceleration import capabilities; print(capabilities())'
