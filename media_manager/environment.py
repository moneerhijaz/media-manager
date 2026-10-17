"""Load supported credentials from private dotenv files without executing them."""
import json
import os
import shlex
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path
from urllib.parse import unquote, urlsplit


SUPPORTED = {"TMDB_READ_TOKEN", "OPENSUBTITLES_USERNAME", "OPENSUBTITLES_API_KEY",
             "OPENSUBTITLES_PASSWORD", "OPENSUBTITLES_TOKEN"}


def installed_source():
    # pip/pipx records the local installation source, even for non-editable
    # installs. This lets a CLI launched from the library find the project's
    # private .env without bundling credentials into the installed package.
    try:
        data = json.loads(distribution("stateful-media-manager").read_text("direct_url.json") or "{}")
        url = urlsplit(data.get("url", ""))
        if url.scheme == "file" and not url.netloc and "dir_info" in data:
            return Path(unquote(url.path))
    except (PackageNotFoundError, ValueError, TypeError, OSError):
        pass
    return None


def load_environment(root=None):
    root = Path(root) if root is not None else Path.cwd()
    source = installed_source()
    paths = [root / ".env", root / ".media-manager" / ".env",
             Path.home() / ".config" / "media-manager" / ".env",
             Path(__file__).resolve().parent.parent / ".env"]
    if source:
        paths.append(source / ".env")
    for path in dict.fromkeys(paths):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            from .utils import MediaManagerError
            raise MediaManagerError(f"Cannot read credential file: {path}") from None
        for line in lines:
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, separator, value = line.partition("=")
            name = name.strip()
            if not separator or name not in SUPPORTED or name in os.environ:
                continue
            try:
                parts = shlex.split(value, comments=True, posix=True)
            except ValueError:
                from .utils import MediaManagerError
                raise MediaManagerError(f"Invalid quoting for {name} in {path}") from None
            if len(parts) == 1 and parts[0]:
                os.environ[name] = parts[0]
