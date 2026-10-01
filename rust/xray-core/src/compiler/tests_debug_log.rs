//! Issue #1934: `compiler.rs`'s unit tests covering the debug_log runtime
//! behavior (AC1/AC2/AC5), relocated verbatim out of its single
//! `#[cfg(test)] mod tests { ... }` body (declared at `compiler.rs` via
//! `#[cfg(test)] #[path = "tests_debug_log.rs"] mod tests_debug_log;`,
//! mirroring the pattern `graph/bind/resolve.rs` already uses) so
//! `compiler.rs` itself stays well under the project's line limit.

use super::*;
use tempfile::TempDir;

#[test]
fn test_compiled_evaluator_can_call_debug_log() {
    // Integration: evaluator code using debug_log() must compile successfully.
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    debug_log("visiting node");
    debug_log(&format!("kind={}", node.kind));
    Vec::new()
}
"#;
    let result = compile_evaluator(user_code, dir.path());
    assert!(
        result.is_ok(),
        "evaluator using debug_log must compile: {:?}",
        result.err().map(|e| e.to_string())
    );
}

#[test]
fn test_debug_log_truncation_limits() {
    // AC5: evaluator calling debug_log 200 times must compile (truncation is runtime).
    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    for i in 0..200usize {
        debug_log(&format!("message {}", i));
    }
    Vec::new()
}
"#;
    let result = compile_evaluator(user_code, dir.path());
    assert!(
        result.is_ok(),
        "evaluator with looping debug_log must compile: {:?}",
        result.err().map(|e| e.to_string())
    );
}

#[test]
fn test_debug_log_truncation_limits_runtime() {
    // Runtime assertion: evaluator calling debug_log 150 times must yield exactly
    // 100 messages (not 150) — the PREAMBLE enforces a hard cap of 100 per evaluation.
    use crate::dynlib::DynlibEvaluator;
    use crate::owned_node::OwnedNode;
    use crate::scanner::Evaluator;

    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    for i in 0..150usize {
        debug_log(&format!("msg {}", i));
    }
    Vec::new()
}
"#;
    let cr = compile_evaluator(user_code, dir.path())
        .expect("evaluator with 150 debug_log calls must compile");

    let evaluator = DynlibEvaluator::load(&cr.so_path)
        .expect("compiled .so must load successfully");

    let node = OwnedNode::new_leaf_for_test("root", "", 1, true);

    evaluator.evaluate_node(&node);
    let messages = evaluator.drain_debug_log();

    assert_eq!(
        messages.len(),
        100,
        "100-message cap must be enforced at runtime: got {} messages",
        messages.len()
    );
    // Verify the FIRST 100 messages are retained (not arbitrary ones).
    for (i, message) in messages.iter().enumerate().take(100usize) {
        assert_eq!(
            message,
            &format!("msg {}", i),
            "message at index {} must be 'msg {}', got: {}",
            i, i, message
        );
    }
}

#[test]
fn test_debug_log_byte_limit_runtime() {
    // Runtime assertion: when messages exceed 10KB total, further messages are
    // silently dropped — enforced by the PREAMBLE 10240-byte guard.
    // Each message is 200 bytes; 51 * 200 = 10200 <= 10240 (fits).
    // 52nd message would push total to 10400 > 10240 (dropped).
    use crate::dynlib::DynlibEvaluator;
    use crate::owned_node::OwnedNode;
    use crate::scanner::Evaluator;

    let dir = TempDir::new().unwrap();
    let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let big_msg: String = std::iter::repeat('b').take(200).collect();
    for _ in 0..60usize {
        debug_log(&big_msg);
    }
    Vec::new()
}
"#;
    let cr = compile_evaluator(user_code, dir.path())
        .expect("evaluator with large debug_log messages must compile");

    let evaluator = DynlibEvaluator::load(&cr.so_path)
        .expect("compiled .so must load successfully");

    let node = OwnedNode::new_leaf_for_test("root", "", 1, true);

    evaluator.evaluate_node(&node);
    let messages = evaluator.drain_debug_log();

    // 51 * 200 = 10200 bytes fits within 10240; 52nd message (200 bytes) would
    // make 10400 > 10240 and is dropped. Exactly 51 messages must be retained.
    assert_eq!(
        messages.len(),
        51,
        "10KB byte cap must be enforced: expected 51 messages, got {}",
        messages.len()
    );
    let expected_msg: String = "b".repeat(200);
    for (i, msg) in messages.iter().enumerate() {
        assert_eq!(
            msg, &expected_msg,
            "message {} must be the 200-byte string, got len={}",
            i,
            msg.len()
        );
    }
}
