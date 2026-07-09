# iRTSP integration guide

How to consume iRTSP's streams from your own software, and — the important part — how the
video and the odometric (IMU / GPS / pose / depth) streams are timestamped so you can fuse
them without guessing at the temporal alignment.

> **Shortcut:** the official Python client, [`irtsp`](https://github.com/ryanrudes/irtsp-python)
> (`pip install irtsp`), implements everything on this page — typed records, the shared clock,
> discovery, depth, and synced video bundles. Read on if you're integrating from another language
> or want the byte-level details.

Everything here is the actual wire format. Field offsets, units, and endianness are taken
directly from `Sources/IMU/IMUWireFormat.swift`, `Sources/IMU/DepthStreamServer.swift`,
`Sources/Motion/StreamClock.swift`, and `Sources/RTP/RTCP.swift`.

---

## 1. The three channels

iRTSP exposes one session as **three independent TCP services**, all advertised over Bonjour
on the local network. They share nothing except **one clock anchor** (see §3), which is what
makes them fusable.

| Channel | Default port | Bonjour type | Transport / format |
|---|---|---|---|
| **Video + audio** | `8554` | `_rtsp._tcp` | Standard **RTSP 1.0 / RTP** (RFC 2326). H.264 or HEVC video @ 90 kHz, AAC-LC audio. Optional Basic/Digest auth. |
| **IMU / odometry** | `8555` | `_irtsp-imu._tcp` | Length-prefixed JSON handshake, then a flat stream of fixed **64-byte little-endian records**. |
| **Depth (LiDAR)** | `8556` | `_irtsp-depth._tcp` | Length-prefixed JSON handshake, then **length-prefixed depth frames** (32-byte header + half-float map). |

The IMU and depth channels are **opt-in** and off by default; the video channel is always on
while streaming. Discover them via Bonjour/mDNS, or connect directly if you already know the
host and ports. The video RTSP URL, its clock rate, and codec are also echoed inside the IMU
and depth handshakes so a client only needs to find one service.

> **Golden rule:** read the JSON handshake and drive your parser from it. Don't hardcode
> offsets you could read from `record_bytes` / `record_types` / `units`. The formats below are
> stable, but the handshake is the source of truth for *this* session (which channels are on,
> the clock anchors, the video URL, etc.).

---

## 2. Video + audio (port 8554)

This is a plain RTSP server. Point any RTSP client at it:

```
ffplay  rtsp://<iphone-ip>:8554/live
ffmpeg  -rtsp_transport tcp -i rtsp://<iphone-ip>:8554/live ...
gst-launch-1.0 rtspsrc location=rtsp://<iphone-ip>:8554/live ! ...
```

Nothing exotic — TCP-interleaved or UDP RTP, RTCP sender reports, SDP describing the codec.
The only thing you need to know for fusion is **how the RTP timestamps map to wall time**,
covered in §4. Video RTP clock rate is **90000 Hz**; audio RTP clock rate is the AAC sample
rate (48000 Hz). Audio/video lip-sync uses the same RTCP mechanism as video↔odometry sync.

---

## 3. The clock model (read this before anything else)

At the start of each streaming session iRTSP captures **one anchor pair**, held in a single
`StreamClock` that is shared by the RTP clocks (video + audio + RTCP), the IMU channel, and the
depth channel:

```
host_anchor = CMClockGetHostTimeClock() seconds   // == mach_absolute_time, seconds
wall_anchor = Unix time (seconds) at that same instant
```

The host clock is the **same axis** as:
- `CMSampleBuffer` presentation timestamps (video/audio PTS),
- CoreMotion `CMLogItem.timestamp` (IMU, barometer),
- `ARFrame.timestamp` (ARKit pose),
- the LiDAR depth frame's presentation timestamp.

So **every** sample iRTSP captures is natively on one monotonic host clock. Each record on the
IMU and depth channels carries **two** timestamps derived from it:

| Field | Meaning | Same axis as |
|---|---|---|
| `host_ts` (f64, seconds) | Host clock (seconds since boot). Monotonic, high-resolution. | Video/audio PTS, CoreMotion, ARKit, depth. |
| `unix_ts` (f64, seconds) | Wall-clock seconds. `unix_ts = wall_anchor + (host_ts − host_anchor)`. | RTCP Sender Report **NTP** timeline. |

The two are a fixed affine map of each other (`unix(host) = wall_anchor + host − host_anchor`),
computed once per session from the anchor. The handshake ships `host_anchor` and `wall_anchor`
under `clock`, so you can convert either way yourself.

**Why two?** `host_ts` is the cleanest axis for aligning the odometry channels with each other
and with the raw video PTS (all monotonic, no wall-clock adjustments). `unix_ts` is what lets
you line odometry up against the **video over RTP**, because RTP frames are located in wall time
by their RTCP Sender Reports (§4). It's also comparable across machines.

> `host_ts` is seconds-since-boot: it is only meaningful within one session/boot and is not
> comparable across app relaunches or reboots. `unix_ts` is wall time. Because the anchor is
> frozen at session start, no mid-session NTP correction will warp your timeline.

---

## 4. Synchronizing video with the odometric streams

This is the whole reason the clock is shared. **You do not need to cross-correlate IMU and
video to discover their time offset — there is no unknown offset.** Both derive from the same
anchor captured once. Here's the exact chain.

### 4.1 What an RTP video timestamp is

For a video access unit with presentation time `pts` (host-clock seconds), iRTSP emits RTP
timestamp:

```
rtp_ts = rtp_base + round((pts − pts0) * 90000)
```

where `pts0` is the first frame's `pts` and `rtp_base` is an arbitrary starting value. So the
RTP timestamp is a **relative 90 kHz tick count with an arbitrary base** — by itself it is *not*
wall time. (This is normal RTP.)

### 4.2 What the RTCP Sender Report gives you

Periodically the server sends an RTCP **Sender Report (SR)** pairing an RTP timestamp with an
NTP wall time. iRTSP computes that NTP time as:

```
sr_unix = wall_anchor + (pts_of_that_packet − host_anchor)   // == unix(host_ts) for the video
sr_ntp  = NTP(sr_unix)                                        // 1900-epoch 64-bit NTP
```

That is *the same function* used to fill `unix_ts` on the IMU and depth records. So the RTCP SR
NTP timeline **is** the odometry `unix_ts` timeline — bit for bit the same wall axis.

### 4.3 The mapping

Given any SR pair `(sr_rtp_ts, sr_unix)` and a video frame's `frame_rtp_ts`:

```
frame_unix = sr_unix + (int32(frame_rtp_ts − sr_rtp_ts)) / 90000.0
```

`frame_unix` is now directly comparable to any odometry record's `unix_ts`. (Use 32-bit wrapped
subtraction on the RTP timestamps.) Equivalently, on the host axis:

