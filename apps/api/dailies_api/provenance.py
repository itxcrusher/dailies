"""Which agent produced an answer.

An answer read back from storage is a claim about a render, made at some point by some
version of an agent. Without the second half of that sentence it is unfalsifiable, and
this project refuses unfalsifiable claims everywhere else: the diagnosis schema rejects a
cause with no evidence, and the visual verdict names the frame it judged.

The gap was not theoretical. SH200 sat on the public board asserting "completed
successfully with no logged errors" at high confidence, with evidence reading "No data
returned" underneath it, because the queries behind it named a metric that does not exist
and misspelled the shot id. The instructions had been fixed since; the board was replaying
an older agent's conclusion and nothing in the payload could tell anyone that.

**A hash of what the agent was told, not a version number a human has to remember to
bump.** A version number is a promise to update it, and the failure mode is a changed
prompt with an unchanged number, which is exactly the state that caused this. The material
here is the model id and the instruction text, so the fingerprint moves when the agent
does, whether or not anyone noticed the agent moved.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

__all__ = ["agent_fingerprint", "fingerprint_of"]

#: Long enough that a collision is not a practical concern, short enough to sit in a log
#: line and in a JSON payload a person may read by hand.
_LENGTH = 12

#: Separates the parts before hashing. A byte that cannot occur in a Python source
#: constant, so no instruction text can impersonate a boundary: without it ("ab", "c") and
#: ("a", "bc") hash identically and two different agents share a fingerprint.
_SEPARATOR = b"\x00"


def fingerprint_of(*parts: str) -> str:
    """A short, stable digest of the material that defines an agent's behaviour."""
    digest = hashlib.sha256(_SEPARATOR.join(part.encode() for part in parts))
    return digest.hexdigest()[:_LENGTH]


@lru_cache(maxsize=1)
def agent_fingerprint() -> str:
    """The fingerprint of the agent this process is running.

    Imported inside the function rather than at module scope. ``visual_qa`` reaches
    ``google.genai`` and this module is imported by the persistence layer, which has to
    stay usable in a process that has no vision dependencies installed. Cached because the
    inputs are module constants and cannot change while the process lives.
    """
    from dailies_api.agent import INVESTIGATOR_INSTRUCTION, INVESTIGATOR_MODEL
    from dailies_api.visual_qa import VISUAL_INSTRUCTION

    return fingerprint_of(INVESTIGATOR_MODEL, INVESTIGATOR_INSTRUCTION, VISUAL_INSTRUCTION)
