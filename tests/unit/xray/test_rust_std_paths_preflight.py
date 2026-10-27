# ^ coupled cases with rust/xray-core/src/validator/tests_std_paths.rs; Python pre-flight must agree with the Rust allowlist

from __future__ import annotations

from typing import Optional

import pytest

from code_indexer.xray.sandbox import validate_rust_evaluator

_ENTRY = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }\n"


def _with_entry(code: str) -> str:
    return code + "\n" + _ENTRY


@pytest.mark.parametrize(
    "code,construct",
    [
        ("fn f() { std::thread::yield_now(); }", "std::thread"),
        ("fn f() { let _ = std::os::unix::ffi::OsStrExt::as_bytes; }", "std::os"),
        ("fn f() { let _ = std::ffi::OsString::new(); }", "std::ffi"),
        ('fn f() { let _ = std::path::Path::new("x"); }', "std::path"),
        ("fn f(_t: std::thread::ThreadId) {}", "std::thread"),
        ("fn f() { ::std::thread::yield_now(); }", "std::thread"),
        ("fn f() { r#std::thread::yield_now(); }", "std::thread"),
        ("fn f() { std::r#process::exit(0); }", "std::process"),
        ("fn f() { std :: thread :: yield_now(); }", "std::thread"),
        ("fn f() { let _ = std /* note */ ::fs::dummy; }", "std::fs"),
        ("fn f() { let _ = core::mem::size_of::<u8>(); }", "core::mem"),
        ("fn f() { let _ = alloc::alloc::Layout::new::<u8>(); }", "alloc::alloc"),
        ('fn f() { let _ = format!("{:?}", std::thread::current()); }', "std::thread"),
    ],
)
def test_rejects_std_path_outside_allowlist(code: str, construct: str) -> None:
    result = validate_rust_evaluator(_with_entry(code))
    assert result.ok is False
    assert result.error_code == "forbidden_stdlib"
    assert result.offending_construct == construct
    assert result.offending_line == 1


@pytest.mark.parametrize(
    "module", ["fs", "net", "process", "env", "io", "os", "thread", "ffi", "path"]
)
@pytest.mark.parametrize(
    "form",
    ["fn f() {{ let _ = std::{m}::item; }}", "use std::{m}::Item;", "use std::{m};"],
)
def test_rejects_every_form_of_a_std_module_outside_allowlist(
    module: str, form: str
) -> None:
    result = validate_rust_evaluator(_with_entry(form.format(m=module)))
    assert result.ok is False
    assert result.error_code == "forbidden_stdlib"
    assert result.offending_construct == f"std::{module}"


@pytest.mark.parametrize(
    "code,construct",
    [
        ("use std as s;\nfn f() { s::thread::yield_now(); }", "std"),
        ("use std::{self as s};", "std"),
        ("use {std as s};", "std"),
        ("use core as c;", "core"),
        ("use std::collections as c;", "std::collections"),
        ("use std::collections::HashMap as Map;", "std::collections"),
        ("use std::{\n    collections::HashMap,\n    thread,\n};", "std::thread"),
        ("use r#std::r#thread;", "std::thread"),
        ("use std;", "std"),
        ("use std::*;", "std"),
        ("use std::os::*;", "std::os"),
        ("use core::mem;", "core::mem"),
    ],
)
def test_rejects_std_use_tree_outside_allowlist_or_renamed(
    code: str, construct: str
) -> None:
    result = validate_rust_evaluator(_with_entry(code))
    assert result.ok is False
    assert result.error_code == "forbidden_stdlib"
    assert result.offending_construct == construct
    assert result.offending_line == 1


_ALLOWED_STD_MODULES = (
    "collections fmt iter cmp str string vec option result ops convert char num hash "
    "borrow default boxed rc cell array slice marker primitive f32 f64 i8 i16 i32 i64 "
    "i128 u8 u16 u32 u64 u128 isize usize error any"
).split()
_STD_ONLY_MODULES = ("collections", "string", "vec", "boxed", "rc")
_ALLOWED_MODULES = (
    [("std", m) for m in _ALLOWED_STD_MODULES]
    + [("core", m) for m in _ALLOWED_STD_MODULES if m not in _STD_ONLY_MODULES]
    + [("alloc", m) for m in _STD_ONLY_MODULES]
)
_REJECTED_MODULES = (
    "fs net process env io os thread sync time ffi path ptr mem alloc panic arch "
    "intrinsics simd hint backtrace"
).split()
_DISALLOWED_MODULES = (
    [(root, m) for root in ("std", "core", "alloc") for m in _REJECTED_MODULES]
    + [("core", m) for m in _STD_ONLY_MODULES]
    + [("alloc", m) for m in ("fmt", "str", "slice", "borrow", "any", "error")]
)
_MODULE_FORMS = (
    "use {r}::{m};",
    "use {r}::{m}::Item;",
    "fn f() {{ let _ = {r}::{m}::item; }}",
)


@pytest.mark.parametrize("form", _MODULE_FORMS)
@pytest.mark.parametrize("root,module", _ALLOWED_MODULES)
def test_accepts_allowlisted_module(root: str, module: str, form: str) -> None:
    result = validate_rust_evaluator(_with_entry(form.format(r=root, m=module)))
    assert result.ok is True, result.reason


@pytest.mark.parametrize("form", _MODULE_FORMS)
@pytest.mark.parametrize("root,module", _DISALLOWED_MODULES)
def test_rejects_module_outside_allowlist(root: str, module: str, form: str) -> None:
    result = validate_rust_evaluator(_with_entry(form.format(r=root, m=module)))
    assert result.ok is False
    assert result.error_code == "forbidden_stdlib"
    assert result.offending_construct == f"{root}::{module}"


@pytest.mark.parametrize("code", ["extern crate std as s;", "extern crate alloc;"])
def test_rejects_extern_crate(code: str) -> None:
    result = validate_rust_evaluator(_with_entry(code))
    assert result.ok is False
    assert result.error_code == "forbidden_extern"


@pytest.mark.parametrize(
    "code",
    [
        "use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};",
        "use std::collections::hash_map::Entry;",
        "use std::fmt::{self, Write};",
        "use std::iter;",
        "use ::std::collections::VecDeque;",
        "use std::{collections::LinkedList, fmt::Display};",
        "use std::collections::*;",
        "fn f() { let _: std::collections::HashSet<u8> = std::collections::HashSet::new(); }",
        "fn f() -> String { std::iter::repeat('b').take(3).collect() }",
        "use crate::OwnedNode as Node;",
        'fn f() { let _ = "std::thread::spawn"; }',
        "// std::process::exit(0)\nfn f() {}",
        "fn f(node: &OwnedNode) { let _ = node.kind.as_str(); }",
    ],
)
def test_accepts_allowlisted_and_non_std_code(code: str) -> None:
    result = validate_rust_evaluator(_with_entry(code))
    assert result.ok is True, result.reason


_LONG_CHAIN = "a::" * 2000


@pytest.mark.parametrize(
    "root,construct",
    [("crate::", None), ("std::collections::", None), ("std::thread::", "std::thread")],
)
def test_long_use_path_is_judged_without_recursion(
    root: str, construct: Optional[str]
) -> None:
    result = validate_rust_evaluator(_with_entry(f"use {root}{_LONG_CHAIN}X;"))
    if construct is None:
        assert result.ok is True, result.reason
    else:
        assert result.ok is False
        assert result.error_code == "forbidden_stdlib"
        assert result.offending_construct == construct
