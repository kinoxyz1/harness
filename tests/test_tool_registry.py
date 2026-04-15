"""测试工具注册表迁移：Anthropic schema 形状、名称提取、filtered。"""
import pytest
from core.tools import registry


class TestSchemaShape:
    """所有注册工具的 schema 必须是 Anthropic 格式。"""

    def test_schemas_are_anthropic_format(self):
        for schema in registry.schemas():
            assert "name" in schema, f"Missing 'name' in schema: {schema}"
            assert "description" in schema, f"Missing 'description' in schema: {schema}"
            assert "input_schema" in schema, f"Missing 'input_schema' in schema: {schema}"
            assert "type" not in schema, f"Legacy 'type' key still present in: {schema}"
            assert "function" not in schema, f"Legacy 'function' key still present in: {schema}"

    def test_expected_tools_registered(self):
        names = {schema["name"] for schema in registry.schemas()}
        expected = {"bash", "edit_file", "find", "read_file", "skill", "todo", "write_file"}
        assert names == expected

    def test_input_schema_has_type_object(self):
        for schema in registry.schemas():
            assert schema["input_schema"]["type"] == "object"

    def test_required_params_extracted(self):
        """registry 能从 input_schema.required 正确提取必填参数。"""
        assert "command" in registry._required_params.get("bash", [])
        assert "path" in registry._required_params.get("read_file", [])
        assert "path" in registry._required_params.get("write_file", [])
        assert "content" in registry._required_params.get("write_file", [])


class TestFiltered:
    def test_filtered_returns_subset(self):
        sub = registry.filtered({"bash", "find"})
        names = {s["name"] for s in sub.schemas()}
        assert names == {"bash", "find"}

    def test_filtered_preserves_handlers(self):
        sub = registry.filtered({"bash"})
        assert sub.has("bash")
        assert not sub.has("read_file")

    def test_filtered_empty(self):
        sub = registry.filtered(set())
        assert sub.schemas() == []
