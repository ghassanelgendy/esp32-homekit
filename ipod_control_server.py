#!/usr/bin/env python
# -*- coding: utf-8 -*-
import cgi
import ctypes
import os
import sqlite3
import struct
import threading
import time
import urllib
import urlparse
import traceback
import SocketServer
import BaseHTTPServer

HTTP_PORT = 5005
DEFAULT_VOLUME_PCT = 25
DB_PATH = '/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb'

# Load Objective-C and iOS frameworks
objc = ctypes.cdll.LoadLibrary('/usr/lib/libobjc.dylib')
cf = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
mp = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/MediaPlayer.framework/MediaPlayer')
av = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/AVFoundation.framework/AVFoundation')

objc.objc_getClass.restype = ctypes.c_void_p
objc.objc_getClass.argtypes = [ctypes.c_char_p]
objc.sel_registerName.restype = ctypes.c_void_p
objc.sel_registerName.argtypes = [ctypes.c_char_p]
objc.objc_msgSend.restype = ctypes.c_void_p
objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

def msg(obj, sel_name, *args, **kwargs):
    restype = kwargs.get('restype', ctypes.c_void_p)
    argtypes = kwargs.get('argtypes', [])
    func = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes))
    sel = objc.sel_registerName(str(sel_name))
    return func(obj, sel, *args)

def to_ns(py_str):
    return msg(objc.objc_getClass('NSString'), 'stringWithUTF8String:', str(py_str), argtypes=[ctypes.c_char_p])

def to_py(ns_str):
    if not ns_str: return ''
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint]
    cf.CFStringGetCString.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(2048)
    cf.CFStringGetCString(ns_str, buf, 2048, 0x08000100)
    return buf.value

def json_escape(s):
    if s is None:
        return ""
    if isinstance(s, unicode):
        s = s.encode('utf-8', 'ignore')
    elif not isinstance(s, str):
        s = str(s)
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', '').replace('\t', ' ')

def pump_runloop(secs=0.1):
    try:
        runloop = msg(objc.objc_getClass('NSRunLoop'), 'currentRunLoop')
        date = msg(objc.objc_getClass('NSDate'), 'dateWithTimeIntervalSinceNow:', ctypes.c_double(secs), argtypes=[ctypes.c_double])
        msg(runloop, 'runUntilDate:', date, argtypes=[ctypes.c_void_p])
    except Exception:
        pass

player_cls = objc.objc_getClass('MPMusicPlayerController')
player = msg(player_cls, 'iPodMusicPlayer')

query_cls = objc.objc_getClass('MPMediaQuery')
avsc_cls = objc.objc_getClass('AVSystemController')
pred_cls = objc.objc_getClass('MPMediaPropertyPredicate')
num_cls = objc.objc_getClass('NSNumber')

user_paused = False
current_playlist_name = ""

def ensure_queue():
    try:
        msg(player, 'setShuffleMode:', 2, argtypes=[ctypes.c_long])
        msg(player, 'setRepeatMode:', 3, argtypes=[ctypes.c_long])
        pump_runloop(0.1)
        item = msg(player, 'nowPlayingItem')
        if not item:
            squery = msg(query_cls, 'songsQuery')
            msg(player, 'setQueueWithQuery:', squery, argtypes=[ctypes.c_void_p])
            msg(player, 'setShuffleMode:', 2, argtypes=[ctypes.c_long])
            msg(player, 'setRepeatMode:', 3, argtypes=[ctypes.c_long])
            pump_runloop(0.1)
    except Exception, e:
        print '[iPod Server] Error ensuring queue:', e

def get_volume():
    try:
        sc = msg(avsc_cls, 'sharedAVSystemController')
        vol = ctypes.c_float(0.0)
        func = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_void_p))
        sel = objc.sel_registerName('getVolume:forCategory:')
        func(sc, sel, ctypes.byref(vol), to_ns('Audio/Video'))
        return int(round(vol.value * 100))
    except Exception:
        return DEFAULT_VOLUME_PCT

def set_volume(val_pct):
    try:
        sc = msg(avsc_cls, 'sharedAVSystemController')
        val_float = max(0.0, min(1.0, float(val_pct) / 100.0))
        func = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p))
        sel = objc.sel_registerName('setVolumeTo:forCategory:')
        func(sc, sel, ctypes.c_float(val_float), to_ns('Audio/Video'))
        func(sc, sel, ctypes.c_float(val_float), to_ns('MediaPlayback'))
        return True
    except Exception, e:
        print '[iPod Server] Error setting volume:', e
        return False

