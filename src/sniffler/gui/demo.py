"""Demo mode: the whole window on fake devices, so the UI can be watched and debugged.

Nothing here opens a port or a LabJack. The window says so in its title and status
bar, and its runs go to a separate folder.
"""

from pathlib import Path

from PySide6.QtCore import QSettings

from sniffler.config import AlicatSettings, Settings, TriggerSettings, TtlOutputSettings
from sniffler.fakes import FakeRig, PulseTrain
from sniffler.gui.app import MainWindow
from sniffler.recipe import Recipe, Schedule, Step, Trial, rig_map_from_settings

RUNS_DIRECTORY = Path("runs-demo")
VALVES = {"valve A": 8, "valve B": 9, "valve C": 10, "valve D": 11}
ODORS = {
    "valve A": "ethyl acetate",
    "valve B": "pentyl acetate",
    "valve C": "geosmin",
    "valve D": "benzaldehyde",
}
MFCS = {"carrier flow": 2000.0, "odor flow": 500.0}
CARRIER = 400.0
ODOR = 100.0
# The fake trigger line: the first pulse comes after the wait is visible, then a
# steady train marks the timeline through the trials.
PULSES = PulseTrain(first_seconds=3.0, period_seconds=1.0)


def demo_settings(runs_directory: Path = RUNS_DIRECTORY) -> Settings:
    return Settings(
        labjack_serial=0,
        alicats={
            name: AlicatSettings(port=f"fake:{name}", units={"mass_flow": "SCCM"}) for name in MFCS
        },
        valves=dict(VALVES),
        runs_directory=runs_directory,
        trigger=TriggerSettings(channel=4),
        ttl_output=TtlOutputSettings(channel=5),
    )


def demo_rig() -> FakeRig:
    return FakeRig(MFCS, MFCS, realistic=True, pulse_train=PULSES)


def _step(seconds: float | None, *open_valves: str, odor: float = ODOR) -> Step:
    valves = dict.fromkeys(VALVES, False)
    for valve in open_valves:
        valves[valve] = True
    return Step(seconds, valves, {"carrier flow": CARRIER, "odor flow": odor})


def _odor_trial(name: str, *valves: str) -> Trial:
    """Three pulses of the named valves together, with a lead-in and a tail."""
    steps = [_step(2.0)]
    for _pulse in range(3):
        steps += [_step(0.5, *valves), _step(0.5)]
    steps.append(_step(1.5))
    return Trial(name, tuple(steps))


def demo_recipe() -> Recipe:
    return Recipe(
        name="demo pulses",
        trials=(
            _odor_trial("odor A", "valve A"),
            _odor_trial("odor B", "valve B"),
            _odor_trial("mixture A + B", "valve A", "valve B"),
            Trial("blank", (_step(3.0, odor=0.0),)),
        ),
        schedule=Schedule({"odor A": 2, "odor B": 2, "mixture A + B": 2, "blank": 2}),
        shutdown=_step(None, odor=0.0),
        notes="Demo recipe on fake devices. Nothing here reaches hardware.",
        valve_contents=dict(ODORS),
    )


def demo_window(
    store: QSettings | None = None, runs_directory: Path = RUNS_DIRECTORY
) -> MainWindow:
    settings = demo_settings(runs_directory)
    rig = demo_rig()
    window = MainWindow(
        settings,
        rig_map_from_settings(settings),
        open_labjack=rig.open_labjack,
        open_alicat=rig.open_alicat,
        read_full_scale=rig.read_full_scale,
        store=store if store is not None else QSettings("sniffler", "sniffler-gui-demo"),
        demo=True,
    )
    # Always the demo recipe: a recipe saved for the real rig names devices the demo
    # does not have, and restoring it would leave Start disabled with no clear reason.
    window.show_recipe(demo_recipe())
    return window
