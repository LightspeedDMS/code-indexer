//! AC7: bounds-checked byte-cursor primitives for `graph::csr::wire`'s
//! positional binary graph-handoff format. Split into its own module
//! (rather than folded into `wire.rs`) so each primitive carries its OWN
//! direct unit test -- `wire.rs`'s single end-to-end round-trip test alone
//! would not otherwise discriminate a broken cursor primitive from a
//! broken higher-level section reader.
//!
//! Every function here is `pub(super)`: visible to `graph::csr::wire`
//! (this module's sibling under `csr`), never a public crate API surface
//! of its own. `wire.rs` (added in the immediately following commit) is
//! the production call site for all three.

use std::io;
use std::mem::size_of;

/// Builds an `io::Error` with kind `InvalidData` -- the ONE error kind
/// every decode failure in this wire format uses (a truncated/corrupt
/// graph file is a data-integrity problem, never a legitimate
/// end-of-stream condition), so a caller mapping onto
/// `AnalyzeStatus::GraphInvalid` never has to match on multiple kinds.
pub(super) fn invalid(msg: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg.to_string())
}

/// THE single bounds-checked cursor primitive every decode in `wire.rs`
/// is built on: advances `*pos` by `len` and returns the byte span it
/// covered, using `checked_add` (never a bare `*pos + len`, which could
/// overflow `usize` on a maliciously large length before any bounds
/// check runs) and `slice::get` (never direct indexing, which panics
/// instead of returning an `io::Error`).
pub(super) fn take<'a>(data: &'a [u8], pos: &mut usize, len: usize) -> io::Result<&'a [u8]> {
    let end = pos.checked_add(len).ok_or_else(|| invalid("cursor position overflow"))?;
    let slice = data.get(*pos..end).ok_or_else(|| invalid("truncated graph file"))?;
    *pos = end;
    Ok(slice)
}

/// Reads a little-endian `u64` record count and validates it against the
/// remaining bytes in `data` -- each record being at least
/// `min_record_bytes` bytes -- BEFORE any caller does
/// `Vec::with_capacity(count)`. A corrupt file claiming an astronomical
/// record count can therefore never force an allocation attempt larger
/// than the file itself could ever justify. Uses `usize::try_from`
/// (never a bare `as usize`), so a count too large for THIS platform's
/// `usize` is rejected rather than silently truncated/wrapped.
pub(super) fn read_count_capped(data: &[u8], pos: &mut usize, min_record_bytes: usize) -> io::Result<usize> {
    let raw = u64::from_le_bytes(take(data, pos, size_of::<u64>())?.try_into().expect("take(8) returns exactly 8 bytes"));
    let count = usize::try_from(raw).map_err(|_| invalid("record count too large for this platform"))?;
    let remaining = data.len().saturating_sub(*pos);
    let min_total = count.checked_mul(min_record_bytes).ok_or_else(|| invalid("record count overflows"))?;
    if min_total > remaining {
        return Err(invalid("record count exceeds remaining file size"));
    }
    Ok(count)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_builds_an_invalid_data_error_preserving_the_message() {
        let err = invalid("something went wrong");
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        assert!(err.to_string().contains("something went wrong"));
    }

    #[test]
    fn take_advances_the_cursor_and_returns_the_exact_byte_span() {
        let data = [1u8, 2, 3, 4, 5];
        let mut pos = 1;
        let span = take(&data, &mut pos, 3).expect("in-bounds take must succeed");
        assert_eq!(span, &[2, 3, 4]);
        assert_eq!(pos, 4);
    }

    #[test]
    fn take_rejects_a_length_that_runs_past_the_end_of_data() {
        let data = [1u8, 2, 3];
        let mut pos = 1;
        let err = take(&data, &mut pos, 10).expect_err("out-of-bounds take must fail, never panic");
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn take_rejects_a_length_that_would_overflow_the_cursor_position() {
        let data = [1u8, 2, 3];
        let mut pos = usize::MAX - 1;
        let err = take(&data, &mut pos, 10).expect_err("cursor overflow must fail, never panic or wrap");
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn read_count_capped_rejects_a_count_whose_minimum_size_exceeds_the_remaining_bytes() {
        // Declares a count of 1000 records at 19 bytes/record (far more
        // than the 4 remaining bytes in `data`) -- a real corrupt-file
        // shape, not a hypothetical one: this is exactly the check that
        // stops a maliciously huge `Vec::with_capacity` request.
        let mut data = 1000u64.to_le_bytes().to_vec();
        data.extend_from_slice(&[0, 0, 0, 0]);
        let mut pos = 0;
        let err = read_count_capped(&data, &mut pos, 19).expect_err("oversized count must be rejected");
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn read_count_capped_accepts_a_count_that_genuinely_fits() {
        let mut data = 2u64.to_le_bytes().to_vec();
        data.extend_from_slice(&[0u8; 38]); // 2 records * 19 bytes/record
        let mut pos = 0;
        let count = read_count_capped(&data, &mut pos, 19).expect("count that fits must be accepted");
        assert_eq!(count, 2);
        assert_eq!(pos, 8, "cursor must advance past the 8-byte u64 count only");
    }
}
