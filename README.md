# iRTSP — support & developer resources

Support pages and integration docs for **iRTSP**, the iOS app that turns your iPhone into a
standard RTSP camera server with time-synced IMU, GPS, LiDAR depth, and ARKit pose — built for
VIO, robotics, and AR. (This repo holds only the public support/docs site, not the app source.)

- **[Support & FAQ](https://ryanrudes.github.io/irtsp-support/)**
- **[Privacy policy](https://ryanrudes.github.io/irtsp-support/privacy.html)**
- **[Integration guide](INTEGRATION.md)** — exact wire formats, the shared clock model, and
  how the **video and odometric streams stay synchronized** (RTP/RTCP ↔ IMU/pose/depth).
- **Example clients** ([examples/](examples/)) —
  [Python](examples/irtsp_client.py) ·
  [C++](examples/irtsp_client.cpp) ·
  [ROS 2](examples/irtsp_ros2_node.py)