```
frame_host = host_anchor + (frame_unix − wall_anchor)   // comparable to record.host_ts
```

### 4.4 In practice you usually get this for free

Mature RTSP stacks already apply RTCP SR to produce NTP/wall-clock presentation times:

- **ffmpeg / libav**: RTP + RTCP handling yields NTP-anchored timestamps; see
  `-use_wallclock_as_timestamps` and the `rtp`/`rtsp` demuxer's NTP fields.
- **GStreamer**: `rtspsrc`/`rtpbin` with `ntp-sync=true` (and optionally RFC 7273) stamps
  buffers on the sender's NTP timeline.
- **live555**: `presentationTime` is synchronized to the sender once the first RTCP SR arrives.

Whichever you use, once RTCP SR has been received, your video frames carry a wall-clock time on
**exactly** the `unix_ts` axis of the odometry records. Then fusion is just: for a frame at
`frame_unix`, take the odometry samples bracketing it and interpolate. No offset search, no
drift (single anchor, single monotonic host clock underneath).

> First-SR latency: RTP timestamps are unanchored until the first RTCP SR arrives (typically
> within the first ~1 s). Buffer a little, or discard video before the first SR, if you need
> wall-clock alignment from frame 0.

---

## 5. The IMU / odometry channel (port 8555)

