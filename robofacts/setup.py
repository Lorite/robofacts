import os

from setuptools import find_packages, setup

package_name = "robofacts"
# realpath, not abspath: a symlink install runs a link to this file from a build folder,
# where no README.md lies (2026-11-26, a lab's build failed).
HERE = os.path.dirname(os.path.realpath(__file__))

# The PyPI page. It is this folder's README, not the repository's: the repository's README
# also describes the middleware backend, which this package must not name.
with open(os.path.join(HERE, "README.md"), encoding="utf-8") as readme:
    long_description = readme.read()


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
    version="0.1.1",
    packages=find_packages(exclude=["test", "test.*"]),
    # Everything the package needs at run time that is not a .py file: the gate's general
    # check scripts and their bash helper, the stop tools, the templates of new projects and
    # the web interface's pages.
    package_data={
        package_name: [
            "lint_pytest.ini", "preflight/checks/*.sh", "preflight/lib/*.sh", "preflight/README.md", "tools/*.sh",
            "ui/templates/*.html", "ui/static/*",
            *files_under("skeleton"),
        ],
    },
    include_package_data=True,
    # pytest: the linter's rules are pytest tests (robofacts/lint.py), and trials lint and
    # trials validate run them. Without it a plain pip install fails at the first lint.
    install_requires=["pyyaml", "pytest"],
    # The web interface (trials ui) needs Flask, the MCP server (trials mcp) the MCP Python
    # SDK, version 2 (its server class changed name from version 1). Everything else runs
    # without them.
    extras_require={"ui": ["flask"], "mcp": ["mcp>=2.3,<3"],
                    "test": ["pytest", "flask", "mcp>=2.3,<3"]},
    python_requires=">=3.10",
    zip_safe=False,
    maintainer="Alejandro Lorite Mora",
    maintainer_email="a.lorite.mora@gmail.com",
    description="RoboFACTS, declarative experiment design for robot trials: campaigns, a T-0 gate, "
                "a runner, a coverage index, a linter and named analyses. Python only: a backend "
                "package supplies how a stack starts and how a trial is recorded.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Lorite/robofacts",
    project_urls={
        "Source": "https://github.com/Lorite/robofacts",
        "Documentation": "https://lorite.github.io/robofacts/",
        "Changelog": "https://github.com/Lorite/robofacts/blob/main/CHANGELOG.md",
        "Issues": "https://github.com/Lorite/robofacts/issues",
    },
    keywords=["robotics", "multi-robot systems", "design of experiments", "reproducibility",
              "experiment management", "simulation", "hardware trials"],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Scientific/Engineering",
    ],
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "trials = robofacts.cli:main",
        ],
    },
)
