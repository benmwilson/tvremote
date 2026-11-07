#!/bin/sh
# Shows the TV page while the app wants it (the flag file in /data) and nothing otherwise, so the browser costs nothing during a stream.
# The app writes a new key at every start and removes the flag whenever a stream starts.
FLAG=/data/tvbrowser.on
KEY=/data/tvkey
pid=
while true; do
  if [ -f "$FLAG" ] && [ -s "$KEY" ]; then
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
      cog -P wl --media-playback-requires-user-gesture=false "http://127.0.0.1:${PORT:-8765}/tv?k=$(cat "$KEY")" >>/tmp/cog.log 2>&1 &  # the browser's own messages (a renderer crash says why here): docker exec tvbrowser tail /tmp/cog.log
      pid=$!
    fi
  elif [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; pid=
  fi
  sleep 0.5
done
