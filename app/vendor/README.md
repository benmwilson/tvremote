# Bundled libraries

- `hls.min.js`: hls.js 1.5.17 (Apache-2.0, https://github.com/video-dev/hls.js), downloaded from
  https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.17/hls.min.js (only the trailing source-map comment was removed).
  Used by "Watch on this computer": Safari plays HLS itself, Chrome, Edge and Firefox need this. Served by the app at /vendor/hls.min.js; nothing is loaded from a CDN.
  To update: replace the file, and note the version here.
