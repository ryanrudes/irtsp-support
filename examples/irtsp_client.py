#!/usr/bin/env python3
"""
Minimal iRTSP odometry client (pure Python, no dependencies).

Connects to the iRTSP IMU / VIO side-channel (default TCP 8555), reads the length-prefixed
JSON handshake, then decodes the flat stream of fixed 64-byte little-endian records and
prints them. This is the custom part of iRTSP; the video is plain RTSP (use ffmpeg/VLC/etc.)
and lines up with these records via the shared clock — see the integration guide:
https://github.com/ryanrudes/irtsp-support/blob/main/INTEGRATION.md

Usage:  python3 irtsp_client.py <iphone-ip> [port]
"""
import json
import socket
import struct
import sys

TYPES = {1: "imu", 2: "gyro", 3: "accel", 5: "intrinsics",
         6: "gnss", 7: "altitude", 8: "heading", 9: "pose"}


def read_exact(sock, n):
    """Read exactly n bytes or raise (TCP recv() may return short reads)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("stream closed")
        buf += chunk
    return bytes(buf)


def main(host, port=8555):
    sock = socket.create_connection((host, port))

    # 1) Handshake: u32 little-endian length, then that many bytes of UTF-8 JSON.
    (hlen,) = struct.unpack("<I", read_exact(sock, 4))
    handshake = json.loads(read_exact(sock, hlen))
    clock = handshake["clock"]
    print("connected. enabled streams:", handshake.get("streams"))
    print(f"clock anchor: host={clock['host_anchor']:.6f}s  wall={clock['wall_anchor']:.6f}s")
    print(f"(unix_ts = wall_anchor + host_ts - host_anchor; == RTP RTCP-SR NTP axis)\n")

    # 2) Records: read exactly 64 bytes; dispatch on byte[0]. seq wraps; use it to spot drops.
    while True:
        r = read_exact(sock, 64)
        typ = r[0]
        seq = struct.unpack_from("<H", r, 2)[0]
        host_ts, unix_ts = struct.unpack_from("<dd", r, 8)   # both f64 seconds

        if typ == 1:  # fused device motion
            gx, gy, gz, ax, ay, az, qx, qy, qz, qw = struct.unpack_from("<10f", r, 24)
            print(f"[{seq:5d}] imu  t={host_ts:.4f} "
                  f"gyro=({gx:+.3f},{gy:+.3f},{gz:+.3f})rad/s "
                  f"accel=({ax:+.3f},{ay:+.3f},{az:+.3f})g "
                  f"q=({qx:+.3f},{qy:+.3f},{qz:+.3f},{qw:+.3f})")
        elif typ == 5:  # camera intrinsics (video pixels)
            fx, fy, ox, oy, w, h = struct.unpack_from("<6f", r, 24)
            print(f"[{seq:5d}] intr fx={fx:.1f} fy={fy:.1f} c=({ox:.1f},{oy:.1f}) size={int(w)}x{int(h)}")
        elif typ == 6:  # GNSS (negatives = invalid)
            lat, lon = struct.unpack_from("<dd", r, 24)
            alt, hacc, vacc, spd, crs, sacc = struct.unpack_from("<6f", r, 40)
            print(f"[{seq:5d}] gnss {lat:.6f},{lon:.6f} alt={alt:.1f}m spd={spd:.2f}m/s crs={crs:.1f}deg")
        elif typ == 7:  # barometric altitude
            rel, press = struct.unpack_from("<2f", r, 24)
            print(f"[{seq:5d}] alt  rel={rel:+.2f}m press={press:.2f}kPa")
        elif typ == 8:  # compass heading (degrees; negative = invalid)
            th, mh, acc = struct.unpack_from("<3f", r, 24)
            print(f"[{seq:5d}] head true={th:.1f} mag={mh:.1f} +/-{acc:.1f}deg")
        elif typ == 9:  # ARKit 6DOF world pose
            tx, ty, tz, track = struct.unpack_from("<4f", r, 24)
            qx, qy, qz, qw = struct.unpack_from("<4f", r, 48)
            print(f"[{seq:5d}] pose t=({tx:+.3f},{ty:+.3f},{tz:+.3f})m track={int(track)} "
                  f"q=({qx:+.3f},{qy:+.3f},{qz:+.3f},{qw:+.3f})")
        else:
            print(f"[{seq:5d}] {TYPES.get(typ, '?%d' % typ)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 irtsp_client.py <iphone-ip> [port]")
        sys.exit(1)
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 8555)
