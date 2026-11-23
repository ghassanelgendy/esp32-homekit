#!/usr/bin/env python
# -*- coding: utf-8 -*-
import cgi
import ctypes
import os
import random
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
AUDIO_DIR = '/var/mobile/Media/TelegramAudio'
TRACKS_TSV = '/var/mobile/Media/TelegramAudio/tracks.tsv'

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

def to_utf8(s):
    if s is None:
        return ''
    if isinstance(s, unicode):
        return s.encode('utf-8', 'ignore')
    return str(s)

def to_unicode(s):
    if s is None:
        return u''
    if isinstance(s, unicode):
        return s
    try:
        return str(s).decode('utf-8', 'ignore')
    except Exception:
        return unicode(str(s), 'latin1', 'ignore')

def to_ns(py_str):
    return msg(objc.objc_getClass('NSString'), 'stringWithUTF8String:', to_utf8(py_str), argtypes=[ctypes.c_char_p])

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

player_lock = threading.RLock()
user_paused = False
current_playlist_name = ""
shuffle_enabled = True
repeat_mode = 'all'  # 'all', 'one', 'off'

def init_audio_session():
    try:
        sess = msg(objc.objc_getClass('AVAudioSession'), 'sharedInstance')
        if sess:
            err = ctypes.c_void_p(0)
            msg(sess, 'setCategory:error:', to_ns('AVAudioSessionCategoryPlayback'), ctypes.byref(err),
                argtypes=[ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], restype=ctypes.c_byte)
            msg(sess, 'setActive:error:', ctypes.c_byte(1), ctypes.byref(err),
                argtypes=[ctypes.c_byte, ctypes.POINTER(ctypes.c_void_p)], restype=ctypes.c_byte)
    except Exception, e:
        print '[iPod Server] AudioSession init warning:', e

def apply_modes_to_ipod():
    try:
        sh_code = 2 if shuffle_enabled else 1
        rp_code = 3 if repeat_mode == 'all' else (2 if repeat_mode == 'one' else 1)
        msg(player, 'setShuffleMode:', sh_code, argtypes=[ctypes.c_long])
        msg(player, 'setRepeatMode:', rp_code, argtypes=[ctypes.c_long])
    except Exception, e:
        print '[iPod Server] Error applying modes to iPod:', e

def ensure_queue():
    player_lock.acquire()
    try:
        apply_modes_to_ipod()
        pump_runloop(0.05)
        item = msg(player, 'nowPlayingItem')
        if not item:
            squery = msg(query_cls, 'songsQuery')
            msg(player, 'setQueueWithQuery:', squery, argtypes=[ctypes.c_void_p])
            apply_modes_to_ipod()
            pump_runloop(0.05)
    except Exception, e:
        print '[iPod Server] Error ensuring queue:', e
    finally:
        player_lock.release()

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
                    val_bool = msg(picked, 'boolValue', restype=ctypes.c_byte)
                    is_picked = bool(val_bool)
                if not is_picked:
                    uid = msg(r, 'objectForKey:', to_ns('RouteUID'), argtypes=[ctypes.c_void_p])
                    if uid:
                        print '[Route Watchdog] Activating route: %s' % rname
                        sel = objc.sel_registerName('pickRouteWithUID:')
                        func = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p))
                        func(sc, sel, uid)
                break
    except Exception:
        pass

# ----------------- TELEGRAM & DB HELPERS -----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.text_factory = unicode
    return conn

def load_telegram_meta():
    meta = {}
    if os.path.exists(TRACKS_TSV):
        try:
            f = open(TRACKS_TSV, 'r')
            try:
                for line in f:
                    line = line.rstrip('\r\n')
                    if not line:
                        continue
                    parts = line.split('\t')
                    fname = parts[0]
                    title = parts[1] if len(parts) > 1 and parts[1] else fname
                    artist = parts[2] if len(parts) > 2 and parts[2] else 'Telegram'
                    dur = 0.0
                    if len(parts) > 3 and parts[3]:
                        try:
                            dur = float(parts[3])
                        except Exception:
                            dur = 0.0
                    meta[fname] = (title, artist, dur)
            finally:
                f.close()
        except Exception, e:
            print '[iPod Server] load_telegram_meta error:', e
    return meta

def save_telegram_meta_entry(fname, title, artist='Telegram', dur=0):
    if not title:
        return
    meta = load_telegram_meta()
    meta[fname] = (title, artist or 'Telegram', float(dur or 0))
    try:
        if not os.path.exists(AUDIO_DIR):
            os.makedirs(AUDIO_DIR)
        f = open(TRACKS_TSV, 'w')
        try:
            for k in sorted(meta.keys()):
                t, a, d = meta[k]
                if isinstance(t, unicode): t = t.encode('utf-8', 'ignore')
                if isinstance(a, unicode): a = a.encode('utf-8', 'ignore')
                f.write('%s\t%s\t%s\t%d\n' % (k, t.replace('\t', ' ').replace('\n', ' '), a.replace('\t', ' ').replace('\n', ' '), int(d)))
        finally:
            f.close()
    except Exception, e:
        print '[iPod Server] save_telegram_meta_entry error:', e

def _derive_tg_title_artist(fname):
    stem = os.path.splitext(fname)[0]
    if stem.startswith('tg_'):
        rest = stem[3:]
        idx = rest.rfind('_')
        if idx > 0:
            ch = rest[:idx]
            mid = rest[idx+1:]
            return ('%s %s' % (ch, mid), 'Telegram (%s)' % ch)
    return (stem, 'Telegram')

