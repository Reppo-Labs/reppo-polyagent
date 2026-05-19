"""Harness loop edge cases (max_tokens must not post empty user messages)."""
import unittest
from unittest.mock import MagicMock, patch

from agent.agent_loop import run_tool_loop


class _Block:
    def __init__(self, type_, **kwargs):
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestAgentLoop(unittest.TestCase):
    def test_max_tokens_ends_without_extra_api_call(self):
        client = MagicMock()
        client.messages.create.side_effect = [
            MagicMock(
                stop_reason="tool_use",
                content=[_Block("tool_use", id="t1", name="check_balance", input={})],
            ),
            MagicMock(
                stop_reason="max_tokens",
                content=[_Block("text", text="partial summary")],
            ),
        ]

        def execute(name, args):
            return {"ok": True, "tool": name}

        messages = [{"role": "user", "content": "go"}]
        n, summary = run_tool_loop(
            client,
            model="claude-test",
            system="sys",
            tools=[],
            messages=messages,
            execute_tool_call=execute,
            max_iterations=5,
        )
        self.assertEqual(n, 2)
        self.assertIn("partial summary", summary)
        self.assertEqual(client.messages.create.call_count, 2)

    def test_tool_use_without_blocks_ends_cleanly(self):
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            stop_reason="tool_use",
            content=[_Block("text", text="thinking only")],
        )
        messages = [{"role": "user", "content": "go"}]
        n, _ = run_tool_loop(
            client,
            model="m",
            system="s",
            tools=[],
            messages=messages,
            execute_tool_call=lambda *_: {},
            max_iterations=3,
        )
        self.assertEqual(n, 1)
        self.assertEqual(len(messages), 1)


if __name__ == "__main__":
    unittest.main()
