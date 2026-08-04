# iRTSP integration guide

How to consume iRTSP's streams from your own software, and — the important part — how the
video and the odometric (IMU / GPS / pose / depth) streams are timestamped so you can fuse
them without guessing at the temporal alignment.

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
| **Video + audio** | `8554` | `_rtsp._tcp` | Standard **RTSP 1.0 / RTP** (RFC 2326). H.264 or HEVC video @ 90 kHz; audio in AAC-LC, HE-AAC, AAC-ELD, Opus, or L16 (uncompressed). Optional Basic/Digest auth. |
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

### 2.1 Remote record trigger (optional, for multi-phone sync)

The phone can record each take to disk (video + the odometry sidecars) independently of streaming.
A controller can arm several phones and start/stop them together with an RTSP `SET_PARAMETER`
carrying `x-irtsp-record`, sent on the already-open control connection (a client must be connected;
the header or a body line both work):

```
SET_PARAMETER rtsp://<iphone-ip>:8554/live RTSP/1.0
CSeq: <n>
Session: <session-id>
x-irtsp-record: on          # "on"/"start"/"1" to begin, "off"/"stop"/"0" to end
```

The response echoes the resulting state as `x-irtsp-record: on|off`. Ordinary players never send
this, so it is invisible to them. The recorded takes each embed the session's `StreamClock` anchors
(host + wall), so the inter-phone start offsets are recoverable from the files afterward — the same
clock model as §3. Recording format/mode is configured on the phone, not over the wire.

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
  then a freshly-stamped snapshot of each state channel that has a value (one type-8
  heading record, one type-11 format record) so a late joiner is current immediately (§5.2a).
  Intrinsics are NOT in this snapshot: from revision 3 type 5 is a continuous per-frame
  channel, so a late joiner has the matrix within one frame anyway (§9.2)

Then, forever:
  a back-to-back stream of fixed 64-byte records. No per-record length. Parser is literally:
      read exactly 64 bytes; switch on byte[0] (the type).
