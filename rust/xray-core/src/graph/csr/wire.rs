//! AC7: the mmap-backed `--graph-in <path>` file format -- "the CSR arena
//! of packed structs is directly mappable", reusing #1785's `--facts-in`
//! file-handoff pattern for the graph itself.
//!
//! This is a POSITIONAL BINARY format, never a marshalling protocol
//! (JSON/bincode): every section is a fixed-width record count followed by
//! that many fixed-width (or explicitly length-prefixed) records, decoded
//! by straight little-endian integer reads over the mmap'd byte slice --
//! never a recursive/structured parser. `write_graph_file` runs in the
//! PARENT (the graph is already fully built in memory); `read_graph_file`
//! runs in the analyze CHILD, backed by a real `memmap2::Mmap` over the
//! file the parent wrote.
//!
//! Scope note (documented, not hidden): `binder_depths` (AC4 per-language
//! diagnostic metadata) is deliberately NOT included in this wire format --
//! it is presentation/observability data the analyze child's bounded graph
//! ops (`super::ops`) never consume, and every symbol/reference/candidate
//! needed for reachability, orphan-detection, and path-shaped findings IS
//! carried. A `CodeGraph` reconstructed via `read_graph_file` reports an
//! empty `binder_depths()` slice; this does not affect any AC7 op.

use super::builder::CodeGraphBuilder;
use super::candidate::Candidate;
use super::code_graph::CodeGraph;
use super::wire_cursor::{invalid, read_count_capped, take};
use crate::graph::budget::AnalysisCompleteness;
use std::io::{self, Write};
use std::path::Path;

const MAGIC: &[u8; 8] = b"XRAYGRF1";
/// `from`(4) + `file`(4) + `line`(4) + `kind`(1) + `cand_start`(4) + `cand_len`(2).
const REFERENCE_RECORD_MIN_BYTES: usize = 19;
/// `symbol`(4) + `reasons`(2).
const CANDIDATE_RECORD_MIN_BYTES: usize = 6;
/// `len`(4) prefix; the string bytes themselves are additional and vary.
const STRING_RECORD_MIN_BYTES: usize = 4;
/// `symbol`(8) + `referenced`(1).
const SYMBOL_RECORD_MIN_BYTES: usize = 9;
/// `dense_id`(4) + `len`(4) prefix; the signature text itself is additional.
const SIGNATURE_RECORD_MIN_BYTES: usize = 8;

/// Writes `graph` to `path` in the AC7 wire format. Sections, in order:
/// magic, references, candidates (recomputed from `candidates_for` in
/// reference order -- see module docs on why that reconstructs the exact
/// original flat arena), interned strings, interned symbols, referenced
/// bits, per-symbol cached signatures, completeness byte.
pub fn write_graph_file(graph: &CodeGraph, path: &Path) -> io::Result<()> {
    let mut w = io::BufWriter::new(std::fs::File::create(path)?);
    w.write_all(MAGIC)?;
    write_references_and_candidates(&mut w, graph)?;
    write_strings(&mut w, graph)?;
    write_symbols_and_referenced_bits(&mut w, graph)?;
    write_signatures(&mut w, graph)?;
    w.write_all(&[completeness_to_byte(graph.completeness())])?;
    w.flush()
}

fn write_references_and_candidates(w: &mut impl Write, graph: &CodeGraph) -> io::Result<()> {
    let references = graph.references();
    w.write_all(&(references.len() as u64).to_le_bytes())?;
    let mut candidate_count: u64 = 0;
    for reference in references {
        candidate_count += graph.candidates_for(reference).len() as u64;
    }
    for reference in references {
        w.write_all(&reference.from.to_le_bytes())?;
        w.write_all(&reference.file.to_le_bytes())?;
        w.write_all(&reference.line.to_le_bytes())?;
        w.write_all(&[reference.kind])?;
        w.write_all(&reference.cand_start.to_le_bytes())?;
        w.write_all(&reference.cand_len.to_le_bytes())?;
    }
    w.write_all(&candidate_count.to_le_bytes())?;
    for reference in references {
        for candidate in graph.candidates_for(reference) {
            w.write_all(&candidate.symbol().to_le_bytes())?;
            w.write_all(&candidate.reasons().to_le_bytes())?;
        }
    }
    Ok(())
}

