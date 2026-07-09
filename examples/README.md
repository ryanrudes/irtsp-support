# iRTSP example clients

Minimal, dependency-light clients for the iRTSP **IMU / VIO side-channel** (default TCP `8555`).
Each connects, reads the length-prefixed JSON handshake, then decodes the flat 64-byte records.
Read the **[integration guide](../INTEGRATION.md)** for the full wire format, all record types,
and the video↔odometry clock model.

| File | Language | What it does | Run |
|---|---|---|---|
| [`irtsp_client.py`](irtsp_client.py) | Python 3 (stdlib only) | Handshake + decode every record type, print | `python3 irtsp_client.py <iphone-ip>` |
| [`irtsp_client.cpp`](irtsp_client.cpp) | C++17 / POSIX sockets | Same, single file, no deps | `g++ -std=c++17 -O2 irtsp_client.cpp -o irtsp_client && ./irtsp_client <iphone-ip>` |
| [`irtsp_ros2_node.py`](irtsp_ros2_node.py) | ROS 2 (rclpy) | Bridge → `sensor_msgs/Imu`, `sensor_msgs/NavSatFix`, `geometry_msgs/PoseStamped` | `python3 irtsp_ros2_node.py --ros-args -p host:=<iphone-ip>` |

The **video** is standard RTSP — `rtsp://<iphone-ip>:8554/live` — so use ffmpeg, VLC, GStreamer,
or live555. It lines up with the records above via the shared clock (see the guide, §4); once
the first RTCP Sender Report arrives, video frames carry a wall-clock time on the exact same
axis as each record's `unix_ts`, so there is no time offset to estimate.
