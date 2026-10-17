"""Files to a cloud phone: the `push_file` helper.

Both ride the session's file routes and the phone's `url.open` op. The
service signs short-lived storage links; this machine uploads to or downloads
from them, and the phone opens one. Nothing here prints a link: a link is a
credential for fifteen minutes.

push_file  upload a file, then the phone's browser opens it under its own
           name. What happens next on the phone is the caller's move.

Cloud phones only for now: it follows the attached session (or --session),
the one tap() drives, and a local phone raises Unsupported.
"""
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import cloud
from .transport import Unsupported

CLOUD_ONLY = "cloud phones only for now: {} needs a cloud phone whose host can open links (a cloud iPhone)"


def _session(what, backend):
    """The attached cloud session reached by a control link, or a clear error."""
    sess = cloud.attached()
    if not sess or sess["link"]["kind"] != "control":
        raise Unsupported(CLOUD_ONLY.format(what) + ("" if sess else "; no cloud phone is attached"))
    if not backend.supports("url.open"):
        raise Unsupported(f"{what}: this phone's host cannot open links yet (no url.open op)")
    return sess


def _put(url, path, headers):
    """PUT the file to its signed link with exactly the headers the service
    named: the store signed them into the link and refuses a PUT without them."""
    data = Path(path).read_bytes()
    req = urllib.request.Request(url, data=data, method="PUT", headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=max(60, len(data) / 200_000)) as reply:
            if reply.status // 100 != 2:
                raise TransferError(f"the upload was refused (HTTP {reply.status})")
    except urllib.error.HTTPError as e:
        raise TransferError(f"the upload was refused (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise TransferError(f"could not reach the file store ({type(e).__name__})") from None
    return len(data)


class TransferError(RuntimeError):
    pass


def push_file(path, backend):
    """Send a file to the phone: it opens in the phone's browser under its own
    name. Returns {filename, size, content_type, expires_at}."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not a file")
    sess = _session("push_file", backend)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    transfer = cloud._api("POST", f"/sessions/{sess['sid']}/files",
                          {"filename": path.name, "content_type": content_type})
    headers = transfer.get("upload_headers") or {"Content-Type": content_type}
    size = _put(transfer["upload_url"], path, headers)
    backend.send("url.open", url=transfer["download_url"])
    print(f"{transfer['filename']} ({size:,} bytes) is open in the phone's browser")
    return {"filename": transfer["filename"], "size": size, "content_type": content_type,
            "expires_at": transfer["expires_at"]}