### 5.1 Framing

```
On connect (server → client):
  [u32 LE handshake_len][handshake_len bytes of UTF-8 JSON]
  (if intrinsics streaming is on, one type-5 record is replayed right after the handshake so a
   late joiner immediately has the camera model)

Then, forever:
  a back-to-back stream of fixed 64-byte records. No per-record length. Parser is literally:
      read exactly 64 bytes; switch on byte[0] (the type).
```

Everything is **little-endian**; floats/doubles are IEEE-754.

### 5.2 Common 64-byte record layout

Every record — regardless of type — shares this header, then a type-specific payload from
offset 24:

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | `type` | 1 imu · 2 gyro · 3 accel · 5 intrinsics · 6 gnss · 7 altitude · 8 heading · 9 pose |
| 1 | u8 | `flags` | 0 |
| 2 | u16 | `seq` | per-channel counter, wraps; use it to detect dropped records |
| 4 | u32 | `reserved` | 0 |
| 8 | f64 | `host_ts` | host-clock seconds (see §3) |
| 16 | f64 | `unix_ts` | wall seconds (see §3) |
| 24..64 | — | payload | 10 × f32 slots (or f64 pairs), meaning depends on `type` |

### 5.3 Payloads by type

**Type 1 — IMU (fused device motion, the default)**

| Offset | Field | Units |
|---|---|---|
| 24 | `gyro.x`,`gyro.y`,`gyro.z` (f32×3) | rad/s |
| 36 | `accel.x`,`accel.y`,`accel.z` (f32×3) | **g** (× 9.80665 for m/s²). CoreMotion `gravity + userAcceleration` (i.e. gravity is included, not removed); face-up at rest ≈ (0, 0, −1). |
| 48 | `quat.x`,`quat.y`,`quat.z`,`quat.w` (f32×4) | attitude, unit quaternion (present only if attitude enabled) |

Reference frames: body axes **X-right, Y-up, Z-out-of-screen**; attitude frame is CoreMotion
`xArbitraryZVertical`. **Rate: fused device motion caps ≈100 Hz** regardless of the requested
`rate_hz`. Always compute the true rate from `host_ts` deltas.

*(Types 2 `gyro` and 3 `accel` carry the same slots but for the raw, unfused single-sensor mode;
in the default fused mode you receive type 1 only.)*

**Type 5 — Camera intrinsics** (pinhole; sent on change, and replayed to late joiners)

| Offset | Field | |
|---|---|---|
| 24 | `fx`, `fy`, `ox` (f32×3) | focal lengths + principal-point x, in **video pixels** |
| 36 | `oy`, `width`, `height` (f32×3) | principal-point y + intrinsics reference resolution |

No lens-distortion model (rectilinear/pinhole assumed). The matrix is for the **video**
resolution; for depth, scale by `depth_width / video_width` (see §6).

**Type 6 — GNSS / location**

| Offset | Field | Units | Invalid |
|---|---|---|---|
| 24 | `lat` (f64) | degrees | |
| 32 | `lon` (f64) | degrees | |
| 40 | `altitude` (f32) | m | |
| 44 | `hAcc` (f32) | m (horizontal accuracy) | negative |
| 48 | `vAcc` (f32) | m (vertical accuracy) | negative |
| 52 | `speed` (f32) | m/s | negative |
| 56 | `course` (f32) | degrees | negative |
| 60 | `speedAcc` (f32) | m/s | negative |

Rate ≈1 Hz. Native timestamp is wall time; its `host_ts` is derived from the anchor.

**Type 7 — Barometric altitude**

| Offset | Field | Units |
|---|---|---|
| 24 | `relativeAltitude` (f32) | m, relative to stream start |
| 28 | `pressure` (f32) | kPa |

Rate ≈1 Hz. `host_ts` is native (host clock).

**Type 8 — Compass heading**

| Offset | Field | Units | Invalid |
|---|---|---|---|
| 24 | `trueHeading` (f32) | degrees | negative |
| 28 | `magneticHeading` (f32) | degrees | |
| 32 | `accuracy` (f32) | degrees | negative |