def route_watchdog():
    """Ensure Soundcore Flare 2 is picked if available"""
    try:
        sc = msg(avsc_cls, 'sharedAVSystemController')
        routes = msg(sc, 'pickableRoutesForCategory:', to_ns('MediaPlayback'), argtypes=[ctypes.c_void_p])
        if not routes: return
        count = msg(routes, 'count', restype=ctypes.c_ulong)
        for i in range(count):
            r = msg(routes, 'objectAtIndex:', i, argtypes=[ctypes.c_ulong])
            rname = to_py(msg(r, 'objectForKey:', to_ns('RouteName'), argtypes=[ctypes.c_void_p]))
            if 'Soundcore' in rname or 'Flare' in rname:
                picked = msg(r, 'objectForKey:', to_ns('RouteCurrentlyPicked'), argtypes=[ctypes.c_void_p])
                is_picked = False
                if picked:
                    val_bool = msg(picked, 'boolValue', restype=ctypes.c_bool)
                    is_picked = bool(val_bool)
                if not is_picked:
                    uid = msg(r, 'objectForKey:', to_ns('RouteUID'), argtypes=[ctypes.c_void_p])
                    if uid:
                        print '[Route Watchdog] Activating route: %s' % rname
                        sel = objc.sel_registerName('pickRouteWithUID:')
                        func = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p))
                        func(sc, sel, uid)
                break
    except Exception, e:
        pass

# ----------------- DB HELPERS -----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.text_factory = unicode
    return conn

def list_playlists():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT container_pid, name FROM container WHERE is_hidden=0 AND smart_is_dynamic=0 ORDER BY name ASC")
    rows = c.fetchall()
    playlists = []
    for pid, name in rows:
        c.execute("SELECT item_pid_data FROM container_items WHERE container_pid=?", (pid,))
        r = c.fetchone()
        cnt = (len(r[0]) / 8) if (r and r[0]) else 0
        playlists.append({
            "pid": str(pid),
            "name": name,
            "count": cnt
        })
    conn.close()
    return playlists

def list_tracks(playlist_name=None):
    conn = get_db()
    c = conn.cursor()
    if playlist_name:
        c.execute("SELECT container_pid FROM container WHERE name=? LIMIT 1", (playlist_name,))
        crow = c.fetchone()
        if not crow:
            conn.close()
            return []
        cpid = crow[0]
        c.execute("SELECT item_pid_data FROM container_items WHERE container_pid=?", (cpid,))
        r = c.fetchone()
        if not r or not r[0]:
            conn.close()
            return []
        pids = list(struct.unpack("<%dq" % (len(r[0]) / 8), r[0]))
        if not pids:
            conn.close()
            return []
        placeholders = ",".join("?" for _ in pids)
        query = ("SELECT ie.item_pid, ie.title, ia.item_artist, ie.duration "
                 "FROM item_extra ie "
                 "LEFT JOIN item i ON ie.item_pid=i.item_pid "
                 "LEFT JOIN item_artist ia ON i.item_artist_pid=ia.item_artist_pid "
                 "WHERE ie.item_pid IN (%s)" % placeholders)
        c.execute(query, pids)
        track_map = {}
        for row in c.fetchall():
            track_map[row[0]] = row
        tracks = []
        for pid in pids:
            if pid in track_map:
                row = track_map[pid]
                tracks.append({
                    "id": str(row[0]),
                    "title": row[1] if row[1] else "Unknown Title",
                    "artist": row[2] if row[2] else "",
                    "duration": row[3] or 0
                })
        conn.close()
        return tracks
    else:
        query = ("SELECT ie.item_pid, ie.title, ia.item_artist, ie.duration "
                 "FROM item_extra ie "
                 "LEFT JOIN item i ON ie.item_pid=i.item_pid "
                 "LEFT JOIN item_artist ia ON i.item_artist_pid=ia.item_artist_pid "
                 "ORDER BY ie.title ASC")
        c.execute(query)
        tracks = []
        for row in c.fetchall():
            tracks.append({
                "id": str(row[0]),
                "title": row[1] if row[1] else "Unknown Title",
                "artist": row[2] if row[2] else "",
                "duration": row[3] or 0
            })
        conn.close()
        return tracks