```

Everything is **little-endian**; floats/doubles are IEEE-754.

### 5.2a State channels vs. event channels (handshake v2)

Not every channel *flows*. The handshake's `emission` map (v2+) classifies each stream:

- **`continuous`** — samples at the channel's own rate while enabled (`imu`, `pose`, `depth`, and
  `intrinsics`, which emits **one record per video frame**; see §9.2).
- **`event`** — a record when the sensor reports (`gnss`, `altitude`, ~1 Hz each).
- **`state`** — `heading` and `format` (type 11, protocol 2.1) carry a *current
  value*, re-emitted only on meaningful change. **Silence on a state channel means "unchanged",
  never "absent"** — and to make that distinction observable, state channels additionally send:
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
| 0 | u8 | `type` | 1 imu · 2 gyro · 3 accel · 5 intrinsics · 6 gnss · 7 altitude · 8 heading · 9 pose · 11 format |
| 1 | u8 | `flags` | type-specific; 0 unless noted. Types 8/11: bit0 = snapshot/keyframe (§5.2a). Type 9: pose flags (§5.3). Type 5: always 0 since revision 3. |
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

> **Units — don't convert twice.** The **wire** carries acceleration in **g**. The `irtsp-python`
> client already normalizes to SI and hands you `accel` in **m/s²**, keeping the raw wire value as
> `accel_g`. So: multiply by 9.80665 only if you are decoding the 64-byte records yourself.

Reference frames: body axes **X-right, Y-up, Z-out-of-screen**; attitude frame is CoreMotion
`xArbitraryZVertical` — i.e. the quaternion is **gravity-referenced** (Z is vertical) with an
**arbitrary, non-north X**. That makes it a usable independent gravity witness, but it carries no
absolute yaw. **Rate: fused device motion caps ≈100 Hz** regardless of the requested `rate_hz`.
Always compute the true rate from `host_ts` deltas.

*(Types 2 `gyro` and 3 `accel` carry the same slots but for the raw, unfused single-sensor mode;
in the default fused mode you receive type 1 only.)*

**Type 5 — Camera intrinsics** (pinhole; **continuous — one record per video frame**, §5.2a)

| Offset | Field | |
|---|---|---|
| 24 | `fx`, `fy`, `ox` (f32×3) | focal lengths + principal-point x, in **video pixels** |
| 36 | `oy`, `width`, `height` (f32×3) | principal-point y + intrinsics reference resolution |
| 48 | `lens_position` (f32) | focus position 0…1; **NaN = unknown** (revision 3) |
| 52 | `focus_mode` (u8) | `0` locked · `1` autoFocus · `2` continuousAutoFocus · `0xFF` unknown |
| 53 | `adjusting_focus` (u8) | `1` lens hunting · `0` settled · `0xFF` unknown |

`host_ts` **is the video frame's presentation timestamp**, so a record joins to a frame by equality.
`flags` (@1) is always `0` — this channel has no snapshots or keyframes. The three *focus* fields are
reported on the ARKit path (`capture_path = 1`) only when the shared `AVCaptureDevice` actually
reports them under ARKit — measured working, see §9.2. `exposure_us`
(@60) is **not** one of them: it is populated on both paths, and on ARKit it comes from
`ARCamera.exposureDuration`, a property of the frame itself, so `exposure_age_ms` there is exactly
`0`. Do not skip it on ARKit — §9.2 needs it every frame.

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
| 40 | `gravityTilt` (f32) | **degrees between ARKit's world +Y and true gravity** (see below) |
| 44 | `gravityAzimuth` (f32) | degrees, which way the frame leans (see below) |
| 48 | `qx`, `qy`, `qz`, `qw` (f32×4) | unit quaternion, world orientation |

The `flags` byte (offset 1) is meaningful on this record type:

| Bit | Name | Meaning |
|---|---|---|
| 0 | `discontinuity` | **The world frame moved under you.** Re-anchor here; do not integrate across this sample. Set whenever bit1/2/3 is set, and on session interruption. |
| 1 | `relocalized` | Tracking recovered (`limited`/`notAvailable` → `normal`). ARKit re-anchors the map at this moment. |
| 2 | `jump` | The pose took a kinematically impossible step (>10 m/s translation, or >45° rotation, between consecutive frames) while tracking stayed `normal` — i.e. a silent loop closure or map merge. |
| 3 | `reset` | **The operator reset tracking — a brand-new world frame starts here.** |

Bit 0 is the one to branch on; bits 1–3 say *why*, and the why matters. Bit 2 exists because ARKit
corrects the world frame on loop closure **without ever leaving `normal` tracking** and without
firing any callback — the pose itself is the only witness, so iRTSP detects those seams
kinematically and reports them here.

Bits 1–2 are **warnings** (the tracker papered something over). Bit 3 is the opposite: **deliberate
and clean** — an operator saw a broken frame and fixed it. Say "a new epoch starts here", not "the
phone teleported".

**A reset is not a skippable sample.** The new frame has a new origin, new yaw and new gravity
alignment; every earlier pose lives in a frame that no longer exists, and **no transform relates the
two sides**. Close the epoch and re-derive every registration. A consumer that skips the flagged
sample and keeps its existing transform will go on producing confident, wrong results.

`host_ts` **stays continuous across a reset** — verified on-device: the step across one is exactly
one frame interval (`ARFrame.timestamp` runs off system uptime, not session start). Only the
*spatial* frame is replaced, never the clock, so §3's shared-clock contract holds.

#### `gravityTilt` — is the world frame actually level?

`worldAlignment = .gravity` promises world +Y is up, but **ARKit finds gravity from motion**. Start a
session with the phone sitting still and barely move it, and the world frame can settle tens of
degrees off vertical — with `trackingState = normal` for every single pose, and nothing in the ARKit
API admitting it. Every downstream product (registration, reprojection, ground-plane fits) is then
silently wrong by that angle.

`gravityTilt` is the angle, in degrees, between ARKit's world +Y and **true gravity from CoreMotion**.
Zero means level.

A tilted frame is one of two things, and they need **opposite** responses. *Un-converged* (mild and
improving): ARKit hasn't seen enough translation, and moving the phone fixes it (measured 5.5° → 0.6°
in 16 s). *Broken*: ARKit settles gravity early in a session and never revisits it, so the frame will
**never** heal — a measured 110° frame was still 100° off after 40 s of walking with good tracking.
The only cure is a **tracking reset** (flag bit 3). Magnitude does not separate the two (a 21.8° frame
healed; a 110° one did not); *trend* does — extrapolate the improvement rate and ask if it ever gets
there. iRTSP's UI does this and says "keep moving" or "frame is broken — reset" accordingly.

> **How frames get broken, and it's the default rig workflow:** start the phone streaming, then leave
> it face-down on the table while you position the other cameras. ARKit initialises with no parallax,
> guesses gravity, locks it in, and drifts. **Carry the phone while you rig, or reset before you record.**

**Why the phone must send this and you cannot compute it.** A client trying to recover the tilt has
to fit the device→camera rotation from gravity samples — and that fit is **rank-deficient whenever
the phone stays upright**, because gravity barely moves in the device frame. The fit absorbs the tilt
and confidently reports ~0° no matter how tilted the world really is. On-device, the device→camera
relationship is a **known constant, not a fit**, so a single sample gives the true answer.

`gravityAzimuth` is `atan2(z, x)` of world-frame gravity's horizontal component — which way the frame
leans. It is meaningless (and numerically unstable) as the tilt approaches zero. Together the pair is
exactly the two degrees of freedom of a unit vector, so you can rebuild world-frame gravity, and
hence the rotation that levels the frame:

```python
import math
t, a = math.radians(gravity_tilt), math.radians(gravity_azimuth)
g_world = (math.sin(t) * math.cos(a), -math.cos(t), math.sin(t) * math.sin(a))
# g_world == (0, -1, 0) exactly when ARKit's frame is perfectly level
```

**It is already a robust estimate — you do not need to median it.** CoreMotion's gravity is a fusion
whose accelerometer correction goes transiently wrong while the device is being accelerated, so a raw
per-sample tilt spikes under motion (measured: a level frame reading 0.3° at rest spiked to 14.5°
while the phone was waved). The phone rejects gravity samples taken above **0.20 g** of linear
acceleration and medians the rest over a **2-second** window. A hand-held board showing sits at a
median of 0.04–0.07 g and never goes more than 0.19 s without an acceptable sample, so the estimate
stays alive throughout; walking (0.1–0.3 g) still feeds it ~92% of its samples.

**NaN means "the phone cannot currently vouch for a value" — treat it as NOT level.** You will see it
in raw IMU mode (no fused gravity), before the first trustworthy sample, and **mid-session whenever
the device has been in sustained motion long enough for every trustworthy sample to age out**. That
last case is deliberate: reporting a stale value with a fresh timestamp would be the same bug as
`trackingState = normal` on a 30°-off frame. Never treat NaN as level, and never treat an exact
`(0.0, 0.0)` pair (what older apps sent) as level either.

Frame: **gravity-aligned world (+Y is up, against gravity; X/Z yaw is arbitrary, not north),
origin at session start**. `host_ts` is `ARFrame.timestamp` (same axis as the video PTS), and the
pose and the video frame carrying the same `host_ts` come from *the same `ARFrame`* — the encoded
image is literally the buffer ARKit derived the pose from, so there is no lens, crop, or warp
between them. Rate follows ARKit (~60 Hz). This is iRTSP's own on-device VIO estimate — useful as
ground-truth/comparison or a prior, not a substitute for your own fusion if you want raw inputs.

**Stabilization is never applied** to a stream carrying odometry: in AR pose mode ARKit's
`capturedImage` is the raw sensor frame (no `AVCaptureConnection` is in the pipeline at all), and
on the normal capture path stabilization is force-disabled whenever any IMU/VIO channel is up.

**Autofocus is ON by default in AR pose mode**, and you should almost certainly leave it on.

The lens moving does mean `fx`/`fy` breathe a few percent mid-stream — the intrinsics records report
that honestly, so your projection is correct per-frame but not *constant*. That sounds like a reason
to lock focus, and it is the conventional advice. It is nevertheless the wrong call for close work,
for a reason that has nothing to do with tracking: **ARKit's locked focus is set for far tracking**.
On an iPhone main camera (f ≈ 6.9 mm, f/1.8) the hyperfocal distance is ≈ 5.3 m — locked at infinity,
nothing nearer than ~5.3 m is sharp; even locked at 1 m the near limit is ~0.84 m. A calibration
board at 0.5 m is outside the sharp zone for any plausible lock, so locking costs you corner
detections on the exact ritual your registration depends on. Measured with autofocus on: 0.58 px
reprojection at 21–24 corners.

Lock focus only when your subject is beyond ~5 m *and* a constant focal length genuinely matters.

> Historical note, so nobody re-derives this from first principles: autofocus was once suspected of
> causing a large pose-vs-image misalignment. It was measured and **exonerated** — the lens hunted
> identically in the good and bad windows (1.98% vs 2.13% `fx` spread), and a ±10% focal sweep moved
> the bad window's error by 1°. The actual culprit was an un-converged ARKit gravity frame, which is
> what `gravityTilt` now reports.

**Type 11 — Camera format / rolling-shutter fingerprint** (protocol **2.1**; state channel —
snapshot on connect + 10 s keyframes + immediate re-emit on change, §5.2a)

Everything a rolling-shutter consumer needs to *key* its own readout calibration, plus the one
quantity it cannot reconstruct itself: **which delivered-image axis the sensor's row readout runs
along**. All values here are **priors** — calibrate readout time and PTS convention yourself and
treat these as a keyed starting point, never ground truth.

| Offset | Field | |
|---|---|---|
| 24 | `format_id` (u32) | stable fingerprint of the capture mode **and the delivered geometry** — lens, capture path, delivered size, rate, binning/cropping, pixel format and `readout_direction`. Key your calibration table on it. It changes iff one of those changes, which includes **rotating the device** (see §5.3) — so a change is not by itself a sensor-mode switch |
| 28 | `width`, `height` (u16×2) | delivered pixels (what the RTP video carries) |
| 32 | `fps` (f32) | frames/sec (from `videoMinFrameDuration`) |
| 36 | `readout_time_s` (f32) | full-frame readout duration, seconds. **NaN = absent** (see `readout_provenance`) |
| 40 | `camera` (u8) | 0 unknown · 1 back-wide · 2 back-ultrawide · 3 back-tele · 4 front · 5 back-LiDAR |
| 41 | `capture_path` (u8) | 0 AVCapture · 1 ARKit |
| 42 | `flags2` (u8) | bit0 binned · bit1 cropped |
| 43 | `readout_direction` (u8) | 0 unknown · 1 `+Y` (top→bottom) · 2 `-Y` · 3 `+X` (left→right) · 4 `-X`. **The axis `α(row)` runs along, in delivered-image coordinates** (after the app's rotation). |
| 44 | `pts_convention` (u8) | 0 unknown · 1 first-row-start · 2 frame-center · 3 last-row-end · 4 exposure-start · **5 readout-instant** (what this app ships — see below) |
| 45 | `pts_provenance` (u8) | 0 unknown · 1 documented · 2 measured |
| 46 | `readout_provenance` (u8) | 0 absent · 1 probed |
| 47 | `direction_provenance` (u8) | **revision 6** — 0 unknown · 1 derived · 2 measured. How `readout_direction` was arrived at. |
| 48..64 | — | reserved (0) |

**`readout_direction` — the field you can't compute yourself.** The sensor scans its native rows
top→bottom; iRTSP rotates the delivered buffer to portrait (the same remap it applies to the
intrinsics), which carries the scan axis onto a *different* delivered axis. Because the app owns
that rotation, only the app knows the mapping — the same "on-device knowledge you can't recover
downstream" category as `gravityTilt`. In the ARKit path (`capture_path=1`) the image is delivered
un-rotated (sensor-native landscape), so the *derivation* gives `+Y` — but read the byte rather
than assuming it: if this format has been measured and the sensor scans against the derivation, the
app ships the measured direction here too. The direction is **derived from the
applied rotation, not measured** — the assumption that native readout is top→bottom, and the exact
sign, are what the gyro characterization below confirms — **and `direction_provenance` (@47) now says
which of those two you are looking at.**

**`direction_provenance` — because a derivation and an observation are not the same claim.** Every
other assertion on this record already carried its own provenance byte; the direction did not, which
left the app's most-trusted field the one a consumer could say least about. `derived` (1) means the
value was computed from the rotation the app applies, resting on the assumption that native readout
runs top→bottom. `measured` (2) means the app's own rolling-shutter measurement observed it for
**this** capture format, recovering direction from banding and not resting on that assumption at all.

**`measured` is still a per-take value, not a stored constant.** The measurement is made in
delivered-image coordinates, and those depend on the rotation the app was applying at the time — so a
`+y` measured with the phone upright is the same physical finding as a `−y` measured inverted, and
neither may be copied onto a take at the other orientation. What the app stores and reuses is the one
thing that survives a rotation: whether the sensor scans the way the derivation assumes. That bit is
applied to *this* take's own derivation, so `readout_direction` always describes the orientation the
frames were actually captured at, whichever provenance it carries. A consumer needs no correction of
its own — but must not cache the direction across an orientation change, any more than it would cache
`width`/`height` across one. `format_id` moves when the direction does, so a change is visible.

Note the deliberate asymmetry with `readout_time_s`: an unmeasured readout *time* is **absent**,
because there is nothing honest to say. An unmeasured *direction* is still reported, because the
derivation is genuinely informative — this byte is what stops it from reading as a measurement.
Producers before revision 6 wrote `0` into this reserved byte, which decodes as `unknown`, so an
older stream never claims a derivation it did not make.

**`readout_time_s` is a per-format constant, not a per-frame value**, and iOS exposes *no* live API
for it — the only source is the `quickTimeMetadataCameraFrameReadoutTime` metadata Apple embeds in a
recorded `.mov`. When the opt-in probe (off by default) has recorded one for this exact format it
rides here with `readout_provenance = probed`; otherwise `readout_time_s` is **NaN** and
`readout_provenance = absent`. **Absent is a valid, non-degraded state** — it means "no prior",
not "degraded". Never read a value without checking the provenance byte.

> **Two facts about this item that are easy to get wrong, both verified against real files.**
>
> 1. **It is track-level metadata on the video track, and its value is microseconds.**
>    `AVMetadataIdentifierQuickTimeMetadataCameraFrameReadoutTime` maps to
>    `mdta/com.apple.quicktime.camera.framereadouttimeinmicroseconds`, and the value is a bare
>    integer — an iPhone 4S clip carries `28512`, i.e. 28.5 ms. It is **not** a `CMTime` and **not**
>    seconds. It also does not appear in the movie header, so `AVAsset.metadata` alone will not find
>    it; you must read the video track's own `metadata`.
> 2. **Recent iPhones appear to have stopped writing it.** Surveyed across stock Camera recordings:
>    present on iPhone 4S / iOS 6.1.3 (28512 µs); absent on iPhone 12 / iOS 17.6.1 and on
>    iPhone 17 Pro / iOS 26.1–27.0. Absent from a third-party `AVCaptureMovieFileOutput` recording on
>    the 17 Pro too, so it is not a matter of capture configuration. Those same modern files do carry
>    the iOS 26 camera keys (`camera.lens_model`, `camera.focal_length.35mm_equivalent`,
>    `camera.lens_irisfnumber`), so the metadata track is alive — this key specifically is not.
>
> On hardware of that generation `readout_provenance = absent` is the expected steady state, not a
> sign the probe is misconfigured. Consumers that need `t_r` there must measure or calibrate it — the
> app's rolling-shutter screen produces an upper bound from lamp banding, but a point estimate needs
> a flicker source in the high hundreds of Hz, or a gyro-based fit.

**`pts_convention` — declared, with provenance.** This is *what instant a frame's PTS denotes*
relative to the readout window, and it is the anchor for the row-time model of §9.2.
`pts_provenance = documented` means this is a **declared default** — from Apple's documentation, from
the pipeline, or from a measurement on a *reference* device; it is **not** a measurement on the phone
you are talking to. `pts_provenance = measured` means it was characterized on that device itself. The
convention may differ between `capture_path = avcapture` and `= arkit`; each path reports its own.

**The shipped value is `readout_instant` (5), and it deliberately claims less than the other four.**
It means: `host_ts` marks a **read** instant — an exposure *end* — of one fixed row, plus a constant
per-format latency `D`. So `t_anchor = host_ts − exposure − D`, where `t_anchor` is when row 0 began
integrating. Two consequences, both easy to get wrong:

* **The anchor moves one-for-one with exposure.** Subtract *this frame's* `exposure_us` (type 5,
  revision 5). Under auto-exposure the shutter moves every frame, and a fixed per-format offset is
  wrong by however far it moved — an error that looks like noise in a fit rather than like a bug.
* **`D` is one unknown, not two, and the row it refers to is not recoverable.** Row 0's read and the
  last row's read differ by exactly `t_r` — but `k·t_r(format)` and a per-format constant offset are
  the same function of format, so no experiment over formats can separate them. `D` is absorbed whole
  by any camera↔clock offset calibration (a gyro fit, a rig sync), and it cancels *exactly* in
  row-to-row and frame-to-frame differences. Relative row timing needs no calibration at all.

> **Characterization status (2026-08-01, replacing the 2026-07-20 placeholder).**
> **Result: the anchor is a read-out instant that tracks exposure one-for-one** — `dt_anchor/d(exposure) = 1`.
>
> **Device · method.** iPhone 17 Pro, iOS 26.1, rear wide, AVCapture, 3840×2160 @ 60 fps. Not the
> gyro method that was planned: an LED driven at a known frequency (Arduino, ~1000 Hz, 14 ppm) is
> imaged, and the *residual phase* of its banding is tracked while only the **exposure time** is
> swept. The LED's phase does not depend on the camera's shutter, so any movement of the fitted
> phase is movement of the anchor — the derivative of the anchor with respect to exposure, read
> directly. ABBA (low-high-high-low) blocking cancels linear clock drift; per-block spread 0.009 rad.
> **Measured slope +3191 rad/s against +πf = +3142 predicted for a read-referenced anchor; the
> neighbouring candidate (exposure-*start*-referenced) is a full πf away.** Decisive.
>
> **What was refuted.** A follow-up test compared formats pairwise to split `D` into `k·t_r`
> (`k = 0` → row 0 is read, `k = 1` → the last row is read). Three pairs **refuted the model**: best
> residual 0.520 rad against 0.02 rad of measurement noise. Formats differ by more than their readout
> times — each carries its own constant offset — and since `k·t_r(format)` and that offset are the
> same function of format, `k` is absorbed entirely. **This is structural. More pairs will not help,
> and the experiment is not worth repeating.**
>
> **Why `documented` and not `measured`.** The result above is one device. It ships as a prior for
> every device, which is exactly what `documented` means here. A build that has run this
> characterization on the phone it is running on may report `measured`; none currently does.

**A mid-session `format_id` change is a take-validity event.** iRTSP forces a single physical camera
and disables auto lens-switching whenever odometry is up, so within a session the format is normally
stable and this record is quiet (snapshot + keyframes only). If `format_id` *does* change mid-stream
it is re-emitted immediately (flags 0) — treat it as "the sensor mode changed under you", not a
routine update.

### 5.4 The handshake fields

The JSON tells you, for this session: `endianness`, `record_bytes` (64), the full
`record_types` map, per-field `*_units`, which `streams` are enabled, the `clock`
(`host_anchor`, `wall_anchor`, `timebase`, and the `rtcp_sync` note), the `video`
(`rtsp_url`, `clock_rate`, `codec`), and the requested/observed rates. Example (abridged):

```json
{
  "protocol": "irtsp-imu", "version": 2, "revision": 5, "endianness": "little", "record_bytes": 64,
  "record_types": {"imu":1,"gyro":2,"accel":3,"intrinsics":5,"gnss":6,"altitude":7,"heading":8,"pose":9,"format":11},
  "gyro_units": "rad/s", "accel_units": "g",
  "intrinsics_units": "…one record per video frame; host_ts IS that frame's PTS; focus telemetry @48/@52/@53…",
  "body_axes": "X-right, Y-up, Z-out-of-screen", "attitude_frame": "xArbitraryZVertical",
  "clock": {"timebase":"mach_absolute_time_seconds","host_anchor":<f64>,"wall_anchor":<f64>,
            "rtcp_sync":"unix_ts matches RTP RTCP SR NTP timeline"},
  "video": {"rtsp_url":"rtsp://…:8554/live","clock_rate":90000,"codec":"H264"},
  "channel_rates_hz": {"imu":"<=100","gnss":"~1",
                       "intrinsics":"= video frame rate (one record per frame)",
                       "heading":"on-change, capped ~1 Hz (immediate if >=5 deg), + keyframes",
                       "altitude":"~1","depth":"<=30 (separate channel)"},
  "emission": {"imu":"continuous","gyro":"continuous","accel":"continuous","pose":"continuous",
               "gnss":"event","altitude":"event","intrinsics":"continuous","heading":"state","format":"state"},
  "state_channels": {"keyframe_interval_s":10,"flags":{"bit0":"snapshot_or_keyframe"},
                     "note":"heading (8) and format (11) ONLY — …snapshot-on-subscribe + keyframe semantics, §5.2a…"},
  "format_channel": {"note":"type-11 priors for rolling-shutter (§5.3)","readout_time":"…","pts_convention":"…"},
  "streams": {"imu":true,"gyro":false,"accel":false,"intrinsics":true,
              "gnss":false,"altitude":false,"heading":false,"pose":false,"format":true}
}
```

`version` is **2** with the state-channel contract (`emission` + `state_channels`, §5.2a).
`revision` distinguishes point releases within a version. **`revision: 2` = protocol "2.2"** added
the type-11 camera-format channel (`format` in `record_types`/`emission`/`streams`, plus
`format_channel`) and was purely additive.

**`revision: 3` reclassifies intrinsics (type 5) from a state channel to a continuous per-frame
channel** — see §9.2 for the full rationale, and `intrinsics_units` in the handshake for the layout.
Unlike revision 2 this is **not purely additive**: snapshots, keyframes and `flags` bit0 are *removed*
from type 5, and the CSV's `flags` column is gone. `version` stays **2** because the degradation is
graceful rather than breaking — a v2.0 consumer implementing the old interval-hold rule (§9.2) still
computes correct values at 16.7 ms granularity, since per-frame rows are a strict refinement of it,
and it never sees `flags = 1` so it never back-extrapolates. The only loss is at the very head of
the stream, which per-frame emission makes at most one frame wide.

Also added in revision 3: focus telemetry at @48/@52/@53 on type 5, and `intrinsics` in
`channel_rates_hz`.

**`revision: 4` adds the age of each focus field** — `i16` signed milliseconds at @54/@56/@58, in
bytes that were reserved. Purely additive: nothing moved, and a revision-3 consumer that ignores them
is unaffected. See §9.2 for why there are three of them.

> **Producer skew.** The Android producer is still at revision 2 semantics for type 5 (thresholded,
> with snapshots and keyframes). Branch on `revision` if you consume both, and do not assume the two
> producers agree on this channel until Android reports `revision: 3`.

**`streams` now describes the motion sensors too.** `imu` is the fused stream (type 1) and is true
only when fused mode is selected *and* motion is switched on; `gyro` / `accel` are the split raw
streams (types 2/3) and are true only in raw mode. All three can be false while the channel is up —
a session can carry intrinsics, pose or heading with no motion records at all. Do not infer "the
odometry channel is on" from `imu`; read the channel's presence from the handshake itself.

A v1 server has none of these keys — treat its state channels as on-change-only, with no snapshots,
keyframes, or format record.


### 5.5 `capture_settings` — verifying what processing was applied (revision 2)

Protocol **2.2** (`version: 2`, `revision: 2`) adds a `capture_settings` object to the odometry
handshake reporting the capture configuration that was actually put in effect, so a consumer can
**verify** rather than assume. Two sub-objects, `audio` and `video`.

Because this channel only exists while odometry is streaming, the calibration-safe overrides are
always in force, and the `video` block reflects that: stabilization `off`, geometric distortion
correction `false`, video HDR `off`, global tone mapping `false`, low-light boost `false`, auto
frame rate `false`, lens switching `locked`, face-driven AF/AE `false`.

**Which fields mean anything depends on the capture path** (`capture_path` on the type-11 format
record: `0` AVCapture, `1` ARKit). In **ARKit pose mode ARKit owns the camera and the app's
`AVCaptureSession` never runs**, so none of the per-device knobs are applied. The values above stay
correct either way — ARKit delivers an unstabilized, undistorted, SDR frame from a single camera at
a fixed rate — but four fields that echo the operator's choice would otherwise report settings the
capture never used, so on the ARKit path they report the truth instead:

| Field | AVCapture path | ARKit path |
|---|---|---|
| `auto_focus_range` | `none` · `near` · `far` | `not_applied` |
| `smooth_auto_focus` | operator's choice | `false` |
| `subject_area_monitoring` | operator's choice | `false` |
| `max_exposure_duration_s` | operator's cap, `0` = device default | `0` |
| `system_video_effects_disabled` | `true` | `false` |
| `arkit_autofocus` | `n/a` | `on` · `off` |

`arkit_autofocus` reports `ARWorldTrackingConfiguration.isAutoFocusEnabled` — the **only** focus
control that applies in pose mode, and therefore the one that decides whether focal length is
constant across the take. `off` pins the lens (ARKit focuses far, so anything nearer than ~5 m goes
soft); `on` — the default — lets it hunt, which breathes the focal length. If you are fitting
intrinsics, this is the field to check.

*Additive:* `version`/`revision` are unchanged. Consumers that ignore unknown keys are unaffected,
and the four re-reported fields only change value on a path where their old value was wrong.

The `audio` block reports `codec` (`aacLC` · `heAAC` · `aacELD` · `opus` · `l16`), `sample_rate`,
`channels`, and — when the app has taken over the audio session — `session_mode`
(`measurement` · `videoRecording` · `default`), `mic_mode`, `mic_data_source` and
`mic_polar_pattern`. When it hasn't, those read `system_default`.

> **Two honest caveats.** (1) This is *not* a hardware readback — a knob the device doesn't support
> silently no-ops, so read it as "what was asked of the device". (2) **Echo cancellation and noise
> suppression are off in every mode**: the app never instantiates a voice-processing audio unit.
> `measurement` additionally minimizes system dynamics processing for the flattest mic response.


---

## 6. The depth channel (port 8556)

Depth resolves the monocular scale ambiguity — the biggest weakness of camera-only VIO — so if
you have it, fuse it. A depth map is ~200 KB, so this channel is **length-prefixed per frame**
rather than fixed-size.

```
On connect: [u32 LE handshake_len][UTF-8 JSON handshake]
Per frame:  [u32 LE frame_len][frame_len bytes = 32-byte header + payload]
Client → server (optional, v2): [u32 LE len][UTF-8 JSON control message] — see compression below
```

**32-byte frame header** (little-endian):

| Offset | Type | Field |
|---|---|---|
| 0 | u8 | `type` = 10 |
| 1 | u8 | `flags` (bit0 = samples are float16, bit1 = payload compressed) |
| 2 | u16 | `seq` |
| 4 | u32 | reserved |
| 8 | f64 | `host_ts` |
| 16 | f64 | `unix_ts` |
| 24 | u16 | `width` |
| 26 | u16 | `height` |
| 28 | u8 | `bytesPerPixel` (2) |
| 29 | u8 | `codec` (0 raw · 1 lzfse · 2 zlib; only meaningful when flags bit1 is set) |
| 30..31 | — | padding |

**Payload**: `width × height` **IEEE-754 half floats**, row-major, each = **distance from the
camera in meters** — raw, or losslessly compressed (below). `host_ts`/`unix_ts` are the same two
axes as §3, so a depth frame drops onto the video/IMU timeline exactly like everything else.

### 6.1 Lossless compression (handshake v2, negotiated)

Raw f16 depth is ~2.2 MB/s at 30 Hz — 99.98% of the link — so v2 servers offer lossless
per-frame payload compression. It is strictly **opt-in**: a client that never negotiates keeps
receiving raw f16, bit-identical to v1.

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

Depth resolution is lower than video; the depth-channel handshake reminds you to scale the
intrinsics (from the IMU channel, type 5) by `depth_width / video_width` before back-projecting.

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
        tx,ty,tz, track  = struct.unpack_from("<4f", r, 24)
        g_tilt, g_azim   = struct.unpack_from("<2f", r, 40)
        qx,qy,qz,qw      = struct.unpack_from("<4f", r, 48)
        if flags & 0x01:        # world frame moved: re-anchor, don't integrate across this sample
            reanchor(relocalized=bool(flags & 0x02), jump=bool(flags & 0x04))
        if g_tilt == g_tilt and g_tilt > 5:   # not NaN, and off vertical
            warn("ARKit world frame is not level — walk the phone around")
    # ... types 5,7,8 similarly per §5.3
```