def telegram_audio_items():
    items = []
    if not os.path.exists(AUDIO_DIR):
        return items
    meta = load_telegram_meta()
    try:
        all_files = [f for f in os.listdir(AUDIO_DIR) if f.lower().endswith(('.mp3', '.m4a'))]
    except Exception:
        return items
    # If prefixed files exist, skip legacy unprefixed duplicate tg<digits>.mp3 files
    prefixed_ids = set()
    for f in all_files:
        if f.startswith('tg_') and '_' in f[3:]:
            prefixed_ids.add(f.rsplit('_', 1)[-1])
    filtered = []
    for f in sorted(all_files, reverse=True):
        if f.startswith('tg') and not f.startswith('tg_') and f[2:] in prefixed_ids:
            continue
        filtered.append(f)
    for fname in filtered:
        if fname in meta:
            title, artist, dur = meta[fname]
        else:
            title, artist = _derive_tg_title_artist(fname)
            dur = 0.0
        items.append({
            'id': 'tg:' + fname,
            'title': title,
            'artist': artist,
            'loc': fname,
            'dur': dur
        })
    return items

def local_library_items():
    """Songs already in the phone's Music library as dicts with title, loc, dur, artist."""
    items = []
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT ie.item_pid, ie.title, bl.path, ie.location, ia.item_artist, ie.duration "
                  "FROM item_extra ie "
                  "JOIN item i ON ie.item_pid=i.item_pid "
                  "LEFT JOIN base_location bl ON i.base_location_id=bl.base_location_id "
                  "LEFT JOIN item_artist ia ON i.item_artist_pid=ia.item_artist_pid "
                  "WHERE ie.location IS NOT NULL AND ie.location != ''")
        for pid, title, base, loc, artist, dur in c.fetchall():
            full = '/var/mobile/Media/' + (base or '') + '/' + loc
            if os.path.exists(full):
                t = title or 'Unknown Title'
                if isinstance(t, unicode):
                    t = t.encode('utf-8', 'ignore')
                a = artist or 'Local Library'
                if isinstance(a, unicode):
                    a = a.encode('utf-8', 'ignore')
                items.append({
                    'id': str(pid),
                    'title': t,
                    'artist': a,
                    'loc': full,
                    'dur': float(dur or 0) / 1000.0 if (dur and dur > 10000) else float(dur or 0)
                })
        conn.close()
    except Exception, e:
        print '[iPod Server] local_library_items error:', e
    return items

def db_playlist_file_items(playlist_name):
    """Return file-backed items for an iTunes DB playlist in order."""
    items = []
    try:
        pl_u = to_unicode(playlist_name)
        pl_utf8 = to_utf8(playlist_name)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT container_pid FROM container WHERE name=? LIMIT 1", (pl_u,))
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
        query = ("SELECT ie.item_pid, ie.title, bl.path, ie.location, ia.item_artist "
                 "FROM item_extra ie "
                 "JOIN item i ON ie.item_pid=i.item_pid "
                 "LEFT JOIN base_location bl ON i.base_location_id=bl.base_location_id "
                 "LEFT JOIN item_artist ia ON i.item_artist_pid=ia.item_artist_pid "
                 "WHERE ie.item_pid IN (%s)" % placeholders)
        c.execute(query, pids)
        tmap = {}
        for row in c.fetchall():
            tmap[row[0]] = row
        for pid in pids:
            if pid in tmap:
                _, title, base, loc, artist = tmap[pid]
                if loc:
                    full = '/var/mobile/Media/' + (base or '') + '/' + loc
                    if os.path.exists(full):
                        t = to_utf8(title or 'Unknown Title')
                        a = to_utf8(artist or pl_utf8)
                        items.append({
                            'id': str(pid),
                            'title': t,
                            'artist': a,
                            'loc': full,
                            'dur': 0.0
                        })
        conn.close()
    except Exception, e:
        print '[iPod Server] db_playlist_file_items error:', e
    return items

def list_playlists():
    tg = telegram_audio_items()
    playlists = []
    if tg:
        playlists.append({
            "pid": "tg_all",
            "name": "Telegram",
            "count": len(tg)
        })
        playlists.append({
            "pid": "tg_mix",
            "name": "Telegram + Local",
            "count": len(tg) + 40
        })
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT container_pid, name FROM container WHERE is_hidden=0 AND smart_is_dynamic=0 ORDER BY name ASC")
    rows = c.fetchall()
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
    pl_utf8 = to_utf8(playlist_name) if playlist_name is not None else None
    if pl_utf8 == "Telegram":
        return [{
            "id": x['id'],
            "title": x['title'],
            "artist": x['artist'],
            "duration": int(x['dur'])
        } for x in telegram_audio_items()]
    if pl_utf8 == "Telegram + Local":
        return list_tracks("Telegram") + list_tracks("__local_only__")

    conn = get_db()
    c = conn.cursor()
    if pl_utf8 and pl_utf8 != "__local_only__":
        c.execute("SELECT container_pid FROM container WHERE name=? LIMIT 1", (to_unicode(playlist_name),))
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
        if playlist_name is None:
            for x in telegram_audio_items():
                tracks.append({
                    "id": x['id'],
                    "title": x['title'],
                    "artist": x['artist'],
                    "duration": int(x['dur'])
                })
        for row in c.fetchall():
            tracks.append({
                "id": str(row[0]),
                "title": row[1] if row[1] else "Unknown Title",
                "artist": row[2] if row[2] else "",
                "duration": row[3] or 0
            })
        conn.close()
        return tracks

