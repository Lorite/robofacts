# Install

This page installs the `trials` command. Every project needs the general package, and a project on ROS 2 also needs the ROS 2 backend.

## What you need

- Python 3.10 or newer.
- For the general package, nothing else: pip installs its two dependencies, PyYAML and pytest. The linter runs its rules with pytest.
- For the ROS 2 backend: ROS 2 Humble and a colcon workspace.

The general package `robofacts` holds the declarations, the planner, the linter, the coverage index, the runner, the [[gate|T-0 gate]] and the built-in `process` [[backends|backend]], which works without any middleware. The ROS 2 backend is a second package, `robofacts_ros2`.

## From PyPI

```bash
pip install robofacts
trials --help
```

Two optional extras add features:

| Extra | Adds | Installs |
| --- | --- | --- |
| `ui` | the web interface, `trials ui` | Flask |
| `mcp` | the MCP server for agents, `trials mcp` | the MCP Python SDK, version 2 |

```bash
pip install 'robofacts[ui]'          # the web interface
pip install 'robofacts[ui,mcp]'      # the web interface and the MCP server
```

## From a clone

A clone also holds the examples and the quickstart scripts. Install the general package from its folder:

```bash
git clone https://github.com/Lorite/robofacts.git
cd robofacts
pip install './robofacts[ui,mcp]'    # or: pip install ./robofacts, without the extras
```

## Check the install

```bash
trials --version     # prints the version number
trials --help        # lists every command
```

Inside a project, `trials roots` shows which project the command found (see [[projects|Projects]]). Outside a project, every command that needs one stops with an error that says how to create one.

## With the ROS 2 backend

`robofacts_ros2` is not on PyPI, because it needs `rclpy`. Build both packages from a clone in your ROS 2 Humble workspace:

```bash
cd ~/ros2_ws/src && git clone https://github.com/Lorite/robofacts.git
cd ~/ros2_ws
rosdep install --from-paths src --ignore-src -y --skip-keys robofacts
colcon build --packages-select robofacts robofacts_ros2
source install/setup.bash
trials --help                        # or: ros2 run robofacts_ros2 trials --help
```

`robofacts_ros2` declares `robofacts` as a dependency, but `robofacts` has no `package.xml` and no rosdep key. The option `--skip-keys robofacts` tells rosdep to skip it, because colcon builds `robofacts` from the same clone, as a plain Python package and before `robofacts_ros2`.

In an environment where ROS 2 Humble is sourced, pip can also install both packages from a clone: `pip install ./robofacts ./robofacts_ros2`. The quickstart image in `docker/Dockerfile` installs them this way.

## Name the backend of a project

Every project names its backend in its `trials.yaml`: `backend: process` without ROS, `backend: ros2` with it. `trials init` writes the line for you. Its option `--backend` takes the name, and the default is `process` (or `$TRIALS_BACKEND` when you set it). Without the line, every command stops and says which line to add. See [[backends|Backends]] and [[trials-yaml|trials.yaml]].

## Next steps

- [[quickstart|Quickstart]]: run the whole loop on an example with one command.
- [[first-project|Your first project]]: create a project of your own.
