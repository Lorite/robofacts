"""The ROS 2 part of the web interface (robofacts.ui): replay in Foxglove, the Foxglove
layouts, the download of a slim bag for the viewer, the clean slate of the rig hosts, and
the pages of Explore and of the viewers.

The general interface finds this module through the backend (the feature ``ui``) and calls
:func:`register` once with what its routes share. ``TEMPLATES`` holds this package's pages
and its ``_viewers.html``, which replaces the general one: the replay links, the layout
chips, the "Viewing" menu entry and the "Kill ROS" button come from there.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Dict, List

from flask import render_template, request, send_file

from robofacts import backend, tools
from robofacts.ui.app import experiment_names, robot_hosts

TEMPLATES = Path(__file__).resolve().parent / "ui" / "templates"


#: The layouts convention lives in the package now (``trial_records``), so
#: ``trials records`` and the UI name the same files. Kept under the old name.
# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #
def foxglove_layouts(experiments_root: Path) -> Dict[str, List[Path]]:
    """The Foxglove layouts each experiment ships: ``experiments/<exp>/config/foxglove*.json``.

    A convention, not a schema: any JSON exported from Foxglove (Layouts -> Export)
    whose name starts with ``foxglove`` (case does not matter) belongs to the bags
    of its experiment.
    """
    out: Dict[str, List[Path]] = {}
    for f in sorted(Path(experiments_root).glob("*/config/*.json")):
        if f.name.lower().startswith("foxglove"):
            out.setdefault(f.parents[1].name, []).append(f)
    return out

def foxglove_link(base_url: str, experiment: str, bag_name: str) -> str:
    """Deep link that opens the laptop's Foxglove on a bag served by this UI.

    Clicked in the operator's browser, so it reaches the laptop's Foxglove even
    though the UI is served from the rig. The URL must end in .mcap.
    """
    return (f"foxglove://open?ds=remote-file&ds.url="
            f"{base_url}/bag/{experiment}/{bag_name}.mcap")

def foxglove_web_link(base_url: str, experiment: str, bag_name: str) -> str:
    """The same bag in Foxglove's web app (app.foxglove.dev, the default org)."""
    return (f"https://app.foxglove.dev/~/view?ds=remote-file&ds.url="
            f"{base_url}/bag/{experiment}/{bag_name}.mcap")


def layouts(root: Path, experiment: str) -> List[str]:
    """The names of the Foxglove layouts an experiment ships, for its pages."""
    return [f.name for f in foxglove_layouts(root).get(experiment, [])]


def template_globals() -> Dict[str, object]:
    """What this package's templates need besides the general interface's globals."""
    return {"fox_link": foxglove_link, "fox_web_link": foxglove_web_link}


