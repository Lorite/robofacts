# Development

This page is for people who change RoboFACTS itself: how to run its tests, what the continuous integration checks, and how to build this site.

## Run the tests

```bash
cd robofacts && python3 -m pytest -q test                      # the general package alone: needs no ROS
python3 -m pytest -q robofacts_ros2/test robofacts/test        # both packages, with both on the Python path
./quickstart.sh                                                # with ROS 2 Humble sourced and both packages installed
```

`robofacts/test` runs with the general package alone. Its tests that exercise the core together with the ROS 2 backend are marked `ros2`, and pytest skips them where `robofacts_ros2` is not installed. `robofacts/test` runs against `robofacts/example_project/`, and `robofacts_ros2/test` against `robofacts_ros2/example_project/`.

## Continuous integration

`.github/workflows/ci.yaml` has three jobs. `core-no-ros` installs only `robofacts` on plain Python images (`python:3.10-slim` to `python:3.14-slim`, one job per version, where the tests that need git are skipped), runs its tests, runs the example with `robofacts/quickstart.sh` from plan to coverage index, runs the tests of `examples/cflib_hover` without a drone, and does the same with a generated project. `colcon-humble` builds both packages on `ros:humble`, runs both test suites and the example campaign. `docker-image` builds the quickstart image.

`.github/workflows/release.yaml` publishes `robofacts` on PyPI when a `v*` tag is pushed. `.github/workflows/docs.yaml` builds this site on every push and pull request, and publishes it from `main`.

## Build this site

The pages are in `docs/`, the navigation is in `mkdocs.yml`.

```bash
python3 -m venv .venv-docs && .venv-docs/bin/pip install -r docs/requirements.txt
.venv-docs/bin/mkdocs serve             # a live preview at http://127.0.0.1:8000
.venv-docs/bin/mkdocs build --strict    # what the docs workflow runs
```

Pages link to each other with wikilinks, as in Obsidian: `[[campaigns]]`, `[[campaigns|a campaign]]` or `[[gate#Hard items|hard items]]`. A link resolves by file name, so every page has a unique file name. The `obsidian-bridge` plugin turns the wikilinks into links, and `mkdocs build --strict` fails on a link that resolves to no page. You can also open the `docs/` folder as an Obsidian vault.

Three reference pages are generated from the code: [[commands]], [[lint-rules|the lint rules]] and [[gate-checks|the gate checks]]. Do not edit them by hand. After a change to a command, a lint rule or a gate check, regenerate them:

```bash
python3 tools/gen_docs_reference.py            # rewrite the generated pages
python3 tools/gen_docs_reference.py --check    # exit 1 when a committed page is stale
```

The colcon job runs the check as a test, so a stale page fails the build.
