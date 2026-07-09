#!/usr/bin/env python3
"""
Minimal iRTSP -> ROS 2 bridge (rclpy).

Connects to the iRTSP IMU / VIO side-channel (default TCP 8555), decodes the 64-byte records,
and republishes them as standard ROS 2 messages:

    /irtsp/imu   sensor_msgs/Imu          (type 1: gyro rad/s, accel converted g->m/s^2, orientation quat)
    /irtsp/fix   sensor_msgs/NavSatFix    (type 6: GNSS)
    /irtsp/pose  geometry_msgs/PoseStamped(type 9: ARKit 6DOF world pose, meters)

Message stamps use the record's `unix_ts` — iRTSP's wall-clock axis, which is identical to the
RTP RTCP Sender-Report NTP timeline. So if you also stamp your RTSP video on the sender's NTP
clock (GStreamer rtpbin ntp-sync, ffmpeg wallclock, live555 presentationTime), the messages
here line up with the video with no offset to estimate. See the integration guide:
    https://github.com/ryanrudes/irtsp-support/blob/main/INTEGRATION.md

Run:  python3 irtsp_ros2_node.py --ros-args -p host:=<iphone-ip>
      (or drop into a package and `ros2 run`.)  Tested with rclpy (ROS 2 Humble+).
"""
import json
import socket
import struct
import threading

import rclpy
from rclpy.node import Node
from builtin_interfaces.msg import Time
from sensor_msgs.msg import Imu, NavSatFix
from geometry_msgs.msg import PoseStamped

G = 9.80665  # g -> m/s^2


def read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("stream closed")
        buf += chunk
    return bytes(buf)


def to_stamp(unix_ts):
    t = Time()
    t.sec = int(unix_ts)
    t.nanosec = int(round((unix_ts - int(unix_ts)) * 1e9))
    return t


class IRTSPBridge(Node):
    def __init__(self):
        super().__init__("irtsp_bridge")
        self.declare_parameter("host", "192.168.1.100")
        self.declare_parameter("port", 8555)
        self.declare_parameter("frame_id", "irtsp")
        self.host = self.get_parameter("host").value
        self.port = int(self.get_parameter("port").value)
        self.frame = self.get_parameter("frame_id").value

        self.pub_imu = self.create_publisher(Imu, "irtsp/imu", 50)
        self.pub_fix = self.create_publisher(NavSatFix, "irtsp/fix", 10)
        self.pub_pose = self.create_publisher(PoseStamped, "irtsp/pose", 50)

        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while rclpy.ok():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                (hlen,) = struct.unpack("<I", read_exact(sock, 4))
                hs = json.loads(read_exact(sock, hlen))
                self.get_logger().info(f"connected to {self.host}:{self.port}; streams={hs.get('streams')}")
                self._loop(sock)
            except Exception as e:  # noqa: BLE001 - keep the bridge alive across drops
                self.get_logger().warning(f"{e}; reconnecting in 1s")
                rclpy.spin_once(self, timeout_sec=1.0)

    def _loop(self, sock):
        while rclpy.ok():
            r = read_exact(sock, 64)
            typ = r[0]
            _host_ts, unix_ts = struct.unpack_from("<dd", r, 8)

            if typ == 1:  # fused device motion
                gx, gy, gz, ax, ay, az, qx, qy, qz, qw = struct.unpack_from("<10f", r, 24)
                m = Imu()
                m.header.stamp = to_stamp(unix_ts)
                m.header.frame_id = self.frame
                m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z = gx, gy, gz
                m.linear_acceleration.x = ax * G
                m.linear_acceleration.y = ay * G
                m.linear_acceleration.z = az * G
                m.orientation.x, m.orientation.y, m.orientation.z, m.orientation.w = qx, qy, qz, qw
                self.pub_imu.publish(m)

            elif typ == 6:  # GNSS
                lat, lon = struct.unpack_from("<dd", r, 24)
                alt = struct.unpack_from("<f", r, 40)[0]
                m = NavSatFix()
                m.header.stamp = to_stamp(unix_ts)
                m.header.frame_id = self.frame
                m.latitude, m.longitude, m.altitude = lat, lon, alt
                self.pub_fix.publish(m)

            elif typ == 9:  # ARKit 6DOF world pose
                tx, ty, tz, _track = struct.unpack_from("<4f", r, 24)
                qx, qy, qz, qw = struct.unpack_from("<4f", r, 48)
                m = PoseStamped()
                m.header.stamp = to_stamp(unix_ts)
                m.header.frame_id = "map"
                m.pose.position.x, m.pose.position.y, m.pose.position.z = tx, ty, tz
                m.pose.orientation.x, m.pose.orientation.y, m.pose.orientation.z, m.pose.orientation.w = qx, qy, qz, qw
                self.pub_pose.publish(m)
            # types 2,3,5,7,8 (raw gyro/accel, intrinsics, altitude, heading) omitted for brevity


def main():
    rclpy.init()
    node = IRTSPBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