def register(ui) -> None:
    """Add the ROS 2 routes to the general interface's app."""
    app, requires_auth, action, root = ui.app, ui.requires_auth, ui.action, ui.root
    error, cache, runs, journal = ui.error, ui.cache, ui.runs, ui.journal

    @app.route("/viewers")
    @requires_auth
    def viewers():
        """How to open the live and recorded views ON THE LAPTOP.

        A page served by the rig cannot spawn RViz on the operator's machine, so
        this hands over a foxglove:// deep link (which the laptop's browser
        resolves to the laptop's Foxglove) and a copy-paste command for RViz.
        Every replayable trial is listed, grouped by experiment with its layout
        beside it: the cap of five per folder hid 20 of 24 trials (2026-10-15).
        """
        groups = []
        for name in experiment_names(root()):
            data = cache.get(name)
            replayable = [r for r in data.records if r.slim_dir is not None]
            groups.append({"experiment": name, "records": replayable, "layouts": data.layouts,
                           "total": len(data.records),
                           "last": replayable[0].started if replayable else ""})
        groups.sort(key=lambda g: g["last"], reverse=True)
        return render_template("viewers.html", groups=groups, crumbs=[("viewing", None)])

    @app.route("/layout/<experiment>/<path:name>")
    @requires_auth
    def layout_file(experiment: str, name: str):
        """A per-experiment Foxglove layout, as a download for Layouts -> Import."""
        for f in foxglove_layouts(root()).get(experiment, []):
            if f.name == name:
                return send_file(f, mimetype="application/json", as_attachment=True, download_name=f.name)
        return {"error": f"no Foxglove layout named {name!r} in {experiment}"}, 404

    @action("/kill_ros")
    @requires_auth
    def kill_ros():
        """Clean slate on the rig hosts — recovery BETWEEN trials.

        Deliberately NOT a way to stop a flying drone: killing the stack takes
        away the commander and the position feed, which leaves the aircraft to
        its firmware failsafe rather than landing it. The e-stop is the tool
        for that, so this refuses while a run is executing unless the operator
        overrides, and the page says which one to reach for.
        """
        if runs.busy() and request.form.get("force") != "yes":
            return error(
                "A trial is running. Killing ROS now would remove the drone's commander "
                "and position feed mid-flight — that is not a stop, it is an abandonment. "
                "Use E-STOP to stop the aircraft, or Abort to end the campaign after this "
                "trial; then clean the slate.", 409)
        results = []
        for spec in robot_hosts(app):
            workspace = app.config["REPO_ROOT"]
            argv = ["bash", "-lc", f"cd {shlex.quote(str(workspace))} && {tools.remote_invocation('kill_ros.sh')}"]
            if spec["host"]:
                from robofacts.remote_exec_utils import RemoteTarget, wrap_command
                host_workspace = Path(spec.get("workspace") or workspace)
                ros_setup, ws_setup = backend.active(root()).host_setup(
                    {k: spec[k] for k in ("ros_setup", "ws_setup") if spec.get(k)}, host_workspace)
                target = RemoteTarget(host=spec["host"], container=spec["container"],
                                      workspace=host_workspace,
                                      ros_setup=ros_setup, ws_setup=ws_setup,
                                      shell=spec["shell"])
                argv = wrap_command(argv, target)
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=90)
                results.append((spec["host"] or "local", proc.returncode == 0,
                                (proc.stdout + proc.stderr).strip()[-600:]))
            except subprocess.TimeoutExpired:
                results.append((spec["host"] or "local", False, "timed out after 90 s"))
        ok = all(r[1] for r in results) and bool(results)
        journal.add("kill-ros", f"clean slate on {len(results)} host(s): "
                                f"{'OK' if ok else 'SOME FAILED'}")
        output = "\n\n".join(f"=== {host} ===\n{text}" for host, _, text in results) \
            or "no hosts in robots.yaml"
        return render_template("stop.html", action="kill ROS", robot="all rig hosts",
                               ok=ok, output=output, crumbs=[("kill ROS", None)])

    @app.route("/bag/<experiment>/<name>.mcap")
    def serve_bag(experiment: str, name: str):
        """Serve a SLIM bag so the laptop's Foxglove can stream it over the tunnel.

        Only slim bags are served, and only by resolving the name against
        the ones that actually exist — never by joining user input onto a path,
        which is how a `..` traversal would get out of the tree. Slim bags are a
        few MB, so streaming beats copying; the heavy originals stay put.

        Deliberately NOT behind requires_auth, with CORS and Range enabled:
        Foxglove (desktop or app.foxglove.dev) fetches this URL itself and
        cannot present the Basic credentials, so an authed route can never be
        opened from the viewer. Read-only data on a localhost/tunnel service;
        every state-changing route stays authed.
        """
        for slim_root in (root() / experiment / "bags_slim",
                          root() / experiment / "bags_issues_slim",
                          root() / experiment / "bags_scratch",
                          root() / experiment / "bags_scratch_slim"):
            if not slim_root.is_dir():
                continue
            for candidate in slim_root.iterdir():
                if candidate.name != name or not candidate.is_dir():
                    continue
                # Only slim copies are served from bags_scratch: a fat scratch bag is
                # hundreds of MB, and a viewer streaming it through the UI is a mistake.
                if slim_root.name.startswith("bags_scratch") and not name.endswith("_slim"):
                    continue
                splits = sorted(candidate.glob("*.mcap"))
                if splits:
                    resp = send_file(splits[0], mimetype="application/octet-stream",
                                     download_name=f"{name}.mcap", conditional=True)
                    resp.headers["Access-Control-Allow-Origin"] = "*"
                    return resp
        return {"error": f"no slim bag named {name!r} in {experiment}"}, 404