def play_by_playlist(pl_name):
    global current_playlist_name, user_paused
    try:
        pred = msg(pred_cls, 'predicateWithValue:forProperty:',
                   to_ns(pl_name), to_ns('name'),
                   argtypes=[ctypes.c_void_p, ctypes.c_void_p])
        pl_query = msg(query_cls, 'playlistsQuery')
        msg(pl_query, 'addFilterPredicate:', pred, argtypes=[ctypes.c_void_p])
        items = msg(pl_query, 'items')
        count = msg(items, 'count', restype=ctypes.c_ulong)
        if count == 0:
            return False, "Playlist empty or not found"
        msg(player, 'setQueueWithQuery:', pl_query, argtypes=[ctypes.c_void_p])
        msg(player, 'play')
        pump_runloop(0.1)
        current_playlist_name = pl_name
        user_paused = False
        return True, "Playing playlist %s (%d tracks)" % (pl_name, count)
    except Exception, e:
        return False, str(e)

def play_by_track_id(track_id):
    global user_paused
    try:
        tid_int = int(track_id)
        # Convert to unsigned 64-bit for NSNumber if negative in Python
        tid_u64 = ctypes.c_ulonglong(tid_int).value
        pid_num = msg(num_cls, 'numberWithUnsignedLongLong:', ctypes.c_ulonglong(tid_u64), argtypes=[ctypes.c_ulonglong])
        pred = msg(pred_cls, 'predicateWithValue:forProperty:', pid_num, to_ns('persistentID'), argtypes=[ctypes.c_void_p, ctypes.c_void_p])
        
        squery = msg(query_cls, 'songsQuery')
        msg(squery, 'addFilterPredicate:', pred, argtypes=[ctypes.c_void_p])
        items = msg(squery, 'items')
        count = msg(items, 'count', restype=ctypes.c_ulong)
        if count == 0:
            return False, "Track not found"
        target_item = msg(items, 'objectAtIndex:', 0, argtypes=[ctypes.c_ulong])
        
        # Keep whole songs queue or current query and seek to item
        all_query = msg(query_cls, 'songsQuery')
        msg(player, 'setQueueWithQuery:', all_query, argtypes=[ctypes.c_void_p])
        msg(player, 'setNowPlayingItem:', target_item, argtypes=[ctypes.c_void_p])
        msg(player, 'play')
        pump_runloop(0.1)
        user_paused = False
        return True, "Track started"
    except Exception, e:
        return False, str(e)