---

## 8. What to expect — rates, drops, gotchas

- **True rate ≠ requested rate.** `rate_hz` in the handshake is a *request* for the IMU/motion
  channel; iPhone fused device motion caps ≈100 Hz. Every channel runs at its own rate (GNSS ~1
  Hz, heading on-change capped ~1 Hz, altitude ~1 Hz, depth ≤30 Hz). **Derive the actual rate
  from `host_ts` deltas**, never from `rate_hz`.
- **State channels are quiet by design.** A parked phone can go minutes without a heading or format
  *change*; the snapshot + 10 s keyframes (§5.2a) are what guarantee you still hold the current
  value. If a v2 stream's state channel produces zero rows over ≥10 s, that is a real fault — flag
  it loudly, don't paper over it. **This no longer applies to intrinsics**: from revision 3 it is
  continuous, so silence there means frames have stopped arriving, which is a much louder fault.
- **Type-11 values are priors, not ground truth** (protocol 2.1). `readout_time_s` may be absent
  (NaN, `readout_provenance = absent`) and `pts_convention` is `documented` until characterized *on
  the device in front of you* — always read the provenance byte, and keep your own calibration
  authoritative. In particular `readout_instant` carries an unresolved per-format constant `D`
  (§5.3); your own camera↔clock offset absorbs it. The one field to
  trust as app-authoritative is `readout_direction` (§5.3). A mid-session `format_id` change is a
  take-validity event, not a routine update.
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

