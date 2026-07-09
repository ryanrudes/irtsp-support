// Minimal iRTSP odometry client (C++17, POSIX sockets, no dependencies).
//
// Connects to the iRTSP IMU / VIO side-channel (default TCP 8555), reads the length-prefixed
// JSON handshake, then decodes the flat stream of fixed 64-byte little-endian records.
// Video is plain RTSP (use ffmpeg/live555/GStreamer) and aligns with these records via the
// shared clock — see the integration guide:
//   https://github.com/ryanrudes/irtsp-support/blob/main/INTEGRATION.md
//
// Build:  g++ -std=c++17 -O2 irtsp_client.cpp -o irtsp_client
// Run:    ./irtsp_client <iphone-ip> [port]
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

// TCP read() may return short; loop until n bytes are in.
static bool read_exact(int fd, void* buf, size_t n) {
    auto* p = static_cast<uint8_t*>(buf);
    for (size_t got = 0; got < n; ) {
        ssize_t k = ::read(fd, p + got, n - got);
        if (k <= 0) return false;
        got += static_cast<size_t>(k);
    }
    return true;
}

// Little-endian field readers — portable regardless of host byte order.
static uint16_t rd_u16(const uint8_t* p) { return uint16_t(p[0]) | uint16_t(p[1]) << 8; }
static uint32_t rd_u32(const uint8_t* p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
static uint64_t rd_u64(const uint8_t* p) {
    uint64_t v = 0; for (int i = 7; i >= 0; --i) v = (v << 8) | p[i]; return v;
}
static float  rd_f32(const uint8_t* p) { uint32_t v = rd_u32(p); float  f; std::memcpy(&f, &v, 4); return f; }
static double rd_f64(const uint8_t* p) { uint64_t v = rd_u64(p); double d; std::memcpy(&d, &v, 8); return d; }

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "usage: %s <iphone-ip> [port]\n", argv[0]); return 1; }
    const char* host = argv[1];
    const char* port = (argc > 2) ? argv[2] : "8555";

    addrinfo hints{}; hints.ai_family = AF_UNSPEC; hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    if (getaddrinfo(host, port, &hints, &res) != 0) { std::perror("getaddrinfo"); return 1; }
    int fd = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0 || ::connect(fd, res->ai_addr, res->ai_addrlen) != 0) { std::perror("connect"); return 1; }
    freeaddrinfo(res);

    // 1) Handshake: u32 LE length + UTF-8 JSON.
    uint8_t len4[4];
    if (!read_exact(fd, len4, 4)) return 1;
    uint32_t hlen = rd_u32(len4);
    std::string hs(hlen, '\0');
    if (!read_exact(fd, hs.data(), hlen)) return 1;
    std::printf("handshake (%u bytes):\n%.*s\n\n", hlen,
                int(hs.size() > 700 ? 700 : hs.size()), hs.c_str());

    // 2) Records: read exactly 64 bytes; dispatch on byte[0]. seq (u16) wraps; use it for drops.
    uint8_t r[64];
    while (read_exact(fd, r, 64)) {
        uint8_t  type    = r[0];
        uint16_t seq     = rd_u16(r + 2);
        double   host_ts = rd_f64(r + 8);
        double   unix_ts = rd_f64(r + 16);
        (void)unix_ts;   // == RTP RTCP-SR NTP axis; use it to align against video

        switch (type) {
            case 1: {  // fused device motion
                float gx = rd_f32(r+24), gy = rd_f32(r+28), gz = rd_f32(r+32);
                float ax = rd_f32(r+36), ay = rd_f32(r+40), az = rd_f32(r+44);
                float qx = rd_f32(r+48), qy = rd_f32(r+52), qz = rd_f32(r+56), qw = rd_f32(r+60);
                std::printf("[%5u] imu  t=%.4f gyro=(%+.3f,%+.3f,%+.3f)rad/s "
                            "accel=(%+.3f,%+.3f,%+.3f)g q=(%+.3f,%+.3f,%+.3f,%+.3f)\n",
                            seq, host_ts, gx, gy, gz, ax, ay, az, qx, qy, qz, qw);
                break;
            }
            case 5:  // camera intrinsics (video pixels)
                std::printf("[%5u] intr fx=%.1f fy=%.1f c=(%.1f,%.1f) size=%dx%d\n",
                            seq, rd_f32(r+24), rd_f32(r+28), rd_f32(r+32), rd_f32(r+36),
                            int(rd_f32(r+40)), int(rd_f32(r+44)));
                break;
            case 6:  // GNSS (negatives = invalid)
                std::printf("[%5u] gnss %.6f,%.6f alt=%.1fm spd=%.2fm/s crs=%.1fdeg\n",
                            seq, rd_f64(r+24), rd_f64(r+32), rd_f32(r+40), rd_f32(r+52), rd_f32(r+56));
                break;
            case 7:  // barometric altitude
                std::printf("[%5u] alt  rel=%+.2fm press=%.2fkPa\n", seq, rd_f32(r+24), rd_f32(r+28));
                break;
            case 8:  // compass heading (degrees; negative = invalid)
                std::printf("[%5u] head true=%.1f mag=%.1f +/-%.1fdeg\n",
                            seq, rd_f32(r+24), rd_f32(r+28), rd_f32(r+32));
                break;
            case 9: {  // ARKit 6DOF world pose
                float tx = rd_f32(r+24), ty = rd_f32(r+28), tz = rd_f32(r+32), track = rd_f32(r+36);
                float qx = rd_f32(r+48), qy = rd_f32(r+52), qz = rd_f32(r+56), qw = rd_f32(r+60);
                std::printf("[%5u] pose t=(%+.3f,%+.3f,%+.3f)m track=%d q=(%+.3f,%+.3f,%+.3f,%+.3f)\n",
                            seq, tx, ty, tz, int(track), qx, qy, qz, qw);
                break;
            }
            default:
                std::printf("[%5u] type %u\n", seq, type);
        }
    }
    ::close(fd);
    return 0;
}
