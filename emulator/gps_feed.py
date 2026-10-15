#!/usr/bin/env python3
# Fake GPS receiver for the emulator's GPS UART (/dev/ttyGPS0 in the guest).
# The launchers (run-window.sh / run-window.ps1) start it; the position comes
# from data/gps (set with: configure gps <lat> <lon>), else the default below.
#
#   gps_feed.py <socket> [<lat> <lon>] [--speed <knots>] [--proto hippo|nmea]
#
# <socket> is a unix socket path, or tcp:HOST:PORT (Windows launcher).
# <lat> <lon> are decimal degrees (south and west negative); --speed > 0 moves
# the position north at that speed.
#
# hippo (default): Trimble "hippo" binary protocol, which the TCC803x unit's
#   gps.tcc803x.so speaks (it ignores NMEA and logs "uart error").
#   Frame: 0x81 <payload + checksum, byte-stuffed> 0x82; bytes 0x80..0x87 are
#   sent as 0x80, b & 0x7f; checksum = -(3 + sum(payload)) & 0xff.
#   Sent once a second:
#     0x32/1 UTC time + DOPs + satellites (sets the fix time and the UI clock)
#     0x31/2 GNSS fix (lat/lon in 180/2^31 deg, alt m, heading 360/65536 deg,
#            speed cm/s, byte 6 = 3-D fix, byte 7 = valid flags)
#     0x30/2 DR solution - the position the module reports to Android
#            (lat/lon/alt/bearing/speed as above, accuracy m at bytes 29-30)
# nmea: plain NMEA sentences (for the TCC897x kit image).
import socket, struct, time, sys

DEFAULT_LAT, DEFAULT_LON = 53.5339, -113.5066      # Edmonton

USAGE = "usage: gps_feed.py <socket|tcp:HOST:PORT> [<lat> <lon>] [--speed <knots>] [--proto hippo|nmea]"
def usage(msg):
    sys.exit("gps_feed.py: %s\n%s" % (msg, USAGE))

pos, opt, a = [], {}, sys.argv[1:]
while a:
    x = a.pop(0)
    if x in ("--speed", "--proto"):
        if not a: usage("%s needs a value" % x)
        opt[x] = a.pop(0)
    elif x in ("-h", "--help"):
        print(USAGE); sys.exit(0)
    else:
        pos.append(x)
if not pos: usage("no socket given")
if len(pos) not in (1, 3): usage("give both <lat> and <lon>, or neither")
SOCK = pos[0]
try:
    LAT = float(pos[1]) if len(pos) == 3 else DEFAULT_LAT
    LON = float(pos[2]) if len(pos) == 3 else DEFAULT_LON
    SPEED_KN = float(opt.get("--speed", 0))
except ValueError:
    usage("coordinates and speed must be numbers")
if not (-90 <= LAT <= 90 and -180 <= LON <= 180): usage("latitude must be -90..90, longitude -180..180")
PROTO = opt.get("--proto", "hippo")
if PROTO not in ("hippo", "nmea"): usage("protocol must be hippo or nmea")
ALT_M, ACC_M, HEADING = 670, 5, 0.0      # heading 0 = driving north

# ---------------------------------------------------------------- NMEA
def nmea(body):
    c=0
    for ch in body: c ^= ord(ch)
    return "$%s*%02X\r\n" % (body, c)

def ddmm(deg, is_lat):
    if is_lat: h = "N" if deg>=0 else "S"
    else:      h = "E" if deg>=0 else "W"
    deg=abs(deg); d=int(deg); m=(deg-d)*60
    w = 2 if is_lat else 3
    return ("%0*d%07.4f" % (w, d, m)), h

def nmea_msgs(t, lat, lon):
    hh=time.strftime("%H%M%S", t); dd=time.strftime("%d%m%y", t)
    la,lah = ddmm(lat, True); lo,loh = ddmm(lon, False)
    return [m.encode() for m in (
      nmea("GPGGA,%s.00,%s,%s,%s,%s,1,09,0.9,%.1f,M,-25.0,M,," % (hh,la,lah,lo,loh,ALT_M)),
      nmea("GPRMC,%s.00,A,%s,%s,%s,%s,%.1f,%.1f,%s,,,A" % (hh,la,lah,lo,loh,SPEED_KN,HEADING,dd)),
      nmea("GPGSA,A,3,01,02,03,04,05,06,07,08,09,,,,1.8,0.9,1.5"),
      nmea("GPGSV,2,1,08,01,40,083,44,02,17,308,43,03,07,344,42,04,22,228,41"),
      nmea("GPGSV,2,2,08,05,57,044,45,06,48,183,46,07,33,118,43,08,12,273,40"))]

