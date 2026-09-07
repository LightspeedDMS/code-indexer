//! Receiver-type resolution substrate for AC1 (receiver-type narrowing)
//! and AC2 (return-type chaining) -- Story #1806, S2b. Bind-time
//! counterpart to `crate::graph::extract::java_receiver`'s extraction-time
//! `ReceiverExpr` construction: this module resolves an ALREADY-BUILT
//! `ReceiverExpr` into a concrete declared type name, using only
//! declared-type evidence already captured in `LocalIndex.typed_names`
//! (AC1, same file as the call) and the repo-wide `RepoNameIndex`/
//! `TypeIndex` (AC2, a chained call's intermediate method may be declared
//! in a DIFFERENT file than the call site -- return types, like every
//! other repo-wide declaration fact this binder uses, are indexed
//! globally). Scope stays bounded to declared-type evidence exactly as
//! the rest of this story: no build, no classpath, no generics/full type
//! inference. Called for real by `super::resolve_all_references`.

use super::families::TypeIndex;
use super::name_index::RepoNameIndex;
use crate::graph::extract::local_index::{DeclarationKind, NameScope, ReceiverExpr, TypedNameRecord};
use crate::graph::identity::SymbolId;
use std::collections::HashMap;

/// Per-file lookup substrate for AC1's "declared types in the same file"
/// scope: a local variable's/parameter's declared type (keyed by its
/// enclosing METHOD symbol -- see `NameScope::Local`) and a field's
/// declared type (keyed by its enclosing TYPE name -- `NameScope::Field`).
/// Built once per file, mirroring `super::scope::FileScope`'s own
/// per-file construction pattern.
pub(crate) struct FileTypedNames {
    locals: HashMap<(SymbolId, String), String>,
    fields: HashMap<(String, String), String>,
}

impl FileTypedNames {
    /// Bounded loop: iterates once per already-extracted `TypedNameRecord`
    /// (finite, fixed by the file's own record count, Rule 14).
    pub(crate) fn build(typed_names: &[TypedNameRecord]) -> Self {
        let mut locals = HashMap::new();
        let mut fields = HashMap::new();
        for record in typed_names {
            match &record.scope {
                NameScope::Local { enclosing_method } => {
                    locals.insert((*enclosing_method, record.name.clone()), record.declared_type.clone());
                }
                NameScope::Field { enclosing_type } => {
                    fields.insert((enclosing_type.clone(), record.name.clone()), record.declared_type.clone());
                }
            }
        }
        FileTypedNames { locals, fields }
    }

    /// Looks up `name`'s declared type, preferring a LOCAL/PARAMETER
    /// binding within `enclosing_method` (most specific -- mirrors
    /// ordinary Java scoping, where a local/parameter shadows a
    /// same-named field), falling back to a FIELD binding on
    /// `enclosing_type`. `None` when neither substrate has evidence --
    /// never a guessed type.
    pub(crate) fn lookup(&self, enclosing_method: Option<SymbolId>, enclosing_type: Option<&str>, name: &str) -> Option<String> {
        if let Some(enclosing_method) = enclosing_method {
            if let Some(declared_type) = self.locals.get(&(enclosing_method, name.to_string())) {
                return Some(declared_type.clone());
            }
        }
        let enclosing_type = enclosing_type?;
        self.fields.get(&(enclosing_type.to_string(), name.to_string())).cloned()
    }
}

/// AC2: `type_name`'s (or one of its transitive supertypes') declared
/// return type for a method named `method_name`. Honest under overloads
/// (Rule 2, anti-fallback): when MULTIPLE declarations of `method_name`
/// exist across `type_name`'s hierarchy and they DISAGREE on return type,
/// this returns `None` rather than guessing one -- a chain can only keep
/// following a return type that is unambiguously known.
fn return_type_of_method_on_type(
    method_name: &str,
    type_name: &str,
    name_index: &RepoNameIndex,
    type_index: &TypeIndex,
) -> Option<String> {
    let mut allowed = type_index.supertypes_of(type_name);
    allowed.insert(type_name.to_string());
    let mut found: Option<&str> = None;
    for decl in name_index.lookup(method_name, DeclarationKind::Method) {
        if !decl.enclosing_type.as_deref().is_some_and(|t| allowed.contains(t)) {
            continue;
        }
        let Some(return_type) = decl.return_type.as_deref() else { continue };
        match found {
            None => found = Some(return_type),
            Some(existing) if existing == return_type => {}
            Some(_) => return None,
        }
    }
    found.map(|t| t.to_string())
}

