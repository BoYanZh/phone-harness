"""Files to a cloud phone and back: `phone-harness push FILE`, `phone-harness pull`.

Both ride the session's file routes and the phone's `url.open` op. The
service signs short-lived storage links; this machine uploads to or downloads
from them, and the phone opens one. Nothing here prints a link: a link is a
credential for fifteen minutes.

push   upload FILE, then the phone's browser opens it under its own name.
       What happens next on the phone (saving it, sharing it) is the
       caller's move.
pull   the phone's browser opens a small upload page; whoever is driving
       the phone picks a file there. This command waits for it, downloads
       it, and prints the local path.
"""
import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import cloud, term
from .remote import Remote

CLI_USAGE = """Usage:
  phone-harness push FILE [--session SID]           send FILE to the cloud phone: it opens in the phone's browser
  phone-harness pull [--out DIR] [--timeout SECONDS] [--session SID]
                                                    the phone opens an upload page; whatever is sent there lands in DIR (default .)
Both need a cloud phone whose host can open links (`supports('url.open')`); today that is a cloud iPhone.
"""

POLL_SECONDS = 2
DEFAULT_WAIT = 300


def _session():
    """The attached (or pinned) cloud session that is reached by a control link."""
    sess = cloud.attached()
    if not sess:
        sys.exit("No cloud phone attached. Start one with `phone-harness cloud start`, or pass --session SID.")
    if sess["link"]["kind"] != "control":
        sys.exit("File transfer needs a cloud phone the host can open links on; this session is an Android over adb "
                 "(use adb push/pull there).")
    return sess


def _remote(sess):
    phone = Remote(sess["link"]["url"], sess["link"]["token"])
    if not phone.supports("url.open"):
        sys.exit("This phone's host cannot open links yet (no url.open op); ask the operator to update the host.")
    return phone


def _put(url, path, content_type):
    data = Path(path).read_bytes()
    req = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=max(60, len(data) / 200_000)) as reply:
            if reply.status // 100 != 2:
                raise CloudTransferError(f"the upload was refused (HTTP {reply.status})")
    except urllib.error.HTTPError as e:
        raise CloudTransferError(f"the upload was refused (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise CloudTransferError(f"could not reach the file store ({type(e).__name__})") from None
    return len(data)


def _get(url, dest):
    try:
        with urllib.request.urlopen(url, timeout=120) as reply, open(dest, "wb") as out:
            digest, size = hashlib.sha256(), 0
            while True:
                chunk = reply.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except urllib.error.HTTPError as e:
        raise CloudTransferError(f"the download was refused (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise CloudTransferError(f"could not reach the file store ({type(e).__name__})") from None
    return size, digest.hexdigest()


class CloudTransferError(RuntimeError):
    pass


def push(args):
    args = list(args)
    pinned = cloud._option(args, "--session")
    if pinned:
        os.environ["PHONE_HARNESS_SESSION"] = pinned
    if len(args) != 1 or args[0].startswith("-"):
        sys.exit(CLI_USAGE)
    path = Path(args[0]).expanduser()
    if not path.is_file():
        sys.exit(f"{path} is not a file")
    sess = _session()
    phone = _remote(sess)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    transfer = cloud._api("POST", f"/sessions/{sess['sid']}/files",
                          {"filename": path.name, "content_type": content_type})
    size = _put(transfer["upload_url"], path, content_type)
    phone.send("url.open", url=transfer["download_url"])
    print(f"{term.mark()} {transfer['filename']} ({size:,} bytes, {content_type}) is open in the phone's browser; "
          f"the link lasts until {time.strftime('%H:%M', time.localtime(transfer['expires_at']))}.")
    return 0


def pull(args):
    args = list(args)
    pinned = cloud._option(args, "--session")
    if pinned:
        os.environ["PHONE_HARNESS_SESSION"] = pinned
    out_dir = Path(cloud._option(args, "--out") or ".").expanduser()
    wait = cloud._option(args, "--timeout")
    wait = int(wait) if wait else DEFAULT_WAIT
    if args:
        sys.exit(CLI_USAGE)
    if not out_dir.is_dir():
        sys.exit(f"{out_dir} is not a directory")
    sess = _session()
    phone = _remote(sess)
    inbox = cloud._api("POST", f"/sessions/{sess['sid']}/files/inbox", {})
    phone.send("url.open", url=inbox["page_url"])
    print(f"{term.mark()} The phone's browser is showing an upload page; choose a file there. "
          f"Waiting up to {wait} s.", flush=True)
    deadline = time.monotonic() + wait
    status = {"state": "waiting"}
    while time.monotonic() < deadline:
        status = cloud._api("GET", f"/sessions/{sess['sid']}/files/{inbox['id']}")
        if status.get("state") == "uploaded":
            break
        time.sleep(POLL_SECONDS)
    if status.get("state") != "uploaded":
        sys.exit(f"Nothing arrived within {wait} s. The page on the phone stays usable until "
                 f"{time.strftime('%H:%M', time.localtime(inbox['expires_at']))}; run pull again to wait longer.")
    name = status.get("filename") or f"phone-{inbox['id']}"
    dest = out_dir / name
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = out_dir / f"{stem}-{n}{suffix}"
        n += 1
    size, sha = _get(status["download_url"], dest)
    print(json.dumps({"path": str(dest), "size": size, "sha256": sha,
                      "content_type": status.get("content_type")}))
    return 0
