"""
AutoSpanLogger - MCP tool call interceptor for automatic Langfuse span creation.

This service wraps MCP tool execution with automatic span logging when traces are active.
"""

import logging
from typing import Any, Callable, Optional

from .langfuse_client import LangfuseClient
from .trace_state_manager import TraceStateManager
from ..utils.config_manager import LangfuseConfig

logger = logging.getLogger(__name__)


class AutoSpanLogger:
    """
    MCP tool call interceptor that automatically creates Langfuse spans.

    When a trace is active for the session, wraps tool execution with span creation
    to capture timing, inputs, outputs, and errors. When no trace is active, executes
    tools without any logging overhead.

    Story #136 follow-up: Supports automatic trace creation on first tool call when
    auto_trace_enabled=True and no trace exists for the session.
    """

    # Sensitive field names to remove from inputs (case-insensitive)
    SENSITIVE_FIELDS = {"password", "token", "secret", "api_key"}

    def __init__(
        self,
        trace_manager: TraceStateManager,
        langfuse_client: LangfuseClient,
        config: Optional[LangfuseConfig] = None,
    ):
        """
        Initialize AutoSpanLogger.

        Args:
            trace_manager: TraceStateManager for accessing active traces
            langfuse_client: LangfuseClient for span creation
            config: Optional LangfuseConfig for auto-trace settings (Story #136 follow-up)
        """
        self.trace_manager = trace_manager
        self.langfuse = langfuse_client
        self.config = config or LangfuseConfig()  # Default to disabled config

    def _sanitize_input(self, arguments: dict) -> dict:
        """
        Remove sensitive fields from input arguments.

        Filters out fields like password, token, secret, api_key (case-insensitive)
        to prevent sensitive data from being logged to Langfuse.

        Args:
            arguments: Original tool arguments

        Returns:
            Sanitized copy of arguments with sensitive fields removed
        """
        if not isinstance(arguments, dict):
            return arguments

        # Create copy and remove sensitive fields (case-insensitive)
        sanitized = {}
        for key, value in arguments.items():
            if key.lower() not in self.SENSITIVE_FIELDS:
                sanitized[key] = value

        return sanitized

    def _summarize_output(self, output: Any) -> Any:
        """
        Summarize large output data to reduce trace size.

        For dicts containing a 'results' list, replaces the list with a summary
        containing result count and description. Other output types are returned
        unchanged.

        Args:
            output: Tool execution result

        Returns:
            Summarized output or original output if no summarization needed
        """
        if not isinstance(output, dict):
            return output

        if "results" not in output:
            return output

        results = output["results"]
        if not isinstance(results, list):
            return output

        # Replace results list with summary
        summarized = output.copy()
        result_count = len(results)
        summarized["result_count"] = result_count
        summarized["summary"] = f"{result_count} results returned"
        del summarized["results"]

        return summarized

    async def intercept_tool_call(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict,
        handler: Callable,
        username: Optional[str] = None,
    ) -> Any:
        """
        Wrap tool execution with span logging if trace is active.

        Story #136 follow-up: If auto_trace_enabled=True and no trace exists,
        automatically creates a trace before executing the tool.

        Args:
            session_id: MCP session ID
            tool_name: Name of the tool being called
            arguments: Tool arguments (will be sanitized for span input)
            handler: Async callable that executes the tool
            username: Optional username for auto-trace user_id (Story #136 follow-up)

        Returns:
            Result from handler execution

        Raises:
            Any exception raised by handler (after capturing in span if trace active)
        """
        # Check if there's an active trace for this session
        trace_ctx = self.trace_manager.get_active_trace(session_id)

        # Story #136 follow-up: Auto-trace creation
        if (
            self.config.enabled
            and self.config.auto_trace_enabled
            and trace_ctx is None
        ):
            # No active trace - auto-create one
            try:
                trace_ctx = self.trace_manager.start_trace(
                    session_id=session_id,
                    topic=f"Auto-trace: {tool_name}",
                    strategy="auto",
                    username=username,
                )
                if trace_ctx:
                    logger.info(
                        f"Auto-created trace {trace_ctx.trace_id} for session {session_id} on tool call: {tool_name}"
                    )
            except Exception as e:
                # Graceful failure: log warning but continue without trace
                logger.warning(
                    f"Auto-trace creation failed for session {session_id}, tool {tool_name}: {e}",
                    exc_info=True,
                )
                # trace_ctx remains None - execution continues without trace

        if not trace_ctx:
            # No active trace - execute handler without logging
            return await handler()

        # Active trace - create span and wrap execution
        # Try to create span, but continue without it if creation fails
        span = None
        try:
            # Sanitize inputs before passing to span
            sanitized_input = self._sanitize_input(arguments)
            span = self.langfuse.create_span(
                trace_id=trace_ctx.trace_id,
                name=tool_name,
                input=sanitized_input,
            )
        except Exception as span_creation_error:
            logger.warning(
                f"Failed to create span for tool {tool_name}: {span_creation_error}",
                exc_info=True,
            )
            # Continue without span - execute handler normally
            return await handler()

        # Span created successfully - wrap handler execution
        try:
            result = await handler()

            # End span with summarized output
            if span:
                try:
                    summarized_result = self._summarize_output(result)
                    span.end(output=summarized_result)
                except Exception as span_error:
                    logger.warning(f"Failed to end span: {span_error}", exc_info=True)

            return result

        except Exception as e:
            # Capture error in span
            if span:
                try:
                    span.end(
                        output={"error": str(e), "error_type": type(e).__name__},
                        level="ERROR",
                    )
                except Exception as span_error:
                    logger.warning(
                        f"Failed to end span with error: {span_error}", exc_info=True
                    )
            # Re-raise the original exception
            raise
