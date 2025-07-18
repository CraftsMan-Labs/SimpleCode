"""Tests for gate behavior."""

from __future__ import annotations

from pathlib import Path

from simplecode.gate import Gate, GateResult


def test_gate_file_loop_pns(tmp_path: Path) -> None:
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    gate = Gate(output_dir=output_dir)

    feedback_file = gate_dir / "test-agent.feedback"
    feedback_file.write_text("PNS")

    result = gate.run(
        agent_id="test-agent",
        last_output="Agent output here",
        resume_fn=lambda f: "response",
        gate_mode="file",
        gate_dir=gate_dir,
    )

    assert result == GateResult.PROCEED


def test_gate_file_loop_rejected(tmp_path: Path) -> None:
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    gate = Gate(output_dir=output_dir)

    rejected_file = gate_dir / "test-agent.rejected"
    rejected_file.write_text("")

    result = gate.run(
        agent_id="test-agent",
        last_output="Agent output here",
        resume_fn=lambda f: "response",
        gate_mode="file",
        gate_dir=gate_dir,
    )

    assert result == GateResult.REJECTED
