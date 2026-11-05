"""Write config/labwc-rc.xml: where the desktop puts each multi-stream window. Run after changing the layouts in app/multi.py.
On the Pi: cp ~/tvremote/config/labwc-rc.xml ~/.config/labwc/rc.xml && kill -HUP $(pgrep -x labwc)"""
import os
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "app"))
import multi  # noqa: E402

with open(os.path.join(root, "config", "labwc-rc.xml"), "w", newline="\n") as f:
    f.write(multi.rules_xml())
