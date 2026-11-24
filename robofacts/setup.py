import os

from setuptools import find_packages, setup

package_name = "robofacts"


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
    # Everything the package needs at run time that is not a .py file: the gate's general
    # check scripts and their bash helper, the stop tools, the templates of new projects and
    # the web interface's pages.
    package_data={
        package_name: [
            "preflight/checks/*.sh", "preflight/lib/*.sh", "preflight/README.md", "tools/*.sh",
            "ui/templates/*.html", "ui/static/*",
            *files_under("skeleton"),
        ],
    },
    include_package_data=True,
    install_requires=["pyyaml"],
    # The web interface (trials ui) needs Flask. Everything else runs without it.
    extras_require={"ui": ["flask"], "test": ["pytest", "flask"]},
    python_requires=">=3.10",
    zip_safe=False,
    maintainer="Alejandro Lorite Mora",
    maintainer_email="a.lorite.mora@gmail.com",
    description="RoboFACTS, declarative experiment design for robot trials: campaigns, a T-0 gate, "
                "a runner, a coverage index, a linter and named analyses. Python only: a backend "
                "package supplies how a stack starts and how a trial is recorded.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "trials = robofacts.cli:main",
        ],
    },
)
