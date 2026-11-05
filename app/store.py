"""One safe way to save a JSON file: write a uniquely named temporary file next to it, then rename it over the real one.
The rename is atomic, so a reader never sees half a file, and the unique name means two saves at once cannot trip over each other."""
import json
import os
import tempfile


def write_json(path, data, mode=0o644, indent=None):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.chmod(tmp, mode)  # mkstemp makes it 0600: set what this file should be (0600 for secrets)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