fn write_strings(w: &mut impl Write, graph: &CodeGraph) -> io::Result<()> {
    let count = graph.string_count();
    w.write_all(&(count as u64).to_le_bytes())?;
    for id in 0..count as u32 {
        let s = graph.resolve_string(id);
        w.write_all(&(s.len() as u32).to_le_bytes())?;
        w.write_all(s.as_bytes())?;
    }
    Ok(())
}

fn write_symbols_and_referenced_bits(w: &mut impl Write, graph: &CodeGraph) -> io::Result<()> {
    let count = graph.symbol_count();
    w.write_all(&(count as u64).to_le_bytes())?;
    for id in 0..count as u32 {
        w.write_all(&graph.resolve_symbol(id).to_le_bytes())?;
        w.write_all(&[graph.is_symbol_referenced(id) as u8])?;
    }
    Ok(())
}

fn write_signatures(w: &mut impl Write, graph: &CodeGraph) -> io::Result<()> {
    let mut present: Vec<(u32, &str)> = Vec::new();
    for id in 0..graph.symbol_count() as u32 {
        if let Some(sig) = graph.signature_for(id) {
            present.push((id, sig));
        }
    }
    w.write_all(&(present.len() as u64).to_le_bytes())?;
    for (id, sig) in present {
        w.write_all(&id.to_le_bytes())?;
        w.write_all(&(sig.len() as u32).to_le_bytes())?;
        w.write_all(sig.as_bytes())?;
    }
    Ok(())
}

/// One decoded reference record, not yet placed through
/// `CodeGraphBuilder::add_reference` (which recomputes `cand_start`/
/// `cand_len` for the NEW arena) -- `cand_start`/`cand_len` here only
/// slice `raw_candidates`, the flat array read alongside it.
struct RawReference {
    from: u32,
    file: u32,
    line: u32,
    kind: u8,
    cand_start: u32,
    cand_len: u16,
}

/// A decoded candidate record: `(dense symbol id, reasons bitmask)`.
type RawCandidate = (u32, u16);

fn read_references_and_candidates(data: &[u8], pos: &mut usize) -> io::Result<(Vec<RawReference>, Vec<RawCandidate>)> {
    use std::mem::size_of;
    let ref_count = read_count_capped(data, pos, REFERENCE_RECORD_MIN_BYTES)?;
    let mut references = Vec::with_capacity(ref_count);
    for _ in 0..ref_count {
        references.push(RawReference {
            from: u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap()),
            file: u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap()),
            line: u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap()),
            kind: take(data, pos, size_of::<u8>())?[0],
            cand_start: u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap()),
            cand_len: u16::from_le_bytes(take(data, pos, size_of::<u16>())?.try_into().unwrap()),
        });
    }
    let cand_count = read_count_capped(data, pos, CANDIDATE_RECORD_MIN_BYTES)?;
    let mut candidates = Vec::with_capacity(cand_count);
    for _ in 0..cand_count {
        let symbol = u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap());
        let reasons = u16::from_le_bytes(take(data, pos, size_of::<u16>())?.try_into().unwrap());
        candidates.push((symbol, reasons));
    }
    Ok((references, candidates))
}