---

## 9. Recorded takes — sidecars and CSV exports

A take recorded on the phone (§2.1) is a self-contained bundle:

```
<take-id>/
  manifest.json      clock anchors, config, durations, videoStartHost
  video.mov          the faithful muxed take
  imu.bin.lzfse      concatenated 64-byte IMUWire records (§5.2), LZFSE whole-file
  depth.bin          per-frame [u32 len][32-byte header][payload]  (§6)
  thumbnail.jpg
```

The sidecars are the **same byte layout as the live channels**, so a parser you already have reads
them with no new code. Everything in §5 applies unchanged.

**Several takes at once.** The library can export a selection in a single archive: `takes_export.zip`,
one folder per take, each holding exactly the files described below for that take. Folders are named
after the take's title, falling back to its bundle id when two selected takes share a title — so the
folder name is stable but not a key; the manifest's `id` is. A one-take export has no wrapping folder.

### 9.1 The advanced exporter's CSVs

The phone can also export per-sensor CSVs from a take. Columns:

| Channel | Header |
|---|---|
| `accel` | `t,host_ts,unix_ts,ax_g,ay_g,az_g` |
| `gyro` | `t,host_ts,unix_ts,gx_rads,gy_rads,gz_rads` |
| `pose` | `t,host_ts,unix_ts,px_m,py_m,pz_m,qx,qy,qz,qw,tracking_state,tracking_reason,flags,gravity_tilt_deg,gravity_azimuth_deg` |
| `intrinsics` | `frame,t,host_ts,unix_ts,fx,fy,ox,oy,width,height,lens_position,focus_mode,adjusting_focus,lens_age,focus_mode_age,adjusting_age,exposure_duration,exposure_age,readout_time,readout_direction,readout_direction_provenance` |

