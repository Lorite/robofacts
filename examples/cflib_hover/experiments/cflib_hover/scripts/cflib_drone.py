"""The drone over cflib, behind a small interface that the hover logic and the gate use.

Everything that touches the radio is here, so the hover logic in cf_hover.py can be tested with
a fake drone. cflib is imported only when a link opens: the tests and the dry run need no cflib.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import yaml

#: The drone's settings, one file for the gate, the reset and the action. $CF_URI overrides `uri`.
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "drone.yaml"


def load_config(path: Optional[Path] = None) -> Dict:
    cfg = yaml.safe_load(Path(path or DEFAULT_CONFIG).read_text(encoding="utf-8")) or {}
    cfg["uri"] = os.environ.get("CF_URI") or cfg["uri"]
    return cfg


class LinkError(RuntimeError):
    """The radio could not be opened or the link did not come up."""


class CflibDrone:
    """One radio link to one Crazyflie. Use as a context manager."""

    def __init__(self, uri: str):
        self.uri = uri
        self.state: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._scf = None
        self._sink: Optional[Callable[[Dict], None]] = None

    # --- the link -------------------------------------------------------------------------
    def __enter__(self) -> "CflibDrone":
        try:
            import cflib.crtp
            from cflib.crazyflie import Crazyflie
            from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
        except ImportError as exc:
            raise LinkError("cflib is not installed in this shell (pip install cflib)") from exc
        cflib.crtp.init_drivers()
        try:
            self._scf = SyncCrazyflie(self.uri, cf=Crazyflie(rw_cache="/tmp/cf_toc_cache"))
            self._scf.open_link()
        except Exception as exc:  # noqa: BLE001 - cflib raises plain exceptions
            first = next((line for line in str(exc).splitlines() if line.strip()), type(exc).__name__)
            raise LinkError(f"cannot open {self.uri}: {first.strip()[:120]} (no Crazyradio, drone "
                            f"off or out of range, or the radio held by another program?)") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self._scf is not None:
            self._scf.close_link()

    @property
    def cf(self):
        return self._scf.cf

    # --- reading --------------------------------------------------------------------------
    def param(self, name: str) -> str:
        return str(self.cf.param.get_value(name))

    def set_param(self, name: str, value) -> None:
        self.cf.param.set_value(name, str(value))

    def has_flow_deck(self) -> bool:
        return self.param("deck.bcFlow2") == "1"

    def start_log(self, sink: Callable[[Dict], None], period_ms: int = 50) -> None:
        """Stream the estimate, the range, the battery and the flow quality into ``sink``."""
        from cflib.crazyflie.log import LogConfig
        self._sink = sink
        conf = LogConfig(name="hover_state", period_in_ms=period_ms)
        for var, kind in (("stateEstimate.x", "float"), ("stateEstimate.y", "float"),
                          ("stateEstimate.z", "float"), ("range.zrange", "uint16_t"),
                          ("pm.vbat", "float"), ("motion.squal", "uint8_t")):
            conf.add_variable(var, kind)

        def on_data(_ts, data, _conf):
            sample = {"x": data["stateEstimate.x"], "y": data["stateEstimate.y"],
                      "z": data["stateEstimate.z"], "range_m": data["range.zrange"] / 1000.0,
                      "vbat": data["pm.vbat"], "squal": data["motion.squal"]}
            with self._lock:
                self.state = sample
            sink(sample)

        conf.data_received_cb.add_callback(on_data)
        self.cf.log.add_config(conf)
        conf.start()

    def latest(self) -> Dict[str, float]:
        with self._lock:
            return dict(self.state)

    def battery_v(self, samples: int = 5, timeout_s: float = 3.0) -> Optional[float]:
        """The median of a few pm.vbat samples, or None."""
        import statistics
        from cflib.crazyflie.log import LogConfig
        values = []
        conf = LogConfig(name="vbat", period_in_ms=100)
        conf.add_variable("pm.vbat", "float")
        conf.data_received_cb.add_callback(lambda _ts, d, _c: values.append(d["pm.vbat"]))
        self.cf.log.add_config(conf)
        conf.start()
        end = time.time() + timeout_s
        while len(values) < samples and time.time() < end:
            time.sleep(0.05)
        conf.stop()
        return statistics.median(values) if values else None

    def reset_estimator(self, timeout_s: float = 10.0) -> bool:
        """Restart the estimator where the drone stands and wait until its variance settles
        (the method of cflib's own examples). False when it does not settle in time."""
        from cflib.crazyflie.log import LogConfig
        self.set_param("kalman.resetEstimation", 1)
        time.sleep(0.1)
        self.set_param("kalman.resetEstimation", 0)
        history = {k: [] for k in ("kalman.varPX", "kalman.varPY", "kalman.varPZ")}
        conf = LogConfig(name="kalman_var", period_in_ms=100)
        for name in history:
            conf.add_variable(name, "float")
        settled = threading.Event()

        def on_data(_ts, data, _conf):
            for name, values in history.items():
                values.append(data[name])
                del values[:-10]
            if all(len(v) == 10 and max(v) - min(v) < 0.001 for v in history.values()):
                settled.set()

        conf.data_received_cb.add_callback(on_data)
        self.cf.log.add_config(conf)
        conf.start()
        ok = settled.wait(timeout_s)
        conf.stop()
        return ok

    # --- commanding -----------------------------------------------------------------------
    def arm(self, on: bool) -> None:
        self.cf.platform.send_arming_request(on)

    def takeoff(self, height_m: float, duration_s: float) -> None:
        self.cf.high_level_commander.takeoff(height_m, duration_s)

    def climb(self, dz_m: float, duration_s: float) -> None:
        self.cf.high_level_commander.go_to(0.0, 0.0, dz_m, 0.0, duration_s, relative=True)

    def land(self, height_m: float, duration_s: float) -> None:
        self.cf.high_level_commander.land(height_m, duration_s)

    def stop_commander(self) -> None:
        self.cf.high_level_commander.stop()

    def emergency_stop(self) -> None:
        """Cut the motors now. The drone falls."""
        self.cf.loc.send_emergency_stop()


def power_cycle(uri: str) -> None:
    """Cut the STM32 power for 2 s through the radio chip, as the lab's reset does. It clears
    log blocks that a crashed earlier session left streaming, and a stuck flow sensor."""
    from cflib.utils.power_switch import PowerSwitch
    ps = PowerSwitch(uri)
    ps.stm_power_down()
    time.sleep(2.0)
    ps.stm_power_up()
    ps.close()
    time.sleep(3.0)


class JsonlLog:
    """The flight log, one JSON object per line, flushed per line so the recorder sees it."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, kind: str, **fields) -> None:
        line = json.dumps({"t": round(time.time(), 3), "kind": kind, **fields})
        with self._lock:
            try:
                self._f.write(line + "\n")
            except (OSError, ValueError):  # a record never breaks a flight
                pass

    def close(self) -> None:
        with self._lock:
            self._f.close()
