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
  then a freshly-stamped snapshot of each state channel that has a value (one type-5
  intrinsics record, one type-8 heading record) so a late joiner is calibrated immediately
  (§5.2a)

Then, forever:
  a back-to-back stream of fixed 64-byte records. No per-record length. Parser is literally:
      read exactly 64 bytes; switch on byte[0] (the type).
```

Everything is **little-endian**; floats/doubles are IEEE-754.

### 5.2a State channels vs. event channels (handshake v2)

Not every channel *flows*. The handshake's `emission` map (v2+) classifies each stream:

- **`continuous`** — samples at the channel's own rate while enabled (`imu`, `pose`, `depth`).
- **`event`** — a record when the sensor reports (`gnss`, `altitude`, ~1 Hz each).
- **`state`** — `intrinsics` and `heading` carry a *current value*, re-emitted only on
  meaningful change. **Silence on a state channel means "unchanged", never "absent"** — and to
  make that distinction observable, state channels additionally send:
  1. **A snapshot on subscribe** — the current value, immediately after the handshake.
  2. **A keyframe every `keyframe_interval_s`** (10 s) — the current value re-asserted to all
     clients, so *any* ≥10 s slice of the stream is self-contained regardless of when you
     joined or what you missed.

  Snapshot/keyframe records are marked **`flags` bit0 = 1** and are **stamped at send time**
  (they assert "the value as of now"); change events carry `flags = 0` and the sensor's own
  timestamp. If you only care about the value, treat both identically; the flag exists so you
  can tell a fresh measurement from a re-assertion.

  A connect-snapshot is sent to one connection only and therefore **reuses the current `seq`
  without incrementing** (it may duplicate the neighbouring record's `seq`); keyframes go to
  everyone and increment `seq` normally. Gap detection on unflagged records is unaffected.

  Heading change events are additionally **rate-capped to ~1 Hz**, except a change ≥5° is
  forwarded immediately (walking fires CoreLocation at ~6 Hz of sub-degree jitter; the cap cuts
  that ~6× with no loss for a coarse yaw witness). The cap never applies to snapshots/keyframes.

Servers older than handshake `version: 2` emit state channels on-change only (plus a best-effort
intrinsics replay at connect) — a short static take can legitimately contain zero rows there.

### 5.2 Common 64-byte record layout

Every record — regardless of type — shares this header, then a type-specific payload from
offset 24:

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | `type` | 1 imu · 2 gyro · 3 accel · 5 intrinsics · 6 gnss · 7 altitude · 8 heading · 9 pose |
| 1 | u8 | `flags` | type-specific; 0 unless noted. Types 5/8: bit0 = snapshot/keyframe (§5.2a). Type 9: pose flags (§5.3). |
| 2 | u16 | `seq` | per-channel counter, wraps; use it to detect dropped records (connect-snapshots reuse the current value — §5.2a) |
| 4 | u32 | `reserved` | 0 |
| 8 | f64 | `host_ts` | host-clock seconds (see §3) |
| 16 | f64 | `unix_ts` | wall seconds (see §3) |
| 24..64 | — | payload | 10 × f32 slots (or f64 pairs), meaning depends on `type` |

### 5.3 Payloads by type

**Type 1 — IMU (fused device motion, the default)**

| Offset | Field | Units |
|---|---|---|
| 24 | `gyro.x`,`gyro.y`,`gyro.z` (f32×3) | rad/s |
| 36 | `accel.x`,`accel.y`,`accel.z` (f32×3) | **g** *on the wire*. CoreMotion `gravity + userAcceleration` (i.e. gravity is included, not removed); face-up at rest ≈ (0, 0, −1). |
| 48 | `quat.x`,`quat.y`,`quat.z`,`quat.w` (f32×4) | attitude, unit quaternion (present only if attitude enabled) |

> **Units — don't convert twice.** The **wire** carries acceleration in **g**. The `irtsp`
> Python client already normalizes to SI and gives you `accel` in **m/s²**, keeping the raw wire
> value as `accel_g`. Multiply by 9.80665 only if you decode the 64-byte records yourself.

Reference frames: body axes **X-right, Y-up, Z-out-of-screen**; attitude frame is CoreMotion
`xArbitraryZVertical` — i.e. the quaternion is **gravity-referenced** (Z vertical) with an
**arbitrary, non-north X**. That makes it a good independent gravity witness, but it carries no
absolute yaw. **Rate: fused device motion caps ≈100 Hz** regardless of the requested
`rate_hz`. Always compute the true rate from `host_ts` deltas.

*(Types 2 `gyro` and 3 `accel` carry the same slots but for the raw, unfused single-sensor mode;
in the default fused mode you receive type 1 only.)*

**Type 5 — Camera intrinsics** (pinhole; state channel — on change + snapshot + 10 s keyframes, §5.2a)

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

State channel (§5.2a): on-change capped ~1 Hz (immediate if ≥5°), plus a snapshot on connect and
10 s keyframes. Change events carry the native wall-clock timestamp; snapshots/keyframes (flags
bit0) are stamped at send time.

**Type 9 — ARKit 6DOF world pose**

| Offset | Field | Units |
|---|---|---|
| 24 | `tx`, `ty`, `tz` (f32×3) | meters, world translation |
| 36 | `trackingState` (f32) | 0 = none, 1 = limited, 2 = normal |
| 40 | `gravityTilt` (f32) | degrees between ARKit's world +Y and **true gravity** (§5.3.1) |
| 44 | `gravityAzimuth` (f32) | degrees; which way the frame leans (§5.3.1) |
| 48 | `qx`, `qy`, `qz`, `qw` (f32×4) | unit quaternion, world orientation |

Frame: **gravity-aligned world (+Y up), origin & yaw at session start**. The pose is
`ARCamera.transform` — the **ARKit camera frame** (in sensor-native landscape: +X right,
+Y up, +Z toward the viewer; optical axis = −Z). To use it with the type-5 intrinsics in a
standard CV pinhole frame (+Z forward, +Y down), apply `R_cv = R_arkit · diag(1, −1, −1)`.
`host_ts` is `ARFrame.timestamp` (same axis as the video PTS), so pose lines up with video
frames directly — the encoded frame *is* the `ARFrame.capturedImage` the pose was derived
from, so there is no lens, crop, or warp between them. Rate matches the AR camera's frame
rate (30–60 Hz; measured 30 Hz on an iPhone 17 Pro). This is iRTSP's own on-device VIO
estimate — useful as ground-truth/comparison or a prior, not a substitute for your own
fusion if you want raw inputs.

**Video stabilization is never applied** to a stream carrying odometry: in AR pose mode
ARKit's `capturedImage` is the raw sensor frame, and on the normal capture path
stabilization is force-disabled whenever any IMU/VIO channel is up.

#### `flags` (offset 1) — the world frame moved

`tracking = normal` is **not** a promise that the pose is continuous.

| Bit | Name | Meaning |
|---|---|---|
| 0 | `discontinuity` | **Re-anchor here; do not integrate across this sample.** Set whenever bit1, bit2 or bit3 is set, and on session interruption. |
| 1 | `relocalized` | Tracking recovered (`limited`/`none` → `normal`); ARKit re-anchors its map at this moment. |
| 2 | `jump` | The pose took a kinematically impossible step (>10 m/s, or >45° rotation, between consecutive frames) while tracking stayed `normal` — a silent loop closure or map merge. |
| 3 | `reset` | **The operator reset tracking.** A brand-new world frame starts here (see below). |
| 4 | `diverged` | **ARKit's position has provably run away. The take is not usable** (see below). |

Pose byte **4** (formerly reserved) carries ARKit's `TrackingState.Reason`, valid when
`trackingState = 1` (limited): `0` none · `1` initializing · `2` excessiveMotion ·
`3` insufficientFeatures · `4` relocalizing · `5` unknown-future-reason. `relocalizing` is the
one to watch — it explains a subsequent world-frame snap-back that otherwise looks like a teleport.

Branch on bit0; bits 1–3 say *why*, and the why matters. Bit 2 exists because ARKit corrects the
world frame on loop closure **without ever leaving `normal`** and without firing any callback — the
pose itself is the only witness, so iRTSP detects those seams kinematically. On a measured outdoor
capture there were 11 such re-anchors (worst: 6.04 m in a single 33 ms sample), every one with
`tracking = normal`.

Bits 1–2 are **data-quality warnings**: something went wrong and the tracker papered over it. Bit 3
is the opposite — it is **deliberate and clean**: an operator noticed a broken frame and fixed it.
Report them differently. "A new epoch starts here" is right for a reset; "the phone teleported" is
not.

#### `diverged` (bit 4) — the take is not usable

The phone's own accelerometer says it is **sitting still** while ARKit's pose **runs away**. This is
not a heuristic or a tuned threshold: it is two sensors that must agree, and don't.

The capture that motivated it — the phone lay on a table for **16 seconds** (accelerometer σ = 0.01
m/s², gyro 0–1 °/s) while its reported position **accelerated to 872 m**, with single-sample steps of
964 m, and `trackingState = normal` throughout.

**The cause is degenerate geometry, not poor features.** The operator had walked a brick plaza with
the camera pointed down; 64–83% of the view was repeating pavers. Every brick corner looks like every
other, so matches alias by one brick — *self-consistently* — and the filter confidently integrates a
phantom flow. A feature-count check sees nothing wrong: the scene is feature-**rich**. Any repeating
planar texture does it — brick, tiling, carpet, decking.

Note what this catches that nothing else can: `gravityTilt` is **structurally blind** to it, because
gravity can be perfectly correct while the position is nonsense. Watch for both; they fail
independently.

The gates are `accel σ < 0.08 m/s²` and `median |gyro| < 2 °/s` sustained ≥ 1.5 s (still), against
ARKit path length `> 0.25 m/s` (moving). A phone on a table measures σ ≈ 0.01 and an ARKit pose speed
of ~0.004 m/s, so the margins are 8× and 60× respectively — it does not cry wolf.

#### `reset` (bit 3) — a new world frame, not a skipped sample

This is the one flag it is not enough to "honour" by dropping a sample. After a reset the world
frame is **new in every respect** — new origin, new yaw, new gravity alignment. Every pose before it
is expressed in a frame that **no longer exists**, and there is **no transform relating the two
sides**. Nothing carries across.

So: close your current epoch, start a fresh one, and re-derive every registration from scratch. A
consumer that merely skips the flagged sample and keeps using its existing transform will silently
go on producing confident, wrong results.

`host_ts` **does** stay continuous across a reset (verified on-device: the step across a reset is
exactly one frame interval). `ARFrame.timestamp` runs off system uptime, not session start, so the
shared-clock contract in §3 holds — pose and video remain on the same axis. It is only the *spatial*
frame that is replaced, never the clock.

#### 5.3.1 `gravityTilt` — is the world frame actually level?

`worldAlignment = .gravity` promises world +Y is up, but **ARKit finds gravity from motion**.
Start a session with the phone sitting still and barely move it, and the world frame can settle
tens of degrees off vertical — with `trackingState = normal` for every pose, and nothing in the
ARKit API admitting it. A measured capture ran **21.8° off**, silently corrupting every
registration derived from it.

`gravityTilt` is the angle between ARKit's world +Y and **true gravity from CoreMotion**. Zero
is level.

**A tilted frame is one of two very different things, and they need opposite responses.**

*Un-converged* (mild, and improving): ARKit simply hasn't seen enough translation yet. Moving the
phone genuinely fixes it — measured 5.5° → 0.6° in 16 seconds on a fresh session, and 21.8° → 2.0°
across two board showings in the field.

*Broken* (and it will never fix itself): ARKit settles its gravity alignment early in a session and
**does not revisit it**. Measured: a frame 110° off — world "up" pointing sideways — was still 100°
off after 40 seconds of walking with 417 poses of `normal` tracking. No amount of movement recovers
this. The only cure is a **tracking reset** (flag bit 3).

Note that magnitude does **not** separate the two — a 21.8° frame healed while a 110° frame did not,
but there is no threshold between them. *Trend* separates them: extrapolate the rate of improvement
and ask whether it will ever reach level. iRTSP's own UI does exactly this, and warns "keep moving"
or "frame is broken — reset" accordingly.

> **How frames get broken — and it's the default rig workflow.** Start the phone streaming, then set
> it face-down on the table while you spend two minutes positioning the other cameras. ARKit
> initialises with no parallax and no visual features, infers gravity from whatever it can, locks
> that in, and drifts. By the time you pick the phone up, its world frame is unrecoverable and
> nothing in ARKit's API will tell you. **Carry the phone while you rig, or reset tracking before you
> record.**

**You cannot compute this on the client.** Recovering it there means fitting a device→camera
rotation from gravity samples, and that fit is **rank-deficient whenever the phone stays
upright**: gravity barely moves in the device frame, so the fit absorbs the tilt and reports
~0° no matter how tilted the world really is. On-device the device→camera relationship is a
**known constant, not a fit**, so a single sample gives the true answer.

`gravityAzimuth` is `atan2(z, x)` of world-frame gravity's horizontal component — meaningless
and unstable as the tilt → 0. Together the pair carries the full two degrees of freedom of a
unit vector, so you can rebuild world-frame gravity and hence the rotation that *levels* the
frame:

```python
t, a = math.radians(gravity_tilt), math.radians(gravity_azimuth)
g_world = (math.sin(t) * math.cos(a), -math.cos(t), math.sin(t) * math.sin(a))
# == (0, -1, 0) exactly when ARKit's frame is perfectly level
```

**It is already a robust estimate — do not median it yourself.** CoreMotion's gravity is a fusion
whose accelerometer correction goes transiently wrong while the device is being accelerated, so a
raw per-sample tilt spikes under motion (measured: a level frame reading 0.3° at rest spiked to
14.5° while the phone was waved around). The phone rejects gravity samples taken above **0.20 g**
of linear acceleration and medians the rest over a **2-second** window.

For reference, so you can predict when it will and won't have a value: a hand-held calibration-board
showing sits at a median of **0.04–0.07 g**, and never goes more than **0.19 s** without an
acceptable sample — so the estimate stays alive throughout, with 90–100% of samples feeding the
median. Walking runs 0.1–0.3 g and still accepts ~92% of samples, which matters because walking is
exactly when ARKit's frame converges.

**NaN means "the phone cannot currently vouch for a value". Treat it as NOT level.** You will see
it in raw IMU mode (no fused gravity), before the first trustworthy sample arrives, and
**mid-session whenever the device has been in sustained motion long enough for every trustworthy
sample to age out**. That last case is deliberate: holding a stale value under a fresh timestamp
would be the same failure as `trackingState = normal` on a 30°-off frame.

Older apps zero-filled these bytes, so also treat an exact `(0.0, 0.0)` pair as *unreported*,
**not** as a perfectly level frame — that mistake is the precise false negative this field exists
to catch.

**Autofocus is ON by default in AR pose mode, and you should leave it on.** A moving lens does
mean `fx`/`fy` breathe a few percent mid-stream (the type-5 records report it honestly, so your
projection is right per-frame but not constant), which sounds like a reason to lock focus. It
isn't, for close work: ARKit's locked focus is set for far tracking, and on an iPhone main
camera (f ≈ 6.9 mm, f/1.8) the hyperfocal distance is ≈ 5.3 m — locked at infinity nothing
nearer than ~5.3 m is sharp, and even locked at 1 m the near limit is ~0.84 m. A calibration
board at 0.5 m is outside the sharp zone for any plausible lock, costing you corner detections
on the exact ritual registration depends on. Autofocus was also suspected of causing a large
pose-vs-image misalignment and was **measured and exonerated** (the lens hunted identically in
the good and bad windows; the culprit was the un-converged gravity frame above). Lock focus only
when your subject is beyond ~5 m *and* a constant focal length genuinely matters.

### 5.4 The handshake fields

The JSON tells you, for this session: `endianness`, `record_bytes` (64), the full
`record_types` map, per-field `*_units`, which `streams` are enabled, the `clock`
(`host_anchor`, `wall_anchor`, `timebase`, and the `rtcp_sync` note), the `video`
(`rtsp_url`, `clock_rate`, `codec`), and the requested/observed rates. Example (abridged):

```json
{
  "protocol": "irtsp-imu", "version": 2, "endianness": "little", "record_bytes": 64,
  "record_types": {"imu":1,"gyro":2,"accel":3,"intrinsics":5,"gnss":6,"altitude":7,"heading":8,"pose":9},
  "gyro_units": "rad/s", "accel_units": "g",
  "body_axes": "X-right, Y-up, Z-out-of-screen", "attitude_frame": "xArbitraryZVertical",
  "clock": {"timebase":"mach_absolute_time_seconds","host_anchor":<f64>,"wall_anchor":<f64>,
            "rtcp_sync":"unix_ts matches RTP RTCP SR NTP timeline"},
  "video": {"rtsp_url":"rtsp://…:8554/live","clock_rate":90000,"codec":"H264"},
  "channel_rates_hz": {"imu":"<=100","gnss":"~1",
                       "heading":"on-change, capped ~1 Hz (immediate if >=5 deg), + keyframes",
                       "altitude":"~1","depth":"<=30 (separate channel)"},
  "emission": {"imu":"continuous","gyro":"continuous","accel":"continuous","pose":"continuous",
               "gnss":"event","altitude":"event","intrinsics":"state","heading":"state"},
  "state_channels": {"keyframe_interval_s":10,"flags":{"bit0":"snapshot_or_keyframe"},
                     "note":"…snapshot-on-subscribe + keyframe semantics, §5.2a…"},
  "streams": {"imu":true,"intrinsics":true,"gnss":false,"altitude":false,"heading":false,"pose":false}
}
```

`version` bumped to **2** with the state-channel contract (`emission` + `state_channels`, §5.2a).
A v1 server has neither key — treat its state channels as on-change-only and don't expect
snapshots or keyframes.

---

## 6. The depth channel (port 8556)

Depth resolves the monocular scale ambiguity — the biggest weakness of camera-only VIO — so if
you have it, fuse it. A depth map is ~200 KB, so this channel is **length-prefixed per frame**
rather than fixed-size.

```
On connect: [u32 LE handshake_len][UTF-8 JSON handshake]
Per frame:  [u32 LE frame_len][frame_len bytes = 32-byte header + payload]
Client → server (optional, v2): [u32 LE len][UTF-8 JSON control message] — see §6.1
```

**32-byte frame header** (little-endian):

| Offset | Type | Field |
|---|---|---|
| 0 | u8 | `type` = 10 |
| 1 | u8 | `flags` (bit0 = samples are float16, bit1 = payload compressed, §6.1) |
| 2 | u16 | `seq` |
| 4 | u32 | reserved |
| 8 | f64 | `host_ts` |
| 16 | f64 | `unix_ts` |
| 24 | u16 | `width` |
| 26 | u16 | `height` |
| 28 | u8 | `bytesPerPixel` (2) |
| 29 | u8 | `codec` (0 raw · 1 lzfse · 2 zlib; only meaningful when flags bit1 is set, §6.1) |
| 30..31 | — | padding |

**Samples**: the payload (decompressed if flags bit1 — §6.1) is `width × height` **IEEE-754
half floats**, row-major,
each = **z-depth in meters** (distance along the optical axis, not radial range — back-project
with `x=(u−cx)·z/fx`, `y=(v−cy)·z/fy`). Always read the per-frame header for the real dims;
the source depends on the capture mode (app ≥ 1.1):

* **Normal mode**: AVFoundation's LiDAR depth output — typically ~320×240, ≤30 Hz, with
  Apple's default hole-filling/smoothing filter applied.
* **ARKit pose mode**: ARKit `sceneDepth` — 256×192 at the AR camera's frame rate, aligned to
  the AR video frames (same `host_ts` axis), so **pose + video + depth stream together** from
  one session. (App 1.0 had no depth channel in AR mode.) `host_ts`/`unix_ts` are the same two axes as §3,
so a depth frame drops onto the video/IMU timeline exactly like everything else.

Depth resolution is lower than video; the depth-channel handshake reminds you to scale the
intrinsics (from the IMU channel, type 5) by `depth_width / video_width` before back-projecting.

### 6.1 Lossless compression (handshake v2, negotiated)

Raw f16 depth is ~2.2 MB/s at 30 Hz — 99.98% of the link — so v2 servers offer lossless
per-frame payload compression. It is strictly **opt-in**: a client that never negotiates keeps
receiving raw f16, bit-identical to v1. (The `irtsp` Python client negotiates automatically.)

To opt in, send (any time after connect):

```
[u32 LE length][UTF-8 JSON]     e.g.  {"compression": "lzfse"}   or  "zlib"  or  "none"
```

Subsequent frames to *your* connection carry a compressed payload, marked `flags` bit1 with the
codec id in header byte 29. Decompressed size is always `width × height × bytesPerPixel`.

- **`zlib`** is raw DEFLATE (RFC 1951, **no** zlib header/checksum): `zlib.decompress(payload, -15)`
  in Python — zero added dependencies anywhere.
- **`lzfse`** is Apple's LZFSE buffer format — faster and tighter, needs a decoder
  (`pyliblzfse` in Python).

Two rules keep decoding simple and safe:
1. **Every frame is independently decodable** — no inter-frame delta, so a dropped frame never
   corrupts the next one and any frame can be decoded in isolation.
2. **Branch on the per-frame flags, not on what you negotiated** — a frame that doesn't shrink
   (rare; noisy scenes) is sent raw with bit1 clear even after opt-in.

The handshake's `compression` object (v2+) lists `supported` codecs and repeats these
instructions; its absence means a v1 server (raw only, don't send control messages).

---

## 7. Putting it together — a fusion recipe

```text
1. Discover services (Bonjour) or use known host + ports.
2. Open the IMU channel (8555):
     read u32 len; read len bytes → parse JSON handshake (keep host_anchor, wall_anchor).
     loop: read exactly 64 bytes; dispatch on byte[0]; decode per §5.3.
3. (Optional) Open the depth channel (8556): read handshake (optionally opt in to
     compression, §6.1); then loop
     read u32 len; read len bytes; split into 32-byte header + payload; decompress if
     flags bit1; reinterpret as the half-float map (§6).
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
  Hz, heading on-change capped ~1 Hz, altitude ~1 Hz, depth ≤30 Hz). **Derive the actual rate
  from `host_ts` deltas**, never from `rate_hz`.
- **State channels are quiet by design.** A parked phone can go minutes without an intrinsics or
  heading *change*; the snapshot + 10 s keyframes (§5.2a) are what guarantee you still hold the
  current value. If a v2 stream's state channel produces zero rows over ≥10 s, that is a real
  fault — flag it loudly, don't paper over it.
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
  channel, freshly-stamped snapshots of the state channels (§5.2a) so you're immediately
  calibrated. On the depth channel, re-send your compression opt-in after reconnecting — codec
  choice is per-connection.
```