# --------------------------------------------------------------- hippo
SEMI = 180.0 / 2**31          # lat/lon unit
HEAD = 360.0 / 65536          # heading/bearing unit

def frame(payload):
    cks = (-(3 + sum(payload))) & 0xff
    out = bytearray([0x81])
    for b in bytes(payload) + bytes([cks]):
        out += bytes([0x80, b & 0x7f]) if 0x80 <= b <= 0x87 else bytes([b])
    out.append(0x82)
    return bytes(out)

def angle16(deg):
    deg = (deg + 180.0) % 360.0 - 180.0
    return int(round(deg / HEAD))

def utc_msg(t, sats=9):
    p = bytearray(23)
    p[0], p[1] = 0x32, 0x01
    struct.pack_into('<H', p, 2, t.tm_year)
    p[4:9] = bytes([t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec])
    struct.pack_into('<HHH', p, 10, 15, 9, 12)    # PDOP, HDOP, VDOP (x0.1)
    p[17] = 0x10                                  # time source
    p[19] = sats
    return p

def gnss_fix(t, lat, lon, speed_cms):
    p = bytearray(47)
    p[0], p[1] = 0x31, 0x02
    tow_ms = ((t.tm_wday + 1) % 7 * 86400 + t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) * 1000
    struct.pack_into('<I', p, 2, tow_ms)
    p[6], p[7] = 0xC1, 0x0F                       # 3-D fix, correct; all valid bits
    struct.pack_into('<iihhH', p, 8, int(round(lat / SEMI)), int(round(lon / SEMI)),
                     ALT_M, angle16(HEADING), speed_cms)
    return p

def dr_solution(lat, lon, speed_cms):
    p = bytearray(55)
    p[0], p[1], p[2], p[3] = 0x30, 0x02, 0x03, 0x1E
    struct.pack_into('<iihhH', p, 9, int(round(lat / SEMI)), int(round(lon / SEMI)),
                     ALT_M, angle16(HEADING), speed_cms)
    struct.pack_into('<H', p, 29, ACC_M)
    return p

def hippo_msgs(t, lat, lon):
    cms = int(round(SPEED_KN * 0.514444 * 100))
    return [frame(utc_msg(t)), frame(gnss_fix(t, lat, lon, cms)), frame(dr_solution(lat, lon, cms))]

# ---------------------------------------------------------------- main
# Nothing is sent until the guest asks for it: tools/waitui.sh sends one byte
# once Android's GPS driver has opened the port. Data sent earlier queues up
# (QEMU, sockets) and reaches the driver stale; it then logs "uart error" and
# restarts its poll every 1.5 s until the queue is gone, which delays the first
# fix by seconds or longer. Everything the guest sends (its signal byte and the
# driver's queries) is read and discarded, so it cannot fill the socket and
# stall the driver. Fallback: start anyway after START_TIMEOUT seconds.
import threading
START_TIMEOUT = 240

def connect():
    while True:
        try:
            if SOCK.startswith("tcp:"):  # tcp:HOST:PORT (Windows launcher)
                host, port = SOCK[4:].rsplit(":", 1)
                return socket.create_connection((host, int(port)))
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(SOCK); return s
        except Exception as e:
            print("waiting for qemu socket...", e, flush=True); time.sleep(1)

def reader(sock, guest):
    try:
        while True:
            d = sock.recv(4096)
            if not d: break
            guest.set()
    except OSError:
        pass

build = hippo_msgs if PROTO == "hippo" else nmea_msgs
lat, lon = LAT, LON
while True:
    s = connect()
    print("connected to", SOCK, "protocol", PROTO, "at", LAT, LON, "speed", SPEED_KN, "kn", flush=True)
    guest = threading.Event()
    threading.Thread(target=reader, args=(s, guest), daemon=True).start()
    if guest.wait(START_TIMEOUT):
        print("guest GPS driver ready: sending", flush=True)
    else:
        print("no signal from the guest after %d s: sending anyway" % START_TIMEOUT, flush=True)
    n = 0
    try:
        while True:
            for m in build(time.gmtime(), lat, lon): s.sendall(m)
            n += 1
            if n % 60 == 0: print("sent", n, "fixes", flush=True)
            if SPEED_KN > 0: lat += (SPEED_KN * 0.514444) / 111320.0
            time.sleep(1)
    except OSError as e:
        print("connection lost:", e, flush=True)
        s.close()