**The CSV spells out what the wire encodes.** `focus_mode` reads `locked` · `autoFocus` ·
`continuousAutoFocus` · `unknown`, and `adjusting_focus` reads `hunting` · `settled` · `unknown`,
rather than the wire's `0`/`1`/`2`/`0xFF`. Same reason `readout_direction` is `+y` and not `1`: a
CSV is read by people and by parsers that infer a type per column, and `255` in an integer column is
a number that silently sorts, averages and plots as 255. A value neither we nor your iOS version
recognises reads `unknown(N)`, keeping it distinct from the sentinel that means *nobody ever
reported* — those are opposite kinds of ignorance. The **wire is unchanged**; this is a rendering
choice in the exporter (§5.1 has the numeric encoding).

`host_ts` / `unix_ts` are the record's own timestamps, exactly as on the wire (§3). For the trace
channels `t` is **movie-relative seconds** — `host_ts − videoStartHost` from the manifest — so
`t = 0` is the first recorded video frame and the CSV lines up with the player timeline.

**`intrinsics.csv` is different in kind, and the difference is the useful part.** Its rows are not
records; they are **frames of the video you just exported**, one each, in order:

> **Exactly one row per frame in the exported video.** `frame` runs `0, 1, 2, … N−1` with no gaps
> and no repeats, and row *i* describes exported frame *i*. `t` is that frame's time **in the
> exported clip**, so a trimmed export starts at `t = 0`.

