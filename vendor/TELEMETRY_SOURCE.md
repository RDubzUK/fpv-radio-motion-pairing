# Telemetry parser source

This directory contains the official [AdrianEddy/telemetry-parser](https://github.com/AdrianEddy/telemetry-parser) source at commit `d45ebf2afce85fa691838fd32b3da8ae2fcac773`.

The published Python 0.3.0 wheel predates newer DJI formats used by the Osmo Nano clips in this project. This source includes DJI CAM metadata detection and newer protobuf variants. The application builds its Python extension from the included source through `uv sync`; Rust/Cargo and network access for build dependencies are needed for the initial build. Cargo locks pin the native dependencies. Upstream licenses remain in the source directory.

Local build changes in `bin/python-module/pyproject.toml`: declare dynamic version metadata and use Maturin 1.8–1.x. No parser source changes. The application cache version includes the upstream revision, invalidating telemetry extracted or marked unsupported by the older wheel.
