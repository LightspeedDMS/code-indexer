"""
Unit tests for API key auto-seeding functionality.

Tests cover:
- Auto-seeding Anthropic API key from ANTHROPIC_API_KEY env var
- Auto-seeding Anthropic API key from ~/.claude.json
- Auto-seeding VoyageAI API key from VOYAGE_API_KEY env var
- Priority order: env var > config file
- No-op when server config already has keys

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import json
import os
import tempfile
from pathlib import Path


from code_indexer.server.services.api_key_management import ApiKeyAutoSeeder


class TestAnthropicKeyAutoSeeding:
    """Test auto-seeding of Anthropic API key."""

    def test_seeds_from_environment_variable(self):
        """AC: Seeds Anthropic key from ANTHROPIC_API_KEY env var."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

            try:
                test_key = "sk-ant-api03-envseeded123456789012345678"
                os.environ["ANTHROPIC_API_KEY"] = test_key

                seeder = ApiKeyAutoSeeder(
                    claude_json_path=str(Path(tmpdir) / ".claude.json")
                )
                result = seeder.get_anthropic_key()

                assert result == test_key
            finally:
                if original_value is not None:
                    os.environ["ANTHROPIC_API_KEY"] = original_value
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_seeds_from_claude_json_when_no_env_var(self):
        """AC: Seeds Anthropic key from ~/.claude.json when no env var."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

            try:
                claude_json_path = Path(tmpdir) / ".claude.json"
                test_key = "sk-ant-api03-jsonseeded12345678901234567"
                claude_json_path.write_text(json.dumps({"apiKey": test_key}))

                seeder = ApiKeyAutoSeeder(
                    claude_json_path=str(claude_json_path)
                )
                result = seeder.get_anthropic_key()

                assert result == test_key
            finally:
                if original_value is not None:
                    os.environ["ANTHROPIC_API_KEY"] = original_value
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_env_var_takes_priority_over_claude_json(self):
        """AC: Environment variable takes priority over ~/.claude.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

            try:
                env_key = "sk-ant-api03-envpriority1234567890123456"
                json_key = "sk-ant-api03-jsonlowpriority12345678901"

                os.environ["ANTHROPIC_API_KEY"] = env_key

                claude_json_path = Path(tmpdir) / ".claude.json"
                claude_json_path.write_text(json.dumps({"apiKey": json_key}))

                seeder = ApiKeyAutoSeeder(
                    claude_json_path=str(claude_json_path)
                )
                result = seeder.get_anthropic_key()

                assert result == env_key
            finally:
                if original_value is not None:
                    os.environ["ANTHROPIC_API_KEY"] = original_value
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_returns_none_when_no_key_available(self):
        """Returns None when no Anthropic key available from any source."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

            try:
                seeder = ApiKeyAutoSeeder(
                    claude_json_path=str(Path(tmpdir) / ".claude.json")
                )
                result = seeder.get_anthropic_key()

                assert result is None
            finally:
                if original_value is not None:
                    os.environ["ANTHROPIC_API_KEY"] = original_value


class TestVoyageAIKeyAutoSeeding:
    """Test auto-seeding of VoyageAI API key."""

    def test_seeds_from_environment_variable(self):
        """AC: Seeds VoyageAI key from VOYAGE_API_KEY env var."""
        original_value = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            test_key = "pa-envseededvoyage123"
            os.environ["VOYAGE_API_KEY"] = test_key

            seeder = ApiKeyAutoSeeder()
            result = seeder.get_voyageai_key()

            assert result == test_key
        finally:
            if original_value is not None:
                os.environ["VOYAGE_API_KEY"] = original_value
            else:
                os.environ.pop("VOYAGE_API_KEY", None)

    def test_returns_none_when_no_key_available(self):
        """Returns None when no VoyageAI key available."""
        original_value = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            seeder = ApiKeyAutoSeeder()
            result = seeder.get_voyageai_key()

            assert result is None
        finally:
            if original_value is not None:
                os.environ["VOYAGE_API_KEY"] = original_value
