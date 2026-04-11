"""测试 todo_manage 工具。"""
from __future__ import annotations

import pytest

from core.tools.todo import (
    PlanningState,
    PlanItem,
    _validate_items,
    _render_progress,
    handle,
    get_state,
    increment_rounds,
    reset_rounds,
)
from core.tools import ToolUseContext


class TestValidateItems:
    """测试 _validate_items 函数。"""

    def test_empty_list_passes(self):
        valid, error = _validate_items([])
        assert valid is True
        assert error == ""

    def test_valid_items_pass(self):
        items = [
            {"content": "任务1", "status": "pending"},
            {"content": "任务2", "status": "in_progress"},
            {"content": "任务3", "status": "completed"},
        ]
        valid, error = _validate_items(items)
        assert valid is True
        assert error == ""

    def test_missing_content_fails(self):
        items = [{"content": "", "status": "pending"}]
        valid, error = _validate_items(items)
        assert valid is False
        assert "缺少 content" in error

    def test_invalid_status_fails(self):
        items = [{"content": "任务", "status": "invalid"}]
        valid, error = _validate_items(items)
        assert valid is False
        assert "status 无效" in error

    def test_multiple_in_progress_fails(self):
        items = [
            {"content": "任务1", "status": "in_progress"},
            {"content": "任务2", "status": "in_progress"},
        ]
        valid, error = _validate_items(items)
        assert valid is False
        assert "最多只能有 1 个 in_progress" in error

    def test_too_many_items_fails(self):
        items = [{"content": f"任务{i}", "status": "pending"} for i in range(13)]
        valid, error = _validate_items(items)
        assert valid is False
        assert "超过限制" in error


class TestRenderProgress:
    """测试 _render_progress 函数。"""

    def test_empty_list(self):
        result = _render_progress([])
        assert "已清空" in result

    def test_pending_items(self):
        items = [PlanItem(content="任务1", status="pending")]
        result = _render_progress(items)
        assert "0/1 完成" in result

    def test_in_progress_items(self):
        items = [PlanItem(content="任务1", status="in_progress")]
        result = _render_progress(items)
        assert "当前: 任务1" in result

    def test_completed_items(self):
        items = [
            PlanItem(content="任务1", status="completed"),
            PlanItem(content="任务2", status="pending"),
        ]
        result = _render_progress(items)
        assert "1/2 完成" in result


class TestHandle:
    """测试 handle 函数。"""

    def test_successful_update(self):
        # 创建 mock context
        context = ToolUseContext(working_dir="/tmp", max_turns=10)

        result = handle({"items": [{"content": "测试任务", "status": "pending"}]}, context)

        assert result.success is True
        assert "计划已更新" in result.output

    def test_validation_failure(self):
        context = ToolUseContext(working_dir="/tmp", max_turns=10)

        result = handle({"items": [{"content": "", "status": "pending"}]}, context)

        assert result.success is False
        assert "参数错误" in result.output


class TestStateAPI:
    """测试对外暴露的状态管理 API。"""

    def setup_method(self):
        """每个测试前重置状态。"""
        from core.tools import todo
        todo._state = PlanningState()

    def test_get_state(self):
        state = get_state()
        assert isinstance(state, PlanningState)
        assert state.items == []

    def test_increment_rounds(self):
        assert increment_rounds() == 1
        assert increment_rounds() == 2

    def test_reset_rounds(self):
        increment_rounds()
        increment_rounds()
        reset_rounds()
        state = get_state()
        assert state.rounds_since_update == 0