def modify_playlist_subproc(action, pl_name, track_id):
    import subprocess
    script = """
import sqlite3, struct, sys, time

action = sys.argv[1]
pl_name = sys.argv[2].decode('utf-8')
tid = int(sys.argv[3])

conn = sqlite3.connect('/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb')
conn.text_factory = unicode
c = conn.cursor()
c.execute("SELECT container_pid FROM container WHERE name=? LIMIT 1", (pl_name,))
crow = c.fetchone()
if not crow:
    print "ERROR: Playlist not found"
    sys.exit(1)
cpid = crow[0]

c.execute("SELECT item_pid_data FROM container_items WHERE container_pid=?", (cpid,))
r = c.fetchone()
pids = []
if r and r[0]:
    pids = list(struct.unpack("<%dq" % (len(r[0]) / 8), r[0]))

if action == 'add':
    if tid not in pids:
        pids.append(tid)
        new_blob = struct.pack("<%dq" % len(pids), *pids)
        c.execute("INSERT OR REPLACE INTO container_items (container_pid, item_pid_data) VALUES (?, ?)", (cpid, sqlite3.Binary(new_blob)))
        c.execute("UPDATE container SET date_modified=? WHERE container_pid=?", (int(time.time()), cpid))
        conn.commit()
    print "OK: Total tracks: %d" % len(pids)
elif action == 'remove':
    if tid in pids:
        pids.remove(tid)
        new_blob = struct.pack("<%dq" % len(pids), *pids) if len(pids) > 0 else ""
        c.execute("INSERT OR REPLACE INTO container_items (container_pid, item_pid_data) VALUES (?, ?)", (cpid, sqlite3.Binary(new_blob)))
        c.execute("UPDATE container SET date_modified=? WHERE container_pid=?", (int(time.time()), cpid))
        conn.commit()
        print "OK: Remaining: %d" % len(pids)
    else:
        print "ERROR: Track not in playlist"
        sys.exit(1)
conn.close()
"""
    try:
        p = subprocess.Popen(
            ["/usr/bin/python", "-c", script, action, pl_name.encode('utf-8'), str(track_id)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        out, err = p.communicate()
        out = out.strip()
        if p.returncode == 0 and out.startswith("OK:"):
            return True, out[4:]
        else:
            return False, out if out else (err.strip() if err else "Unknown error")
    except Exception, e:
        return False, str(e)

def add_track_to_playlist(pl_name, track_id):
    return modify_playlist_subproc('add', pl_name, track_id)

def remove_track_from_playlist(pl_name, track_id):
    return modify_playlist_subproc('remove', pl_name, track_id)

# ----------------- HTTP SERVER -----------------
class ReusableHTTPServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

class iPodHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json_str(self, body, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        pool_cls = objc.objc_getClass('NSAutoreleasePool')
        pool = msg(pool_cls, 'alloc')
        msg(pool, 'init')
        try:
            parsed = urlparse.urlparse(self.path)
            path = parsed.path

            if path == '/upload':
                content_len = int(self.headers.getheader('content-length', 0))
                environ = {
                    'REQUEST_METHOD': 'POST',
                    'CONTENT_TYPE': self.headers.getheader('content-type', ''),
                    'CONTENT_LENGTH': str(content_len)
                }
                form = cgi.FieldStorage(fp=self.rfile, environ=environ)
                if 'file' not in form:
                    self.send_json_str('{"status": "error", "message": "No file parameter"}', 400)
                    return
                file_item = form['file']
                pl_item = form.getvalue('playlist', '')
                
                filename = getattr(file_item, 'filename', '') or 'uploaded_track.mp3'
                filename = os.path.basename(filename)
                file_bytes = file_item.value

                # Save to F00
                fdir = "/var/mobile/Media/iTunes_Control/Music/F00"
                if not os.path.exists(fdir):
                    os.makedirs(fdir)
                import string, random
                rand_code = "".join(random.choice(string.ascii_uppercase) for _ in range(4))
                ext = os.path.splitext(filename)[1].lower() or ".mp3"
                disk_filename = rand_code + ext
                disk_path = os.path.join(fdir, disk_filename)

                f = open(disk_path, "wb")
                try:
                    f.write(file_bytes)
                finally:
                    f.close()
                os.chmod(disk_path, 0666)

                # Update SQLite database via subproc helper
                clean_title = os.path.splitext(filename)[0]
                import subprocess
                code = """
import sqlite3, random, struct, sys, time

item_pid = random.randint(1000000000000000000, 8000000000000000000)
title = sys.argv[1].decode('utf-8')
location = sys.argv[2]
size = int(sys.argv[3])
pl_name = sys.argv[4].decode('utf-8') if len(sys.argv) > 4 else ""
now_ts = int(time.time())

conn = sqlite3.connect('/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb')
conn.text_factory = unicode
c = conn.cursor()

c.execute("SELECT base_location_id FROM base_location WHERE path LIKE '%F00%' LIMIT 1")
brow = c.fetchone()
base_loc_id = brow[0] if brow else 0

c.execute("INSERT INTO item (item_pid, media_type, base_location_id, location_kind_id) VALUES (?, ?, ?, ?)",
          (item_pid, 8, base_loc_id, 7586312160907434959))

c.execute("INSERT INTO item_extra (item_pid, title, location, date_created, date_modified, file_size) VALUES (?, ?, ?, ?, ?, ?)",
          (item_pid, title, location, now_ts, now_ts, size))

if pl_name:
    c.execute("SELECT container_pid FROM container WHERE name=? LIMIT 1", (pl_name,))
    crow = c.fetchone()
    if crow:
        cpid = crow[0]
        c.execute("SELECT item_pid_data FROM container_items WHERE container_pid=?", (cpid,))
        r = c.fetchone()
        pids = []
        if r and r[0]:
            pids = list(struct.unpack("<%dq" % (len(r[0]) / 8), r[0]))
        pids.append(item_pid)
        new_blob = struct.pack("<%dq" % len(pids), *pids)
        c.execute("INSERT OR REPLACE INTO container_items (container_pid, item_pid_data) VALUES (?, ?)", (cpid, sqlite3.Binary(new_blob)))
        c.execute("UPDATE container SET date_modified=? WHERE container_pid=?", (now_ts, cpid))

conn.commit()
conn.close()
print "SUCCESS"
"""
                title_bytes = clean_title.encode('utf-8') if isinstance(clean_title, unicode) else clean_title
                args = ["/usr/bin/python", "-c", code, title_bytes, disk_filename, str(len(file_bytes))]
                if pl_item:
                    args.append(pl_item)
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = p.communicate()

                self.send_json_str('{"status": "ok", "message": "Uploaded %s successfully"}' % json_escape(clean_title))
            else:
                self.send_json_str('{"error": "not found"}', 404)
        except Exception, e:
            print '[iPod Server] do_POST Exception:\n', traceback.format_exc()
            self.send_json_str('{"error": "%s"}' % json_escape(str(e)), 500)
        finally:
            msg(pool, 'release')

    def do_GET(self):
        global user_paused, current_playlist_name
        pool_cls = objc.objc_getClass('NSAutoreleasePool')
        pool = msg(pool_cls, 'alloc')
        msg(pool, 'init')

        try:
            parsed = urlparse.urlparse(self.path)
            path = parsed.path
            query = cgi.parse_qs(parsed.query) if parsed.query else {}

            if path == '/play':
                user_paused = False
                ensure_queue()
                msg(player, 'play')
                pump_runloop(0.1)
                self.send_json_str('{"status": "ok", "action": "play"}')

            elif path == '/pause':
                user_paused = True
                msg(player, 'pause')
                pump_runloop(0.1)
                self.send_json_str('{"status": "ok", "action": "pause"}')

            elif path == '/toggle':
                pump_runloop(0.05)
                state = msg(player, 'playbackState', restype=ctypes.c_long)
                if state == 1:
                    user_paused = True
                    msg(player, 'pause')
                    pump_runloop(0.1)
                    self.send_json_str('{"status": "ok", "action": "paused"}')
                else:
                    user_paused = False
                    ensure_queue()
                    msg(player, 'play')
                    pump_runloop(0.1)
                    self.send_json_str('{"status": "ok", "action": "playing"}')

            elif path == '/next':
                user_paused = False
                msg(player, 'skipToNextItem')
                pump_runloop(0.1)
                self.send_json_str('{"status": "ok", "action": "next"}')

            elif path == '/prev':
                user_paused = False
                msg(player, 'skipToPreviousItem')
                pump_runloop(0.1)
                self.send_json_str('{"status": "ok", "action": "prev"}')

            elif path == '/volume':
                lvl = query.get('level', [''])[0]
                if lvl:
                    set_volume(lvl)
                self.send_json_str('{"status": "ok", "volume": %d}' % get_volume())

            elif path == '/playlists':
                pls = list_playlists()
                items_str = []
                for p in pls:
                    items_str.append('{"id": "%s", "name": "%s", "count": %d}' % (
                        p['pid'], json_escape(p['name']), p['count']
                    ))
                body = '{"status": "ok", "playlists": [%s]}' % (",".join(items_str))
                self.send_json_str(body)

            elif path == '/tracks':
                pl_name = query.get('playlist', [''])[0]
                if pl_name:
                    try:
                        pl_name = urllib.unquote(pl_name).decode('utf-8')
                    except Exception:
                        pl_name = urllib.unquote(pl_name)
                tracks = list_tracks(pl_name if pl_name else None)
                items_str = []
                for t in tracks:
                    items_str.append('{"id": "%s", "title": "%s", "artist": "%s", "duration": %d}' % (
                        t['id'], json_escape(t['title']), json_escape(t['artist']), t['duration']
                    ))
                body = '{"status": "ok", "tracks": [%s]}' % (",".join(items_str))
                self.send_json_str(body)

            elif path == '/play_playlist':
                pl_name = query.get('name', [''])[0]
                if pl_name:
                    try:
                        pl_name = urllib.unquote(pl_name).decode('utf-8')
                    except Exception:
                        pl_name = urllib.unquote(pl_name)
                ok, message = play_by_playlist(pl_name)
                self.send_json_str('{"status": "%s", "message": "%s"}' % (
                    "ok" if ok else "error", json_escape(message)
                ))

            elif path == '/play_track':
                track_id = query.get('id', [''])[0]
                ok, message = play_by_track_id(track_id)
                self.send_json_str('{"status": "%s", "message": "%s"}' % (
                    "ok" if ok else "error", json_escape(message)
                ))

            elif path == '/add_to_playlist':
                pl_name = query.get('playlist', [''])[0]
                track_id = query.get('track_id', [''])[0]
                if pl_name:
                    try:
                        pl_name = urllib.unquote(pl_name).decode('utf-8')
                    except Exception:
                        pl_name = urllib.unquote(pl_name)
                ok, message = add_track_to_playlist(pl_name, track_id)
                self.send_json_str('{"status": "%s", "message": "%s"}' % (
                    "ok" if ok else "error", json_escape(message)
                ))

            elif path == '/remove_from_playlist':
                pl_name = query.get('playlist', [''])[0]
                track_id = query.get('track_id', [''])[0]
                if pl_name:
                    try:
                        pl_name = urllib.unquote(pl_name).decode('utf-8')
                    except Exception:
                        pl_name = urllib.unquote(pl_name)
                ok, message = remove_track_from_playlist(pl_name, track_id)
                self.send_json_str('{"status": "%s", "message": "%s"}' % (
                    "ok" if ok else "error", json_escape(message)
                ))

            elif path == '/status' or path == '/':
                pump_runloop(0.05)
                state_code = msg(player, 'playbackState', restype=ctypes.c_long)
                state_str = 'playing' if state_code == 1 else ('paused' if state_code == 2 else 'stopped')
                
                title = ''
                artist = ''
                album = ''
                cur_pid = 0
                item = msg(player, 'nowPlayingItem')
                if item:
                    title = to_py(msg(item, 'valueForProperty:', to_ns('title'), argtypes=[ctypes.c_void_p]))
                    artist = to_py(msg(item, 'valueForProperty:', to_ns('artist'), argtypes=[ctypes.c_void_p]))
                    album = to_py(msg(item, 'valueForProperty:', to_ns('albumTitle'), argtypes=[ctypes.c_void_p]))
                    pid_num = msg(item, 'valueForProperty:', to_ns('persistentID'), argtypes=[ctypes.c_void_p])
                    if pid_num:
                        u64 = msg(pid_num, 'unsignedLongLongValue', restype=ctypes.c_ulonglong)
                        cur_pid = ctypes.c_longlong(u64).value

                body = '{"state": "%s", "title": "%s", "artist": "%s", "album": "%s", "volume": %d, "current_track_id": "%s", "playlist": "%s"}' % (
                    state_str, json_escape(title), json_escape(artist), json_escape(album),
                    get_volume(), str(cur_pid), json_escape(current_playlist_name)
                )
                self.send_json_str(body)
            else:
                self.send_json_str('{"error": "not found"}', 404)
        except Exception, e:
            print '[iPod Server] do_GET Exception:\n', traceback.format_exc()
            self.send_json_str('{"error": "%s"}' % json_escape(str(e)), 500)
        finally:
            msg(pool, 'release')

def run_server():
    print '[iPod Server] Starting iPod Music REST API Server on port %d...' % HTTP_PORT
    ensure_queue()
    set_volume(DEFAULT_VOLUME_PCT)
    msg(player, 'play')
    pump_runloop(0.2)

    server = ReusableHTTPServer(('0.0.0.0', HTTP_PORT), iPodHandler)
    server.timeout = 5.0

    while True:
        server.handle_request()
        
        # Periodic watchdog
        pool_cls = objc.objc_getClass('NSAutoreleasePool')
        pool = msg(pool_cls, 'alloc')
        msg(pool, 'init')
        try:
            route_watchdog()
            pump_runloop(0.1)
            if not user_paused:
                state_code = msg(player, 'playbackState', restype=ctypes.c_long)
                if state_code != 1:
                    ensure_queue()
                    msg(player, 'play')
                    pump_runloop(0.1)
        except Exception:
            pass
        msg(pool, 'release')

if __name__ == '__main__':
    run_server()
