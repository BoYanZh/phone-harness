#!/usr/bin/env python3
"""Passive focus/Space observation. No input, activation, titles, or text reads."""
import argparse
import ctypes
import json
import os
import signal
import time
from pathlib import Path

import ApplicationServices as AX
import Quartz as Q
from AppKit import (NSWorkspace, NSWorkspaceDidActivateApplicationNotification,
                    NSWorkspaceActiveSpaceDidChangeNotification,
                    NSWorkspaceApplicationKey)
from Foundation import NSDate, NSRunLoop

parser = argparse.ArgumentParser()
parser.add_argument('--seconds', type=float, default=600)
parser.add_argument('--output', required=True)
args = parser.parse_args()
if not 1 <= args.seconds <= 1800:
    parser.error('duration must be between 1 and 1800 seconds')
out = Path(args.output)
out.parent.mkdir(parents=True, exist_ok=True)
stream = out.open('a', buffering=1)
stop = False
def halt(*_):
    global stop
    stop = True
signal.signal(signal.SIGTERM, halt)
signal.signal(signal.SIGINT, halt)

def write(kind, **data):
    stream.write(json.dumps({'time_ns': time.time_ns(),
                             'monotonic_ns': time.monotonic_ns(),
                             'kind': kind, **data}, separators=(',', ':'))+'\n')

def attr(element, name):
    if element is None:
        return None, -1
    error, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return (value if not error else None), error

def pid(element):
    if element is None:
        return None
    error, value = AX.AXUIElementGetPid(element, None)
    return None if error else int(value)

workspace = NSWorkspace.sharedWorkspace()
# Read-only API signature used by Hammerspoon extensions/spaces/private.h.
# https://github.com/Hammerspoon/hammerspoon/blob/master/extensions/spaces/private.h
sky = ctypes.CDLL('/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight')
sky.SLSMainConnectionID.argtypes = []
sky.SLSMainConnectionID.restype = ctypes.c_int
sky.SLSGetActiveSpace.argtypes = [ctypes.c_int]
sky.SLSGetActiveSpace.restype = ctypes.c_uint64
connection = sky.SLSMainConnectionID()
center = workspace.notificationCenter()
space_serial = 0
def activated(note):
    app = note.userInfo().get(NSWorkspaceApplicationKey)
    write('activation_notification', pid=app.processIdentifier() if app else None,
          app=app.localizedName() if app else None,
          bundle=app.bundleIdentifier() if app else None)
def space_changed(_):
    global space_serial
    space_serial += 1
    write('space_change_notification', space_serial=space_serial)
observers = [
    center.addObserverForName_object_queue_usingBlock_(
        NSWorkspaceDidActivateApplicationNotification, None, None, activated),
    center.addObserverForName_object_queue_usingBlock_(
        NSWorkspaceActiveSpaceDidChangeNotification, None, None, space_changed),
]
system = AX.AXUIElementCreateSystemWide()
AX.AXUIElementSetMessagingTimeout(system, .05)
last = None
last_heartbeat = 0
start = time.monotonic()
samples = 0
max_gap = 0
previous_sample = start
write('watch_start', duration_seconds=args.seconds, requested_interval_ms=20,
      observer_pid=os.getpid(),
      ax_trusted=bool(AX.AXIsProcessTrusted()),
      collected='app/PID, focus role/PID, visible window IDs/order, Space-change notifications; no text or key events')
try:
    while not stop and time.monotonic()-start < args.seconds:
        t = time.monotonic()
        max_gap = max(max_gap, t-previous_sample)
        previous_sample = t
        focused_app, focus_error = attr(system, 'AXFocusedApplication')
        focused_element, element_error = attr(system, 'AXFocusedUIElement')
        element_role, _ = attr(focused_element, 'AXRole')
        front = workspace.frontmostApplication()
        apps = [a for a in workspace.runningApplications()
                if a.bundleIdentifier() == 'com.apple.ScreenContinuity']
        mirror_pid = apps[0].processIdentifier() if len(apps)==1 else None
        mirror_front, mirror_error = attr(
            AX.AXUIElementCreateApplication(mirror_pid) if mirror_pid else None,
            'AXFrontmost')
        windows = Q.CGWindowListCopyWindowInfo(
            Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
            Q.kCGNullWindowID) or []
        normal = [w for w in windows if w.get('kCGWindowLayer')==0
                  and w.get('kCGWindowAlpha',1)>0]
        top = normal[0] if normal else {}
        mirror_windows = [{'id':w.get('kCGWindowNumber'),'depth':i,
                           'bounds':dict(w.get('kCGWindowBounds',{}))}
                          for i,w in enumerate(normal)
                          if w.get('kCGWindowOwnerPID')==mirror_pid]
        state = {'focused_app_pid':pid(focused_app), 'focus_ax_error':focus_error,
                 'focused_element_pid':pid(focused_element),
                 'focused_element_role':str(element_role) if element_role else None,
                 'element_ax_error':element_error,
                 'workspace_front_pid':front.processIdentifier() if front else None,
                 'workspace_front_app':front.localizedName() if front else None,
                 'top_window_pid':top.get('kCGWindowOwnerPID'),
                 'top_window_id':top.get('kCGWindowNumber'),
                 'top_window_app':top.get('kCGWindowOwnerName'),
                 'mirror_pid':mirror_pid,
                 'mirror_ax_frontmost':bool(mirror_front) if not mirror_error else None,
                 'mirror_ax_error':mirror_error,
                 'mirror_windows':mirror_windows, 'space_serial':space_serial,
                 'active_space_id':int(sky.SLSGetActiveSpace(connection))}
        samples += 1
        if state != last or t-last_heartbeat >= 1:
            cursor = Q.CGEventGetLocation(Q.CGEventCreate(None))
            write('state' if state!=last else 'heartbeat', sample=samples,
                  cursor=[cursor.x,cursor.y], **state)
            last = state
            last_heartbeat = t
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.02))
finally:
    for observer in observers:
        center.removeObserver_(observer)
    write('watch_end', samples=samples, elapsed_seconds=time.monotonic()-start,
          max_sample_gap_ms=round(max_gap*1000,3))
    stream.close()