That makes the file joinable by row index — `intrinsics[i]` is the matrix for frame `i`, full stop —
without matching timestamps at all. `host_ts` / `unix_ts` are still there, unchanged from the wire,
for aligning against other devices.

**Use `host_ts`, not `t`, for anything about *when* a frame was exposed.** They are different kinds of
number and only one is a measurement:

* `host_ts` is the frame's capture timestamp, straight from the sensor — irregular, because the
  hardware is. A measured 60 fps take stepped by 16.673 ms rather than the nominal 16.667, with
  tens of nanoseconds of variation between consecutive steps: a true rate of 59.977 Hz.
* `t` is where the frame sits in the **exported movie's** timeline, read from that file's sample
  table. It lands on a regular 1/fps grid, because a movie's timeline is regular. It is a playback
  coordinate — right for scrubbing a player and for a trimmed clip that starts at zero, wrong for
  timing analysis.

Neither is interpolated or invented: `t` is genuinely the movie's own presentation time, not
`index ÷ fps`. But the movie's timeline having been normalised to a grid is exactly why it cannot
answer questions about the sensor.

**A short step in `t` is not a dropped frame.** A `.mov` timeline is an integer tick count — usually
timescale 600, where a nominal 60 fps frame is exactly 10 ticks. A real sensor does not run at
exactly 60 Hz, so each frame's true interval is a fraction of a tick off, and the container absorbs
the accumulated error by occasionally emitting a step one tick short. Measured on a 5399-frame take:
`host_ts` averaged 16.665602 ms (a true 60.0038 Hz) = 9.9993609 ticks, a deficit of 0.00064 ticks per
frame, which reaches a whole tick every ~1565 frames — so the file contained exactly **three** steps
of 0.015 s (9 ticks) among 5395 of 0.016666… (10 ticks), against 3.45 predicted. At each of those
points `host_ts` stepped completely normally.

So: irregularities in `t` describe the container, and `frame_gaps.csv` is the only thing that reports
lost frames. Deriving a frame rate from `t` returns the nominal rate by construction; derive it from
`host_ts`.

If a frame's record is missing, the row is still there, carrying its `frame` and `t` with the
measurement columns **empty**. A row is never filled in by carrying the previous frame's matrix
forward: a held value that looks like a measured one is worse than a blank. (This is rare — it means
a frame arrived with no intrinsic-matrix attachment. The app counts and logs it.)

### 9.1b Capture settings and lens calibration in the export

Alongside the CSVs, an export carries two JSON artifacts:

**`<take>_capture_settings.json`** — the capture state **as applied**, read back from the device,
with the requested config alongside so a divergence is visible: CV-mode overrides, stabilization,
`isGeometricDistortionCorrectionEnabled` (read from hardware, not the toggle), lens switching,
device/camera identity, delivered resolution, `format_id`, OS/app versions — and the zoom: its value
at take start plus a timestamped log of every change during the take (host-clock axis). An empty log
means the zoom never moved; that is stated, not implied.

**`<take>_distortion.json`** (always) **and `<take>_distortion.bin`** (when recorded) — Apple's
per-frame lens calibration from the depth path. **This is live, per-frame data, not a per-module
constant** — measured on an iPhone 17 Pro: the lookup table changed on every one of 535 frames while
focus swept 4.4 %, worst-case pixel effect 1.769 px at reference dimensions, with the distortion
centre wandering sub-pixel per frame (OIS). Each record therefore carries its own `host_ts`.

The JSON is written even when there is no data, with an explicit `status: "unavailable"` and a
reason (`depth_not_streaming` vs `recorded_by_earlier_app_version`) — an absent file would be
ambiguous between those and a failure. When data exists it carries: the record layout; the
calibration's **reference dimensions** and the take's delivered dimensions (they generally differ,
and can differ in aspect — a crop, not a scale); the explicit reference→delivered affine mapping
**with the intrinsic-matrix pair it was derived from**, so the derivation is auditable rather than
trusted; a per-take `maxPixelShiftPx` summary so a consumer can decide whether per-frame variation
matters without parsing the stream; and ten worked **sample points** per table, computed on device
from the take's first record — validate your lookup implementation against them in one assertion
rather than settling table direction and radius normalisation from prose.

> **Comparing tables: use `r·|Δv|` (pixels), never per-entry relative deviation.** The table's
> entries approach zero near the centre, so float jitter in the sixth decimal place prints as
> hundreds of percent while moving nothing by a hundredth of a pixel. Pixel displacement is
> form-independent — both documented lookup-table conventions differ by exactly `r·Δv`.

### 9.1a `frame_gaps.csv` — the frames that didn't make it

Exported alongside `intrinsics.csv`, always, because the one-to-one guarantee above is only half the
story: some frames the phone *captured* are not in the exported video, and their measurements would
otherwise vanish without trace.

```
after_frame,dropped,t_first,t_last,host_ts_first,host_ts_last
41,2,1.9812,1.9979,42145.2011,42145.2178
```

Read as: **two captured frames were lost immediately after exported frame 41**. `after_frame` is an
index into the *exported* video (the same numbering as `intrinsics.csv`), and `-1` means the run
precedes the first exported frame. Runs are collapsed, so one row is one gap. An empty file — header
only — means every captured frame in the exported range made it, which is the common case for a
lightly-loaded configuration.

Why frames go missing at all: a record is emitted at **capture**, off the same sample buffer as the
pixels; a frame in the movie is one that then survived **encode and write**. Between those points a
frame can be refused by the encoder when it is saturated (deliberately — the alternative pushes
backpressure into capture and costs every channel, not just video), refused by a writer that isn't
ready, or dropped at the head because a passthrough track must open on a keyframe. Records outside
the trim range are *not* listed here: those weren't dropped, they were cut.

### 9.2 Reading the intrinsics data

Intrinsics are a **continuous per-frame channel** (§5.2a): on the wire, one record per video frame,
with `host_ts` equal to that frame's presentation timestamp, so the join is by equality rather than
by holding a value over an interval. Nothing to back-extrapolate, nothing to carry forward.

The exported `intrinsics.csv` goes one step further and does the join for you — one row per
*exported* frame, indexed by `frame` (§9.1). If you are reading the **raw sidecar** rather than the
CSV, you are on the wire contract: match `host_ts` to the frame's PTS (or `t` to movie-relative
time, within half a frame period for float safety), and expect a few more records than the movie has
frames, for the reasons in §9.1a.

