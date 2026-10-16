import os

from setuptools import find_packages, setup

package_name = "declarative_robot_trials"


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
    # Everything the package needs at run time that is not a .py file: the gate's
    # check scripts and their bash helper, the web UI's templates, the stop and
    # clean-slate tools, the experiment template and the shared config defaults.
    package_data={
        package_name: [
            "preflight/checks/*.sh", "preflight/lib/*.sh", "preflight/README.md",
            "ui/templates/*.html", "ui/static/*", "tools/*.sh",
            *files_under("skeleton"),
        ],
    },
    include_package_data=True,
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools", "pyyaml", "numpy"],
    extras_require={"ui": ["flask"], "test": ["pytest", "flask"]},
    python_requires=">=3.10",
    zip_safe=False,
    maintainer="Alejandro Lorite Mora",
    maintainer_email="a.lorite.mora@gmail.com",
    description="Declarative experiment design for ROS 2 trials: campaigns, a T-0 gate, "
                "a runner, a coverage index, a linter and named analyses.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "trials = declarative_robot_trials.cli:main",
        ],
    },
)
