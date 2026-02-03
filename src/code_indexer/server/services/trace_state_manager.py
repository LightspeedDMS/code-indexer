"""
Trace State Manager for CIDX Server (Story #136).

Manages per-session trace stacks to support:
- Nested research sessions (stack-based trace management)
- Active trace tracking for automatic span creation
- Graceful trace lifecycle (start, end, cleanup)
- Thread-safe state management
"""

import logging
import threading
from dataclasses import dataclass
from typing import Optional, Dict, List, Any

from code_indexer.server.services.langfuse_client import LangfuseClient

logger = logging.getLogger(__name__)


@dataclass
class TraceContext:
    """
    Context for an active trace.

    Attributes:
        trace_id: Unique identifier for this trace
        trace: Langfuse trace object
        parent_trace_id: Optional parent trace ID for nested traces
    """

    trace_id: str
    trace: Any  # Langfuse trace object
    parent_trace_id: Optional[str] = None


class TraceStateManager:
    """
    Manages trace state for MCP sessions.

    Maintains per-session stacks of active traces to support:
    - Nested research sessions (user can start a trace, do research, start
      a sub-trace for focused investigation, end sub-trace, continue original)
    - Automatic span creation (tool calls create spans under active trace)
    - Clean session termination (cleanup removes all traces for session)

    Thread-safe with lock protection for concurrent session access.

    Example usage:
        manager = TraceStateManager(langfuse_client)

        # Start research session
        ctx = manager.start_trace(session_id="s1", topic="authentication")

        # Nested trace for focused investigation
        ctx2 = manager.start_trace(session_id="s1", topic="oauth-flow")
        # ... research ...
        manager.end_trace(session_id="s1", score=0.9)

        # Back to original trace
        manager.end_trace(session_id="s1", score=0.8)

        # Cleanup on session end
        manager.cleanup_session(session_id="s1")
    """

    def __init__(self, langfuse_client: LangfuseClient):
        """
        Initialize TraceStateManager.

        Args:
            langfuse_client: LangfuseClient instance for creating traces
        """
        self._langfuse = langfuse_client
        self._session_trace_stacks: Dict[str, List[TraceContext]] = {}
        self._lock = threading.Lock()

    def start_trace(
        self,
        session_id: str,
        topic: str,
        strategy: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        username: Optional[str] = None,
    ) -> Optional[TraceContext]:
        """
        Start a new trace for the given session.

        If a trace is already active for this session, the new trace becomes
        a nested trace (pushed onto the stack).

        Args:
            session_id: MCP session ID
            topic: Research topic (e.g., "authentication", "performance")
            strategy: Optional research strategy (e.g., "semantic_then_fts")
            metadata: Optional metadata dict for additional context
            username: Optional username for the session owner

        Returns:
            TraceContext if trace created successfully, None otherwise
        """
        # Prepare metadata
        trace_metadata = {"topic": topic}
        if strategy:
            trace_metadata["strategy"] = strategy
        if metadata:
            trace_metadata.update(metadata)

        # Create trace via Langfuse client
        trace = self._langfuse.create_trace(
            name="research-session",
            session_id=session_id,
            metadata=trace_metadata,
            user_id=username,  # Langfuse uses user_id, we accept username
        )

        if trace is None:
            # Langfuse disabled or error
            return None

        with self._lock:
            # Get parent trace ID if this is a nested trace
            parent_trace_id = None
            if session_id in self._session_trace_stacks:
                stack = self._session_trace_stacks[session_id]
                if stack:
                    parent_trace_id = stack[-1].trace_id

            # Create context
            context = TraceContext(
                trace_id=trace.id, trace=trace, parent_trace_id=parent_trace_id
            )

            # Push to session stack
            if session_id not in self._session_trace_stacks:
                self._session_trace_stacks[session_id] = []
            self._session_trace_stacks[session_id].append(context)

            logger.info(
                f"Started trace {trace.id} for session {session_id} "
                f"(topic: {topic}, parent: {parent_trace_id})"
            )

            return context

    def get_active_trace(self, session_id: str) -> Optional[TraceContext]:
        """
        Get the currently active trace for a session.

        Returns the most recently started trace (top of stack).

        Args:
            session_id: MCP session ID

        Returns:
            TraceContext if active trace exists, None otherwise
        """
        with self._lock:
            if session_id not in self._session_trace_stacks:
                return None

            stack = self._session_trace_stacks[session_id]
            if not stack:
                return None

            return stack[-1]  # Top of stack

    def end_trace(
        self,
        session_id: str,
        score: Optional[float] = None,
        feedback: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> Optional[TraceContext]:
        """
        End the currently active trace for a session.

        Pops the most recent trace from the stack and optionally adds a score.

        Args:
            session_id: MCP session ID
            score: Optional score value (0.0 to 1.0)
            feedback: Optional feedback text
            outcome: Optional outcome description (unused currently)

        Returns:
            TraceContext of ended trace if successful, None otherwise
        """
        with self._lock:
            if session_id not in self._session_trace_stacks:
                logger.warning(f"end_trace called for unknown session: {session_id}")
                return None

            stack = self._session_trace_stacks[session_id]
            if not stack:
                logger.warning(
                    f"end_trace called with empty stack for session: {session_id}"
                )
                return None

            # Pop from stack
            context = stack.pop()

            logger.info(f"Ended trace {context.trace_id} for session {session_id}")

        # Add score if provided (outside lock to avoid holding during I/O)
        if score is not None:
            self._langfuse.score(
                trace_id=context.trace_id,
                name="user-feedback",
                value=score,
                comment=feedback,
            )

        # Flush to ensure data is sent
        self._langfuse.flush()

        return context

    def cleanup_session(self, session_id: str) -> None:
        """
        Clean up all traces for a session.

        Removes all traces from the session stack and flushes pending data.
        Called when MCP session terminates.

        Args:
            session_id: MCP session ID to clean up
        """
        with self._lock:
            if session_id in self._session_trace_stacks:
                trace_count = len(self._session_trace_stacks[session_id])
                del self._session_trace_stacks[session_id]
                logger.info(f"Cleaned up {trace_count} traces for session {session_id}")

        # Flush any pending data
        self._langfuse.flush()