**Consecutive rows are usually near-identical, and that is correct.** The matrix genuinely moves
every frame: optical image stabilisation shifts the principal point continuously, and focus changes
the focal length. Expect `ox`/`oy` to jitter on a handheld take, and `fx`/`fy` to move when the lens
refocuses. If you want a change threshold, apply your own — you know your error budget.

**Focus telemetry tells you _why_ `fx` moved.** `lens_position` (0…1), `focus_mode`
(`0` locked, `1` autoFocus, `2` continuousAutoFocus, `0xFF` unknown) and `adjusting_focus`
(`1` hunting, `0` settled, `0xFF` unknown). **`exposure_duration` is not one of these**: it is
present on both paths, and on ARKit it is exact (age `0`), not recency-joined.

**On the ARKit path the three focus fields are reported when — and only when — the device actually
reports them.** `ARCamera` exposes no lens position, no focus mode and no adjusting flag; there is no
such API. But the *shared* `AVCaptureDevice` is documented as readable "at any time, regardless of
focus mode" and as key-value observable, and nothing requires the observer to be the session driving
the camera. So pose mode now watches it while ARKit holds the lens.

The mechanism is built so it cannot fabricate. Unlike the AVCapture path, it **never seeds from a
read** — a read proves nothing there, since `lensPosition` defaults to `1.0` and a stale value from
an earlier session would look identical to a live one. Only a KVO callback is recorded, and a
callback cannot arrive unless the property genuinely changed. So:

* Notifications arrive → real readings, real ages, same semantics as AVCapture.
* No notifications → no readings at all, all three unknown, ages blank — exactly the old behaviour.

**A blank age therefore keeps one meaning on both paths: nothing was ever reported.** It never means
"reported, but we're unsure when". Read the ages, not the presence of the column.

