import os

from setuptools import find_packages, setup

package_name = "robofacts_ros2"


def files_under(subdir: str) -> list:
    """Every file below <package>/<subdir>, as package_data paths. Listed explicitly
    because setuptools' package_data globs do not recurse (`**` is one level)."""
    out = []
    for folder, _, names in os.walk(os.path.join(package_name, subdir)):
        for name in names:
            if "__pycache__" in folder or name.endswith(".pyc"):
                continue
            out.append(os.path.relpath(os.path.join(folder, name), package_name))
    return sorted(out)


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test", "test.*"]),
    # Everything the package needs at run time that is not a .py file: the ROS 2 gate
    # checks and their bash helper, the robot adapters' checks, the web interface's
    # templates, the clean-slate tool and the ROS 2 experiment template.
    package_data={
        package_name: [
            "checks/*.sh", "checks/README.md", "lib/*.sh", "preflight/lib/*.sh",
            "adapters/*/checks/*.sh", "adapters/*/*.yaml",
            "ui/templates/*.html", "tools/*.sh",
            *files_under("skeleton"),
        ],
    },
    include_package_data=True,
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools", "robofacts", "pyyaml", "numpy"],
    extras_require={"ui": ["flask"], "test": ["pytest", "flask"]},
    python_requires=">=3.10",
    zip_safe=False,
    maintainer="Alejandro Lorite Mora",
    maintainer_email="a.lorite.mora@gmail.com",
    description="The ROS 2 backend of RoboFACTS: ros2 launch, ros2 bag record, the bag readers, "
                "the ROS 2 gate checks, the robot adapters and the web interface.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            # The command of the general package, also where `ros2 run` looks for it.
            "trials = robofacts.cli:main",
        ],
    },
)
