FPV Matcher · Windows x64 test build
====================================

This is a portable, experimental test build. Extract the entire ZIP folder; do
not move only FPV Matcher.exe out of its _internal folder.

Requirements
------------
- Windows 10 or 11, 64-bit.
- FFmpeg and ffprobe installed and available on PATH. Windows download links
  are listed by the FFmpeg project at https://ffmpeg.org/download.html#build-windows.
- No Python or Rust installation is needed to run this package.

Start on this PC
----------------
Double-click Start-FPV-Matcher.bat, then open http://127.0.0.1:8767/ in a
browser. Keep the FPV Matcher console window open while using the app; close
that window to stop the server.

Allow LAN access
----------------
Double-click Start-FPV-Matcher-LAN.bat to bind the server to all network
interfaces. Find this PC's IPv4 address with `ipconfig`, then browse to
http://<this-PC-IPv4-address>:8767/ from a trusted device on the same network.
Windows Firewall may ask whether to allow access. This test build has no login;
use the LAN launcher only on a trusted private network, and do not port-forward
it to the internet.

Data and limitations
--------------------
- Sessions, caches and exports are stored under
  %USERPROFILE%\.fpv-radio-motion-pairing, separate from this extracted folder.
- Saved rate presets are in _internal\rate_presets.yaml beside the bundled app.
- This standard build uses CPU processing. CUDA-enabled OpenCV is not bundled.
- Preview, analysis and export require FFmpeg/ffprobe. The app checks for them
  and reports missing tools in its system status.
- This package is an early test snapshot, not an installer or supported release.
  Please send the project owner the error text and Windows version if something
  fails.