/// AC1/AC2: resolves `receiver`'s declared type, given the call site's
/// own context. `enclosing_type`/`enclosing_method` are the CALL SITE's
/// own context: used both for `ReceiverExpr::None`/`SelfOrSuper` ("this
/// object's" type IS the enclosing type) and as the scope
/// `typed_names.lookup` resolves an `Identifier` receiver against.
///
/// Non-recursive (Rule 14): walks OUTWARD from `receiver`'s outermost
/// `Chained` wrapping down to its base (identifier/self/other),
/// collecting the chain of intermediate method names into a `Vec`, then
/// resolves the base's type and follows the chain FORWARD through
/// declared return types. The `Vec` is bounded by the same cap
/// `crate::graph::extract::java_receiver::build_receiver_expr` already
/// enforced when IT built `receiver` at extraction time -- this function
/// introduces no new unbounded loop.
pub(crate) fn resolve_receiver_type(
    receiver: &ReceiverExpr,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    typed_names: &FileTypedNames,
    name_index: &RepoNameIndex,
    type_index: &TypeIndex,
) -> Option<String> {
    let mut current = receiver;
    let mut method_chain: Vec<&str> = Vec::new();
    while let ReceiverExpr::Chained { method_name, receiver: inner } = current {
        method_chain.push(method_name.as_str());
        current = inner;
    }
    let mut resolved_type = match current {
        ReceiverExpr::None | ReceiverExpr::SelfOrSuper => enclosing_type.map(|t| t.to_string())?,
        ReceiverExpr::Identifier(name) => typed_names.lookup(enclosing_method, enclosing_type, name)?,
        ReceiverExpr::Other => return None,
        ReceiverExpr::Chained { .. } => unreachable!("the while loop above strips every Chained layer"),
    };
    for method_name in method_chain.into_iter().rev() {
        resolved_type = return_type_of_method_on_type(method_name, &resolved_type, name_index, type_index)?;
    }
    Some(resolved_type)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::bind::FileForBind;
    use crate::graph::extract::local_index::{Declaration, DeclarationKind as DK, LocalIndex, MethodOwnerRecord, MethodReturnTypeRecord};
    use crate::graph::identity::make_symbol_id;

    /// AC1: a local/parameter binding must be preferred over a
    /// same-named field -- the discriminating case ordinary Java scoping
    /// requires (shadowing).
    #[test]
    fn file_typed_names_prefers_a_local_binding_over_a_field_of_the_same_name() {
        let method_symbol = make_symbol_id(1, 0);
        let records = vec![
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Local".to_string(),
                scope: NameScope::Local { enclosing_method: method_symbol },
            },
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Field".to_string(),
                scope: NameScope::Field { enclosing_type: "Owner".to_string() },
            },
        ];
        let typed_names = FileTypedNames::build(&records);
        assert_eq!(typed_names.lookup(Some(method_symbol), Some("Owner"), "x"), Some("Local".to_string()));
        // Without a matching local (different method), falls back to the field.
        assert_eq!(
            typed_names.lookup(Some(make_symbol_id(1, 99)), Some("Owner"), "x"),
            Some("Field".to_string())
        );
        assert_eq!(typed_names.lookup(None, None, "neverDeclared"), None);
    }

    fn method_decl(name: &str, file_id: u32, local: u32) -> Declaration {
        Declaration {
            kind: DK::Method,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(file_id, local),
            param_count: Some(0),
            param_types: Vec::new(),
            is_varargs: false,
        }
    }

    /// AC1: `obj.doSomething()` where `obj` is a locally-declared `Foo`
    /// resolves to `"Foo"`.
    #[test]
    fn resolve_receiver_type_resolves_a_simple_identifier_via_typed_names() {
        let method_symbol = make_symbol_id(1, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "obj".to_string(),
            declared_type: "Foo".to_string(),
            scope: NameScope::Local { enclosing_method: method_symbol },
        }]);
        let name_index = RepoNameIndex::build(&[]);
        let type_index = TypeIndex::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("obj".to_string()),
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(resolved, Some("Foo".to_string()));
    }

    /// AC2: THE central discriminating case named in the story --
    /// `auth.realm().requireX()`'s receiver (as seen from `requireX`) is
    /// `Chained { method_name: "realm", receiver: Identifier("auth") }`.
    /// `auth`'s declared type is `Auth`; `Auth.realm()` declares return
    /// type `Realm` -- the resolved receiver type must be `"Realm"`,
    /// proving the chain was followed, not just the base.
    #[test]
    fn resolve_receiver_type_follows_a_chained_call_through_a_declared_return_type() {
        const AUTH_FILE_ID: u32 = 5;
        let mut auth_file = LocalIndex::new();
        auth_file.declarations.push(method_decl("realm", AUTH_FILE_ID, 0));
        auth_file.method_owners.push(MethodOwnerRecord {
            method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
            enclosing_type: "Auth".to_string(),
        });
        auth_file.method_return_types.push(MethodReturnTypeRecord {
            method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
            return_type: "Realm".to_string(),
        });
        let files = vec![FileForBind { file_id: AUTH_FILE_ID, language: "java".to_string(), index: auth_file }];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);

        let method_symbol = make_symbol_id(9, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "auth".to_string(),
            declared_type: "Auth".to_string(),
            scope: NameScope::Local { enclosing_method: method_symbol },
        }]);

        let receiver = ReceiverExpr::Chained {
            method_name: "realm".to_string(),
            receiver: Box::new(ReceiverExpr::Identifier("auth".to_string())),
        };
        let resolved = resolve_receiver_type(
            &receiver,
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(resolved, Some("Realm".to_string()));
    }
}
