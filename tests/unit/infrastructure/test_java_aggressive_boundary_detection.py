"""
Aggressive Java boundary detection tests that push the limits of line tracking.

These tests specifically target edge cases that could cause context pollution,
content bleeding between chunks, and line number inaccuracies in Java code.
"""

import pytest
from textwrap import dedent

from code_indexer.config import IndexingConfig
from code_indexer.indexing.chunker import TextChunker


class TestJavaAggressiveBoundaryDetection:
    """Test Java code that specifically challenges boundary detection."""

    @pytest.fixture
    def text_chunker(self):
        """Create text chunker with small chunk size to force aggressive splitting."""
        config = IndexingConfig()
        config.chunk_size = 800  # Small to force splits in tricky places
        config.chunk_overlap = 50
        return TextChunker(config)

    def test_deeply_nested_exception_chaining(self, text_chunker):
        """Test complex exception chaining that could cause bleeding."""
        code = dedent(
            """
            public class ComplexExceptionHandler {
                public void processRequest(UserRequest request) throws ProcessingException {
                    try {
                        validateRequest(request);
                        processUserData(request.getUserData());
                        performBusinessLogic(request);
                    } catch (ValidationException e) {
                        throw new ProcessingException(
                            "Request validation failed for user: " + request.getUserId() + ". " +
                            "The following validation errors occurred: " +
                            "- Email format is invalid: " + e.getFieldValue() + " " +
                            "- Password strength requirements not met " +
                            "- User age must be between 18 and 120 years " +
                            "- Phone number format is incorrect " +
                            "Please correct these issues and try again.",
                            "VALIDATION_FAILED",
                            e
                        );
                    } catch (DataAccessException e) {
                        throw new ProcessingException(
                            "Database operation failed while processing request " + request.getRequestId() + ". " +
                            "Error details: " + e.getMessage() + ". " +
                            "This could be due to: " +
                            "- Network connectivity issues " +
                            "- Database server unavailable " +
                            "- Insufficient database permissions " +
                            "- Query timeout occurred " +
                            "Please contact system administrator if problem persists.",
                            "DATABASE_ERROR",
                            e
                        );
                    } catch (BusinessLogicException e) {
                        if (e.getErrorCode().equals("INSUFFICIENT_FUNDS")) {
                            throw new ProcessingException(
                                "Transaction cannot be completed due to insufficient funds. " +
                                "Account balance: $" + e.getCurrentBalance() + ". " +
                                "Required amount: $" + e.getRequiredAmount() + ". " +
                                "Additional fees: $" + e.getAdditionalFees() + ". " +
                                "Please ensure sufficient funds are available and try again. " +
                                "You can add funds through: " +
                                "- Online banking transfer " +
                                "- Credit card deposit " +
                                "- Bank branch visit " +
                                "- Mobile app quick transfer",
                                "INSUFFICIENT_FUNDS",
                                e
                            );
                        } else {
                            throw new ProcessingException(
                                "Business rule violation occurred: " + e.getRuleName() + ". " +
                                "Rule description: " + e.getRuleDescription() + ". " +
                                "Current value: " + e.getCurrentValue() + ". " +
                                "Expected value: " + e.getExpectedValue() + ". " +
                                "Violation severity: " + e.getSeverity() + ". " +
                                "This indicates a serious business logic error that requires attention.",
                                "BUSINESS_RULE_VIOLATION",
                                e
                            );
                        }
                    }
                }

                private void cleanup() {
                    // Cleanup resources
                }
            }
        """
        ).strip()

        chunks = text_chunker.chunk_text(code)
        original_lines = code.splitlines()

        # Verify no bleeding between exception handling blocks
        for i, chunk in enumerate(chunks):
            chunk_text = chunk["text"]

            # If chunk contains "INSUFFICIENT_FUNDS" it should NOT contain "BUSINESS_RULE_VIOLATION"
            if (
                "INSUFFICIENT_FUNDS" in chunk_text
                and "BUSINESS_RULE_VIOLATION" in chunk_text
            ):
                # Check if this is actually correct based on line ranges
                expected_content = self._extract_expected_content(
                    original_lines, chunk["line_start"], chunk["line_end"]
                )
                if not (
                    "INSUFFICIENT_FUNDS" in expected_content
                    and "BUSINESS_RULE_VIOLATION" in expected_content
                ):
                    pytest.fail(
                        f"Chunk {i + 1} contains bleeding between different exception types! "
                        f"Lines {chunk['line_start']}-{chunk['line_end']} should not contain both."
                    )

            # If chunk contains part of an error message, it should contain the complete message
            if (
                "Transaction cannot be completed due to insufficient funds."
                in chunk_text
            ):
                assert "- Mobile app quick transfer" in chunk_text, (
                    f"Chunk {i + 1} has incomplete INSUFFICIENT_FUNDS error message"
                )

            if "Business rule violation occurred:" in chunk_text:
                assert "This indicates a serious business logic error" in chunk_text, (
                    f"Chunk {i + 1} has incomplete BUSINESS_RULE_VIOLATION error message"
                )

    def _extract_expected_content(self, original_lines, start_line, end_line):
        """Extract expected content based on line numbers."""
        start_idx = start_line - 1
        end_idx = end_line - 1

        if start_idx < 0 or end_idx >= len(original_lines):
            return ""

        return "\\n".join(original_lines[start_idx : end_idx + 1])