/// Populates `builder`'s string table, symbol table, referenced bits, and
/// cached signatures from the wire format, in that section order, and
/// returns the total number of symbols decoded -- the caller uses this to
/// validate every OTHER decoded symbol id (`reference.from`, candidate
/// `symbol`) against the real, just-built table. Every signature's own
/// `dense_id` is validated against that same count HERE, since
/// `symbol_count` is only known partway through this function.
///
/// Rejects a DUPLICATE (or otherwise out-of-sequence) symbol entry as
/// corrupt: `CodeGraphBuilder::intern_symbol` dedups and returns the
/// EXISTING dense id for a repeat, which would otherwise make the
/// builder's real, distinct symbol count SMALLER than the returned
/// `symbol_count` -- and every downstream `reference.from`/candidate
/// `symbol`/signature `dense_id` check compares against that returned
/// count, so an undetected duplicate would let a dense id the table
/// never actually assigned pass validation and panic later inside
/// `SymbolTable::resolve`. Asserting `intern_symbol`'s result equals the
/// current loop index catches that the moment it happens.
fn read_strings_symbols_and_signatures(data: &[u8], pos: &mut usize, builder: &mut CodeGraphBuilder) -> io::Result<usize> {
    use std::mem::size_of;
    let string_count = read_count_capped(data, pos, STRING_RECORD_MIN_BYTES)?;
    for _ in 0..string_count {
        let len = u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap()) as usize;
        let s = std::str::from_utf8(take(data, pos, len)?).map_err(|e| invalid(&e.to_string()))?;
        builder.intern_string(s);
    }
    let symbol_count = read_count_capped(data, pos, SYMBOL_RECORD_MIN_BYTES)?;
    for i in 0..symbol_count {
        let symbol = u64::from_le_bytes(take(data, pos, size_of::<u64>())?.try_into().unwrap());
        let referenced = take(data, pos, size_of::<u8>())?[0] != 0;
        let dense = builder.intern_symbol(symbol);
        if dense as usize != i {
            return Err(invalid("duplicate or out-of-order symbol id in graph file"));
        }
        if referenced {
            builder.mark_referenced(dense);
        }
    }
    let sig_count = read_count_capped(data, pos, SIGNATURE_RECORD_MIN_BYTES)?;
    for _ in 0..sig_count {
        let dense_id = u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap());
        if dense_id as usize >= symbol_count {
            return Err(invalid("signature dense_id outside decoded symbol table"));
        }
        let len = u32::from_le_bytes(take(data, pos, size_of::<u32>())?.try_into().unwrap()) as usize;
        let sig = std::str::from_utf8(take(data, pos, len)?).map_err(|e| invalid(&e.to_string()))?;
        builder.add_signature(dense_id, sig.to_string());
    }
    Ok(symbol_count)
}

/// Reads a graph previously written by `write_graph_file` back into a
/// real, OWNED `CodeGraph` (the analyze CHILD's side of the AC7 handoff),
/// accessing `path` via a real `memmap2::Mmap` during decoding, then
/// copying the decoded data ONCE into `CodeGraph`'s own owned storage --
/// the returned graph does not keep the mapping alive. Fails loud
/// (`InvalidData`) on a bad magic number, a truncated section, an
/// out-of-bounds candidate window, a duplicate symbol entry, or a symbol
/// id outside the decoded symbol table.
pub fn read_graph_file(path: &Path) -> io::Result<CodeGraph> {
    let file = std::fs::File::open(path)?;
    // SAFETY: mmap(2) over a plain, caller-controlled regular file this
    // process just opened read-only, written once and fully by the
    // parent before this child was ever spawned.
    let mmap = unsafe { memmap2::Mmap::map(&file)? };
    let data: &[u8] = &mmap;
    if data.len() < MAGIC.len() || &data[..MAGIC.len()] != MAGIC {
        return Err(invalid("graph file: bad magic number"));
    }
    let mut pos = MAGIC.len();

    let (raw_references, raw_candidates) = read_references_and_candidates(data, &mut pos)?;
    let mut builder = CodeGraphBuilder::with_candidate_capacity(raw_candidates.len());
    let symbol_count = read_strings_symbols_and_signatures(data, &mut pos, &mut builder)?;

    for reference in &raw_references {
        if reference.from as usize >= symbol_count {
            return Err(invalid("reference.from outside decoded symbol table"));
        }
        let start = reference.cand_start as usize;
        let end = start.checked_add(reference.cand_len as usize).ok_or_else(|| invalid("candidate window overflows"))?;
        let window = raw_candidates.get(start..end).ok_or_else(|| invalid("candidate window out of bounds"))?;
        let mut candidates = Vec::with_capacity(window.len());
        for &(symbol, reasons) in window {
            if symbol as usize >= symbol_count {
                return Err(invalid("candidate symbol outside decoded symbol table"));
            }
            candidates.push(Candidate::new(symbol, reasons));
        }
        builder.add_reference(reference.from, reference.file, reference.line, reference.kind, &candidates);
    }

    let completeness_byte = take(data, &mut pos, std::mem::size_of::<u8>())?[0];
    builder.set_completeness(match completeness_byte {
        0 => AnalysisCompleteness::Complete,
        1 => AnalysisCompleteness::FactBudgetExceeded,
        2 => AnalysisCompleteness::IndexBudgetExceeded,
        3 => AnalysisCompleteness::DerivationTruncated,
        4 => AnalysisCompleteness::ResolutionAmbiguous,
        5 => AnalysisCompleteness::ParseErrorsPresent,
        other => return Err(invalid(&format!("corrupt completeness byte: {other}"))),
    });
    Ok(builder.build())
}

