"""Human-in-the-loop gates for pipeline checkpoints.

Gates create a conversational feedback loop within the same CLI session.
The human can provide feedback (which is passed back to the agent via session resume),
and only advances the pipeline when the human types "PNS" (Proceed to Next Step).
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

PNS_KEYWORD = "PNS"
APPROVE_KEYWORDS = frozenset({"pns", "approve"})
REJECT_KEYWORDS = frozenset({"reject", "quit", "abort", "stop"})
FILE_POLL_INTERVAL_SECONDS = 5


class GateResult(enum.Enum):
    PROCEED = "proceed"
    REJECTED = "rejected"
    CHECKPOINTED = "checkpointed"


def _gate_decision(human_input: str) -> GateResult | None:
    if human_input.upper() == PNS_KEYWORD or human_input.lower() in APPROVE_KEYWORDS:
        return GateResult.PROCEED
    if human_input.lower() in REJECT_KEYWORDS:
        return GateResult.REJECTED
    return None


class Gate:
    """Conversational human-in-the-loop gate.

    After an agent completes its prompts, the gate:
    1. Shows the agent's output to the human
    2. Reads human input
    3. If PNS → pipeline proceeds to next agent
    4. If reject/quit/abort → pipeline stops
    5. Otherwise → passes feedback to the same CLI session via resume_fn,
       shows the agent's response, and loops back to step 2
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self._gate_round = 0

    def run(
        self,
        agent_id: str,
        last_output: str,
        resume_fn: Callable[[str], str],
        gate_mode: str = "stdin",
        gate_dir: Path | None = None,
    ) -> GateResult:
        if gate_mode == "checkpoint":
            return GateResult.CHECKPOINTED
        if gate_mode == "file":
            if gate_dir is None:
                gate_dir = self.output_dir / "gates"
            gate_dir.mkdir(parents=True, exist_ok=True)
            return self._file_loop(agent_id, last_output, resume_fn, gate_dir)
        return self._stdin_loop(agent_id, last_output, resume_fn)

    def _stdin_loop(
        self,
        agent_id: str,
        last_output: str,
        resume_fn: Callable[[str], str],
    ) -> GateResult:
        print(f"\n{'=' * 60}")
        print(f"  GATE: Agent '{agent_id}' completed")
        print(f"{'=' * 60}")
        print(f"\n{last_output}\n")
        print(f"{'=' * 60}")
        print("  Type feedback to send to the agent (same session).")
        print("  Type 'approve' or 'PNS' to proceed to the next agent.")
        print("  Type 'stop', 'reject', 'quit', or 'abort' to stop the pipeline.")
        print(f"{'=' * 60}\n")

        while True:
            try:
                human_input = input("[gate] > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return GateResult.REJECTED

            if not human_input:
                continue

            decision = _gate_decision(human_input)
            if decision is not None:
                logger.info("Gate '%s': %s", agent_id, decision.value)
                return decision

            self._gate_round += 1
            self._save_gate_artifact(f"gate-{self._gate_round}-input.md", human_input)

            print(f"\n  Sending feedback to agent '{agent_id}'...\n")
            response = resume_fn(human_input)
            self._save_gate_artifact(f"gate-{self._gate_round}-output.md", response)

            print(f"\n{response}\n")

    def _file_loop(
        self,
        agent_id: str,
        last_output: str,
        resume_fn: Callable[[str], str],
        gate_dir: Path,
    ) -> GateResult:
        gate_file = gate_dir / f"{agent_id}.gate"
        feedback_file = gate_dir / f"{agent_id}.feedback"
        rejected_file = gate_dir / f"{agent_id}.rejected"

        gate_file.write_text(last_output)
        logger.info(
            "Gate '%s': waiting for feedback at %s (write 'approve' or '%s' to proceed)",
            agent_id,
            feedback_file,
            PNS_KEYWORD,
        )

        while True:
            if rejected_file.exists():
                rejected_file.unlink()
                gate_file.unlink(missing_ok=True)
                logger.info("Gate '%s': rejected via file", agent_id)
                return GateResult.REJECTED

            if feedback_file.exists():
                human_input = feedback_file.read_text().strip()
                feedback_file.unlink()

                if not human_input:
                    continue

                decision = _gate_decision(human_input)
                if decision is not None:
                    gate_file.unlink(missing_ok=True)
                    logger.info("Gate '%s': %s via file", agent_id, decision.value)
                    return decision

                self._gate_round += 1
                self._save_gate_artifact(f"gate-{self._gate_round}-input.md", human_input)

                response = resume_fn(human_input)
                self._save_gate_artifact(f"gate-{self._gate_round}-output.md", response)

                gate_file.write_text(response)

            time.sleep(FILE_POLL_INTERVAL_SECONDS)

    def _save_gate_artifact(self, name: str, content: str) -> None:
        p = self.output_dir / name
        p.write_text(content)