def get_playlist_named_tracks(playlist_name=None):
    raw = list_tracks(playlist_name if playlist_name else None)
    title_counts = {}
    for t in raw:
        t_str = t.get('title') or 'Unknown Title'
        if isinstance(t_str, unicode):
            t_str = t_str.encode('utf-8', 'ignore')
        t_str = t_str.strip() or 'Unknown Title'
        title_counts[t_str] = title_counts.get(t_str, 0) + 1

    used_labels = {}
    named = []
    for t in raw:
        t_str = t.get('title') or 'Unknown Title'
        if isinstance(t_str, unicode):
            t_str = t_str.encode('utf-8', 'ignore')
        t_str = t_str.strip() or 'Unknown Title'
        a_str = t.get('artist') or ''
        if isinstance(a_str, unicode):
            a_str = a_str.encode('utf-8', 'ignore')
        a_str = a_str.strip()

        if title_counts.get(t_str, 0) > 1 and a_str:
            base_label = '%s — %s' % (t_str, a_str)
        else:
            base_label = t_str

        if base_label in used_labels:
            used_labels[base_label] += 1
            label = '%s (%d)' % (base_label, used_labels[base_label])
        else:
            used_labels[base_label] = 1
            label = base_label

        named.append({
            'label': label,
            'id': str(t.get('id', '')),
            'title': t_str,
            'artist': a_str,
            'duration': int(t.get('duration', 0))
        })
    return named

_cached_playlist_tracks_map_json = None

def invalidate_tracks_cache():
    global _cached_playlist_tracks_map_json
    _cached_playlist_tracks_map_json = None

def get_playlist_tracks_map_json():
    global _cached_playlist_tracks_map_json
    if _cached_playlist_tracks_map_json is not None:
        return _cached_playlist_tracks_map_json
    pls = list_playlists()
    pl_entries = []
    for p in pls:
        pname = p['name']
        if isinstance(pname, unicode):
            pname_u = pname
        else:
            try:
                pname_u = pname.decode('utf-8')
            except Exception:
                pname_u = pname
        if p['count'] <= 0 and pname_u not in (u'Telegram', u'Telegram + Local', u'1', u'AA - Quran', u'AB - Quran', u'قرآن'):
            continue
        named = get_playlist_named_tracks(pname_u)
        labels_json = ','.join('"%s"' % json_escape(x['label']) for x in named)
        pl_entries.append('"%s": [%s]' % (json_escape(pname_u), labels_json))
    _cached_playlist_tracks_map_json = '{"status": "ok", "playlists": {%s}}' % (','.join(pl_entries))
    return _cached_playlist_tracks_map_json

# ---------------------------------------------------------------------------
# Telegram/file stream queue: plays MP3s from AUDIO_DIR (or local files/URLs)
# with AVAudioPlayer (or AVPlayer for remote http URLs), fully thread-safe
# and synced with Home Assistant's /play /pause /toggle /next /prev /status
# /shuffle /repeat endpoints.
# ---------------------------------------------------------------------------
av_player = None
av_is_audio_player = True
url_mode = False
url_base_queue = []     # unshuffled list of dicts: title, artist, loc, dur
url_queue = []          # active list of dicts: title, artist, loc, dur
url_index = 0
url_paused = False
url_elapsed = 0.0
url_last_tick = 0.0
url_started_at = 0.0
url_last_pos = 0.0
url_playlist_name = "Telegram"

def _loc_to_url(loc):
    if loc.startswith('http://') or loc.startswith('https://') or loc.startswith('file://'):
        return loc
    if loc.startswith('/'):
        return 'file://' + urllib.quote(loc)
    return 'file://' + AUDIO_DIR + '/' + urllib.quote(loc)

def _loc_to_filepath(loc):
    if loc.startswith('http://') or loc.startswith('https://'):
        return None
    if loc.startswith('file://'):
        return urllib.unquote(loc[7:])
    if loc.startswith('/'):
        return loc
    return os.path.join(AUDIO_DIR, loc)

def _av_stop_current():
    global av_player, av_is_audio_player
    if av_player:
        try:
            if av_is_audio_player:
                msg(av_player, 'stop')
            else:
                msg(av_player, 'pause')
                msg(av_player, 'replaceCurrentItemWithPlayerItem:', 0, argtypes=[ctypes.c_void_p])
            msg(av_player, 'release')
        except Exception:
            pass
        av_player = None

def _av_is_actually_playing():
    if not url_mode or not av_player:
        return False
    try:
        if av_is_audio_player:
            return bool(msg(av_player, 'isPlaying', restype=ctypes.c_byte))
        else:
            rate = msg(av_player, 'rate', restype=ctypes.c_float)
            return rate > 0.01
    except Exception:
        return not url_paused

