"""
midi_utils.py — MIDI output port helpers
=========================================
Reusable module. Requires mido and a suitable backend (python-rtmidi).

Example
-------
    from midi_utils import open_midi_port
    import mido

    midi_out = open_midi_port("loopMIDI port")
    midi_out.send(mido.Message("note_on", note=60, velocity=100, channel=0))
    midi_out.close()
"""

import sys

import mido


def open_midi_port(port_name: str) -> mido.ports.BaseOutput:
    """
    Open a MIDI output port by (partial) name, case-insensitive.

    Parameters
    ----------
    port_name : str
        Substring of the desired port name (e.g. "loopMIDI port").

    Returns
    -------
    mido.ports.BaseOutput
        Open MIDI output port. Caller is responsible for closing it.

    Raises
    ------
    SystemExit
        If no matching port is found.
    """
    available = mido.get_output_names()
    match = next((p for p in available if port_name.lower() in p.lower()), None)
    if match is None:
        print(f"MIDI port '{port_name}' nebyl nalezen.", file=sys.stderr)
        print("Dostupné MIDI porty:", available, file=sys.stderr)
        sys.exit(1)
    return mido.open_output(match)