> **Measured (2026-08-03, iPhone 17 Pro / iOS 27, ARKit 1920×1440 @ 60, autofocus on): the shared
> device does report.** Over a 429-frame take `lens_position` moved across 0.000–0.753 in steps of
> exactly **1/255** — the lens driver's own 8-bit quantization — and correlated **r = −0.84** with
> `fx`, which comes from a completely independent source (ARKit's per-frame intrinsics attachment).
> Two unrelated signals describing the same lens movement is what makes the reading real; a stale or
> default value cannot correlate with anything. `focus_mode` and `adjusting_focus` tracked it
> coherently (mode `1`/hunting during the rack, mode `2`/settled after), and `adjusting_focus` was
> unknown for exactly the **first 5 frames** — the no-seed rule visible in the data, reporting
> nothing until something was genuinely reported.
>
> `lens_age` ran a median of **40 ms** with a p90 of 157 ms and a maximum of 706 ms. The long ages
> are not a fault: they occur while the lens is *parked*, where no change means no notification, and
> the age growing is the mechanism telling you so. Compare the AVCapture path, which seeds and so
> starts from a fresh reading. **Do not treat a large `lens_age` as bad data — treat it as an
> accurate statement about how long the lens has been still.**
>
> Note the size of the effect this exposes: `fx` swung **12.1%** (1273.8 → 1427.7) across that take.
> Focus breathing of that magnitude is why a single take-wide intrinsic matrix is the wrong model
> here, and why the per-frame rows exist.

**These three are joined to the frame by recency, not by identity — and revision 4 says how loosely.**
`fx`/`fy`/`ox`/`oy` come from an attachment on the frame's own sample buffer: same object, same
instant, exact. The focus fields do not. They are `AVCaptureDevice` properties watched by KVO, which
fires on the device's own schedule, so the record carries whatever was last reported. Revision 4 adds
the staleness so you can decide whether to trust a given row:

| Field | Offset | Meaning |
|---|---|---|
| `lens_age_ms` | i16 @54 | ms from the `lensPosition` report to **this frame's** exposure |
| `focus_mode_age_ms` | i16 @56 | same, for `focusMode` |
| `adjusting_age_ms` | i16 @58 | same, for `isAdjustingFocus` |

**Always `>= 0`.** Each field carries the newest report **at or before that frame's exposure**, never
a later one. A frame's PTS is its capture instant but it does not reach the app until ~2 frame
intervals later, so a report can easily be newer than the frame and older than the record — stamping
it on would attribute a lens state to an exposure that never had it (a hunt starting 3 ms after frame
N would mark frame N as hunting). `-32768` means there was no reading at or before this frame, which
reads as unknown rather than borrowing a later one. Values saturate rather than wrap.

**Three ages, not one**, because the three properties have three independent KVO observers:
`focusMode` changes rarely, `lensPosition` moves continuously through a hunt, and `isAdjustingFocus`
flips only at its ends. A single age would be accurate for at most one of them.

The CSV's three age columns are in **seconds**, like every other time column in the file — the wire
carries them as `i16` milliseconds only because a 64-byte record has no room for three `f32`s. "Never
reported" is an **empty cell** there, not the wire's `-32768`.

Why not read the device at frame time and make them exact? Because that is a lock-taking call on the
60 fps capture path — the one place it must not go. The cache is the right structure; stating its age
is the missing half.

> **Changed in revision 3.** This channel used to be a state channel: emitted only when the matrix
> moved more than a pixel, with snapshots and 10 s keyframes, a `flags` column, and carry-forward on
> trimmed exports. All of that is gone. The old design quantised a continuous signal into apparent
> discrete "events" — and because its comparison baseline was the last *emitted* value, a slow drift
> produced a scattering of rows that read like several separate changes. It also stamped records with
> "now" instead of the frame's PTS, which is why a row could never be attributed to a specific frame.
> If you implemented the old interval-hold rule, it still yields correct answers at 16.7 ms
> granularity — per-frame rows are a strict refinement of it — but it is no longer the intended read,
> and the `flags` column no longer exists.

### 9.2a Placing a row's exposure in time

A CMOS sensor exposes and reads **one row at a time**: two sweeps run down the frame, a reset sweep
that starts each row's exposure and, `exposure` later, a readout sweep that ends it. Both travel at
the same rate, so with `t_r` = `readout_time_s` (type-11 format record) and `H` = rows:

```
t_anchor          = host_ts - exposure_duration - D    # D: per-format constant, see below
exposure_start(k) = t_anchor + alpha(k) * t_r
exposure_end(k)   = t_anchor + alpha(k) * t_r + exposure_duration
exposure_mid(k)   = t_anchor + alpha(k) * t_r + exposure_duration / 2
```

and the single best "frame time" — the middle line at mid-exposure, which is what to use when
treating the frame as one instant:

```
t_frame = t_anchor + t_r / 2 + exposure_duration / 2
        = host_ts - exposure_duration / 2 + t_r / 2 - D
```

> **`host_ts` marks a READ-OUT instant — measured, not assumed.** An earlier revision of this
> section warned that `delta` was unknown and uncertain "by about one `exposure_duration`". It has
> since been measured on an iPhone 17 Pro, and that is exactly what it turned out to be. The type-11
> record says so on the wire: `pts_convention = readout_instant` (5). It shipped as `first_row_start`
> until 2026-08-03; if you pinned that value, the model above is the correction.
>
> The method: the residual phase of a driven LED cannot give the anchor on its own (the anchor and
> the LED's own phase are inseparable), but it *can* give the anchor's **derivative with respect to
> exposure**, because the LED's phase does not depend on the camera's exposure. Measured slope
> +3191 rad/s against +πf = +3142 predicted for a read-out instant, with a per-block spread of
> 0.009 rad and the neighbouring hypothesis a full πf away. So `dc/dE = 1`.
>
> **The consequence that matters: the anchor moves one-for-one with the exposure.** Under
> auto-exposure it therefore changes *every frame*, so use the per-frame `exposure_us` from the
> type-5 record (revision 5). A fixed per-format offset is wrong by however much the exposure moves.
> `exposure_us` is populated on **both** capture paths — on ARKit it comes from
> `ARCamera.exposureDuration` and is exact (`exposure_age_ms = 0`), unlike the focus fields beside
> it, which really are AVCapture-only. A take whose `exposure_us` column is empty throughout predates
> this and cannot be row-corrected; only its row *differences* are recoverable.
>
> **`D` is a per-format constant and is not decomposable.** It contains whichever read instant is
> meant — the first row leaving the sensor (`D = 0`) or the last (`D = t_r`) — plus any per-format
> pipeline latency. An attempt to separate them by comparing formats with different `t_r` was
> **refuted by its own data**: three format pairs could not be fitted by any single model, best
> residual 0.520 rad against 0.02 rad of measurement noise. Formats differ by more than their
> readout time, and once that is true `k·t_r(format)` and a per-format offset are the same function
> of format — so no number of further comparisons can separate them. This is a limit of the
> approach, not of the data, and it is not worth retrying.
>
> `D` being *constant per format* is the good case: any pipeline that estimates a camera-to-clock or
> camera-to-IMU offset absorbs it. What such a pipeline cannot absorb is the exposure term, because
> that varies frame to frame — and that term is now known exactly.

**Row-to-row and frame-to-frame differences are exact**; only absolute alignment to an external
clock needs `D`. The difference between two rows of the same frame is `(k2 - k1)/(H-1) * t_r` with
nothing unknown in it, and two frames' rows differ by that plus the difference of their `host_ts`
and half the difference of their exposures.

Using `host_ts` alone (i.e. `t_anchor = host_ts`, `alpha = 0`) biases you by `exposure/2 - t_r/2 + D`
— systematic rather than noise, and at 2160p60 the `t_r/2` term alone is 1.2 ms.

**`alpha` runs along `readout_direction`, and the axis may be x.** The sweep is fixed to the sensor,
but the app rotates the delivered buffer, so what the sensor reads as "rows" can arrive as *columns*.
In **delivered-image pixel coordinates** — origin top-left, `x` right, `y` down, the same frame the
intrinsics `ox`/`oy` are expressed in — with `W`,`H` the delivered width and height:

| `readout_direction` | swept first | `alpha` at pixel (x, y) | divide by |
|---|---|---|---|
| `+y` | top edge | `y / (H-1)` | **H** |
| `-y` | bottom edge | `(H-1-y) / (H-1)` | **H** |
| `+x` | left edge | `x / (W-1)` | **W** |
| `-x` | right edge | `(W-1-x) / (W-1)` | **W** |

The trap is the last column: when the direction is `±x` the swept lines are **columns**, so the
divisor is `W`, not `H`, and a pixel's readout time depends on its `x` and not its `y` at all.
Concretely, when the value is `derived` this app reports `+y` when it applies no rotation, `-x` at 90 degrees, `-y` at 180 and
`+x` at 270.

**What is measured and what is assumed.** `exposure_duration` is the device's own value — on
AVCapture sampled asynchronously (so `exposure_age` says how stale), on ARKit exact and `age 0`.
`readout_time_s` is Apple's own `CameraFrameReadoutTime` for that capture format, present only when
the readout probe is enabled — `readout_provenance = absent` otherwise, which is a valid state
meaning nobody has probed that format, and the *expected* state on recent iPhones, which stopped
writing that metadata (§5.3).

**Which instant `host_ts` names is now measured, but only partly.** It is a read-out instant that
moves one-for-one with exposure, so `t_anchor = host_ts − exposure_duration − D`. What remains
unknown is `D`, a **constant per format** — and unlike the old one-`exposure_duration` uncertainty,
a constant is the benign kind: it is absorbed by any camera↔clock offset you calibrate, and it
cancels exactly in every row-to-row and frame-to-frame difference. The part that used to be silently
wrong — an anchor that drifts with the shutter — is the part that is now pinned. See §5.3 for the
measurement and for why `D` cannot be decomposed further.

### 9.2b Which of these are instants, and which are durations

Everything in the file is seconds, but they are not all the same kind of number, and mixing them is
the easy mistake:

| Column | Kind | Axis |
|---|---|---|
| `host_ts` | **instant** | the phone's monotonic host clock — the axis video PTS, IMU, pose and depth all share |
| `unix_ts` | **instant** | wall clock, the same instant expressed on the RTCP/NTP axis |
| `t` | **instant** | the *exported movie's* playback timeline, not a clock (§9.1) |
| `exposure_duration`, `readout_time` | **duration** | lengths of an interval — not on any timeline |
| `lens_age`, `focus_mode_age`, `adjusting_age`, `exposure_age` | **duration** | offsets *back from this row's* `host_ts` |

So there is nothing to convert. The durations are elapsed seconds on the same clock `host_ts` is
expressed in, which is what makes them directly composable:

```
report_instant   = host_ts - exposure_age          # when the shutter value was actually reported
t_anchor         = host_ts - exposure_duration - D # D: per-format constant, see §5.3
exposure_start(n)= t_anchor + alpha(n) * readout_time
t_frame          = t_anchor + readout_time/2 + exposure_duration/2
                 = host_ts - exposure_duration/2 + readout_time/2 - D
```

> **These are the §9.2a formulas, and they are the only correct ones.** This block previously
> carried the pre-`readout_instant` form — `host_ts + alpha·readout_time` and
> `host_ts + readout_time/2 + exposure_duration/2` — which drops `D` and has the **wrong sign on the
> exposure term**. Under auto-exposure that error moves frame to frame, so it appears in a fit as
> noise rather than as a constant offset a calibration can absorb: up to ~16 ms at 60 fps. If you
> implemented from this table rather than from §9.2a, re-derive.

The one that is *not* interchangeable is `t`: it is an instant, but on the container's timeline
rather than a clock, so never mix it with the durations above. Use `host_ts`.

### 9.3 Intrinsics resolution vs. video resolution

`width` / `height` in an intrinsics record are the resolution **the intrinsics are expressed in** —
the frames the camera actually delivered — and as of app 1.5 that is also the resolution of
`video.mov`. The app never upscales: the encoder and the recording writer are both sized from the
format capture resolved to, on the ARKit and AVCapture paths alike, and the settings screen offers
only formats the active mode can actually produce. So the size you pick, the size that is captured,
and the size in the file are one and the same.

This matters because several things restrict which formats exist — ARKit pose mode, LiDAR depth,
Multi-Cam, and 10-bit (HDR / Log) color pipelines. Previously a restricted mode still encoded at the
*requested* size, upscaling from smaller source frames and leaving the intrinsics in a different
pixel frame from the video. That no longer happens.

**Older takes (app ≤1.4):** do not assume the two agree. Check the intrinsics `width`/`height`
against the video's dimensions and, if they differ, scale `fx`, `fy`, `ox`, `oy` by the ratio — they
all scale linearly. The type-11 `format` record (§5.3, protocol 2.1) carries the delivered geometry
if you want to confirm per take rather than infer.

Scaling intrinsics to the image you are actually using remains the right habit regardless: it is
already required for depth (§6 — scale by `depth_width / video_width`), and it costs nothing when
the ratio is 1.