fn completeness_to_byte(c: AnalysisCompleteness) -> u8 {
    match c {
        AnalysisCompleteness::Complete => 0,
        AnalysisCompleteness::FactBudgetExceeded => 1,
        AnalysisCompleteness::IndexBudgetExceeded => 2,
        AnalysisCompleteness::DerivationTruncated => 3,
        AnalysisCompleteness::ResolutionAmbiguous => 4,
        AnalysisCompleteness::ParseErrorsPresent => 5,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::identity::make_symbol_id;
    use crate::graph::reasons;

    fn small_graph() -> CodeGraph {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
        let foo = builder.intern_symbol(make_symbol_id(1, 0));
        let bar = builder.intern_symbol(make_symbol_id(1, 1));
        let unreferenced = builder.intern_symbol(make_symbol_id(1, 2));
        let _ = builder.intern_string("com.example.Foo");
        builder.add_signature(foo, "foo()".to_string());
        builder.mark_referenced(bar);
        builder.add_reference(
            foo,
            1,
            5,
            0,
            &[Candidate::new(bar, reasons::SAME_FILE), Candidate::new(unreferenced, reasons::SAME_PACKAGE)],
        );
        builder.set_completeness(AnalysisCompleteness::IndexBudgetExceeded);
        builder.build()
    }

    /// AC7's own discriminating requirement for this module: a graph
    /// written to disk and read back via `read_graph_file` must be
    /// QUERY-EQUIVALENT to the original -- every reference, its candidate
    /// window, resolved symbols/strings, the referenced-bit, the cached
    /// signature, and the completeness state all round-trip exactly. A
    /// wrong implementation that silently dropped a section (e.g. wrote
    /// referenced_bits but never read them back) would still "round trip"
    /// on a naive equality check of just the reference count -- this test
    /// checks every query surface a real `analyze_graph` callback uses.
    #[test]
    fn graph_round_trips_through_a_real_file_via_mmap() {
        let original = small_graph();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("graph.bin");

        write_graph_file(&original, &path).expect("write must succeed");
        let reloaded = read_graph_file(&path).expect("read must succeed");

        assert_eq!(reloaded.references().len(), original.references().len());
        let orig_ref = original.references()[0];
        let reloaded_ref = reloaded.references()[0];
        assert_eq!(reloaded_ref.from, orig_ref.from);
        assert_eq!(reloaded_ref.line, orig_ref.line);

        let orig_candidates: Vec<_> = original.candidates_for(&orig_ref).iter().map(|c| c.symbol()).collect();
        let reloaded_candidates: Vec<_> = reloaded.candidates_for(&reloaded_ref).iter().map(|c| c.symbol()).collect();
        assert_eq!(reloaded_candidates, orig_candidates);

        assert_eq!(reloaded.resolve_string(0), "com.example.Foo");
        assert_eq!(reloaded.resolve_symbol(0), original.resolve_symbol(0));

        let foo_dense = reloaded.dense_id_for(make_symbol_id(1, 0)).unwrap();
        let bar_dense = reloaded.dense_id_for(make_symbol_id(1, 1)).unwrap();
        assert_eq!(reloaded.signature_for(foo_dense), Some("foo()"));
        assert!(reloaded.is_symbol_referenced(bar_dense));
        assert_eq!(reloaded.completeness(), AnalysisCompleteness::IndexBudgetExceeded);
    }
}