Event-driven (bursts to ~10+ Hz on motion). Native timestamp is wall time.

**Type 9 — ARKit 6DOF world pose**

| Offset | Field | Units |
|---|---|---|
| 24 | `tx`, `ty`, `tz` (f32×3) | meters, world translation |
| 36 | `trackingState` (f32) | 0 = none, 1 = limited, 2 = normal |
| 48 | `qx`, `qy`, `qz`, `qw` (f32×4) | unit quaternion, world orientation |

Frame: **gravity-aligned world, origin at session start**. `host_ts` is `ARFrame.timestamp`
(same axis as the video PTS), so pose lines up with video frames directly. Rate matches the AR camera's
frame rate (30–60 Hz; measured 30 Hz on an iPhone 17 Pro). This is iRTSP's own on-device VIO estimate — useful as ground-truth/comparison or a
prior, not a substitute for your own fusion if you want raw inputs.

### 5.4 The handshake fields

The JSON tells you, for this session: `endianness`, `record_bytes` (64), the full
`record_types` map, per-field `*_units`, which `streams` are enabled, the `clock`
(`host_anchor`, `wall_anchor`, `timebase`, and the `rtcp_sync` note), the `video`
(`rtsp_url`, `clock_rate`, `codec`), and the requested/observed rates. Example (abridged):

```json
{
  "protocol": "irtsp-imu", "version": 1, "endianness": "little", "record_bytes": 64,
  "record_types": {"imu":1,"gyro":2,"accel":3,"intrinsics":5,"gnss":6,"altitude":7,"heading":8,"pose":9},
  "gyro_units": "rad/s", "accel_units": "g",
  "body_axes": "X-right, Y-up, Z-out-of-screen", "attitude_frame": "xArbitraryZVertical",
  "clock": {"timebase":"mach_absolute_time_seconds","host_anchor":<f64>,"wall_anchor":<f64>,
            "rtcp_sync":"unix_ts matches RTP RTCP SR NTP timeline"},
  "video": {"rtsp_url":"rtsp://…:8554/live","clock_rate":90000,"codec":"H264"},
  "channel_rates_hz": {"imu":"<=100","gnss":"~1","heading":"event-driven (bursts to ~10+)",
                       "altitude":"~1","depth":"<=30 (separate channel)"},
  "streams": {"imu":true,"intrinsics":true,"gnss":false,"altitude":false,"heading":false,"pose":false}
}
```

---

## 6. The depth channel (port 8556)

Depth resolves the monocular scale ambiguity — the biggest weakness of camera-only VIO — so if
you have it, fuse it. A depth map is ~200 KB, so this channel is **length-prefixed per frame**
rather than fixed-size.

```
On connect: [u32 LE handshake_len][UTF-8 JSON handshake]
Per frame:  [u32 LE frame_len][frame_len bytes = 32-byte header + samples]
```

**32-byte frame header** (little-endian):

| Offset | Type | Field |
|---|---|---|
| 0 | u8 | `type` = 10 |
| 1 | u8 | `flags` (bit0 = samples are float16) |
| 2 | u16 | `seq` |
| 4 | u32 | reserved |
| 8 | f64 | `host_ts` |
| 16 | f64 | `unix_ts` |
| 24 | u16 | `width` |
| 26 | u16 | `height` |
| 28 | u8 | `bytesPerPixel` (2) |
| 29..31 | — | padding |

**Samples**: immediately after the header, `width × height` **IEEE-754 half floats**, row-major,
each = **distance from the camera in meters**. `host_ts`/`unix_ts` are the same two axes as §3,
so a depth frame drops onto the video/IMU timeline exactly like everything else.

Depth resolution is lower than video; the depth-channel handshake reminds you to scale the
intrinsics (from the IMU channel, type 5) by `depth_width / video_width` before back-projecting.

---

## 7. Putting it together — a fusion recipe