def _av_start(index):
    global av_player, av_is_audio_player, url_index, url_paused, url_elapsed, url_last_tick, url_started_at, url_last_pos, user_paused
    player_lock.acquire()
    try:
        if not url_queue:
            return False
        url_index = index % len(url_queue)
        item = url_queue[url_index]
        _av_stop_current()
        user_paused = True
        try:
            msg(player, 'pause')
        except Exception:
            pass
        init_audio_session()

        fpath = _loc_to_filepath(item['loc'])
        if fpath and os.path.exists(fpath):
            nsurl = msg(objc.objc_getClass('NSURL'), 'fileURLWithPath:', to_ns(fpath), argtypes=[ctypes.c_void_p])
            if nsurl:
                err = ctypes.c_void_p(0)
                ap = msg(msg(objc.objc_getClass('AVAudioPlayer'), 'alloc'),
                         'initWithContentsOfURL:error:', nsurl, ctypes.byref(err),
                         argtypes=[ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
                if ap:
                    av_player = ap
                    av_is_audio_player = True
                    msg(av_player, 'prepareToPlay')
                    msg(av_player, 'play')
                    try:
                        dur = float(msg(av_player, 'duration', restype=ctypes.c_double))
                        if dur > 0:
                            item['dur'] = dur
                    except Exception:
                        pass

        if not av_player:
            nsurl = msg(objc.objc_getClass('NSURL'), 'URLWithString:', to_ns(_loc_to_url(item['loc'])), argtypes=[ctypes.c_void_p])
            if not nsurl:
                return False
            av_player = msg(msg(objc.objc_getClass('AVPlayer'), 'alloc'), 'initWithURL:', nsurl, argtypes=[ctypes.c_void_p])
            av_is_audio_player = False
            msg(av_player, 'play')

        url_paused = False
        url_elapsed = 0.0
        url_last_pos = 0.0
        now = time.time()
        url_started_at = now
        url_last_tick = now
        pump_runloop(0.15)
        return True
    finally:
        player_lock.release()

def _normalize_queue_item(i, default_artist='Telegram'):
    if isinstance(i, dict):
        title = i.get('title') or 'Unknown Title'
        loc = i.get('loc') or ''
        dur = float(i.get('dur') or 0.0)
        artist = i.get('artist') or default_artist
        tid = i.get('id') or ('tg:' + os.path.basename(loc))
    else:
        loc = i[1] if len(i) > 1 else ''
        title = i[0] if len(i) > 0 and i[0] else loc
        dur = float(i[2]) if len(i) > 2 and i[2] else 0.0
        artist = i[3] if len(i) > 3 and i[3] else default_artist
        tid = 'tg:' + os.path.basename(loc)
    # Enrich from tracks.tsv if title looks like a generic ID (e.g. "Neinaa 648")
    bname = os.path.basename(loc)
    meta = load_telegram_meta()
    if bname in meta:
        m_title, m_artist, m_dur = meta[bname]
        if m_title and (title == bname or title.startswith('Neinaa ') or title.startswith('Qu_Arabia ') or title.startswith('Faeel_khair_41 ')):
            title = m_title
        if m_artist and artist == 'Telegram':
            artist = m_artist
        if not dur and m_dur:
            dur = m_dur
    if isinstance(title, unicode):
        title = title.encode('utf-8', 'ignore')
    if isinstance(artist, unicode):
        artist = artist.encode('utf-8', 'ignore')
    return {'id': tid, 'title': title, 'artist': artist, 'loc': loc, 'dur': dur}

def play_queue(items, start=0, mix_local=False, shuffle=None, local_max=0, playlist_name='Telegram', keep_start=False):
    """items: list of [title, location, duration_seconds] or dicts. mix_local adds the Music
    library songs; shuffle randomises the final order."""
    global url_mode, url_base_queue, url_queue, user_paused, shuffle_enabled, url_playlist_name, current_playlist_name
    player_lock.acquire()
    try:
        raw_items = [_normalize_queue_item(x) for x in items]
        if not raw_items and not mix_local:
            raw_items = [_normalize_queue_item(x) for x in telegram_audio_items()]
        if mix_local:
            loc = local_library_items()
            if local_max and len(loc) > local_max:
                loc = random.sample(loc, local_max)
            raw_items += [_normalize_queue_item(x, default_artist='Local Library') for x in loc]
        if not raw_items:
            return False, "Empty queue"

        url_base_queue = list(raw_items)
        if shuffle is not None:
            shuffle_enabled = bool(shuffle)
            apply_modes_to_ipod()

        url_queue = list(url_base_queue)
        if shuffle_enabled and len(url_queue) > 1:
            if (keep_start or start > 0) and 0 <= start < len(url_queue):
                first = url_queue[start]
                rest = url_queue[:start] + url_queue[start+1:]
                random.shuffle(rest)
                url_queue = [first] + rest
                start = 0
            else:
                random.shuffle(url_queue)
                start = 0

        url_playlist_name = playlist_name or 'Telegram'
        current_playlist_name = url_playlist_name
        user_paused = True
        msg(player, 'pause')
        url_mode = True
        _av_start(start)
        return True, "Playing %d tracks" % len(url_queue)
    except Exception, e:
        return False, str(e)
    finally:
        player_lock.release()

def url_control(path):
    global url_paused, url_last_tick, url_started_at
    player_lock.acquire()
    try:
        playing = _av_is_actually_playing()
        if path == '/pause' or (path == '/toggle' and playing):
            if av_player:
                msg(av_player, 'pause')
            url_paused = True
            pump_runloop(0.1)
            return 'paused'
        if path == '/play' or path == '/toggle':
            if not av_player and url_queue:
                _av_start(url_index)
            elif av_player:
                init_audio_session()
                msg(av_player, 'play')
                url_paused = False
                now = time.time()
                url_last_tick = now
                url_started_at = now
                pump_runloop(0.1)
                if not _av_is_actually_playing():
                    _av_start(url_index)
            return 'playing'
        if path == '/next':
            _av_start(url_index + 1)
            return 'next'
        if path == '/prev':
            _av_start(url_index - 1)
            return 'prev'
        return 'noop'
    finally:
        player_lock.release()

def url_tick():
    """Called from the main loop and before /status: keep url_paused synced with actual
    AVAudioPlayer state and advance when the current track has finished."""
    global url_elapsed, url_last_tick, url_last_pos, url_paused
    player_lock.acquire()
    try:
        if not url_mode or not av_player or not url_queue:
            url_last_tick = time.time()
            return
        now = time.time()
        playing = _av_is_actually_playing()
        if playing:
            if url_paused:
                url_paused = False
            try:
                if msg(player, 'playbackState', restype=ctypes.c_long) == 1:
                    msg(player, 'pause')
            except Exception:
                pass
            url_elapsed += max(0.0, now - url_last_tick)
            url_last_tick = now
            if av_is_audio_player:
                try:
                    pos = float(msg(av_player, 'currentTime', restype=ctypes.c_double))
                    if pos > 0:
                        url_last_pos = pos
                except Exception:
                    pass
            return

        # Not playing right now
        url_last_tick = now
        if url_paused:
            try:
                if msg(player, 'playbackState', restype=ctypes.c_long) == 1:
                    stop_queue()
            except Exception:
                pass
            return

        # Grace period immediately after _av_start
        if now - url_started_at < 1.5:
            return

        finished = False
        dur = url_queue[url_index].get('dur', 0.0)
        if av_is_audio_player:
            try:
                pos = float(msg(av_player, 'currentTime', restype=ctypes.c_double))
            except Exception:
                pos = 0.0
            if pos < 0.5 and (url_last_pos > 0.5 or (now - url_started_at) > 2.0):
                finished = True
            elif dur > 0 and url_last_pos >= dur - 1.5:
                finished = True
            else:
                # Paused externally mid-track (e.g. Bluetooth speaker powered off)
                url_paused = True
                return
        else:
            if dur > 0 and url_elapsed >= dur - 1.0:
                finished = True
            elif url_elapsed > 3.0:
                finished = True

        if finished:
            if repeat_mode == 'one':
                _av_start(url_index)
            elif repeat_mode == 'all':
                _av_start((url_index + 1) % len(url_queue))
            else:
                if url_index + 1 < len(url_queue):
                    _av_start(url_index + 1)
                else:
                    url_paused = True
    finally:
        player_lock.release()

def stop_queue():
    global url_mode, user_paused
    player_lock.acquire()
    try:
        _av_stop_current()
        url_mode = False
        user_paused = False
    finally:
        player_lock.release()

def set_shuffle_mode(mode_str):
    global shuffle_enabled, url_queue, url_index
    player_lock.acquire()
    try:
        m = str(mode_str or '').lower().strip()
        if m in ('on', 'true', '1'):
            shuffle_enabled = True
        elif m in ('off', 'false', '0'):
            shuffle_enabled = False
        else:
            shuffle_enabled = not shuffle_enabled

        apply_modes_to_ipod()

        if url_mode and url_queue:
            cur = url_queue[url_index]
            base = url_base_queue if url_base_queue else list(url_queue)
            if shuffle_enabled:
                rest = [x for x in base if x.get('loc') != cur.get('loc')]
                random.shuffle(rest)
                url_queue = [cur] + rest
                url_index = 0
            else:
                url_queue = list(base)
                for idx, x in enumerate(url_queue):
                    if x.get('loc') == cur.get('loc'):
                        url_index = idx
                        break
        return shuffle_enabled
    finally:
        player_lock.release()

def set_repeat_mode(mode_str):
    global repeat_mode
    player_lock.acquire()
    try:
        m = str(mode_str or '').lower().strip()
        if m in ('all', 'repeat'):
            repeat_mode = 'all'
        elif m in ('one', 'once', 'repeat_one', 'repeat_once', '1'):
            repeat_mode = 'one'
        elif m in ('off', 'none', 'false', '0'):
            repeat_mode = 'off'
        else:
            if repeat_mode == 'all':
                repeat_mode = 'one'
            elif repeat_mode == 'one':
                repeat_mode = 'off'
            else:
                repeat_mode = 'all'
        apply_modes_to_ipod()
        return repeat_mode
    finally:
        player_lock.release()

def play_by_playlist(pl_name):
    global current_playlist_name, user_paused
    player_lock.acquire()
    try:
        pl_utf8 = to_utf8(pl_name)
        if pl_utf8 == 'Telegram':
            tg_items = telegram_audio_items()
            if not tg_items:
                return False, "No Telegram tracks found"
            return play_queue(tg_items, start=0, mix_local=False, shuffle=shuffle_enabled, playlist_name='Telegram')
        if pl_utf8 == 'Telegram + Local':
            tg_items = telegram_audio_items()
            return play_queue(tg_items, start=0, mix_local=True, shuffle=shuffle_enabled, local_max=40, playlist_name='Telegram + Local')

        db_items = db_playlist_file_items(pl_name)
        if len(db_items) > 0:
            return play_queue(db_items, start=0, mix_local=False, shuffle=shuffle_enabled, playlist_name=pl_utf8)

        stop_queue()
        pred = msg(pred_cls, 'predicateWithValue:forProperty:',
                   to_ns(pl_utf8), to_ns('name'),
                   argtypes=[ctypes.c_void_p, ctypes.c_void_p])
        pl_query = msg(query_cls, 'playlistsQuery')
        msg(pl_query, 'addFilterPredicate:', pred, argtypes=[ctypes.c_void_p])
        items = msg(pl_query, 'items')
        mp_count = msg(items, 'count', restype=ctypes.c_ulong) if items else 0
        if mp_count == 0:
            return False, "Playlist empty or not found"

        msg(player, 'setQueueWithQuery:', pl_query, argtypes=[ctypes.c_void_p])
        apply_modes_to_ipod()
        msg(player, 'play')
        pump_runloop(0.1)
        current_playlist_name = pl_utf8
        user_paused = False
        return True, "Playing playlist %s (%d tracks)" % (pl_utf8, mp_count)
    except Exception, e:
        return False, str(e)
    finally:
        player_lock.release()

def play_by_track_id(track_id, pl_name=None):
    global user_paused, current_playlist_name
    player_lock.acquire()
    try:
        tid_str = to_utf8(track_id).strip()
        pl_utf8 = to_utf8(pl_name).strip() if pl_name else ''
        if not tid_str:
            return False, "No track specified"

        # Resolve human-readable track label / title / search query if not a raw ID
        is_raw_id = tid_str.startswith('tg:')
        if not is_raw_id:
            try:
                int(tid_str)
                is_raw_id = True
            except Exception:
                is_raw_id = False

        if not is_raw_id:
            search_pl = pl_utf8 or current_playlist_name or 'Telegram + Local'
            named = get_playlist_named_tracks(search_pl)
            matched = None
            for entry in named:
                if entry['label'] == tid_str or entry['title'] == tid_str:
                    matched = entry
                    break
            if not matched:
                all_named = get_playlist_named_tracks(None)
                for entry in all_named:
                    if entry['label'] == tid_str or entry['title'] == tid_str:
                        matched = entry
                        break
                # Substring search fallback (case-insensitive)
                if not matched:
                    q_low = tid_str.lower()
                    for entry in named:
                        if q_low in entry['label'].lower() or q_low in entry['title'].lower() or q_low in entry['artist'].lower():
                            matched = entry
                            break
                if not matched:
                    q_low = tid_str.lower()
                    for entry in all_named:
                        if q_low in entry['label'].lower() or q_low in entry['title'].lower() or q_low in entry['artist'].lower():
                            matched = entry
                            break
            if not matched:
                return False, "Track not found: %s" % tid_str
            tid_str = matched['id']

        if tid_str.startswith('tg:'):
            fname = tid_str[3:]
            tg_items = telegram_audio_items()
            start_idx = 0
            for idx, x in enumerate(tg_items):
                if x['loc'] == fname or x['id'] == tid_str:
                    start_idx = idx
                    break
            mix = (pl_utf8 == 'Telegram + Local') or (not pl_utf8 and current_playlist_name == 'Telegram + Local')
            target_pl = 'Telegram + Local' if mix else 'Telegram'
            return play_queue(tg_items, start=start_idx, mix_local=mix, shuffle=shuffle_enabled,
                              local_max=40 if mix else 0, playlist_name=target_pl, keep_start=True)

        tid_int = int(tid_str)
        if pl_utf8 and pl_utf8 not in ('Telegram', 'Telegram + Local'):
            db_items = db_playlist_file_items(pl_utf8)
            for idx, x in enumerate(db_items):
                if x['id'] == str(tid_int):
                    return play_queue(db_items, start=idx, mix_local=False, shuffle=shuffle_enabled,
                                      playlist_name=pl_utf8, keep_start=True)

        # Fallback: find the track in SQLite and queue it with the active playlist or local library
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT ie.title, bl.path, ie.location, ia.item_artist "
                  "FROM item_extra ie "
                  "JOIN item i ON ie.item_pid=i.item_pid "
                  "LEFT JOIN base_location bl ON i.base_location_id=bl.base_location_id "
                  "LEFT JOIN item_artist ia ON i.item_artist_pid=ia.item_artist_pid "
                  "WHERE ie.item_pid=? LIMIT 1", (tid_int,))
        row = c.fetchone()
        conn.close()
        if row and row[2]:
            full = '/var/mobile/Media/' + (row[1] or '') + '/' + row[2]
            if os.path.exists(full):
                t = to_utf8(row[0] or 'Unknown Title')
                a = to_utf8(row[3] or pl_utf8 or 'Local Library')
                target_item = {'id': str(tid_int), 'title': t, 'artist': a, 'loc': full, 'dur': 0.0}
                all_loc = local_library_items()
                rest_loc = [x for x in all_loc if x['id'] != str(tid_int)]
                if len(rest_loc) > 60:
                    rest_loc = random.sample(rest_loc, 60)
                return play_queue([target_item] + rest_loc,
                                  start=0, mix_local=False, shuffle=shuffle_enabled,
                                  playlist_name=pl_utf8 or current_playlist_name or 'Local Library', keep_start=True)
        return False, "Track not found"
    except Exception, e:
        return False, str(e)
    finally:
        player_lock.release()

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
    invalidate_tracks_cache()
    return modify_playlist_subproc('add', pl_name, track_id)

def remove_track_from_playlist(pl_name, track_id):
    invalidate_tracks_cache()
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
                import string
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

                # Also save to TelegramAudio if uploaded to Telegram playlist
                clean_title = os.path.splitext(filename)[0]
                if pl_item == 'Telegram':
                    if not os.path.exists(AUDIO_DIR):
                        os.makedirs(AUDIO_DIR)
                    tg_dest = os.path.join(AUDIO_DIR, 'tg_upload_' + disk_filename)
                    f2 = open(tg_dest, "wb")
                    try:
                        f2.write(file_bytes)
                    finally:
                        f2.close()
                    os.chmod(tg_dest, 0666)
                    save_telegram_meta_entry('tg_upload_' + disk_filename, clean_title, 'Uploaded', 0)

                # Update SQLite database via subproc helper
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
                invalidate_tracks_cache()

                self.send_json_str('{"status": "ok", "message": "Uploaded %s successfully"}' % json_escape(clean_title))
            elif path == '/put':
                qparams = cgi.parse_qs(parsed.query) if parsed.query else {}
                name = os.path.basename(urllib.unquote(qparams.get('name', [''])[0]))
                if not name or not name.lower().endswith(('.mp3', '.m4a', '.tsv', '.txt')):
                    self.send_json_str('{"status": "error", "message": "bad name"}', 400)
                    return
                if not os.path.exists(AUDIO_DIR):
                    os.makedirs(AUDIO_DIR)
                    os.chmod(AUDIO_DIR, 0777)
                remaining = int(self.headers.getheader('content-length', 0))
                dest = os.path.join(AUDIO_DIR, name)
                f = open(dest, 'wb')
                try:
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                finally:
                    f.close()
                os.chmod(dest, 0666)
                title_p = urllib.unquote(qparams.get('title', [''])[0])
                artist_p = urllib.unquote(qparams.get('artist', ['Telegram'])[0])
                dur_p = urllib.unquote(qparams.get('dur', ['0'])[0])
                if title_p and name.lower().endswith(('.mp3', '.m4a')):
                    save_telegram_meta_entry(name, title_p, artist_p, dur_p)
                invalidate_tracks_cache()
                self.send_json_str('{"status": "ok", "message": "saved %s"}' % json_escape(name))
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

            if path == '/shuffle':
                mode_arg = query.get('mode', query.get('state', ['toggle']))[0]
                new_sh = set_shuffle_mode(mode_arg)
                self.send_json_str('{"status": "ok", "shuffle": %s}' % ('true' if new_sh else 'false'))
                return

            elif path == '/repeat':
                mode_arg = query.get('mode', query.get('state', ['cycle']))[0]
                new_rp = set_repeat_mode(mode_arg)
                self.send_json_str('{"status": "ok", "repeat": "%s"}' % new_rp)
                return

            elif url_mode and path in ('/play', '/pause', '/toggle', '/next', '/prev'):
                self.send_json_str('{"status": "ok", "action": "%s"}' % url_control(path))
                return

            elif url_mode and (path == '/status' or path == '/'):
                player_lock.acquire()
                try:
                    url_tick()
                    if not url_mode:
                        # url_tick exited url_mode because iPodMusicPlayer took over
                        pass
                    else:
                        cur = url_queue[url_index] if url_queue else {'title': 'None', 'artist': 'Telegram'}
                        actual_playing = _av_is_actually_playing() or (not url_paused and (time.time() - url_started_at < 1.5))
                        st_str = 'playing' if actual_playing else 'paused'
                        art_str = cur.get('artist') or 'Telegram'
                        pl_str = url_playlist_name or 'Telegram'
                        self.send_json_str('{"state": "%s", "title": "%s", "artist": "%s", "album": "%d/%d", "volume": %d, "current_track_id": "%s", "playlist": "%s", "shuffle": %s, "repeat": "%s"}' % (
                            st_str, json_escape(cur.get('title', '')), json_escape(art_str),
                            url_index + 1, len(url_queue), get_volume(),
                            json_escape(cur.get('id', 'q%d' % url_index)),
                            json_escape(pl_str),
                            'true' if shuffle_enabled else 'false',
                            repeat_mode))
                        return
                finally:
                    player_lock.release()

            if path == '/play':
                player_lock.acquire()
                try:
                    user_paused = False
                    ensure_queue()
                    msg(player, 'play')
                    pump_runloop(0.1)
                finally:
                    player_lock.release()
                self.send_json_str('{"status": "ok", "action": "play"}')

            elif path == '/pause':
                player_lock.acquire()
                try:
                    user_paused = True
                    msg(player, 'pause')
                    pump_runloop(0.1)
                finally:
                    player_lock.release()
                self.send_json_str('{"status": "ok", "action": "pause"}')

            elif path == '/toggle':
                player_lock.acquire()
                try:
                    pump_runloop(0.05)
                    state = msg(player, 'playbackState', restype=ctypes.c_long)
                    if state == 1:
                        user_paused = True
                        msg(player, 'pause')
                        pump_runloop(0.1)
                        act = 'paused'
                    else:
                        user_paused = False
                        ensure_queue()
                        msg(player, 'play')
                        pump_runloop(0.1)
                        act = 'playing'
                finally:
                    player_lock.release()
                self.send_json_str('{"status": "ok", "action": "%s"}' % act)

            elif path == '/next':
                player_lock.acquire()
                try:
                    user_paused = False
                    msg(player, 'skipToNextItem')
                    pump_runloop(0.1)
                finally:
                    player_lock.release()
                self.send_json_str('{"status": "ok", "action": "next"}')

            elif path == '/prev':
                player_lock.acquire()
                try:
                    user_paused = False
                    msg(player, 'skipToPreviousItem')
                    pump_runloop(0.1)
                finally:
                    player_lock.release()
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

            elif path == '/playlist_tracks_map':
                self.send_json_str(get_playlist_tracks_map_json())

            elif path == '/track_names':
                pl_name = query.get('playlist', [''])[0]
                if pl_name:
                    try:
                        pl_name = urllib.unquote(pl_name).decode('utf-8')
                    except Exception:
                        pl_name = urllib.unquote(pl_name)
                named = get_playlist_named_tracks(pl_name if pl_name else None)
                labels_str = ','.join('"%s"' % json_escape(x['label']) for x in named)
                body = '{"status": "ok", "playlist": "%s", "count": %d, "tracks": [%s]}' % (
                    json_escape(pl_name or 'All'), len(named), labels_str
                )
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
                        json_escape(t['id']), json_escape(t['title']), json_escape(t['artist']), t['duration']
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

            elif path == '/play_queue':
                try:
                    items = []
                    for rec in query.get('q', [''])[0].split(';'):
                        if rec:
                            parts = [urllib.unquote(x) for x in rec.split(',')]
                            if len(parts) >= 2 and parts[0] and parts[1]:
                                bname = os.path.basename(parts[1])
                                if bname.lower().endswith(('.mp3', '.m4a')) and not parts[0].startswith('Neinaa '):
                                    save_telegram_meta_entry(bname, parts[0], 'Telegram', parts[2] if len(parts) > 2 else 0)
                            items.append(parts)
                    start_i = int(query.get('start', ['0'])[0])
                    mix = query.get('local', ['0'])[0] == '1'
                    shuf_param = query.get('shuffle', [None])[0]
                    shuf = (shuf_param == '1') if shuf_param is not None else None
                    lmax = int(query.get('localmax', ['0'])[0])
                    pl_label = 'Telegram + Local' if mix else 'Telegram'
                    ok, message = play_queue(items, start_i, mix, shuf, lmax, playlist_name=pl_label)
                except Exception, e:
                    ok, message = False, str(e)
                self.send_json_str('{"status": "%s", "message": "%s"}' % (
                    "ok" if ok else "error", json_escape(message)))

            elif path == '/stop_queue':
                stop_queue()
                self.send_json_str('{"status": "ok", "action": "stop_queue"}')

            elif path == '/play_track':
                track_id = query.get('id', query.get('title', ['']))[0]
                if track_id:
                    try:
                        track_id = urllib.unquote(track_id).decode('utf-8')
                    except Exception:
                        track_id = urllib.unquote(track_id)
                pl_name = query.get('playlist', [''])[0]
                if pl_name:
                    try:
                        pl_name = urllib.unquote(pl_name).decode('utf-8')
                    except Exception:
                        pl_name = urllib.unquote(pl_name)
                ok, message = play_by_track_id(track_id, pl_name if pl_name else None)
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
                player_lock.acquire()
                try:
                    pump_runloop(0.05)
                    state_code = msg(player, 'playbackState', restype=ctypes.c_long)
                    state_str = 'playing' if state_code == 1 else ('paused' if state_code == 2 else 'paused')
                    
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
                finally:
                    player_lock.release()

                body = '{"state": "%s", "title": "%s", "artist": "%s", "album": "%s", "volume": %d, "current_track_id": "%s", "playlist": "%s", "shuffle": %s, "repeat": "%s"}' % (
                    state_str, json_escape(title), json_escape(artist), json_escape(album),
                    get_volume(), str(cur_pid), json_escape(current_playlist_name),
                    'true' if shuffle_enabled else 'false',
                    repeat_mode
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
    init_audio_session()
    ensure_queue()
    set_volume(DEFAULT_VOLUME_PCT)
    tg_items = telegram_audio_items()
    if tg_items:
        # Start with Telegram + Local queue ready
        play_queue(tg_items, start=0, mix_local=True, shuffle=True, local_max=40, playlist_name='Telegram + Local')
    else:
        msg(player, 'play')
        pump_runloop(0.2)

    server = ReusableHTTPServer(('0.0.0.0', HTTP_PORT), iPodHandler)
    server.timeout = 1.0
    last_route_check = 0.0

    while True:
        server.handle_request()
        
        # Periodic watchdog
        pool_cls = objc.objc_getClass('NSAutoreleasePool')
        pool = msg(pool_cls, 'alloc')
        msg(pool, 'init')
        try:
            now = time.time()
            if now - last_route_check >= 5.0:
                route_watchdog()
                last_route_check = now
            pump_runloop(0.05)
            if url_mode:
                url_tick()
            elif not user_paused and repeat_mode != 'off':
                player_lock.acquire()
                try:
                    state_code = msg(player, 'playbackState', restype=ctypes.c_long)
                    if state_code != 1:
                        ensure_queue()
                        msg(player, 'play')
                        pump_runloop(0.05)
                finally:
                    player_lock.release()
        except Exception:
            pass
        msg(pool, 'release')

if __name__ == '__main__':
    run_server()
