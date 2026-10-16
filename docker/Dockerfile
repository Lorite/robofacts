# The quickstart and CI image: the official ROS 2 Humble image plus what the
# example project needs. Not for a project's own use: there, the package is
# installed into the project's ROS 2 workspace (colcon) or environment (pip).
#
#   docker build -t trials-quickstart -f docker/Dockerfile .
#   docker run --rm trials-quickstart                 # the whole loop on the example
#   docker run --rm trials-quickstart pytest -q test  # the test suite
# Stage 1: the dependencies only (`--target deps`), for CI runs that build the
# package themselves with colcon.
FROM ros:humble AS deps

RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-humble-demo-nodes-cpp \
        ros-humble-rosbag2 \
        ros-humble-rosbag2-py \
        ros-humble-rosbag2-storage-mcap \
        ros-humble-rmw-cyclonedds-cpp \
        python3-pip \
        python3-yaml \
        python3-numpy \
        python3-flask \
        python3-pytest \
        git \
    && rm -rf /var/lib/apt/lists/*

# The gate refuses to run under an unknown DDS configuration (it checks the kernel
# receive buffer against what the configuration demands), so the image states one.
RUN printf '%s\n' \
        '<?xml version="1.0" encoding="UTF-8"?>' \
        '<CycloneDDS xmlns="https://cdds.io/config"><Domain id="any"><General><AllowMulticast>default</AllowMulticast></General></Domain></CycloneDDS>' \
        > /etc/cyclonedds.xml
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    CYCLONEDDS_URI=file:///etc/cyclonedds.xml

# Stage 2: the package installed with pip, and the quickstart as the default command.
FROM deps AS quickstart
WORKDIR /opt/robofacts
COPY . .
RUN pip install --no-cache-dir . && trials --version

ENTRYPOINT ["/bin/bash", "-c", "source /opt/ros/humble/setup.bash && exec \"$@\"", "--"]
CMD ["./quickstart.sh"]