```text
1. Discover services (Bonjour) or use known host + ports.
2. Open the IMU channel (8555):
     read u32 len; read len bytes → parse JSON handshake (keep host_anchor, wall_anchor).
     loop: read exactly 64 bytes; dispatch on byte[0]; decode per §5.3.
3. (Optional) Open the depth channel (8556): read handshake; then loop
     read u32 len; read len bytes; split into 32-byte header + half-float map (§6).
4. Open the video (8554) with your RTSP client. After the first RTCP SR, each video frame
     has a wall-clock time on the unix_ts axis (§4). If your client exposes only RTP ts,
     apply §4.3 yourself using the SR pair.
5. Fuse: for a video frame at time t (unix or host — pick one axis and convert everything to it
     with the anchor), gather IMU samples around t and integrate/interpolate; sample the nearest
     depth frame; apply intrinsics (scaled for depth). No time-offset estimation is required —
     the streams are already on one clock.
```

Minimal record decode (Python-style pseudocode):

```python
import struct
TYPES = {1:"imu",2:"gyro",3:"accel",5:"intrinsics",6:"gnss",7:"altitude",8:"heading",9:"pose"}

def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c: raise ConnectionError
        buf += c
    return buf

# handshake
(hlen,) = struct.unpack("<I", read_exact(sock, 4))
handshake = json.loads(read_exact(sock, hlen))

# records
while True:
    r = read_exact(sock, 64)
    typ, flags, seq = r[0], r[1], struct.unpack_from("<H", r, 2)[0]
    host_ts, unix_ts = struct.unpack_from("<dd", r, 8)
    if typ == 1:  # imu
        gx,gy,gz, ax,ay,az, qx,qy,qz,qw = struct.unpack_from("<10f", r, 24)
    elif typ == 6:  # gnss
        lat, lon = struct.unpack_from("<dd", r, 24)
        alt, hacc, vacc, spd, crs, sacc = struct.unpack_from("<6f", r, 40)
    elif typ == 9:  # pose
        tx,ty,tz, track = struct.unpack_from("<4f", r, 24)
        qx,qy,qz,qw     = struct.unpack_from("<4f", r, 48)
    # ... types 5,7,8 similarly per §5.3
```

---

## 8. What to expect — rates, drops, gotchas

- **True rate ≠ requested rate.** `rate_hz` in the handshake is a *request* for the IMU/motion
  channel; iPhone fused device motion caps ≈100 Hz. Every channel runs at its own rate (GNSS ~1
  Hz, heading event-driven, altitude ~1 Hz, depth ≤30 Hz). **Derive the actual rate from
  `host_ts` deltas**, never from `rate_hz`.
- **Drops, not backpressure.** Both odometry channels are fire-and-forget with bounded buffers:
  if your socket backs up, the server *drops* records/frames for you rather than stalling
  capture or buffering unboundedly. Detect gaps with the per-channel `seq` counter. Keep your
  reader draining promptly.
- **Interleaving.** On the IMU channel all types share one stream and one `seq` sequence, in
  send order (≈time order). Across channels (IMU vs depth vs video), arrival order is
  independent — **align by timestamp, not arrival**.
- **Endianness / types.** Everything little-endian; `lat`/`lon` and both timestamps are f64,
  the rest f32. "Invalid" sentinels are negative values for the CoreLocation-derived fields
  (`hAcc`, `vAcc`, `speed`, `course`, `speedAcc`, `trueHeading`, `accuracy`).
- **Frames.** Body/IMU axes: X-right, Y-up, Z-out-of-screen. Attitude (type 1) is CoreMotion
  `xArbitraryZVertical`. ARKit pose (type 9) is a **gravity-aligned world** frame with origin at
  session start — a different frame from the IMU attitude; don't conflate them.
- **Units recap.** gyro rad/s · accel g · translation/altitude/depth meters · pressure kPa ·
  lat/lon/heading/course degrees · speed m/s · intrinsics in video pixels.
- **Reconnecting mid-session** re-sends the handshake (with the same anchors) and, on the IMU
  channel, replays the latest intrinsics so you're immediately calibrated.
```
